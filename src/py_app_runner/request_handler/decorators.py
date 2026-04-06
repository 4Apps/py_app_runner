import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Concatenate, TypeAlias, TypeVar, cast

from database_wrapper_pgsql import DBWrapperPgsqlAsync
from typing_extensions import ParamSpec

from py_app_runner.http_exception import HTTPException
from py_app_runner.registry import AppRegistry
from py_app_runner.request_handler.handlers import RequestHandlerHelper

rate_limit_logger = logging.getLogger(__name__ + ".rate_limit")
audit_logger = logging.getLogger(__name__ + ".audit")

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T", bound=type)
SelfT = TypeVar("SelfT", bound=RequestHandlerHelper)

AsyncMethod: TypeAlias = Callable[Concatenate[SelfT, P], Awaitable[R]]
SyncMethod: TypeAlias = Callable[Concatenate[SelfT, P], R]


def action(name: str) -> Callable[[SyncMethod], SyncMethod]:
    def decorator(func: SyncMethod) -> SyncMethod:
        # function objects don't have this attribute in typeshed -> tell the type checker
        cast(object, func)._action_name = name  # type: ignore[attr-defined]

        @wraps(func)
        def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
            return func(self, *args, **kwargs)

        cast(object, wrapper)._action_name = name  # type: ignore[attr-defined]
        return wrapper

    return decorator


def authenticated(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        bh = getattr(self, "bridge_handler", None)
        if bh is None or getattr(bh, "current_user", None) is None:
            raise HTTPException("Not authorized", 4010, http_status=401)
        return await func(self, *args, **kwargs)

    return wrapper


def require_auth_for_actions(cls: T) -> T:
    """
    Class decorator: wraps every method that has `_action_name` with @authenticated.
    """

    for name, attr in list(vars(cls).items()):
        action_name = getattr(attr, "_action_name", None)
        if action_name is None:
            continue

        # Wrap only callables (methods)
        if callable(attr):
            # Important: keep the marker so dispatch still works
            wrapped = authenticated(attr)  # type: ignore[reportUnknownLambdaType]
            cast(object, wrapped)._action_name = action_name  # type: ignore[attr-defined]
            setattr(cls, name, wrapped)

    return cls


def with_tx(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        pg_conn = getattr(self, "pg_conn", None)
        if pg_conn is None:
            raise HTTPException("Database connection not linked (pg_conn missing)", 5000)

        async with pg_conn.transaction():
            return await func(self, *args, **kwargs)

    return wrapper


def with_db(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        bh = getattr(self, "bridge_handler", None)
        if bh is None:
            raise HTTPException("Bridge handler not linked", 5000)

        async with bh.db_pools.main_db_pool as (pg_conn, pg_cur):
            if not pg_conn or not pg_cur:
                raise HTTPException("Failed to connect to database server", 5000)

            self.pg_conn = pg_conn
            self.pg_cur = pg_cur
            self.db_wrapper = DBWrapperPgsqlAsync(pg_cur)

            try:
                return await func(self, *args, **kwargs)
            finally:
                for attr in ("pg_conn", "pg_cur", "db_wrapper"):
                    if hasattr(self, attr):
                        delattr(self, attr)

    return wrapper


def with_cache(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        bh = getattr(self, "bridge_handler", None)
        if bh is None:
            raise HTTPException("Bridge handler not linked", 5000)

        async with bh.db_pools.cache_db_pool as redis_con:
            if not redis_con:
                raise HTTPException("Failed to connect to cache server", 5000)

            self.redis_con = redis_con
            try:
                return await func(self, *args, **kwargs)
            finally:
                if hasattr(self, "redis_con"):
                    delattr(self, "redis_con")

    return wrapper


def with_cache_and_db(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        bh = getattr(self, "bridge_handler", None)
        if bh is None:
            raise HTTPException("Bridge handler not linked", 5000)

        async with bh.db_pools.cache_db_pool as redis_con:
            if not redis_con:
                raise HTTPException("Failed to connect to cache server", 5000)

            self.redis_con = redis_con
            try:
                async with bh.db_pools.main_db_pool as (pg_conn, pg_cur):
                    if not pg_conn or not pg_cur:
                        raise HTTPException("Failed to connect to database server", 5000)

                    self.pg_conn = pg_conn
                    self.pg_cur = pg_cur
                    self.db_wrapper = DBWrapperPgsqlAsync(pg_cur)

                    try:
                        return await func(self, *args, **kwargs)
                    finally:
                        for attr in ("pg_conn", "pg_cur", "db_wrapper"):
                            if hasattr(self, attr):
                                delattr(self, attr)
            finally:
                if hasattr(self, "redis_con"):
                    delattr(self, "redis_con")

    return wrapper


def rate_limit(max_requests: int, window_seconds: int) -> Callable[[AsyncMethod], AsyncMethod]:
    """
    Redis-backed fixed-window rate limiter.
    Key is derived from user ID (if authenticated) or client IP.
    Must be applied AFTER @with_cache or @with_cache_and_db (so self.redis_con exists).
    Returns HTTP 429 with Retry-After header when limit is exceeded.
    """

    def decorator(func: AsyncMethod) -> AsyncMethod:
        @wraps(func)
        async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
            redis_con = getattr(self, "redis_con", None)
            if redis_con is None:
                rate_limit_logger.warning("rate_limit requires Redis; skipping enforcement")
                return await func(self, *args, **kwargs)

            bh = getattr(self, "bridge_handler", None)
            user = getattr(bh, "current_user", None) if bh else None

            if user and getattr(user, "id", None):
                identity = f"user:{user.id}"
            elif bh:
                identity = f"ip:{bh.request.remote_ip}"
            else:
                identity = "unknown"

            action_name = getattr(func, "_action_name", func.__name__)
            key = f"rl:{action_name}:{identity}"

            current = await redis_con.incr(key)
            if current == 1:
                await redis_con.expire(key, window_seconds)

            if current > max_requests:
                ttl = await redis_con.ttl(key)
                raise HTTPException(
                    f"Rate limit exceeded. Try again in {ttl} seconds.",
                    code=4029,
                    http_status=429,
                )

            return await func(self, *args, **kwargs)

        return wrapper

    return decorator


def audited(
    event_type: str | None = None,
    *,
    sensitive_fields: tuple[str, ...] = ("password",),
) -> Callable[[AsyncMethod], AsyncMethod]:
    """
    Audit-log decorator. Logs a record after the wrapped action succeeds.

    Must be placed AFTER @with_db in the decorator stack (needs self.db_wrapper).
    Place it as the innermost decorator, closest to the method body.

    If *event_type* is None the action name from @action is used
    (e.g. "create" -> stored as-is; the caller can supply a dotted name
    like "users.create" for clarity).
    """

    def decorator(func: AsyncMethod) -> AsyncMethod:
        @wraps(func)
        async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
            result = await func(self, *args, **kwargs)

            # Best-effort audit logging
            try:
                db_wrapper = getattr(self, "db_wrapper", None)
                if db_wrapper is None:
                    audit_logger.warning("@audited requires @with_db; skipping audit")
                    return result

                resolved_event = event_type or getattr(func, "_action_name", func.__name__)

                bh = getattr(self, "bridge_handler", None)
                user = getattr(bh, "current_user", None) if bh else None
                user_id: int | None = getattr(user, "id", None)

                ip: str | None = None
                user_agent: str | None = None
                if bh:
                    ip = bh.request.remote_ip
                    user_agent = bh.request.headers.get("User-Agent")

                # Build details from input_data (first positional arg after self)
                details = _sanitize_input(args[0] if args else {}, sensitive_fields)

                audit_fn = AppRegistry.audit_log_fn()
                if audit_fn is None:
                    audit_logger.debug("No audit_log_fn configured; skipping audit")
                    return result

                await audit_fn(
                    db_wrapper,
                    company_id=None,
                    event_type=resolved_event,
                    user_id=user_id,
                    details=details,
                    ip=ip,
                    user_agent=user_agent,
                )
            except Exception:
                audit_logger.exception("@audited failed for event_type=%s", event_type)

            return result

        return wrapper

    return decorator


def _sanitize_input(input_data: Any, sensitive_fields: tuple[str, ...]) -> dict[str, Any]:
    """Strip sensitive fields from input_data before storing in audit details."""
    if not isinstance(input_data, dict):
        return {}
    return {k: "***" if k in sensitive_fields else v for k, v in input_data.items()}

import datetime
import hashlib
import hmac
import inspect
import logging
from asyncio import Future
from collections.abc import Callable
from time import time
from typing import Any, Literal
from uuid import UUID

from database_wrapper_pgsql import DBWrapperPgsqlAsync, PgConnectionTypeAsync, PgCursorTypeAsync
from redis.asyncio import Redis as RedisClientAsync
from tornado import httputil, web

from py_app_runner.registry import AppRegistry

from ..db_pools import DbPools
from ..http_exception import HTTPException
from ..pybridge import PyBridge
from ..return_model import MessageModel, ReturnModel, StatusModel
from ..timer import Timer
from ..utils import (
    format_body_for_log,
    format_response_for_log,
    json_decode,
    json_encode_bytes,
    redact_headers,
    safe_decode,
    sha1_prefix,
)
from ..wbcm.wb_connection_manager import WbConnectionManager
from .auth_service import AuthService

BridgeRequestData = dict[str, Any]
BridgeRequest = Callable[[str, BridgeRequestData, "RequestHandlerBase"], Any]
RequiresApiKey = bool | None
RequestParams = dict[str, Any]


class WebApplicationBase(web.Application):
    """Tornado web application expansion to initialize stuff like database connections and/or sessions"""

    pybridge: PyBridge
    db_pools: DbPools
    wb_connection_manager: WbConnectionManager

    def set_pybridge(self, pybridge: PyBridge) -> None:
        """Set pybridge"""
        self.pybridge = pybridge

    def set_db_pools(self, db_pools: DbPools) -> None:
        """Set database pools"""
        self.db_pools = db_pools

    def set_wb_connection_manager(self, wb_connection_manager: WbConnectionManager) -> None:
        """Set WebBridge connection manager"""
        self.wb_connection_manager = wb_connection_manager


class RequestHandlerBase(web.RequestHandler):
    """Base request handler"""

    ####################
    ### Properties #####
    ####################

    # Instance variables
    logger_name: str
    logger: logging.Logger
    timer: Timer

    # Temporary variables
    _current_user_obj: Any | None
    _impersonator_obj: Any | None
    json_args: dict[str, Any] | None
    require_json_response: bool
    module = None
    uid: str | None
    auth_token: str | None

    _context: dict[str, Any]

    auth_service: AuthService

    # Overrides
    application: WebApplicationBase  # type: ignore[assignment]

    @property
    def pybridge(self) -> PyBridge:
        return self.application.pybridge

    @property
    def db_pools(self) -> DbPools:
        return self.application.db_pools

    @property
    def wb_connection_manager(self) -> WbConnectionManager:
        return self.application.wb_connection_manager

    @property
    def context(self) -> RequestParams:
        raise NotImplementedError

    #########################
    ### Class lifecycle #####
    #########################

    def __init__(
        self,
        application: WebApplicationBase,
        request: httputil.HTTPServerRequest,
        **kwargs: Any,
    ):
        self.logger_name = f"{__name__}.{self.__class__.__name__}"
        self.logger = logging.getLogger(self.logger_name)
        self.timer = Timer("WebHandlerBase")
        self.require_json_response = False
        self.json_args = None
        self.uid = None
        self.auth_token = None
        self.auth_service = AuthService(self.logger)

        super().__init__(application, request, **kwargs)

    #################
    ### Helpers #####
    #################

    def find_service(self, service_name: str | None) -> ReturnModel[tuple[BridgeRequest | None, RequiresApiKey | None]]:
        if service_name is None:
            return ReturnModel(error=MessageModel(text="No service was provided.", code=-1000))

        service_name = service_name.replace("-", "_")
        if service_name not in self.pybridge.services_placeholder:
            return ReturnModel(error=MessageModel(text=f'Module "{service_name}" not found.', code=-1010))

        service = self.pybridge.services_placeholder[service_name]
        bridge_request = getattr(service, "bridge_request", None)
        if not callable(bridge_request):
            return ReturnModel(
                error=MessageModel(text=f'"{service_name}" does not support bridge requests', code=-1020)
            )

        # Check if there is an API key required method
        requires_api_key = getattr(service, "requires_api_key", None)

        return ReturnModel(
            result=(
                bridge_request,
                requires_api_key,
            ),
        )

    def get_request_data(self, force_action: str | None = None) -> dict[str, Any]:
        query_params = self.request.query_arguments
        decoded_params: dict[str, Any] = {}

        for k, v in query_params.items():
            # Check if there's only one item in the list
            if len(v) == 1:
                decoded_params[k] = v[0].decode("utf-8")
            else:
                # Decode each item in the list
                decoded_params[k] = [item.decode("utf-8") for item in v]

        request_body = {}
        if hasattr(self, "json_args") and self.json_args:
            request_body = self.json_args

        if force_action:
            request_body["action"] = force_action

        return {**decoded_params, **request_body}

    def error(
        self,
        msg: str | HTTPException,
        code: int = -1,
        http_status: int = 400,
    ) -> None: ...

    def log_request(
        self,
        *,
        response: Any | None = None,
        error: Exception | None = None,
        level: str = "info",
        include_body: bool = True,
        max_field_chars: int = 4000,
        shrink_strings_to: int = 256,
        cap_list_items: int = 50,
    ) -> None:
        """
        Unified request logger.

        - If `error` provided -> logs at WARNING by default (override with `level`).
        - If `response` provided -> logs a trimmed response.
        - No field-name assumptions; generic shrink/trim for huge payloads.
        - Set `include_body=False` to avoid logging potentially massive bodies (keeps only a summary).
        """
        logger: logging.Logger = getattr(self, "logger", logging.getLogger(__name__))

        # choose level
        level = (level or "").lower()
        if error and level == "info":
            level = "warning"
        log = getattr(logger, level, logger.info)

        req = getattr(self, "request", None)
        method = getattr(req, "method", "?")
        path = getattr(req, "path", getattr(req, "uri", "?"))
        headers = redact_headers(dict(getattr(req, "headers", {}) or {}))
        args = getattr(req, "arguments", None)
        body_raw = getattr(req, "body", b"")

        # quick body size + hash (without rendering the whole thing)
        if isinstance(body_raw, (bytes, bytearray)):
            body_len = len(body_raw)
            body_short = f"<{body_len} bytes sha1={sha1_prefix(bytes(body_raw))}>"
        else:
            body_text = safe_decode(body_raw)
            body_len = len(body_text)
            body_short = f"<{body_len} chars>"

        if error:
            log("Got error while processing request: %s", error)

        # concise one-liner
        log(
            "Request %s %s | len(body)=%s | args=%s",
            method,
            path,
            body_len,
            args if isinstance(args, dict) else "<n/a>",  # type: ignore
        )

        # details go at DEBUG unless we're already warning/error, then use same level
        detail_log = logger.debug if level in ("info", "debug") else log

        detail_log("Headers: %s", headers)

        if include_body:
            body_for_log = format_body_for_log(
                body_raw,
                max_chars=max_field_chars,
                shrink_strings_to=shrink_strings_to,
                cap_list_items=cap_list_items,
            )
            detail_log("Body: %s", body_for_log)
        else:
            detail_log("Body: %s", body_short)

        if response is not None:
            resp_for_log = format_response_for_log(
                response,
                max_chars=max_field_chars,
                shrink_strings_to=shrink_strings_to,
                cap_list_items=cap_list_items,
            )
            detail_log("Response: %s", resp_for_log)

    ###############
    ### Prepare ###
    ###############

    async def prepare(self):
        res = super().prepare()
        if inspect.isawaitable(res):
            await res

        # Debug
        headers = dict(self.request.headers)
        headers.pop("Authorization", None)

        self.logger.debug(f"Request: {self.request}")
        self.logger.debug(f"Headers: {headers}")
        self.logger.debug(f"Body: {self.request.body!r}")
        self.logger.debug(f"Arguments: {self.request.arguments.keys()}")

        self._ensure_content_type()
        await self._ensure_current_user()

    def _ensure_content_type(self):
        # Content-Type
        content_type = self.request.headers.get("Content-Type", "")  # type: ignore
        if (
            content_type.startswith("application/json") or content_type.startswith("application/x-json")
        ) and self.request.body:
            try:
                self.json_args = json_decode(self.request.body)

            except Exception as e:
                self.log_request(error=e)
                self.error("Invalid JSON body", 400, http_status=400)
                self.finish()
                return

        # What to expect in return
        accept = self.request.headers.get("Accept", "application/json")  # type: ignore
        if accept == "application/json":
            self.set_header("Content-Type", "application/json; charset=UTF-8")
            self.require_json_response = True
        else:
            self.set_header("Content-Type", "text/html; charset=UTF-8")

    async def _ensure_current_user(self):
        # Run BEFORE get/post/etc.
        self._impersonator_obj = None

        # Clear Tornado's cached current_user so the property re-evaluates.
        # Critical for WebSocket handlers where the same instance handles
        # multiple messages and the token can change between them.
        if hasattr(self, "_current_user"):
            del self._current_user

        token = self.auth_token
        if not token:
            token = self.request.headers.get("Authorization", "")
            if token.startswith("Bearer "):
                token = token.removeprefix("Bearer ").strip()
            else:
                token = None

        if not token:
            self._current_user_obj = None
            return

        self.auth_token = token

        # Try normal user token first
        payload = self.auth_service.verify_access_jwt(token, expected_type="user")
        is_impersonation = False

        if not payload:
            # Try impersonation token
            payload = self.auth_service.verify_access_jwt(token, expected_type="impersonation")
            if payload:
                is_impersonation = True

        if not payload:
            self._current_user_obj = None
            return

        user_public_id = payload["sub"]

        # Defensive UUID validation
        try:
            UUID(user_public_id)
        except ValueError:
            self._current_user_obj = None
            return

        UsersModel = AppRegistry.users_model()
        async with self.db_pools.main_db_pool as (pg_conn, pg_cur):
            if not pg_cur or not pg_conn:
                raise HTTPException("Database is not initialized", 500, http_status=500)

            user = await UsersModel.get_by_public_id(user_public_id, pg_cur)

            if is_impersonation:
                imp_public_id = payload.get("imp")
                if imp_public_id:
                    impersonator = await UsersModel.get_by_public_id(imp_public_id, pg_cur)
                    if impersonator and not impersonator.disabled_at and not impersonator.deleted_at:
                        self._impersonator_obj = impersonator

        if not user or user.disabled_at or user.deleted_at:
            self._current_user_obj = None
            return

        # Cache it for this request
        self._current_user_obj = user

    @property
    def impersonator(self) -> Any | None:
        """Returns the superadmin identity if current request uses an impersonation token."""
        return getattr(self, "_impersonator_obj", None)

    def get_current_user(self):
        # Must be sync in Tornado; just return cached value.
        return self._current_user_obj


class RequestHandlerApiKeys(RequestHandlerBase):
    #########################
    ### Validate api keys ###
    #########################

    def verify_api_key(self, api_key_secret: str, stored_hash: str) -> bool:
        h = hashlib.sha256()
        h.update(AppRegistry.config()["api_key_pepper"].encode())
        h.update(api_key_secret.encode())
        computed = h.hexdigest()

        return hmac.compare_digest(computed, stored_hash)

    async def has_valid_key_db(self, api_key: str, db_cur: PgCursorTypeAsync) -> str | Literal[True]:
        api_key_split = api_key.split(".", 1)
        if len(api_key_split) != 2:
            return "Invalid API KEY"

        api_key_prefix, api_key_secret = api_key_split

        # Load from db
        async with self.timer.aenter("request_handler.has_valid_key_db.load_from_db"):
            db_wrapper = DBWrapperPgsqlAsync(db_cur)
            api_key_model = AppRegistry.api_keys_model()
            api_key_record = await db_wrapper.get_by_key(api_key_model(), id_key="key_prefix", id_value=api_key_prefix)

        if not api_key_record:
            return "Invalid API Key"

        if api_key_record.disabled_at:
            return "API key is disabled"

        if not self.verify_api_key(api_key_secret, api_key_record.secret_hash):
            return "Invalid API key"

        async with self.timer.aenter("request_handler.has_valid_key_db.use_key"):
            await api_key_record.use_key(db_cur)

        return True

    def has_valid_key(self, api_key: str) -> str | Literal[True]:
        configured_key = AppRegistry.config().get("api", {}).get("key")
        if not configured_key:
            return "API key not configured"

        if (isinstance(configured_key, list) and api_key in configured_key) or api_key == configured_key:
            return True

        return "Invalid API key"

    async def has_valid_api_key(self, custom_api_key: str | None = None) -> str | Literal[True]:
        api_key: str | None = custom_api_key or self.request.headers.get("X-API-Key", None)  # type: ignore
        if api_key is None:
            api_key = self.get_argument("api_key", None)

        if api_key is None:
            return "API key is missing"

        api_key = str(api_key)

        if AppRegistry.api_key_use_db():
            async with self.db_pools.main_db_pool as (pg_conn, pg_cur):
                if not pg_cur or not pg_conn:
                    raise HTTPException("Database is not initialized")
                async with pg_conn.transaction():
                    return await self.has_valid_key_db(api_key, pg_cur)

        return self.has_valid_key(api_key)


class WebHandlerBase(RequestHandlerApiKeys):
    """Web request handler base"""

    ##################
    ### Properties ###
    ##################

    @property
    def context(self) -> RequestParams:
        if not hasattr(self, "_context"):
            self._context = {
                "base_uri": "/",
                "module": self.module,
                "config": AppRegistry.config(),
                "timestamp": int(time()),
                "now": datetime.datetime.now(),
            }

        return self._context

    ###########################
    ### Tornado overrides #####
    ###########################

    def render(self, template_name: str, **kwargs: Any) -> Future[Any]:
        self.context.update(kwargs)

        if "/" in template_name:
            template_name = f"{self.application.settings['routes_path']}/{template_name}"

        return super().render(template_name, **self.context)

    def render_string(self, template_name: str, **kwargs: Any) -> bytes:
        self.context.update(kwargs)

        return super().render_string(template_name, **self.context)

    ##########################
    ### Request handling #####
    ##########################
    def write(self, chunk: str | bytes | dict[str, Any], wrap_in_data: bool = True):
        if isinstance(chunk, StatusModel) or isinstance(chunk, MessageModel) or isinstance(chunk, ReturnModel):
            chunk = chunk.to_dict()

        if wrap_in_data:
            chunk = {"data": chunk}

        if isinstance(chunk, dict) or isinstance(chunk, list):
            response = json_encode_bytes(chunk, pretty=self.application.settings.get("debug", False))

            self.set_header("Content-Type", "application/json; charset=UTF-8")
            super().write(response)  # type: ignore
        elif isinstance(chunk, HTTPException):
            self.error(chunk)
        else:
            super().write(chunk)  # type: ignore

    def error(
        self,
        msg: str | HTTPException,
        code: int = -1,
        http_status: int = 400,
    ) -> None:
        if isinstance(msg, HTTPException):
            self.set_status(msg.http_status)
            self.write({"error": msg.to_dict()})
            return

        if http_status:
            self.set_status(http_status)

        message: dict[str, Any] = {"error": {"code": code, "msg": msg}}
        self.write(message, wrap_in_data=False)


class RequestHandlerHelper:
    _action_name: str

    bridge_handler: RequestHandlerBase

    pg_conn: PgConnectionTypeAsync
    pg_cur: PgCursorTypeAsync
    db_wrapper: DBWrapperPgsqlAsync
    redis_con: RedisClientAsync

    # Cache for registered actions per class to avoid re-scanning
    _action_registry: dict[type, dict[str, Callable[..., Any]]] = {}

    @classmethod
    def _get_actions(cls) -> dict[str, Callable[..., Any]]:
        """Lazy load and cache actions for the class"""
        if cls not in cls._action_registry:
            registry = {}
            # Inspect all members of the class
            for attr_name in dir(cls):
                method = getattr(cls, attr_name)
                # Check if it has our decorator tag
                if hasattr(method, "_action_name"):
                    registry[method._action_name] = method
            cls._action_registry[cls] = registry
        return cls._action_registry[cls]

    async def handle_request(
        self,
        action: str,
        input_data: dict[str, Any],
        bridge_handler: RequestHandlerBase,
    ) -> Any:
        """
        Generic dispatcher that looks up the action in the registry.
        """
        self.bridge_handler = bridge_handler
        registry = self._get_actions()
        handler_method = registry.get(action)

        if not handler_method:
            handler_method = registry.get("default")

        if not handler_method:
            raise HTTPException(f"Unknown action `{action}`", 1234)

        # Call the method
        return await handler_method(self, input_data)

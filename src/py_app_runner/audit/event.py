"""The event, and the ambient context it is filled in from.

The request id has to be per-task, which is why it is a ContextVar the bridge sets per
request rather than anything process-wide. A single server process handles many requests at
once, so a shared id would make "everything that happened in this request" return somebody
else's changes alongside your own - and the trail would look correct while saying something
false.
"""

import contextlib
import contextvars
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any

CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"


@dataclass(frozen=True)
class Actor:
    type: str = ""
    id: str = ""
    name: str = ""


@dataclass(frozen=True)
class RequestContext:
    request_id: str = ""
    url: str = ""
    ip_address: str = ""
    user_agent: str = ""
    actor: Actor = field(default_factory=Actor)


# Defaulted to None rather than to an empty RequestContext. The instance would be shared by
# every task that never entered request_context(), and while this one is frozen, a default
# that is an object at all is the shape that goes wrong the moment somebody makes it
# mutable.
_context: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "py_app_runner_audit_context", default=None
)

_EMPTY = RequestContext()


def new_request_id() -> str:
    """32 hex characters. Groups every change made during one request or CLI run."""

    return secrets.token_hex(16)


def current_context() -> RequestContext:
    """An empty context outside any request, so recording never depends on having entered
    one - a CLI command that audits a change still writes a row, just without an actor."""

    return _context.get() or _EMPTY


def set_context(context: RequestContext) -> contextvars.Token[RequestContext | None]:
    return _context.set(context)


@contextlib.contextmanager
def request_context(
    actor: Actor | None = None,
    url: str = "",
    ip_address: str = "",
    user_agent: str = "",
    request_id: str | None = None,
) -> Iterator[RequestContext]:
    """Establish the ambient context for one request, or one CLI run.

    The bridge enters this per request; a command enters it once for the whole command. The
    token is reset on the way out so a task that runs after this one does not inherit it.
    """

    context = RequestContext(
        request_id=request_id if request_id is not None else new_request_id(),
        url=url,
        ip_address=ip_address,
        user_agent=user_agent,
        actor=actor or Actor(),
    )

    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


@dataclass(frozen=True)
class AuditEvent:
    event: str
    entity_type: str
    entity_id: str = ""
    module: str = ""
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    actor_type: str = ""
    actor_id: str = ""
    actor_name: str = ""
    request_id: str = ""
    url: str = ""
    ip_address: str = ""
    user_agent: str = ""
    tags: list[str] = field(default_factory=list)
    context: dict[str, Any] | None = None
    created_at: Any = None

    def with_resolved(self, context: RequestContext) -> "AuditEvent":
        """Fill in only the fields that are still empty.

        A caller that named the actor explicitly - an import recording who requested it
        rather than whoever happens to be logged in - keeps what it passed.
        """

        return replace(
            self,
            actor_type=self.actor_type or context.actor.type,
            actor_id=self.actor_id or context.actor.id,
            actor_name=self.actor_name or context.actor.name,
            request_id=self.request_id or context.request_id,
            url=self.url or context.url,
            ip_address=self.ip_address or context.ip_address,
            user_agent=self.user_agent or context.user_agent,
        )

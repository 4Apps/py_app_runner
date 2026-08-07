"""Turning the `name` on a row into something callable.

What is stored is what was pushed - the alias, not the resolved target - so rows outlive
the deploy that wrote them. That is the whole point of allowing an alias: a class can be
renamed or moved without orphaning every job already in the table.
"""

import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast, runtime_checkable

from py_app_runner.queue.job import Job, QueueError

# A resolved handler is either an object with an async `handle`, or a plain async callable.
HandlerCallable = Callable[[dict[str, Any], Job], Awaitable[None]]


@runtime_checkable
class Handler(Protocol):
    async def handle(self, payload: dict[str, Any], job: Job) -> None: ...


def _import_target(path: str) -> Any:
    """Import "package.module:attr", or "package.module.attr"."""

    if ":" in path:
        module_name, _, attribute = path.partition(":")
    else:
        module_name, _, attribute = path.rpartition(".")

    if not module_name or not attribute:
        raise QueueError(f'No handler {path!r}: expected "module:attribute" or "module.attribute".')

    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise QueueError(f"No handler {path!r}: {e}") from e

    try:
        return getattr(module, attribute)
    except AttributeError as e:
        raise QueueError(f"No handler {path!r}: {module_name} has no {attribute!r}.") from e


def resolve(name: str, handlers: dict[str, Any] | None = None) -> HandlerCallable:
    """Resolve a job name to something awaitable.

    Order: a configured alias wins, then the name is treated as an import path. The alias
    short-circuits entirely, which is what lets a renamed class keep working without
    touching the rows that name it.
    """

    configured = (handlers or {}).get(name)
    target = configured if configured is not None else name

    if isinstance(target, str):
        target = _import_target(target)

    # The cast is the honest shape of this function: what comes back from an import is
    # `object` as far as the type checker is concerned, and only the callable check below
    # establishes anything more. Checking that a handler is *awaitable* is not possible
    # here without calling it, so that failure surfaces at run time - where the worker
    # already routes it through the same release/fail path as any other job failure.
    if inspect.isclass(target):
        instance = target()
        handle = getattr(instance, "handle", None)
        if handle is None or not callable(handle):
            raise QueueError(f"Handler {name!r} resolved to {target!r}, which has no handle() method.")

        return cast(HandlerCallable, handle)

    if callable(target):
        return cast(HandlerCallable, target)

    raise QueueError(f"Handler {name!r} resolved to {target!r}, which is not callable.")


def assert_resolvable(name: str, handlers: dict[str, Any] | None = None) -> None:
    """Check at push time, not at run time.

    A name that resolves to nothing fails on every attempt and then sits in the failed
    table, having consumed its whole retry budget to discover something that was knowable
    at the call site.
    """

    if not name:
        raise QueueError("A job needs a handler name.")

    resolve(name, handlers)

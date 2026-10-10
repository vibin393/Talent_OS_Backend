"""In-process domain events (AD-9).

Services call ``emit(session, event)``; the event is buffered on the session and dispatched to the
registered handlers only **after** that session's transaction commits. A rollback discards the buffer.
Handlers are synchronous and should be cheap (typically: enqueue a Celery task with ``tenant_id`` first).
A failing handler is logged and does not affect other handlers (the data is already committed).

Savepoints (Epic 13 deferred-work fix): each buffered event is tagged with the innermost
``SessionTransaction`` it was emitted in. Rolling back a savepoint drops the events emitted inside it (or
inside any savepoint nested in it); events of a released savepoint stay with its parent (their tag's parent
chain is unaffected by the release), so the outer commit dispatches them. An outer rollback drops everything.

Eager Celery (Epic 13 deferred-work fix): while handlers run, ``DISPATCHING`` holds the session's deferred
task list. ``tasks.runtime.enqueue`` in eager mode appends ``(task, args, kwargs)`` there instead of running
the task inside the commit; ``flush_deferred`` runs them once ``commit()`` has returned (``database``) and a
request middleware flushes any leftovers at request end.

In-transaction handlers (Epic 66, AD-79, the transactional outbox): ``subscribe_in_tx(event_type, handler)`` registers
``handler(session, event)``, run in the session's ``before_commit`` of the OUTERMOST commit for every event still buffered
(events of rolled-back savepoints were already dropped). Handlers may only ``session.add(...)`` rows -- no I/O, no
queries, no ``emit``; the rows flush with the transaction. A handler error aborts the commit (the outbox row is part of
the transaction). After-commit ``subscribe`` handlers are unchanged.
"""

from collections import defaultdict
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

_PENDING_KEY = "pending_domain_events"
_DEFERRED_KEY = "deferred_eager_tasks"
# Set by ``database.TosAsyncSession.commit``: defer eager tasks of this session's after-commit handlers.
DEFER_INFO_KEY = "defer_eager_tasks"

Handler = Callable[[Any], None]
DeferredTask = tuple[Any, tuple[Any, ...], dict[str, Any]]

_handlers: dict[type, list[Handler]] = defaultdict(list)
TxHandler = Callable[[Session, Any], None]
_tx_handlers: dict[type, list[TxHandler]] = defaultdict(list)

# The deferred-task list of the session whose after-commit handlers are running (None outside dispatch).
DISPATCHING: ContextVar[list[DeferredTask] | None] = ContextVar("talent_os_events_dispatching", default=None)
# Request-level safety net: deferred lists not flushed by their session (see ``flush_deferred``).
_REQUEST_DEFERRED: ContextVar[list[list[DeferredTask]] | None] = ContextVar(
    "talent_os_request_deferred", default=None
)


def subscribe[E](event_type: type[E], handler: Callable[[E], None]) -> None:
    if handler not in _handlers[event_type]:
        _handlers[event_type].append(handler)


def handles[E](event_type: type[E]) -> Callable[[Callable[[E], None]], Callable[[E], None]]:
    """Register the decorated function as a handler (decorator form of ``subscribe``)."""

    def decorator(fn: Callable[[E], None]) -> Callable[[E], None]:
        subscribe(event_type, fn)
        return fn

    return decorator


def subscribe_in_tx[E](event_type: type[E], handler: Callable[[Session, E], None]) -> None:
    """Epic 66: run ``handler(sync_session, event)`` inside the committing transaction (``before_commit``)."""
    if handler not in _tx_handlers[event_type]:
        _tx_handlers[event_type].append(handler)


def unsubscribe_in_tx[E](event_type: type[E], handler: Callable[[Session, E], None]) -> None:
    if handler in _tx_handlers.get(event_type, []):
        _tx_handlers[event_type].remove(handler)


@event.listens_for(Session, "before_commit")
def _before_commit(session: Session) -> None:
    # Fires for SAVEPOINT releases too; only the outermost commit writes (events of a released savepoint stay
    # buffered with the parent; those of a rolled-back savepoint were dropped by ``_after_rollback``).
    if not _tx_handlers or session.get_nested_transaction() is not None:
        return
    pending = session.info.get(_PENDING_KEY)
    if not pending:
        return
    for domain_event, _tx in list(pending):
        for event_type in type(domain_event).__mro__:
            for handler in list(_tx_handlers.get(event_type, ())):
                handler(session, domain_event)  # an error propagates: the commit is aborted


def unsubscribe[E](event_type: type[E], handler: Callable[[E], None]) -> None:
    if handler in _handlers.get(event_type, []):
        _handlers[event_type].remove(handler)


def _innermost(session: Session) -> SessionTransaction | None:
    return session.get_nested_transaction() or session.get_transaction()


def emit(session: AsyncSession | Session, domain_event: object) -> None:
    """Buffer ``domain_event`` on ``session``; dispatched after the session commits."""
    sync_session = session.sync_session if isinstance(session, AsyncSession) else session
    sync_session.info.setdefault(_PENDING_KEY, []).append((domain_event, _innermost(sync_session)))


def pending_events(session: AsyncSession | Session) -> list[object]:
    sync_session = session.sync_session if isinstance(session, AsyncSession) else session
    return [e for e, _ in sync_session.info.get(_PENDING_KEY, [])]


def _dispatch(domain_event: object) -> None:
    for event_type in type(domain_event).__mro__:
        for handler in list(_handlers.get(event_type, ())):
            try:
                handler(domain_event)
            except Exception:
                logger.exception(
                    "Domain event handler {} failed for {}", handler, type(domain_event).__name__
                )


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    # SQLAlchemy also fires ``after_commit`` when a SAVEPOINT (``begin_nested``) is released; the outer
    # transaction is still open then, so keep the buffer until the real commit (Epic 6 fix).
    if session.get_nested_transaction() is not None:
        return
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    token = None
    if session.info.pop(DEFER_INFO_KEY, False):
        deferred: list[DeferredTask] = session.info.setdefault(_DEFERRED_KEY, [])
        registry = _REQUEST_DEFERRED.get()
        if registry is not None and not any(deferred is d for d in registry):
            registry.append(deferred)
        token = DISPATCHING.set(deferred)
    try:
        for domain_event, _tx in pending:
            _dispatch(domain_event)
    finally:
        if token is not None:
            DISPATCHING.reset(token)


def _within(tx: SessionTransaction | None, ancestor: SessionTransaction) -> bool:
    cur = tx
    while cur is not None:
        if cur is ancestor:
            return True
        cur = cur.parent
    return False


@event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction: SessionTransaction) -> None:
    if previous_transaction.parent is None:
        # An outermost rollback discards the whole buffer (also fires when no DB connection was used yet).
        session.info.pop(_PENDING_KEY, None)
        return
    if not previous_transaction.nested:
        return
    pending = session.info.get(_PENDING_KEY)
    if pending:
        session.info[_PENDING_KEY] = [(e, tx) for e, tx in pending if not _within(tx, previous_transaction)]


def _run_deferred(items: list[DeferredTask]) -> None:
    while items:
        task, args, kwargs = items.pop(0)
        try:
            task.delay(*args, **kwargs)
        except Exception:
            logger.exception("Could not run deferred task {}", getattr(task, "name", task))


def flush_deferred(session: Session | None = None) -> None:
    """Run the eager tasks deferred by after-commit handlers: those of ``session``, or (no argument) every
    list registered in the current request context (middleware safety net)."""
    if session is not None:
        items = session.info.pop(_DEFERRED_KEY, None)
        if items:
            _run_deferred(items)
        return
    registry = _REQUEST_DEFERRED.get()
    if registry:
        for items in list(registry):
            _run_deferred(items)
        registry.clear()


def begin_request_scope() -> Any:
    """Start the request-level deferred registry; returns the token for ``end_request_scope``."""
    return _REQUEST_DEFERRED.set([])


def end_request_scope(token: Any) -> None:
    try:
        flush_deferred()
    finally:
        _REQUEST_DEFERRED.reset(token)


def defer_if_dispatching(task: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    """Called by ``tasks.runtime.enqueue`` in eager mode: True when the call was deferred."""
    deferred = DISPATCHING.get()
    if deferred is None:
        return False
    deferred.append((task, args, kwargs))
    return True

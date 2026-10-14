"""Thread-safe cooperative cancellation for Runtime async boundaries."""

from __future__ import annotations

import asyncio
import inspect
from threading import RLock
from typing import Awaitable, Dict, Optional, TypeVar

T = TypeVar("T")


class CancellationRequested(Exception):
    def __init__(self, reason: Optional[str] = None) -> None:
        self.reason = reason or "run cancelled"
        super().__init__(self.reason)


class CancellationToken:
    """A token that can be cancelled safely from an HTTP request thread."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._cancelled = False
        self._reason: Optional[str] = None
        self._waiters: Dict[asyncio.Future[str], asyncio.AbstractEventLoop] = {}

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def cancel(self, reason: Optional[str] = None) -> bool:
        resolved_reason = reason or "run cancelled"
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._reason = resolved_reason
            waiters = list(self._waiters.items())
            self._waiters.clear()

        for future, loop in waiters:
            try:
                loop.call_soon_threadsafe(_resolve_waiter, future, resolved_reason)
            except RuntimeError:
                # The owning loop already stopped; cancellation state is still durable.
                continue
        return True

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancellationRequested(self.reason)

    async def wait_cancelled(self) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        with self._lock:
            if self._cancelled:
                return self._reason or "run cancelled"
            self._waiters[future] = loop

        try:
            return await future
        finally:
            with self._lock:
                self._waiters.pop(future, None)

    async def run(self, operation: Awaitable[T]) -> T:
        """Run an awaitable until it completes or this token is cancelled."""

        if self.is_cancelled:
            _close_unstarted_coroutine(operation)
            raise CancellationRequested(self.reason)

        operation_task = asyncio.ensure_future(operation)
        cancellation_task = asyncio.create_task(self.wait_cancelled())
        try:
            done, _ = await asyncio.wait(
                {operation_task, cancellation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done or self.is_cancelled:
                if not operation_task.done():
                    operation_task.cancel()
                await _consume_cancelled_operation(operation_task)
                raise CancellationRequested(self.reason)

            cancellation_task.cancel()
            await _consume_cancelled_waiter(cancellation_task)
            result = await operation_task
            self.raise_if_cancelled()
            return result
        except asyncio.CancelledError:
            operation_task.cancel()
            cancellation_task.cancel()
            raise
        finally:
            if not cancellation_task.done():
                cancellation_task.cancel()


def _resolve_waiter(future: asyncio.Future[str], reason: str) -> None:
    if not future.done():
        future.set_result(reason)


def _close_unstarted_coroutine(operation: Awaitable[object]) -> None:
    if inspect.iscoroutine(operation):
        operation.close()


async def _consume_cancelled_operation(task: asyncio.Future[object]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        return


async def _consume_cancelled_waiter(task: asyncio.Future[object]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return

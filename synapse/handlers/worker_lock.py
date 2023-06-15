# Copyright 2023 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
from types import TracebackType
from typing import TYPE_CHECKING, Dict, Optional, Tuple, Type
from weakref import WeakSet

import attr

from twisted.internet import defer
from twisted.internet.interfaces import IReactorTime

from synapse.logging.context import PreserveLoggingContext
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage.databases.main.lock import Lock, LockStore
from synapse.util.async_helpers import timeout_deferred

if TYPE_CHECKING:
    from synapse.server import HomeServer


class WorkerLocksHandler:
    """A class for waiting on taking out locks, rather than using the storage
    functions directly (which don't support awaiting).
    """

    def __init__(self, hs: "HomeServer") -> None:
        self._reactor = hs.get_reactor()
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._replication_handler = hs.get_replication_command_handler()
        self._notifier = hs.get_notifier()

        # Map from lock name/key to set of `WaitingLock` that are active for
        # that lock.
        self._locks: Dict[Tuple[str, str], WeakSet[WaitingLock]] = {}

        self._clock.looping_call(self._cleanup_locks, 30_000)

        self._notifier.add_lock_released_callback(self._on_lock_released)

    def acquire_lock(self, lock_name: str, lock_key: str) -> "WaitingLock":
        """Acquire a standard lock, returns a context manager that will block
        until the lock is acquired.

        Usage:
            async with handler.acquire_lock(name, key):
                # Do work while holding the lock...
        """

        lock = WaitingLock(
            reactor=self._reactor,
            store=self._store,
            handler=self,
            lock_name=lock_name,
            lock_key=lock_key,
            write=None,
        )

        self._locks.setdefault((lock_name, lock_key), WeakSet()).add(lock)

        return lock

    def acquire_read_write_lock(
        self,
        lock_name: str,
        lock_key: str,
        *,
        write: bool,
    ) -> "WaitingLock":
        """Acquire a read/write lock, returns a context manager that will block
        until the lock is acquired.

        Usage:
            async with handler.acquire_read_write_lock(name, key, write=True):
                # Do work while holding the lock...
        """

        lock = WaitingLock(
            reactor=self._reactor,
            store=self._store,
            handler=self,
            lock_name=lock_name,
            lock_key=lock_key,
            write=write,
        )

        self._locks.setdefault((lock_name, lock_key), WeakSet()).add(lock)

        return lock

    def notify_lock_released(self, lock_name: str, lock_key: str) -> None:
        """Notify that a lock has been released.

        Pokes both the notifier and replication.
        """

        self._replication_handler.send_lock_released(lock_name, lock_key)
        self._notifier.notify_lock_released(lock_name, lock_key)

    def _on_lock_released(self, lock_name: str, lock_key: str) -> None:
        """Called when a lock has been released.

        Wakes up any locks that might bew waiting on this.
        """
        locks = self._locks.get((lock_name, lock_key))
        if not locks:
            return

        def _wake_deferred(deferred: defer.Deferred) -> None:
            if not deferred.called:
                deferred.callback(None)

        for lock in locks:
            self._clock.call_later(0, _wake_deferred, lock.deferred.callback)

    @wrap_as_background_process("_cleanup_locks")
    async def _cleanup_locks(self) -> None:
        """Periodically cleans out stale entries in the locks map"""
        self._locks = {key: value for key, value in self._locks.items() if value}


@attr.s(auto_attribs=True, eq=False)
class WaitingLock:
    reactor: IReactorTime
    store: LockStore
    handler: WorkerLocksHandler
    lock_name: str
    lock_key: str
    write: Optional[bool]
    deferred: "defer.Deferred[None]" = attr.Factory(defer.Deferred)
    _inner_lock: Optional[Lock] = None
    _retry_interval: float = 0.1

    async def __aenter__(self) -> None:
        while self._inner_lock is None:
            if self.write is not None:
                lock = await self.store.try_acquire_read_write_lock(
                    self.lock_name, self.lock_key, write=self.write
                )
            else:
                lock = await self.store.try_acquire_lock(self.lock_name, self.lock_key)

            if lock:
                self._inner_lock = lock
                break

            self.deferred = defer.Deferred()
            try:
                with PreserveLoggingContext():
                    await timeout_deferred(
                        deferred=self.deferred,
                        timeout=self._get_next_retry_interval(),
                        reactor=self.reactor,
                    )
            except Exception:
                pass

        return await self._inner_lock.__aenter__()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> Optional[bool]:
        assert self._inner_lock

        self.handler.notify_lock_released(self.lock_name, self.lock_key)

        return await self._inner_lock.__aexit__(exc_type, exc, tb)

    def _get_next_retry_interval(self) -> float:
        next = self._retry_interval
        self._retry_interval = max(5, next * 2)
        return next * random.uniform(0.9, 1.1)

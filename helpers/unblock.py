"""run blocking operations safely off the event loop.

- `executor` (threads): io and gil-releasing native work (numpy/pil, disk, pydantic).
- process pool (`to_process_with_timeout`): pure-python cpu work that would hold the gil.
  spawned (not forked) so it behaves the same on windows and linux.
"""

import asyncio
import multiprocessing
import signal
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from typing import Any, Callable

executor = ThreadPoolExecutor(max_workers=64)

_process_pool: "ProcessPoolExecutor | None" = None
_process_lock = threading.Lock()


def _worker_init() -> None:
    # sigkill the worker when its parent dies, so a bot crash can't leave orphans (linux only)
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            PR_SET_PDEATHSIG = 1
            sigkill = getattr(signal, "SIGKILL", 9)  # not defined on windows (linux-only branch)
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, sigkill)
        except Exception:
            pass


def _new_process_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init,
    )


def _get_process_pool() -> ProcessPoolExecutor:
    # a broken pool self-heals: throw it away and rebuild so one worker death doesn't wedge everything
    global _process_pool
    with _process_lock:
        pool = _process_pool
        if pool is None or getattr(pool, "_broken", False):
            if pool is not None:
                print(
                    "[unblock] process pool broke (a worker died); recreating it",
                    file=sys.stderr,
                )
                pool.shutdown(wait=False, cancel_futures=True)
            pool = _new_process_pool()
            _process_pool = pool
        return pool


def shutdown() -> None:
    global _process_pool
    if _process_pool is not None:
        _process_pool.shutdown(wait=False, cancel_futures=True)
        _process_pool = None


def to_thread(func: Callable, *args, **kwargs) -> None:
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()


async def to_process_with_timeout(
    func: Callable, *args: Any, timeout: int = 20, **kwargs: Any
) -> Any:
    # func must be importable (module-level) and its args/return picklable. the timeout cancels
    # the await, not the worker
    loop = asyncio.get_running_loop()
    for attempt in range(2):
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_get_process_pool(), partial(func, *args, **kwargs)),
                timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"Function {func.__name__} timed out after {timeout} seconds")
        except BrokenProcessPool:
            if attempt:
                raise

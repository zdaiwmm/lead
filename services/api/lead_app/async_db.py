"""Move whole short transactions off the event loop; never share connections."""

import asyncio
import weakref

from .resource_pressure import bounded_int

_limits = weakref.WeakKeyDictionary()


async def run(function, *args, **kwargs):
    loop = asyncio.get_running_loop()
    limit = _limits.setdefault(loop, asyncio.Semaphore(bounded_int("LEAD_DB_THREAD_SLOTS", 4, 1, 16)))
    async with limit:
        return await asyncio.to_thread(function, *args, **kwargs)

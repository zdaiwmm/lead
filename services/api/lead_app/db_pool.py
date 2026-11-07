"""Bounded runtime connection reuse. Every borrower still opens its own transaction."""

import atexit
import threading
from collections import OrderedDict

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import database_kwargs

_pools = OrderedDict()
_lock = threading.Lock()


def pool():
    from .resource_pressure import bounded_int

    kwargs = database_kwargs()
    maximum = bounded_int("LEAD_DB_POOL_MAX", 12, 1, 32)
    key = (tuple(sorted(kwargs.items())), maximum)
    with _lock:
        if key not in _pools:
            while len(_pools) >= 4:
                _, old = _pools.popitem(last=False)
                old.close()
            _pools[key] = ConnectionPool(
                kwargs={**kwargs, "row_factory": dict_row},
                min_size=0,
                max_size=maximum,
                max_waiting=64,
                timeout=5,
                max_idle=60,
                max_lifetime=600,
                open=True,
                check=ConnectionPool.check_connection,
                name="analytics-runtime-" + str(len(_pools) + 1),
            )
        _pools.move_to_end(key)
        return _pools[key]


def close_all():
    with _lock:
        for item in _pools.values():
            item.close()
        _pools.clear()


atexit.register(close_all)

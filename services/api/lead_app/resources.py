"""Fail before new browser/media writes when evidence storage is unavailable."""

import shutil
from contextlib import contextmanager
from threading import Lock

from .config import STATE


def ensure_capacity(minimum_mb=128):
    free = shutil.disk_usage(STATE).free
    if free < minimum_mb * 1024**2:
        raise ValueError("本机可用空间不足，已停止新的浏览器或媒体提交")
    return free


_reserved = 0
_lock = Lock()


@contextmanager
def reserve_disk(size_bytes):
    global _reserved
    with _lock:
        if shutil.disk_usage(STATE).free - _reserved < size_bytes + 128 * 1024**2:
            raise ValueError("可用空间不足以安全处理媒体")
        _reserved += size_bytes
    try:
        yield
    finally:
        with _lock:
            _reserved -= size_bytes

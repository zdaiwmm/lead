"""Local MMDB lookup only; no visitor IP is sent to a third-party API."""

import hashlib
import ipaddress
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE, env

_lock = threading.RLock()
_reader = None
_signature = None
_metadata = {}


def database_path():
    return (
        Path(env().get("LEAD_GEOIP_DATABASE") or STATE / "geoip/country.mmdb")
        .expanduser()
        .resolve()
    )


def _open():
    global _reader, _signature, _metadata
    path = database_path()
    info = path.stat()
    signature = (str(path), info.st_ino, info.st_size, info.st_mtime_ns)
    if signature != _signature:
        import maxminddb

        fresh = maxminddb.open_database(str(path))
        meta = fresh.metadata()
        country = (fresh.get("8.8.8.8") or {}).get("country", {}).get("iso_code")
        if not country or not re.fullmatch("[A-Z]{2}", country):
            fresh.close()
            raise ValueError("country database schema invalid")
        old = _reader
        _reader, _signature = fresh, signature
        _metadata = {
            "database_type": meta.database_type,
            "build_epoch": meta.build_epoch,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "provider": "DB-IP"
            if "dbip" in meta.database_type.lower()
            else meta.database_type,
        }
        if old:
            old.close()
    return _reader


def status():
    if env().get("LEAD_GEOIP_ENABLED", "1") == "0":
        return {"state": "disabled"}
    try:
        with _lock:
            _open()
            stale = (
                datetime.now(timezone.utc).timestamp() - _metadata["build_epoch"]
                > 75 * 86400
            )
            return {"state": "stale" if stale else "ready", **_metadata}
    except Exception:
        return {"state": "unavailable"}


def lookup(value):
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return {"country": None, "status": "missing_ip"}
    if not address.is_global:
        return {"country": None, "status": "non_public_ip"}
    if env().get("LEAD_GEOIP_ENABLED", "1") == "0":
        return {"country": None, "status": "disabled"}
    try:
        with _lock:
            record = _open().get(str(address)) or {}
            country = record.get("country", {})
            code = country.get("iso_code")
            if not code or not re.fullmatch("[A-Z]{2}", code):
                return {"country": None, "status": "not_found", **_metadata}
            return {
                "country": code,
                "country_name": country.get("names", {}).get("zh-CN")
                or country.get("names", {}).get("en")
                or code,
                "status": "resolved",
                "stale": datetime.now(timezone.utc).timestamp()
                - _metadata["build_epoch"]
                > 75 * 86400,
                **_metadata,
            }
    except Exception:
        return {"country": None, "status": "database_unavailable"}

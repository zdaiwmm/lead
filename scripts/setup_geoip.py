"""Install the public DB-IP Lite country MMDB with attribution and provenance."""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import maxminddb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.analytics_geo import database_path


def install(month, destination):
    if not re.fullmatch(r"20[0-9]{2}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("invalid month")
    url = f"https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz"
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
        compressed = Path(folder) / "country.gz"
        temporary = Path(folder) / "country.mmdb"
        size = 0
        with (
            httpx.stream("GET", url, timeout=60, follow_redirects=False) as response,
            compressed.open("wb") as output,
        ):
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise ValueError("download exceeds limit")
                output.write(chunk)
        size = 0
        with gzip.open(compressed, "rb") as source, temporary.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise ValueError("database exceeds limit")
                output.write(chunk)
        with maxminddb.open_database(str(temporary)) as reader:
            meta = reader.metadata()
            if not (reader.get("8.8.8.8") or {}).get("country", {}).get("iso_code"):
                raise ValueError("invalid country database")
            record = {
                "source": url,
                "license": "CC-BY-4.0",
                "attribution": "IP Geolocation by DB-IP",
                "attribution_url": "https://db-ip.com",
                "sha256": hashlib.sha256(temporary.read_bytes()).hexdigest(),
                "build_epoch": meta.build_epoch,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        provenance = destination.with_suffix(".json")
        provenance.write_text(json.dumps(record, indent=2) + "\n")
        provenance.chmod(0o600)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=datetime.now(timezone.utc).strftime("%Y-%m"))
    parser.add_argument("--destination", default=str(database_path()))
    args = parser.parse_args()
    print(json.dumps(install(args.month, args.destination)))

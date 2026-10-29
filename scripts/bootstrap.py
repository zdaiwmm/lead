"""Create only project-local configuration. Never print credentials."""

import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
state = Path(os.environ.get("LEAD_STATE_DIR") or ROOT / ".state").resolve()
state.mkdir(exist_ok=True, mode=0o700)
config = state / "config.json"
if not config.exists():
    values = {
        "owner_password": secrets.token_urlsafe(32),
        "runtime_password": secrets.token_urlsafe(32),
        "session_secret": secrets.token_urlsafe(48),
        "owner_id": secrets.token_hex(16),
        "api_origin": "http://127.0.0.1:8088",
        "web_origin": "http://localhost:3100",
        "temporal_address": "127.0.0.1:17233",
        "browser_concurrency": 3,
    }
    config.write_text(json.dumps(values, indent=2))
    os.chmod(config, 0o600)
else:
    values = json.loads(config.read_text())
if not values.get("auth_password"):
    values["auth_password"] = secrets.token_urlsafe(32)
    temporary = state / "config.next"
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(values, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, config)
env = state / "infra.env"
env.write_text("LEAD_DB_OWNER_PASSWORD=" + values["owner_password"] + "\n")
os.chmod(env, 0o600)
print("Local configuration ready.")

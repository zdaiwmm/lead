import base64
import hashlib
import hmac
import time

from .config import CONFIG


def token(subject, ttl=86400):
    data = f"{subject}|{int(time.time()) + ttl}".encode()
    sig = hmac.new(CONFIG["session_secret"].encode(), data, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(data).decode() + "." + sig


def valid(value, subject):
    try:
        encoded, sig = value.split(".")
        data = base64.urlsafe_b64decode(encoded)
        expected = hmac.new(
            CONFIG["session_secret"].encode(), data, hashlib.sha256
        ).hexdigest()
        who, expiry = data.decode().rsplit("|", 1)
        return (
            hmac.compare_digest(sig, expected)
            and who == subject
            and int(expiry) > time.time()
        )
    except (ValueError, TypeError):
        return False


def internal_token():
    return hmac.new(
        CONFIG["session_secret"].encode(), b"browser-gateway", hashlib.sha256
    ).hexdigest()

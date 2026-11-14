#!/usr/bin/env python3
"""Fixed-purpose Xray adapter; root provisioned, then exec'd as a separate UID.

Only the deployment operator's fixed, private configuration is an entry point.
The check-client-env argument only validates inherited client proxy settings.
There are no path arguments, server environment overrides or config dump mode.
Never import this from the Codex user's checkout as root; install a root-owned
copy at SCRIPT and a separately verified official Xray 26.10.31 binary at XRAY.
See lead-codex-egress.service for the installation contract and remaining gates.
"""
import copy
import fcntl
import json
import os
import pwd
import re
import resource
import stat
import sys
import uuid
from pathlib import PurePosixPath

SCRIPT = "/opt/lead-codex-egress/egress.py"
XRAY = "/opt/lead-codex-egress/xray"
OPERATIONS = "/etc/lead-codex-egress/egress.json"
LEGACY_SOURCE = "/srv/lead-prod-vault/incoming/lead-model-xray/config.json"
DOMAINS = ("auth.openai.com", "chatgpt.com", "api.openai.com")
MAX_BYTES = 1048576


class ConfigError(ValueError):
    """Messages contain fixed labels only, never private input or paths."""


def require(condition):
    if not condition:
        raise ConfigError("invalid private egress configuration")


def fields(value, names):
    require(type(value) is dict and set(value) == set(names))
    return value


def canonical_path(value):
    require(type(value) is str and value.startswith("/") and "\x00" not in value)
    require(str(PurePosixPath(value)) == value and ".." not in PurePosixPath(value).parts)
    require(value != "/")
    return value


def trusted_open(path, private=True, legacy=False):
    """Walk trusted root-owned directories by descriptor; never follow symlinks."""
    canonical_path(path)
    require(not legacy or path == LEGACY_SOURCE)
    legacy_parents = {str(PurePosixPath(LEGACY_SOURCE).parent), str(PurePosixPath(LEGACY_SOURCE).parent.parent)}
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    current = "/"
    try:
        for part in PurePosixPath(path).parts[1:-1]:
            st = os.fstat(directory)
            require((st.st_uid == 0 or legacy and current in legacy_parents and st.st_uid == 1000)
                and stat.S_ISDIR(st.st_mode) and not st.st_mode & 0o022)
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory)
            os.close(directory)
            directory = following
            current = str(PurePosixPath(current) / part)
        st = os.fstat(directory)
        require((st.st_uid == 0 or legacy and current in legacy_parents and st.st_uid == 1000)
            and stat.S_ISDIR(st.st_mode) and not st.st_mode & 0o022)
        descriptor = os.open(PurePosixPath(path).name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        try:
            st = os.fstat(descriptor)
            require((st.st_uid == 0 or legacy and st.st_uid == 1000)
                and stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
            require(not st.st_mode & 0o7022)
            if private:
                require(stat.S_IMODE(st.st_mode) in (0o400, 0o600))
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(directory)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def read_private_json(path, legacy=False):
    try:
        with os.fdopen(trusted_open(path, legacy=legacy), "rb") as source:
            data = source.read(MAX_BYTES + 1)
        require(len(data) <= MAX_BYTES)
        return json.loads(data, object_pairs_hook=unique_object,
            parse_constant=lambda _: require(False))
    except Exception:
        raise ConfigError("private egress input unavailable or invalid") from None


def validate_operations(operations):
    require(type(operations) is dict)
    required = {"source_config", "outbound_tag", "listen_port", "network_user", "auth_file"}
    require(set(operations) in (required, required | {"source_owner_uid"}))
    canonical_path(operations["source_config"])
    canonical_path(operations["auth_file"])
    owner = operations.get("source_owner_uid", 0)
    require(type(owner) is int and owner in (0, 1000))
    require(owner == 0 or operations["source_config"] == LEGACY_SOURCE)
    require(operations["auth_file"].startswith("/etc/"))
    require(type(operations["listen_port"]) is int and 1024 <= operations["listen_port"] <= 65535)
    require(type(operations["outbound_tag"]) is str and
        re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operations["outbound_tag"]) is not None)
    user = operations["network_user"]
    require(type(user) is str and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) is not None)
    require(user not in ("root", "lead-codex", "nobody"))


def validate_auth(auth):
    fields(auth, ("user", "pass"))
    for key, minimum in (("user", 16), ("pass", 32)):
        value = auth[key]
        require(type(value) is str and minimum <= len(value) <= 128)
        require(re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None and len(set(value)) >= 12)
    # This validates format, not entropy. The operator MUST generate both values
    # randomly; this service deliberately never creates or prints credentials.


def check_client_env(environment):
    """Validate only inherited strings, without reading files or requiring root."""
    proxy = environment.get("HTTPS_PROXY")
    require(type(proxy) is str)
    match = re.fullmatch(r"http://([A-Za-z0-9_-]{16,128}):([A-Za-z0-9_-]{32,128})@127\.0\.0\.1:([1-9][0-9]{3,4})", proxy)
    require(match is not None)
    require(1024 <= int(match[3]) <= 65535)
    validate_auth({"user": match[1], "pass": match[2]})
    bypass = environment.get("NO_PROXY")
    require(type(bypass) is str and bool(bypass))
    entries = bypass.split(",")
    require(all(item.strip() in ("localhost", "127.0.0.1", "::1", "shui.click") for item in entries))
    for key, value in environment.items():
        if key.upper() in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            require(value == proxy)
        if key.upper() == "NO_PROXY":
            require(value == bypass)
    require("NODE_USE_ENV_PROXY" not in environment)


def selected_outbound(source, tag):
    require(type(source) is dict and type(source.get("outbounds")) is list)
    matches = [item for item in source["outbounds"] if type(item) is dict and item.get("tag") == tag]
    require(len(matches) == 1)
    outbound = matches[0]
    fields(outbound, ("tag", "protocol", "settings", "streamSettings"))
    require(outbound["protocol"] == "vless")
    settings = fields(outbound["settings"], ("vnext",))
    require(type(settings["vnext"]) is list and len(settings["vnext"]) == 1)
    endpoint = fields(settings["vnext"][0], ("address", "port", "users"))
    require(type(endpoint["address"]) is str and
        re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", endpoint["address"]) is not None)
    require(type(endpoint["port"]) is int and 1 <= endpoint["port"] <= 65535)
    require(type(endpoint["users"]) is list and len(endpoint["users"]) == 1)
    user = fields(endpoint["users"][0], ("id", "encryption", "flow"))
    require(type(user["id"]) is str)
    try:
        uuid.UUID(user["id"])
    except (ValueError, AttributeError):
        raise ConfigError("invalid private egress configuration") from None
    require(user["encryption"] == "none" and user["flow"] in ("", "xtls-rprx-vision"))
    stream = fields(outbound["streamSettings"], ("network", "security", "realitySettings"))
    require(stream["network"] == "raw" and stream["security"] == "reality")
    reality = fields(stream["realitySettings"], ("serverName", "fingerprint", "password", "shortId", "spiderX"))
    require(type(reality["serverName"]) is str and
        re.fullmatch(r"[A-Za-z0-9.-]{1,253}", reality["serverName"]) is not None)
    require(type(reality["fingerprint"]) is str and reality["fingerprint"] in
        ("chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized"))
    require(type(reality["password"]) is str and
        re.fullmatch(r"[A-Za-z0-9_-]{43}", reality["password"]) is not None)
    require(type(reality["shortId"]) is str and
        re.fullmatch(r"(?:[a-fA-F0-9]{2}){0,8}", reality["shortId"]) is not None)
    require(type(reality["spiderX"]) is str and reality["spiderX"].startswith("/") and
        len(reality["spiderX"]) <= 2048 and not any(ord(c) < 32 for c in reality["spiderX"]))
    result = copy.deepcopy(outbound)
    result["tag"] = "codex-upstream"
    return result


def build_config(operations, source, auth):
    validate_operations(operations)
    validate_auth(auth)
    outbound = selected_outbound(source, operations["outbound_tag"])
    return {
        "log": {"access": "none", "error": "none", "loglevel": "none"},
        "inbounds": [{"tag": "codex-connect", "listen": "127.0.0.1", "port": operations["listen_port"],
            "protocol": "http", "settings": {"users": [copy.deepcopy(auth)], "allowTransparent": False},
            "sniffing": {"enabled": True, "destOverride": ["tls"], "routeOnly": False,
                "metadataOnly": False, "ipsExcluded": ["0.0.0.0/0", "::/0"]}}],
        "outbounds": [{"tag": "deny", "protocol": "blackhole", "settings": {}}, outbound],
        "routing": {"domainStrategy": "AsIs", "rules": [
            {"type": "field", "ip": ["0.0.0.0/0", "::/0"], "outboundTag": "deny"},
            {"type": "field", "inboundTag": ["codex-connect"], "network": "tcp", "port": "443",
                "protocol": ["tls"], "domain": ["full:" + domain for domain in DOMAINS],
                "outboundTag": "codex-upstream"},
            {"type": "field", "network": "tcp,udp", "outboundTag": "deny"},
        ]},
    }


def network_identity(name):
    user = pwd.getpwnam(name)
    codex = pwd.getpwnam("lead-codex")
    require(user.pw_uid > 0 and user.pw_gid > 0 and user.pw_uid not in (1000, codex.pw_uid)
        and user.pw_gid not in (1000, codex.pw_gid))
    require(user.pw_shell in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"))
    return user.pw_uid, user.pw_gid


def verify_drop(uid, gid):
    """Read fixed process metadata after dropping IDs; no private file access."""
    try:
        with open("/proc/self/status", encoding="ascii") as source:
            status = dict(line.split(":", 1) for line in source if ":" in line)
        require([int(value) for value in status["Uid"].split()] == [uid] * 4)
        require([int(value) for value in status["Gid"].split()] == [gid] * 4)
        require(not status["Groups"].strip() and status["NoNewPrivs"].strip() == "1")
        require(all(int(status[key].strip(), 16) == 0 for key in ("CapEff", "CapPrm", "CapInh", "CapAmb")))
    except Exception:
        raise ConfigError("egress privilege drop rejected") from None


def exec_xray(config, uid, gid):
    """Replace root wrapper, never retain a root supervisor or child process."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    memory = os.memfd_create("codex-egress", 0x01 | 0x02)  # CLOEXEC | ALLOW_SEALING (Linux).
    try:
        data = json.dumps(config, separators=(",", ":")).encode()
        offset = 0
        while offset < len(data):
            written = os.write(memory, data[offset:])
            require(written > 0)
            offset += written
        os.lseek(memory, 0, os.SEEK_SET)
        fcntl.fcntl(memory, 1033, 0x01 | 0x02 | 0x04 | 0x08)  # F_ADD_SEALS; SEAL/SHRINK/GROW/WRITE.
        os.dup2(memory, 0, inheritable=True)
    finally:
        os.close(memory)
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    verify_drop(uid, gid)
    os.execve(XRAY, [XRAY, "run", "-c", "stdin:"], {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})


def main():
    try:
        if sys.argv[1:] == ["check-client-env"]:
            check_client_env(os.environ)
            return 0
        require(len(sys.argv) == 1 and os.geteuid() == 0 and sys.platform == "linux")
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        for code in (SCRIPT, XRAY):
            os.close(trusted_open(code, private=False))
        operations = read_private_json(OPERATIONS)
        validate_operations(operations)
        uid, gid = network_identity(operations["network_user"])
        source = (read_private_json(operations["source_config"], legacy=True)
            if operations.get("source_owner_uid") == 1000 else read_private_json(operations["source_config"]))
        config = build_config(operations, source,
            read_private_json(operations["auth_file"]))
        exec_xray(config, uid, gid)
    except Exception:
        print("codex egress configuration or startup rejected", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    sys.exit(main())

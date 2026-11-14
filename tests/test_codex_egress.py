"""Synthetic inputs only: no real account, proxy, service or network activity."""
import copy
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE / "infra/local-executor/egress.py"
spec = importlib.util.spec_from_file_location("codex_egress", SCRIPT)
egress = importlib.util.module_from_spec(spec) if spec else None
if SCRIPT.exists():
    spec.loader.exec_module(egress)


def seed():
    return {"outbounds": [{
        "tag": "synthetic-existing", "protocol": "vless",
        "settings": {"vnext": [{"address": "edge.example.invalid", "port": 443,
            "users": [{"id": "00000000-0000-4000-8000-000000000000", "encryption": "none",
                "flow": "xtls-rprx-vision"}]}]},
        "streamSettings": {"network": "raw", "security": "reality",
            "realitySettings": {"serverName": "edge.example.invalid", "fingerprint": "chrome",
                "password": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "shortId": "0123456789abcdef",
                "spiderX": "/"}},
    }]}


def operations():
    return {"source_config": "/etc/synthetic-source/seed.json", "outbound_tag": "synthetic-existing",
        "listen_port": 18181, "network_user": "synthetic-egress", "auth_file": "/etc/synthetic/auth.json"}


AUTH = {"user": "synthetic_6B8AtMDi3Qz19V2f", "pass": "synthetic_q9AZ04XdLbN7kP2yU6mE83Wr"}
PROXY = "http://" + AUTH["user"] + ":" + AUTH["pass"] + "@127.0.0.1:18181"
STATUS = "Uid:\t987 987 987 987\nGid:\t986 986 986 986\nGroups:\nNoNewPrivs:\t1\n" + \
    "CapEff:\t0000000000000000\nCapPrm:\t0000000000000000\nCapInh:\t0000000000000000\nCapAmb:\t0000000000000000\n"


class ClientEnvironmentTests(unittest.TestCase):
    def test_validates_without_opening_root_files_or_requiring_root(self):
        with patch.object(egress.sys, "argv", ["egress.py", "check-client-env"]), \
             patch.dict(egress.os.environ, {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost,127.0.0.1,::1,shui.click"}, clear=True), \
             patch.object(egress, "read_private_json", side_effect=AssertionError("must not read root files")), \
             patch.object(egress.os, "geteuid", side_effect=AssertionError("must not need root")):
            self.assertEqual(egress.main(), 0)

    def test_rejects_missing_weak_remote_ambiguous_or_conflicting_proxies(self):
        cases = [{}, {"HTTPS_PROXY": ""}, {"HTTPS_PROXY": "http://127.0.0.1:18181"},
            {"HTTPS_PROXY": PROXY.replace(":18181", ":443")},
            {"HTTPS_PROXY": PROXY.replace("127.0.0.1", "localhost")},
            {"HTTPS_PROXY": PROXY.replace("127.0.0.1", "remote.example")},
            {"HTTPS_PROXY": PROXY + "/path"}, {"HTTPS_PROXY": PROXY + "#secret"},
            {"HTTPS_PROXY": PROXY.replace("http://", "https://")},
            {"HTTPS_PROXY": PROXY.replace(AUTH["pass"], "weak")},
            {"HTTPS_PROXY": PROXY, "https_proxy": PROXY + "/"},
            {"HTTPS_PROXY": PROXY, "HTTP_PROXY": ""},
            {"HTTPS_PROXY": PROXY, "ALL_PROXY": PROXY.replace(":18181", ":18182")},
            {"HTTPS_PROXY": PROXY, "NODE_USE_ENV_PROXY": "1"}]
        for env in cases:
            with self.subTest(env="redacted"), self.assertRaises(egress.ConfigError):
                egress.check_client_env(env)
        egress.check_client_env({"HTTPS_PROXY": PROXY, "https_proxy": PROXY, "HTTP_PROXY": PROXY,
            "ALL_PROXY": PROXY, "NO_PROXY": "localhost,shui.click", "no_proxy": "localhost,shui.click"})

    def test_rejects_bypass_variants_and_reports_no_credentials(self):
        for bypass in ("", "*", ".openai.com", "api.openai.com", "shui.click.evil", ".shui.click",
                "127.0.0.0/8", "localhost:443", "localhost,,shui.click"):
            with self.subTest(bypass=bypass), self.assertRaises(egress.ConfigError):
                egress.check_client_env({"HTTPS_PROXY": PROXY, "NO_PROXY": bypass})
        with self.assertRaises(egress.ConfigError):
            egress.check_client_env({"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost", "no_proxy": "shui.click"})
        output = io.StringIO()
        with patch.object(egress.sys, "argv", ["egress.py", "check-client-env"]), \
             patch.dict(egress.os.environ, {"HTTPS_PROXY": PROXY + "/"}, clear=True), redirect_stderr(output):
            self.assertEqual(egress.main(), 78)
        self.assertNotIn(AUTH["user"], output.getvalue())
        self.assertNotIn(AUTH["pass"], output.getvalue())


class EntrypointTests(unittest.TestCase):
    def test_egress_component_can_build_a_default_deny_config(self):
        self.assertTrue(callable(getattr(egress, "build_config", None)), "egress component is missing")
        self.assertEqual(egress.build_config(operations(), seed(), AUTH)["outbounds"][0]["protocol"], "blackhole")


class ConfigurationTests(unittest.TestCase):
    def test_legacy_owner_trust_is_explicit_and_limited_to_verified_path(self):
        for owner in (974, 987, 999, True):
            with self.subTest(owner=owner), self.assertRaises(egress.ConfigError):
                egress.validate_operations({**operations(), "source_owner_uid": owner})
        with self.assertRaises(egress.ConfigError):
            egress.validate_operations({**operations(), "source_owner_uid": 1000})
        egress.validate_operations({**operations(), "source_config":
            "/srv/lead-prod-vault/incoming/lead-model-xray/config.json", "source_owner_uid": 1000})

    def test_builds_authenticated_loopback_and_does_not_mutate_source(self):
        original = seed()
        before = copy.deepcopy(original)
        config = egress.build_config(operations(), original, AUTH)
        self.assertEqual(original, before)
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(config["inbounds"][0]["port"], 18181)
        self.assertEqual(config["inbounds"][0]["protocol"], "http")
        self.assertEqual(config["inbounds"][0]["settings"], {"users": [AUTH], "allowTransparent": False})
        self.assertEqual(config["outbounds"][0]["protocol"], "blackhole")
        actual = copy.deepcopy(config["outbounds"][1])
        actual["tag"] = "synthetic-existing"
        self.assertEqual(actual, original["outbounds"][0])
        self.assertEqual(config["log"], {"access": "none", "error": "none", "loglevel": "none"})

    def test_rejects_unsafe_operations_schema_and_weak_or_missing_auth(self):
        for key, value in [("listen_port", 80), ("listen_port", True), ("listen_port", 65536),
                ("source_config", "relative.json"), ("source_config", "/etc/../secret"),
                ("auth_file", "/tmp/auth.json"), ("network_user", "root"),
                ("network_user", "lead-codex"), ("exec", "/bin/sh"), ("listen", "0.0.0.0")]:
            bad = {**operations(), key: value}
            with self.subTest(key=key, value=value), self.assertRaises(egress.ConfigError):
                egress.build_config(bad, seed(), AUTH)
        for auth in [None, {}, {"user": "a", "pass": "password"}, {**AUTH, "accounts": []},
                {**AUTH, "user": "contains:colon"}, {**AUTH, "pass": "x" * 32}]:
            with self.subTest(auth="redacted"), self.assertRaises(egress.ConfigError):
                egress.build_config(operations(), seed(), auth)

    def test_rejects_unknown_transport_and_unsafe_tls_or_additional_sources(self):
        mutations = [
            ("protocol", "freedom"), ("proxySettings", {"tag": "existing-api"}),
            ("sendThrough", "127.0.0.1"), ("mux", {"enabled": True}),
        ]
        for key, value in mutations:
            bad = seed()
            bad["outbounds"][0][key] = value
            with self.subTest(key=key), self.assertRaises(egress.ConfigError):
                egress.build_config(operations(), bad, AUTH)
        for setting in [
            {"network": "unknown", "security": "tls"},
            {"network": "tcp", "security": "none"},
            {"network": "tcp", "security": "tls", "tlsSettings": {"allowInsecure": True}},
            {"network": "tcp", "security": "tls", "tlsSettings": {"privateKey": "SECRET"}},
            {"network": "tcp", "security": "tls", "tlsSettings": {"certificates": [{"keyFile": "/private/key"}]}},
            {"network": "tcp", "security": "tls", "sockopt": {"dialerProxy": "old-api"}},
        ]:
            bad = seed()
            bad["outbounds"][0]["streamSettings"] = setting
            with self.subTest(setting="redacted"), self.assertRaises(egress.ConfigError):
                egress.build_config(operations(), bad, AUTH)
        bad = seed()
        bad["outbounds"].append(copy.deepcopy(bad["outbounds"][0]))
        with self.assertRaises(egress.ConfigError):
            egress.build_config(operations(), bad, AUTH)

    def test_rejects_missing_or_extra_client_and_transport_fields(self):
        for part, key, value in [
                ("client", "flow", "unknown-flow"), ("client", "encryption", "unknown-encryption"),
                ("client", "id", "not-a-uuid"), ("client", "reverse", {}),
                ("reality", "privateKey", "SYNTHETIC_SECRET"),
                ("reality", "keyFile", "/private/synthetic"),
                ("reality", "allowInsecure", True), ("reality", "serverName", "invalid host"),
                ("reality", "password", "not-a-public-key"), ("reality", "shortId", "z0"),
                ("reality", "fingerprint", "unknown"), ("reality", "spiderX", "https://other.invalid")]:
            bad = seed()
            target = (bad["outbounds"][0]["settings"]["vnext"][0]["users"][0] if part == "client"
                else bad["outbounds"][0]["streamSettings"]["realitySettings"])
            target[key] = value
            with self.subTest(part=part, key=key), self.assertRaises(egress.ConfigError) as caught:
                egress.build_config(operations(), bad, AUTH)
            self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))
        bad = seed()
        bad["outbounds"][0]["settings"]["vnext"][0]["users"].append(
            copy.deepcopy(bad["outbounds"][0]["settings"]["vnext"][0]["users"][0]))
        with self.assertRaises(egress.ConfigError):
            egress.build_config(operations(), bad, AUTH)
        with self.assertRaises(egress.ConfigError):
            egress.build_config(operations(), {"outbounds": []}, AUTH)

    def test_rule_targets_are_exact_and_tls_443_only_with_default_deny(self):
        config = egress.build_config(operations(), seed(), AUTH)
        self.assertEqual(set(config), {"inbounds", "outbounds", "routing", "log"})
        self.assertEqual(config["routing"]["domainStrategy"], "AsIs")
        allow = [rule for rule in config["routing"]["rules"] if rule["outboundTag"] != "deny"]
        self.assertEqual(len(allow), 1)
        self.assertEqual(set(allow[0]["domain"]), {
            "full:auth.openai.com", "full:chatgpt.com", "full:api.openai.com"})
        self.assertEqual(allow[0]["port"], "443")
        self.assertEqual(allow[0]["network"], "tcp")
        self.assertEqual(allow[0]["protocol"], ["tls"])
        self.assertEqual(config["inbounds"][0]["sniffing"], {"enabled": True, "destOverride": ["tls"],
            "routeOnly": False, "metadataOnly": False, "ipsExcluded": ["0.0.0.0/0", "::/0"]})
        self.assertEqual(config["routing"]["rules"][-1]["outboundTag"], "deny")


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "private.json"
        self.path.write_text(json.dumps(AUTH))
        self.path.chmod(0o600)
        real = os.fstat

        def synthetic_root_metadata(fd):
            st = real(fd)
            values = list(st)
            values[4] = 0  # Owner is the sole filesystem fact simulated on this non-root Mac.
            if stat.S_ISDIR(st.st_mode):
                values[0] = stat.S_IFDIR | 0o755
            return os.stat_result(values)

        self.meta = patch.object(egress.os, "fstat", side_effect=synthetic_root_metadata)
        self.meta.start()
        self.addCleanup(self.meta.stop)

    def test_reads_private_regular_file_and_rejects_permission_widening(self):
        self.assertEqual(egress.read_private_json(str(self.path)), AUTH)
        for mode in (0o644, 0o640, 0o666):
            self.path.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(egress.ConfigError):
                egress.read_private_json(str(self.path))

    def test_legacy_owner_exception_applies_to_exact_file_and_two_parents_only(self):
        incoming = self.root / "incoming"
        directory = incoming / "legacy"
        directory.mkdir(parents=True)
        source = directory / "config.json"
        source.write_text(json.dumps(AUTH))
        source.chmod(0o600)
        original = egress.os.fstat
        allowed = {incoming.stat().st_ino, directory.stat().st_ino, source.stat().st_ino}

        def owners(fd):
            st = original(fd)
            data = list(st)
            if st.st_ino in allowed:
                data[4] = 1000
            return os.stat_result(data)

        with patch.object(egress, "LEGACY_SOURCE", str(source)), patch.object(egress.os, "fstat", side_effect=owners):
            self.assertEqual(egress.read_private_json(str(source), legacy=True), AUTH)
            with self.assertRaises(egress.ConfigError):
                egress.read_private_json(str(source))
            with self.assertRaises(egress.ConfigError):
                egress.read_private_json(str(self.path), legacy=True)
            allowed.add(self.root.stat().st_ino)
            with self.assertRaises(egress.ConfigError):
                egress.read_private_json(str(source), legacy=True)

    def test_rejects_symlink_file_and_symlink_ancestor(self):
        link = self.root / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(egress.ConfigError):
            egress.read_private_json(str(link))
        directory = self.root / "directory"
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(egress.ConfigError):
            egress.read_private_json(str(directory / "private.json"))

    def test_rejects_duplicate_keys_oversize_nonregular_missing_and_malformed(self):
        for body in ['{"pass":"SECRET","pass":"duplicate"}', '{"SECRET":', "x" * (1048576 + 1)]:
            self.path.write_text(body)
            with self.subTest(body="redacted"), self.assertRaises(egress.ConfigError) as caught:
                egress.read_private_json(str(self.path))
            self.assertNotIn("SECRET", str(caught.exception))
        with self.assertRaises(egress.ConfigError):
            egress.read_private_json(str(self.root / "missing"))
        with self.assertRaises(egress.ConfigError):
            egress.read_private_json(str(self.root))

    def test_rejects_nonroot_file_and_writable_ancestor(self):
        with patch.object(egress.os, "fstat", side_effect=lambda fd: SimpleNamespace(
                st_uid=501, st_mode=stat.S_IFREG | 0o600)):
            with self.assertRaises(egress.ConfigError):
                egress.read_private_json(str(self.path))

    def test_rejects_hardlinks(self):
        os.link(self.path, self.root / "other.json")
        with self.assertRaises(egress.ConfigError):
            egress.read_private_json(str(self.path))
        with patch.object(egress.os, "fstat", return_value=SimpleNamespace(
                st_uid=0, st_mode=stat.S_IFDIR | 0o777)):
            with self.assertRaises(egress.ConfigError):
                egress.read_private_json(str(self.path))


class LaunchTests(unittest.TestCase):
    def test_zero_capabilities_all_ids_and_no_new_privileges_are_mandatory(self):
        with patch("builtins.open", mock_open(read_data=STATUS)) as opened:
            egress.verify_drop(987, 986)
            opened.assert_called_once_with("/proc/self/status", encoding="ascii")
        for unsafe in [STATUS.replace("987", "0"), STATUS.replace("986", "0"),
                STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
                STATUS.replace("Groups:\n", "Groups:\t1000\n"),
                *[STATUS.replace(cap + ":\t0000000000000000", cap + ":\t0000000000000001")
                    for cap in ("CapEff", "CapPrm", "CapInh", "CapAmb")]]:
            with self.subTest(status="synthetic"), patch("builtins.open", mock_open(read_data=unsafe)), \
                 self.assertRaises(egress.ConfigError):
                egress.verify_drop(987, 986)

    def test_drops_all_groups_and_saved_ids_before_exec_with_memory_stdin(self):
        events = []
        config = egress.build_config(operations(), seed(), AUTH)
        with tempfile.TemporaryFile() as memory:
            fd = memory.fileno()
            with patch.object(egress.os, "memfd_create", return_value=fd, create=True), \
                 patch.object(egress.fcntl, "fcntl", side_effect=lambda *args: events.append("seal")), \
                 patch.object(egress.os, "dup2", side_effect=lambda *args, **kw: events.append("stdin")), \
                 patch.object(egress.os, "close"), \
                 patch.object(egress.resource, "setrlimit"), \
                 patch.object(egress.os, "setgroups", side_effect=lambda groups: events.append(("groups", groups))), \
                 patch.object(egress.os, "setresgid", side_effect=lambda *ids: events.append(("gid", ids)), create=True), \
                 patch.object(egress.os, "setresuid", side_effect=lambda *ids: events.append(("uid", ids)), create=True), \
                 patch.object(egress, "verify_drop", side_effect=lambda *ids: events.append("verified")), \
                 patch.object(egress.os, "execve", side_effect=lambda *args: events.append(("exec", args))):
                egress.exec_xray(config, 987, 986)
            memory.seek(0)
            self.assertEqual(json.load(memory), config)
        self.assertEqual(events[:5], ["seal", "stdin", ("groups", []), ("gid", (986, 986, 986)),
            ("uid", (987, 987, 987))])
        self.assertEqual(events[5], "verified")
        self.assertEqual(events[6], ("exec", ("/opt/lead-codex-egress/xray",
            ["/opt/lead-codex-egress/xray", "run", "-c", "stdin:"],
            {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})))

    def test_exec_never_occurs_if_any_privilege_drop_fails(self):
        for failure in ("setgroups", "setresgid", "setresuid"):
            with self.subTest(failure=failure), tempfile.TemporaryFile() as memory, \
                 patch.object(egress.os, "memfd_create", return_value=memory.fileno(), create=True), \
                 patch.object(egress.fcntl, "fcntl"), patch.object(egress.os, "dup2"), \
                 patch.object(egress.os, "close"), patch.object(egress.resource, "setrlimit"), \
                 patch.object(egress.os, "setgroups"), \
                 patch.object(egress.os, "setresgid", create=True), \
                 patch.object(egress.os, "setresuid", create=True), \
                 patch.object(egress.os, failure, side_effect=OSError("SECRET")), \
                 patch.object(egress.os, "execve") as execute:
                with self.assertRaises(OSError):
                    egress.exec_xray({}, 987, 986)
                execute.assert_not_called()

    def test_main_has_fixed_entrypoint_and_redacts_all_errors(self):
        for argv in [["egress.py", "--config", "/private/SECRET"], ["egress.py"]]:
            output = io.StringIO()
            with patch.object(egress.sys, "argv", argv), patch.object(egress.os, "geteuid", return_value=0), \
                 patch.object(egress.sys, "platform", "linux"), patch.object(egress.resource, "setrlimit"), \
                 patch.object(egress, "trusted_open", return_value=999), patch.object(egress.os, "close"), \
                 patch.object(egress, "read_private_json", side_effect=OSError("SECRET")), \
                 redirect_stderr(output):
                self.assertEqual(egress.main(), 78)
            self.assertNotIn("SECRET", output.getvalue())

    def test_checks_fixed_code_and_config_before_launch_and_resolves_a_separate_uid(self):
        reads = []
        verified = []
        launched = []
        users = {"synthetic-egress": SimpleNamespace(pw_uid=987, pw_gid=986, pw_shell="/usr/sbin/nologin"),
            "lead-codex": SimpleNamespace(pw_uid=974, pw_gid=974, pw_shell="/usr/sbin/nologin")}
        def private(path):
            reads.append(path)
            return {"/etc/lead-codex-egress/egress.json": operations(),
                "/etc/synthetic-source/seed.json": seed(), "/etc/synthetic/auth.json": AUTH}[path]
        with patch.object(egress.sys, "argv", ["egress.py"]), \
             patch.object(egress.sys, "platform", "linux"), patch.object(egress.os, "geteuid", return_value=0), \
             patch.object(egress, "trusted_open", side_effect=lambda path, private: verified.append(path) or 999), \
             patch.object(egress.os, "close"), patch.object(egress, "read_private_json", side_effect=private), \
             patch.object(egress.pwd, "getpwnam", side_effect=users.__getitem__), \
             patch.object(egress.resource, "setrlimit"), \
             patch.object(egress, "exec_xray", side_effect=lambda config, uid, gid: launched.append((config, uid, gid))):
            self.assertEqual(egress.main(), 0)
        self.assertEqual(verified, ["/opt/lead-codex-egress/egress.py", "/opt/lead-codex-egress/xray"])
        self.assertEqual(reads, ["/etc/lead-codex-egress/egress.json", "/etc/synthetic-source/seed.json",
            "/etc/synthetic/auth.json"])
        self.assertEqual(launched[0][1:], (987, 986))

    def test_rejects_root_shared_codex_identity_and_login_shell(self):
        codex = SimpleNamespace(pw_uid=974, pw_gid=974, pw_shell="/usr/sbin/nologin")
        for uid, gid, shell in [(0, 986, "/bin/false"), (987, 0, "/bin/false"),
                (974, 986, "/bin/false"), (987, 974, "/bin/false"),
                (1000, 986, "/bin/false"), (987, 1000, "/bin/false"), (987, 986, "/bin/sh")]:
            account = SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_shell=shell)
            with patch.object(egress.pwd, "getpwnam", side_effect=[account, codex]), \
                 self.subTest(uid=uid, gid=gid, shell=shell), self.assertRaises(egress.ConfigError):
                egress.network_identity("synthetic-egress")


if __name__ == "__main__":
    unittest.main()

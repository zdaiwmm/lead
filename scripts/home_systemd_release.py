#!/usr/bin/python3 -IB
"""Trusted, node-local hook for poll_home_release.py. Install outside release images."""

import argparse
import base64
import fcntl
import grp
import hashlib
import io
import json
import os
import pwd
import re
import shlex
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from email.parser import BytesParser
from pathlib import Path
from uuid import UUID

VAULT = Path("/srv/lead-prod-vault")
STORE = VAULT / "lead-git-releases"
TOOLS = Path("/usr/local/lib/lead-home-release")
SHA = re.compile("[a-f0-9]{40}")
BIN = Path("/run/lead-home/bin")
SERVICES = BIN / "original/services"
WEB = BIN / "web"
# Only application processes. Database, network and authentication keep their own units.
ROLES = {"api": SERVICES, "web": WEB, "collector": SERVICES, "worker": SERVICES,
         "browser": SERVICES, "maintenance": SERVICES}


class Refused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def run(*args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout.rstrip("\n")


def public_main():
    """Read only the fixed public GitHub ref, without proxy or redirect trust."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'ambiguous public main response')
            value[key] = item
        return value

    request = urllib.request.Request('https://api.github.com/repos/zdaiwmm/lead/git/ref/heads/main',
        headers={'User-Agent': 'lead-home-release', 'Accept': 'application/vnd.github+json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            require(response.status == 200, 'public main response unavailable')
            raw = response.read(65537)
        require(len(raw) <= 65536, 'public main response too large')
        value = json.loads(raw, object_pairs_hook=unique)
    except (OSError, ValueError, RecursionError):
        raise Refused('public main reference unavailable') from None
    require(type(value) is dict and value.get('ref') == 'refs/heads/main', 'public main reference differs')
    target = value.get('object')
    require(type(target) is dict and target.get('type') == 'commit'
            and type(target.get('sha')) is str and SHA.fullmatch(target['sha']), 'public main commit differs')
    return target['sha']


def sync_public_main(mirror, sha):
    """An imported mirror is usable only with fresh, unchanged public proof."""
    require(SHA.fullmatch(sha), 'invalid full SHA')
    published = public_main()

    def local_main():
        try:
            value = run('git', '--git-dir', mirror, 'rev-parse', '--verify', '--quiet', 'refs/heads/main')
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
            return None
        require(SHA.fullmatch(value), 'local main reference unavailable')
        return value

    if local_main() != published:
        run('git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null', '--git-dir', mirror,
            'fetch', '--no-tags', 'https://github.com/zdaiwmm/lead.git',
            '+refs/heads/main:refs/heads/main', timeout=120)
    require(public_main() == published, 'public main changed during source verification')
    require(local_main() == published, 'local main differs from public main')
    run('git', '--git-dir', mirror, 'merge-base', '--is-ancestor', sha, 'refs/heads/main')
    return published


def unit(role):
    return role if role.endswith((".service", ".socket")) else f"lead-home-{role}.service"


def property_of(role, prop):
    return run("systemctl", "show", unit(role), "-p", prop, "--value")


def safe_layout():
    # This is also available to the unprivileged, read-only inventory account.
    for role in ("api", "web", "collector"):
        bindings = property_of(role, "BindReadOnlyPaths") + property_of(role, "DropInPaths")
        require("chamber-" not in bindings.lower(), "shared Chamber application mount")


def image(sha):
    require(SHA.fullmatch(sha), "invalid full SHA")
    p = STORE / sha
    # A root-approved CI image, mounted read-only at its permanent name.
    require(p.resolve(strict=True) == p and p.stat().st_uid == 0, "untrusted release image")
    require(run("findmnt", "-n", "-M", p, "-o", "FSTYPE") == "squashfs", "unexpected release filesystem")
    require(all((p / name).resolve(strict=True) == p / name for name in ("services", "web", "REVISION")),
            "image content is redirected")
    options = run("findmnt", "-n", "-M", p, "-o", "VFS-OPTIONS").split(",")
    require("ro" in options and (p / "REVISION").read_text().strip() == sha, "unsealed release image")
    return p


def process(role):
    pid = property_of(role, "MainPID")
    require(pid.isdecimal() and int(pid) > 1 and property_of(role, "ActiveState") == "active",
            f"{role} process unavailable")
    return Path("/proc") / pid


def bound_tree(proc, target):
    # Kernel mount records, including more specific mounts which could mask the code.
    entries = [line.split() for line in (proc / "mountinfo").read_text().splitlines()]
    nested = [e[4] for e in entries if e[4].startswith(str(target) + "/")]
    cache = str(WEB / ".next/cache")
    require(all(target == WEB and path == cache for path in nested), "unexpected application overlay")
    exact = [e for e in entries if e[4] == str(target)]
    require(len(exact) == 1 and "ro" in exact[0][5].split(","), "unbound or writable application tree")
    return proc / "root" / target.relative_to("/")


def application_roles():
    if (TOOLS / "home_live_maintenance.py").is_file():
        # These processes also bind current/services at process start. Independent
        # channel executors have their own cutover; the registry covers final CLI verify.
        return ROLES | {"browser-status": SERVICES, "lead-chamber-analytics.service": SERVICES}
    return ROLES


def current():
    safe_layout()
    # The selector is only a lookup hint. Every running process must see the immutable image.
    link = STORE / "current"
    require(link.is_symlink(), "no attested live release selector")
    sha = os.readlink(link)
    release = image(sha)
    for role, target in application_roles().items():
        proc = process(role)
        root = bound_tree(proc, target)
        require(root.samefile(release / target.name), f"{role} runs a different image")
        require(process(role) == proc, f"{role} changed during probe")
    return sha



# Separate operator registry: application switching never restarts these services.
EXECUTOR_REGISTRY = Path("/etc/lead-home-release/executors.json")
PROC = Path("/proc")
EXECUTOR_PYTHON = Path("/run/lead-home/bin/py/bin/python3.12")
EXECUTOR_PYTHON_SOURCE = Path("/srv/lead-prod-vault/lead-home-live/bin/py/bin/python3.12")


def executor_path(value):
    require(isinstance(value, str) and re.fullmatch(r"/[a-zA-Z0-9_./-]+", value),
            "invalid executor path")
    path = Path(value)
    require(str(path) == value and ".." not in path.parts, "noncanonical executor path")
    return path


def executor_regular(path, *, private=False, owner=False):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), "executor file is redirected or not regular")
    require(not info.st_mode & (0o077 if private else 0o022), "unsafe executor file permissions")
    if owner:
        require(info.st_uid == 0, "executor config is not operator owned")
    require(info.st_size <= 4 * 1024 * 1024, "executor evidence too large")
    return path.read_bytes()


def executor_config(path):
    require(path.parent == EXECUTOR_REGISTRY.parent, "binding outside executor config directory")
    # Check every host ancestor: an attacker must not be able to replace the directory.
    for directory in (path.parent, *path.parent.parents):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "unsafe executor config directory")
    return executor_regular(path, owner=True)


def executor_binding(data):
    # Deliberately duplicate the small public schema instead of importing image code as root.
    fields = {"version", "channel", "executor_id", "agent_id", "profile_slot", "release",
              "adapter_root", "entrypoint", "adapter_files", "receipt"}
    require(isinstance(data, dict) and set(data) == fields, "invalid executor binding fields")
    require(type(data['version']) is int and data['version'] == 1, "unsupported executor binding version")
    require(data['channel'] in ('telegram', 'reddit'), "unsupported executor channel")
    for key in ('executor_id', 'profile_slot'):
        require(isinstance(data[key], str) and re.fullmatch(r'[a-z0-9][a-z0-9:_-]{0,95}', data[key]),
                "invalid executor identity")
    require(isinstance(data['agent_id'], str) and str(UUID(data['agent_id'])) == data['agent_id'],
            "invalid executor agent")
    require(isinstance(data['entrypoint'], str) and
            re.fullmatch(r'[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*:[a-z_][a-z0-9_]*', data['entrypoint']),
            "invalid executor entrypoint")
    for key in ('release', 'adapter_root', 'receipt'):
        executor_path(data[key])
    release = Path(data['release'])
    require(release.is_relative_to('/run') and release != Path('/run'), "invalid executor release path")
    files = data['adapter_files']
    require(isinstance(files, dict) and files, "missing adapter manifest")
    for name, digest in files.items():
        require(isinstance(name, str) and re.fullmatch(r'[a-zA-Z0-9_./-]+\.py', name)
                and not name.startswith('/') and not any(p in ('.', '..', '') for p in name.split('/'))
                and isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest), "invalid adapter manifest")
    require(data['entrypoint'].split(':')[0].replace('.', '/') + '.py' in files,
            "missing adapter entrypoint")
    paths = [Path(data[key]) for key in ('release', 'adapter_root', 'receipt')]
    require(all(not a.is_relative_to(b) and not b.is_relative_to(a)
                for i, a in enumerate(paths) for b in paths[i + 1:]), "overlapping executor paths")
    return data


def executor_view(proc, target):
    # /proc/PID/root itself is a kernel magic link; every component beneath it must be real.
    view = proc / 'root'
    for part in target.parts[1:]:
        view /= part
        require(not stat.S_ISLNK(view.lstat().st_mode), "redirected executor process path")
    return view


def executor_mount(proc, target):
    entries = [line.split() for line in (proc / 'mountinfo').read_text().splitlines()]
    require(all(len(e) >= 10 and '-' in e for e in entries), "invalid executor mount evidence")
    require(not any(e[4].startswith(str(target) + '/') for e in entries), "unexpected executor overlay")
    exact = [e for e in entries if e[4] == str(target)]
    require(len(exact) == 1 and 'ro' in exact[0][5].split(','), "unbound or writable executor tree")
    return executor_view(proc, target)


def executor_hashes(root, *, adapter=False):
    require(stat.S_ISDIR(root.lstat().st_mode), "missing executor source directory")
    result = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "redirected executor source")
        if stat.S_ISDIR(info.st_mode):
            continue
        if adapter:
            require(path.suffix == '.py', "unlisted adapter code or data")
        else:
            require(path.suffix not in ('.pyc', '.pyo', '.so'), "unexpected compiled executor code")
        if path.suffix == '.py':
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    require(result, "empty executor source")
    return result



def executor_interpreter(proc, argv):
    require(argv and argv[0] == str(EXECUTOR_PYTHON), "unexpected executor interpreter command")
    expected = EXECUTOR_PYTHON_SOURCE
    # Resolve only the final executable symlink, checking each link and its parents.
    # A venv Python symlink is legitimate; a writable source or redirected parent is not.
    for _ in range(32):
        for parent in expected.parents:
            info = parent.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                    "unsafe interpreter directory")
        info = expected.lstat()
        require(info.st_uid == 0, "interpreter source is not operator owned")
        if not stat.S_ISLNK(info.st_mode):
            require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022 and info.st_mode & 0o111,
                    "unsafe interpreter source")
            break
        target = Path(os.readlink(expected))
        expected = Path(os.path.normpath(target if target.is_absolute() else expected.parent / target))
    else:
        raise Refused("interpreter symlink chain is too long")
    require((proc / 'exe').samefile(expected), "executor actual interpreter mismatch")
    require((proc / 'root' / EXECUTOR_PYTHON.relative_to('/')).samefile(expected),
            "executor mounted interpreter mismatch")
    actual = executor_path(os.readlink(proc / 'exe'))
    entries = [line.split() for line in (proc / 'mountinfo').read_text().splitlines()]
    for target in (EXECUTOR_PYTHON, actual):
        covering = [e for e in entries if len(e) >= 10 and target.is_relative_to(e[4])]
        require(covering, "missing interpreter mount")
        depth = max(len(Path(e[4]).parts) for e in covering)
        closest = [e for e in covering if len(Path(e[4]).parts) == depth]
        require(len(closest) == 1 and 'ro' in closest[0][5].split(','),
                "writable or ambiguous interpreter mount")


def executor_process(service):
    pid = property_of(service, 'MainPID')
    invocation = property_of(service, 'InvocationID')
    require(pid.isdecimal() and int(pid) > 1 and property_of(service, 'ActiveState') == 'active',
            "executor process unavailable")
    require(re.fullmatch(r'[a-f0-9]{32}', invocation), "executor invocation unavailable")
    return pid, invocation



def verify_executor_writable_layout(entry, binding, proc):
    # The stable operator-owned layout outlives shared application revisions.
    # Only provisioning derives it from the preexisting unit, never from a plan field.
    path = EXECUTOR_REGISTRY.parent / (entry['channel'] + '-layout.json')
    raw = executor_config(path)
    require(not path.lstat().st_mode & 0o077, 'executor writable layout is not private')
    layout = json.loads(raw)
    require(isinstance(layout, dict) and set(layout) == {
        'version', 'channel', 'service', 'binding', 'writable_bindings'}
        and type(layout['version']) is int and layout['version'] == 1
        and all(layout[key] == entry[key] for key in entry), 'executor writable layout identity mismatch')
    rows = layout['writable_bindings']
    require(isinstance(rows, list), 'invalid executor writable layout')
    expected = set()
    for pair in rows:
        require(isinstance(pair, list) and len(pair) == 2, 'invalid executor writable mapping')
        expected.add(tuple(str(executor_path(value)) for value in pair))
    require(len(expected) == len(rows) and len({target for _, target in expected}) == len(expected),
            'ambiguous executor writable mapping')
    receipt = (str(STORE / 'executors' / entry['channel'] / 'receipt'), str(Path(binding['receipt']).parent))
    require(receipt in expected, 'executor writable receipt mapping missing')
    require(executor_writable_bindings(entry['service'], entry['channel']) == expected,
            'executor writable bindings differ from original layout')
    mounts = [line.split() for line in (proc / 'mountinfo').read_text().splitlines()]
    for source, target in sorted(expected):
        exact = [row for row in mounts if len(row) >= 10 and row[4] == target]
        require(len(exact) == 1 and 'rw' in exact[0][5].split(','), 'executor data mount is missing or read-only')
        view = executor_view(proc, Path(target))
        require(view.samefile(Path(source)), 'executor writable mount source changed')
    return raw


def verify_executor_entries(sha, entries, *, both_channels=True):
    """Verify the same live evidence for initial single-service and final fleet acceptance."""
    release = image(sha)
    expected_source = release / 'services/api/lead_app'
    require(expected_source.resolve(strict=True) == expected_source, 'redirected release source')
    expected_core = executor_hashes(expected_source)
    require('channel_executor.py' in expected_core, "release lacks executor launcher")
    services, identities, pids, channels, proofs, observed = set(), set(), set(), set(), [], []
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {'service', 'channel', 'binding'}, "invalid registry entry")
        service = entry['service']
        require(isinstance(service, str) and re.fullmatch(r'lead-[a-z0-9][a-z0-9-]{0,100}\.service', service),
                "invalid executor service")
        require(service not in services, "duplicate executor service")
        services.add(service)
        config = executor_path(entry['binding'])
        config_bytes = executor_config(config)
        binding = executor_binding(json.loads(config_bytes))
        require(binding['channel'] == entry['channel'], "executor channel mismatch")
        require(binding['executor_id'] not in identities, "duplicate executor identity")
        identities.add(binding['executor_id'])
        channels.add(binding['channel'])
        pid, invocation = executor_process(service)
        require(pid not in pids, "shared executor process")
        pids.add(pid)
        proc = PROC / pid
        mounts = (proc / 'mountinfo').read_bytes()
        root = executor_mount(proc, Path(binding['release']))
        require(root.samefile(release), "executor runs a different image")
        require((root / 'REVISION').read_text().strip() == sha, "executor revision mismatch")
        # A receipt does not attest loaded memory on its own: pair boot evidence with
        # the immutable exact release mount and the live, unchanged systemd invocation.
        command = (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        args = [arg.decode() for arg in command]
        executor_interpreter(proc, args)
        require(len(args) in (4, 5) and args[1:-2] in (['-I', '-B'], ['-IB'])
                and args[-2:] == [binding['release'] + '/services/api/lead_app/channel_executor.py', str(config)],
                "unexpected executor launcher")
        process_config = executor_view(proc, config)
        require(executor_regular(process_config, owner=True) == config_bytes, "executor binding drift")
        adapter_root = executor_mount(proc, Path(binding['adapter_root']))
        adapter_files = executor_hashes(adapter_root, adapter=True)
        require(adapter_files == binding['adapter_files'], "executor adapter drift")
        core_files = executor_hashes(executor_view(proc, Path(binding['release']) / 'services/api/lead_app'))
        require(core_files == expected_core, "executor common source drift")
        receipt = executor_view(proc, Path(binding['receipt']))
        require(stat.S_ISDIR(receipt.parent.lstat().st_mode) and not receipt.parent.lstat().st_mode & 0o077,
                "unsafe executor receipt directory")
        proof = json.loads(executor_regular(receipt, private=True))
        expected = dict(version=1, channel=binding['channel'], executor_id=binding['executor_id'],
                        revision=sha, pid=int(pid), invocation=invocation, core_files=core_files,
                        adapter_files=adapter_files, boot_sha256=expected_core['channel_executor.py'],
                        binding_sha256=hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        require(proof == expected, "executor boot evidence mismatch")
        layout_bytes = verify_executor_writable_layout(entry, binding, proc)
        proofs.append(proof)
        observed.append((service, pid, invocation, config, config_bytes, mounts, args, entry, binding, layout_bytes))
    if both_channels:
        require(channels == {'telegram', 'reddit'}, "both executor channels are required")
    for service, pid, invocation, config, config_bytes, mounts, args, entry, binding, layout_bytes in observed:
        require(executor_process(service) == (pid, invocation), "executor changed during verification")
        executor_interpreter(PROC / pid, args)
        require((PROC / pid / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0') ==
                [arg.encode() for arg in args], 'executor command changed during verification')
        require((PROC / pid / 'mountinfo').read_bytes() == mounts, 'executor mounts changed during verification')
        require(executor_config(config) == config_bytes, "executor config changed during verification")
        require(verify_executor_writable_layout(entry, binding, PROC / pid) == layout_bytes,
                'executor writable layout changed during verification')
    return proofs


def verify_executor_entry(sha, entry):
    return verify_executor_entries(sha, [entry], both_channels=False)[0]


def verify_executors(sha):
    # Provisioning accepts one consumer at a time; final release acceptance needs both.
    registry_bytes = executor_config(EXECUTOR_REGISTRY)
    registry = json.loads(registry_bytes)
    require(isinstance(registry, dict) and set(registry) == {'version', 'executors'}
            and type(registry['version']) is int and registry['version'] == 1
            and isinstance(registry['executors'], list) and registry['executors'], "invalid executor registry")
    proofs = verify_executor_entries(sha, registry['executors'])
    require(executor_config(EXECUTOR_REGISTRY) == registry_bytes, "executor registry changed during verification")
    return proofs



EXECUTOR_SYSTEMD = Path('/etc/systemd/system')
EXECUTOR_TEMPLATE = 'services/channel_adapters/systemd/executor.conf.in'


def executor_json(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode()


def executor_digest(data):
    return hashlib.sha256(data).hexdigest()


def executor_directory(path, *, create=False, private=False):
    if create and not path.exists():
        executor_directory(path.parent, create=True)
        path.mkdir(mode=0o700)
    for directory in (path, *path.parents):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'unsafe executor provisioning directory')
    if private:
        require(not path.lstat().st_mode & 0o077, 'executor journal directory is not private')


def executor_host_file(path, *, private=False):
    executor_directory(path.parent)
    return executor_regular(path, private=private, owner=True)


def executor_atomic(path, content, *, mode=0o600, uid=0, gid=0):
    executor_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.executor-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            os.chown(temporary, uid, gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        executor_sync(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def executor_sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def executor_file_backup(path):
    if not os.path.lexists(path):
        return None
    data = executor_host_file(path)
    info = path.lstat()
    return dict(bytes=base64.b64encode(data).decode(), mode=stat.S_IMODE(info.st_mode),
                uid=info.st_uid, gid=info.st_gid)


def executor_restore_file(path, saved):
    if saved is None:
        path.unlink(missing_ok=True)
        executor_sync(path.parent)
    else:
        executor_atomic(path, base64.b64decode(saved['bytes'], validate=True),
                        mode=saved['mode'], uid=saved['uid'], gid=saved['gid'])


def executor_selector(path):
    if not os.path.lexists(path):
        return None
    executor_directory(path.parent)
    require(path.is_symlink() and path.lstat().st_uid == 0, 'unsafe executor selector')
    value = os.readlink(path)
    target = executor_path(value)
    require(target.parent == STORE and SHA.fullmatch(target.name), 'invalid executor selector target')
    return value


def executor_select(path, value):
    if value is None:
        path.unlink(missing_ok=True)
    else:
        with tempfile.TemporaryDirectory(prefix='.selector-', dir=path.parent) as directory:
            temporary = Path(directory) / 'current'
            temporary.symlink_to(value)
            os.replace(temporary, path)
    executor_sync(path.parent)


def executor_maintenance(sha, agent_id):
    # Only this already-installed trusted host adapter accesses the operator database.
    # No application/adapter code is imported, no hold/restore/drain action is requested.
    require(SHA.fullmatch(sha) and str(UUID(agent_id)) == agent_id, 'invalid maintenance identity')
    site = TOOLS / 'home_live_maintenance.py'
    executor_host_file(site)
    program = (
        'import runpy;m=runpy.run_path(' + repr(str(site)) + ');'
        'key=m["KEY"];fingerprint=m["FINGERPRINT"];busy=m["BUSY"];'
        'sha=' + repr(sha) + ';aid=' + repr(agent_id) + ';'
        'query=f"SELECT NOT ({busy}) AND NOT EXISTS(SELECT FROM lead.agents WHERE desired_state=\'running\') "'
        '+f"AND EXISTS(SELECT FROM lead.agents a WHERE id=\'{aid}\' AND desired_state=\'paused\' "'
        '+f"AND onboarding_state#>>\'{{{key},sha}}\'=\'{sha}\' AND onboarding_state#>\'{{{key},before}}\'={fingerprint}) "'
        '+f"AND NOT EXISTS(SELECT FROM lead.agents a WHERE onboarding_state ? \'{key}\' AND "'
        '+f"(onboarding_state#>>\'{{{key},sha}}\' IS DISTINCT FROM \'{sha}\' OR desired_state<>\'paused\' "'
        '+f"OR onboarding_state#>\'{{{key},before}}\' IS DISTINCT FROM {fingerprint}))";'
        'print(m["site_sql"](query))'
    )
    require(run('/usr/bin/python3', '-IB', '-c', program) == 't',
            'same-revision maintenance hold and idle state required')



def executor_existing_bindings(service, prop='BindReadOnlyPaths'):
    require(prop in ('BindReadOnlyPaths', 'BindPaths'), 'unsupported executor binding property')
    result = set()
    for value in property_of(service, prop).split():
        parts = value.split(':')
        require(len(parts) in (2, 3) and (len(parts) == 2 or parts[2] == 'rbind'),
                'unsupported existing read-only binding')
        source, target = str(executor_path(parts[0])), str(executor_path(parts[1]))
        result.add((source, target))
    return result



def executor_writable_bindings(service, channel):
    bindings = executor_existing_bindings(service, 'BindPaths')
    receipt = (str(STORE / 'executors' / channel / 'receipt'), '/run/lead-executor/receipt')
    for source, name in bindings:
        target = Path(name)
        protected = (Path('/run/lead-executor'), EXECUTOR_REGISTRY.parent, SERVICES, BIN)
        require((source, name) == receipt or not any(
            target.is_relative_to(path) or path.is_relative_to(target) for path in protected),
            'existing writable binding overlaps executor code or configuration')
    return bindings


def executor_render_template(template, values, writable_bindings):
    # Generate complete lines so an empty list cannot emit a second BindPaths=
    # reset and discard the read-only mounts emitted just above it.
    receipt = (values['@RECEIPT_SOURCE@'], '/run/lead-executor/receipt')
    values = {**values, '@PRESERVED_WRITABLE_BINDINGS@': '\n'.join(
        'BindPaths=' + source + ':' + target + ':rbind' for source, target in sorted(writable_bindings)
        if (source, target) != receipt)}
    placeholders = set(re.findall(r'@[A-Z_]+@', template))
    require(placeholders == set(values) == {'@BINDING@', '@PRESERVED_READONLY_BINDINGS@',
            '@PRESERVED_WRITABLE_BINDINGS@', '@CORE_SOURCE@', '@ADAPTER_SOURCE@',
            '@RECEIPT_SOURCE@', '@ENV_SOURCE@'}, 'unexpected executor template parameters')
    for key, value in values.items():
        template = template.replace(key, value)
    require('@' not in template, 'unexpanded executor template')
    return template


def executor_command_identity(value):
    match = re.fullmatch(r'\{ path=(.*?) ; argv\[\]=(.*?) ; ignore_errors=(yes|no) ; .* \}', value)
    require(match is not None and '} {' not in value, 'unsupported executor command evidence')
    return list(match.groups())


def executor_provision_plan(path, sha):
    raw = executor_config(path)
    require(not path.lstat().st_mode & 0o077, 'executor provision plan is not private')
    plan = json.loads(raw)
    require(isinstance(plan, dict) and set(plan) == {
        'version', 'channel', 'service', 'binding', 'core_mode', 'preserved_readonly_bindings', 'expected'},
        'invalid executor provisioning fields')
    require(type(plan['version']) is int and plan['version'] == 1, 'unsupported executor provisioning version')
    binding = executor_binding(plan['binding'])
    channel, service = plan['channel'], plan['service']
    require(channel == binding['channel'] and path.name == channel + '-provision.json', 'provision channel mismatch')
    require(isinstance(service, str) and re.fullmatch(r'lead-[a-z0-9][a-z0-9-]{0,100}\.service', service),
            'invalid executor provision service')
    require(plan['core_mode'] in ('main-current', 'independent'), 'invalid executor core selector mode')
    require(binding['release'] == '/run/lead-executor/release'
            and binding['adapter_root'] == '/run/lead-executor/adapter'
            and binding['receipt'] == '/run/lead-executor/receipt/boot.json', 'nonstandard executor binding paths')
    expected = plan['expected']
    require(isinstance(expected, dict) and set(expected) == {
        'fragment_sha256', 'dropins', 'binding_sha256', 'selector'}, 'invalid prior executor evidence')
    require(isinstance(expected['dropins'], dict), 'invalid prior dropin evidence')
    for value in [expected['fragment_sha256'], *expected['dropins'].values()]:
        require(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value), 'invalid prior file digest')
    require(expected['binding_sha256'] is None or isinstance(expected['binding_sha256'], str)
            and re.fullmatch('[a-f0-9]{64}', expected['binding_sha256']), 'invalid prior binding digest')
    if expected['selector'] is not None:
        selector = executor_path(expected['selector'])
        require(selector.parent == STORE and SHA.fullmatch(selector.name), 'invalid prior selector')
    base = STORE / 'executors' / channel
    adapter = base / 'adapters' / executor_digest(executor_json(binding['adapter_files']))
    executor_directory(adapter)
    for file in adapter.rglob('*'):
        info = file.lstat()
        require(info.st_uid == 0 and not info.st_mode & 0o022, 'unsafe adapter source ownership')
    require(executor_hashes(adapter, adapter=True) == binding['adapter_files'], 'provision adapter source drift')
    environment = EXECUTOR_REGISTRY.parent / (channel + '.env')
    executor_config(environment)
    require(not environment.lstat().st_mode & 0o077, 'executor environment is not private')
    preserved = plan['preserved_readonly_bindings']
    require(isinstance(preserved, list) and preserved, 'missing preserved executor bindings')
    targets = set()
    inherited = executor_existing_bindings(service)
    environments = set(re.findall(r'(/[^ ]+) \(ignore_errors=(?:yes|no)\)',
                                  property_of(service, 'EnvironmentFiles')))
    for item in preserved:
        require(isinstance(item, dict) and set(item) == {'source', 'target'}, 'invalid preserved binding')
        source, target = executor_path(item['source']), executor_path(item['target'])
        # Existing unit mappings are preserved, never newly granted. Data/socket
        # ownership belongs to the original service and need not be root-only.
        pair = (str(source), str(target))
        require(pair in inherited or (str(source) in environments and target == Path('/run/lead-home/model.env')),
                'preserved binding is not existing service access')
        info = source.lstat()
        require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode),
                'unsupported existing binding source')
        trusted_bin = target == BIN and source == EXECUTOR_PYTHON_SOURCE.parents[2] and pair in inherited
        reserved = (Path('/run/lead-executor'), Path('/etc/lead-home-release'), SERVICES)
        require((trusted_bin or not any(target.is_relative_to(p) or p.is_relative_to(target) for p in reserved))
                and target.suffix not in ('.py', '.pyc', '.pyo', '.so'), 'preserved binding overlays executor code')
        if target == BIN or target.is_relative_to(BIN / 'py') or target == BIN / 'site':
            require(trusted_bin or pair in inherited, 'unreviewed interpreter dependency binding')
            executor_directory(source.parent)
            require(info.st_uid == 0 and not info.st_mode & 0o022, 'unsafe interpreter dependency source')
        require(target not in targets, 'duplicate preserved binding')
        targets.add(target)
    return plan, raw


def executor_service_snapshot(service, expected, config, selector, dropin):
    fragment = executor_path(property_of(service, 'FragmentPath'))
    require(executor_digest(executor_host_file(fragment)) == expected['fragment_sha256'], 'executor unit drift')
    paths = property_of(service, 'DropInPaths').split()
    require(set(paths) == set(expected['dropins']) and len(paths) == len(set(paths)), 'executor dropin set drift')
    unit_files = {str(fragment): executor_file_backup(fragment)}
    for name in paths:
        path = executor_path(name)
        reader_append = (path in public_reader_paths()[2:]
                         and executor_host_file(path) == public_reader_dropin())
        require(path.name <= dropin.name or reader_append, 'later executor dropin would override provisioning')
        require(executor_digest(executor_host_file(path)) == expected['dropins'][name], 'executor dropin drift')
        unit_files[name] = executor_file_backup(path)
    saved_config = executor_file_backup(config)
    actual_config = None if saved_config is None else executor_digest(base64.b64decode(saved_config['bytes']))
    require(actual_config == expected['binding_sha256'], 'prior executor binding drift')
    require(executor_selector(selector) == expected['selector'], 'prior executor selector drift')
    require(property_of(service, 'ActiveState') == 'active', 'existing executor service must be active')
    return dict(config=saved_config, dropin=executor_file_backup(dropin), selector=expected['selector'],
                layout=executor_file_backup(config.with_name(config.stem + '-layout.json')),
                exec_start=executor_command_identity(property_of(service, 'ExecStart')), active=True, unit_files=unit_files,
                readonly_bindings=sorted(executor_existing_bindings(service)),
                writable_bindings=sorted(executor_writable_bindings(service, config.stem)),
                environment_files=property_of(service, 'EnvironmentFiles'))


def provision_executor(sha, plan_path):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node dispatcher and exact SHA required')
    path = executor_path(str(plan_path))
    executor_directory(STORE)
    lock_path = STORE / 'deploy.lock'
    if os.path.lexists(lock_path):
        executor_host_file(lock_path)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        baseline = current()
        release = image(sha)
        require(release.stat().st_mode & 0o005 == 0o005,
                'executor image root must be readable and traversable by the service user')
        mirror = STORE / 'lead.git'
        sync_public_main(mirror, sha)
        plan, raw = executor_provision_plan(path, sha)
        channel, service, binding = plan['channel'], plan['service'], plan['binding']
        if plan['core_mode'] == 'main-current':
            require(baseline == sha, 'application current must match provisioning revision')
        config = EXECUTOR_REGISTRY.parent / (channel + '.json')
        base = STORE / 'executors' / channel
        selector = base / 'current'
        dropin = EXECUTOR_SYSTEMD / (service + '.d') / 'zzzz-lead-executor.conf'
        entry = dict(service=service, channel=channel, binding=str(config))
        journal_dir = base / 'journal'
        executor_directory(journal_dir, create=True, private=True)
        journal_path = journal_dir / (sha + '.json')
        if os.path.lexists(journal_path):
            journal = json.loads(executor_host_file(journal_path, private=True))
            require(journal.get('plan_sha256') == executor_digest(raw), 'different executor plan already journaled')
            require(journal.get('state') != 'rolled-back', 'previous executor provisioning was restored; review its journal')
            try:
                proof = verify_executor_entry(sha, entry)
            except (Refused, OSError, ValueError, subprocess.SubprocessError):
                raise Refused('incomplete executor provisioning; inspect journal without replaying') from None
            journal['state'] = 'verified'
            executor_atomic(journal_path, executor_json(journal))
            return proof
        executor_maintenance(sha, binding['agent_id'])
        if plan['core_mode'] == 'main-current':
            require(plan['expected']['selector'] is None and not os.path.lexists(selector),
                    'main-current executor must not own a separate selector')
        previous = executor_service_snapshot(service, plan['expected'], config, selector, dropin)
        template_path = release / EXECUTOR_TEMPLATE
        require(template_path.resolve(strict=True) == template_path, 'redirected executor template')
        template = template_path.read_text()
        values = {
            '@BINDING@': str(config),
            '@PRESERVED_READONLY_BINDINGS@': ' '.join(item['source'] + ':' + item['target']
                                                    for item in plan['preserved_readonly_bindings']),
            '@CORE_SOURCE@': str(STORE / 'current' if plan['core_mode'] == 'main-current' else selector),
            '@ADAPTER_SOURCE@': str(base / 'adapters' / executor_digest(executor_json(binding['adapter_files']))),
            '@RECEIPT_SOURCE@': str(base / 'receipt'),
            '@ENV_SOURCE@': str(EXECUTOR_REGISTRY.parent / (channel + '.env')),
        }
        template = executor_render_template(template, values, previous['writable_bindings'])
        user = pwd.getpwnam(property_of(service, 'User'))
        group_name = property_of(service, 'Group')
        gid = grp.getgrnam(group_name).gr_gid if group_name else user.pw_gid
        require(user.pw_uid > 0 and gid > 0, 'executor cannot be provisioned as root')
        receipt = base / 'receipt'
        if os.path.lexists(receipt):
            info = receipt.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == user.pw_uid and not info.st_mode & 0o077,
                    'unsafe existing executor receipt directory')
        else:
            receipt.mkdir(mode=0o700)
            os.chown(receipt, user.pw_uid, gid)
        executor_directory(dropin.parent, create=True)
        journal = dict(version=1, sha=sha, channel=channel, service=service, plan_sha256=executor_digest(raw),
                       state='prepared', previous=previous, entry=entry)
        executor_atomic(journal_path, executor_json(journal))
        executor_maintenance(sha, binding['agent_id'])
        require(current() == baseline, 'application changed before executor stop')
        require(executor_config(path) == raw, 'executor plan changed before stop')
        require(executor_service_snapshot(service, plan['expected'], config, selector, dropin) == previous,
                'executor baseline changed before stop')
        try:
            run('systemctl', 'stop', service, timeout=120)
            require(property_of(service, 'ActiveState') == 'inactive', 'executor failed to stop')
            journal['state'] = 'stopped'
            executor_atomic(journal_path, executor_json(journal))
            executor_atomic(config, executor_json(binding), mode=0o640, gid=gid)
            writable = set(map(tuple, previous['writable_bindings'])) | {(str(receipt), '/run/lead-executor/receipt')}
            layout = dict(version=1, **entry, writable_bindings=sorted(writable))
            executor_atomic(config.with_name(channel + '-layout.json'), executor_json(layout))
            executor_atomic(dropin, template.encode(), mode=0o644)
            if plan['core_mode'] == 'independent':
                executor_select(selector, str(STORE / sha))
            journal['state'] = 'installed'
            executor_atomic(journal_path, executor_json(journal))
            run('systemctl', 'daemon-reload')
            run('systemctl', 'start', service, timeout=120)
            proof = wait_executor_entry(sha, entry)
            executor_maintenance(sha, binding['agent_id'])
            journal['state'] = 'verified'
            executor_atomic(journal_path, executor_json(journal))
            return proof
        except Exception:
            # Catch known failures only. A killed process leaves its durable journal for
            # readback on the next call; it never automatically repeats the stop/install.
            try:
                journal['state'] = 'rolling-back'
                executor_atomic(journal_path, executor_json(journal))
                run('systemctl', 'stop', service, timeout=120)
                executor_restore_file(config, previous['config'])
                executor_restore_file(config.with_name(channel + '-layout.json'), previous['layout'])
                executor_restore_file(dropin, previous['dropin'])
                if plan['core_mode'] == 'independent':
                    executor_select(selector, previous['selector'])
                run('systemctl', 'daemon-reload')
                run('systemctl', 'start', service, timeout=120)
                require(property_of(service, 'ActiveState') == 'active'
                        and executor_command_identity(property_of(service, 'ExecStart')) == previous['exec_start'], 'old executor did not recover')
                require(executor_service_snapshot(service, plan['expected'], config, selector, dropin) == previous,
                        'old executor files did not recover')
                journal['state'] = 'rolled-back'
                executor_atomic(journal_path, executor_json(journal))
            except Exception:
                raise Refused('executor rollback incomplete; maintenance retained; inspect journal') from None
            raise Refused('executor candidate failed; previous files and service restored') from None



def wait_executor_entry(sha, entry):
    deadline = time.monotonic() + 30
    while True:
        try:
            return verify_executor_entry(sha, entry)
        except (Refused, OSError, ValueError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise Refused('executor startup evidence did not become ready') from None
            time.sleep(1)


def register_executors(sha):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node dispatcher and exact SHA required')
    executor_directory(STORE)
    lock_path = STORE / 'deploy.lock'
    if os.path.lexists(lock_path):
        executor_host_file(lock_path)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(current() == sha, 'application current must match registry revision')
        entries = []
        for channel in ('telegram', 'reddit'):
            path = EXECUTOR_REGISTRY.parent / (channel + '-provision.json')
            plan, raw = executor_provision_plan(path, sha)
            journal_path = STORE / 'executors' / channel / 'journal' / (sha + '.json')
            journal = json.loads(executor_host_file(journal_path, private=True))
            entry = dict(service=plan['service'], channel=channel,
                         binding=str(EXECUTOR_REGISTRY.parent / (channel + '.json')))
            require(journal.get('state') == 'verified' and journal.get('sha') == sha
                    and journal.get('channel') == channel and journal.get('entry') == entry
                    and journal.get('plan_sha256') == executor_digest(raw), 'executor provisioning is not verified')
            entries.append(entry)
        proofs = verify_executor_entries(sha, entries)
        content = executor_json(dict(version=1, executors=entries))
        journal_dir = STORE / 'executors/registry-journal'
        executor_directory(journal_dir, create=True, private=True)
        journal_path = journal_dir / (sha + '.json')
        previous = executor_file_backup(EXECUTOR_REGISTRY)
        if previous is not None:
            old = json.loads(base64.b64decode(previous['bytes']))
            require(old == json.loads(content), 'registry already contains a different executor set')
            proofs = verify_executors(sha)
            if os.path.lexists(journal_path):
                journal = json.loads(executor_host_file(journal_path, private=True))
                require(journal.get('sha') == sha and journal.get('registry_sha256') == executor_digest(content),
                        'registry journal identity mismatch')
                journal['state'] = 'verified'
                executor_atomic(journal_path, executor_json(journal))
            return proofs
        require(not os.path.lexists(journal_path), 'incomplete registry registration; inspect prior journal')
        journal = dict(version=1, sha=sha, state='prepared', previous=previous,
                       registry_sha256=executor_digest(content))
        executor_atomic(journal_path, executor_json(journal))
        try:
            executor_atomic(EXECUTOR_REGISTRY, content)
            proofs = verify_executors(sha)
            journal['state'] = 'verified'
            executor_atomic(journal_path, executor_json(journal))
            return proofs
        except Exception:
            executor_restore_file(EXECUTOR_REGISTRY, previous)
            journal['state'] = 'rolled-back'
            executor_atomic(journal_path, executor_json(journal))
            raise Refused('executor registry verification failed; previous registry restored') from None


def reviewed_executor_image_files(mirror, sha, extra):
    name = 'scripts/build_home_image.sh'
    if name not in extra:
        return set()
    require(run('git', '--git-dir', mirror, 'rev-parse', f'{sha}:{name}') ==
            '7079b3b48ec7d727800c5959ba6f321dfb24c07c', 'executor image builder requires compatibility review')
    return {name}


# This is a separate, anonymous transport. It never owns a business identity,
# imports application config, or shares the Codex broker/egress credentials.
PUBLIC_READER_SERVICE = 'lead-public-reader.service'
PUBLIC_READER_SOCKET = 'lead-public-reader.socket'
PUBLIC_READER_RUNTIME = Path('/run/lead-public-reader')
PUBLIC_READER_ROOT = Path('/var/lib/lead-public-reader/root')
PUBLIC_READER_SYSTEMD = Path('/etc/systemd/system')
PUBLIC_READER_PLAN = Path('/etc/lead-home-release/public-reader-provision.json')
PUBLIC_READER_PYTHON = Path('/var/lib/lead-public-reader-runtime')
PUBLIC_READER_RUNTIME_PLAN = Path('/etc/lead-home-release/public-reader-runtime-provision.json')
PUBLIC_READER_OS_PYTHONS = (('3.14', Path('/usr/bin/python3.14')), ('3.12', Path('/usr/bin/python3.12')))
PUBLIC_READER_PUBLIC_USR = Path('/usr')
PUBLIC_READER_RETIRED = Path('/var/lib/lead-public-reader-retired')
PUBLIC_READER_MOUNTINFO = Path('/proc/self/mountinfo')
PUBLIC_READER_SITE = EXECUTOR_PYTHON_SOURCE.parents[2] / 'site'
PUBLIC_READER_INACCESSIBLE = Path('/run/systemd/inaccessible/dir')
PUBLIC_READER_DEPENDENCIES = frozenset(('httpx', 'httpcore', 'anyio', 'certifi', 'idna',
    'h11', 'sniffio', 'beautifulsoup4', 'soupsieve', 'typing-extensions'))
PUBLIC_READER_CONSUMERS = ('api', 'worker')
PUBLIC_READER_SOURCE_FILES = (
    'services/api/lead_app/public_reader.py', 'services/api/lead_app/fetch.py',
    'services/public_reader/systemd/lead-public-reader.service.in',
    'services/public_reader/systemd/lead-public-reader.socket.in',
)
PUBLIC_READER_REUSE_FILES = (*PUBLIC_READER_SOURCE_FILES, 'pyproject.toml', 'requirements.lock.txt')
PUBLIC_READER_CONNECTOR_REVISION = '1d46e4562ab2c23a20f0f177cfb251394fd5cf5d'
PUBLIC_READER_CONNECTOR_FILES = {
    'services/local-executor/src/runner.ts': 'b0ef9ab2a6bae948795a24d97889db782ae2a4ec',
    'services/local-executor/test/server-worker.test.ts': 'daeb3ec7c00f69173a46c1556bb9655f909d9d47',
}
PUBLIC_READER_CONNECTOR_PACKAGE = '9b3224ab63a0871ebee2ca4492d8757cdf9bb52ef5b709dd662c7fa0b368b58a'
PUBLIC_READER_CONNECTOR_RUNNER = '179cadd5848d0929da527e3c8ad350b998916d7172fc0b0b97269883a9c8c630'


def public_reader_dropin():
    # Deliberately append: resetting either Bind list also resets the other one.
    return (f'[Service]\nBindReadOnlyPaths={PUBLIC_READER_RUNTIME}:{PUBLIC_READER_RUNTIME}\n'
            f'Environment=LEAD_PUBLIC_READER_SOCKET={PUBLIC_READER_RUNTIME}/reader.sock\n').encode()


def public_reader_paths():
    return [PUBLIC_READER_SYSTEMD / PUBLIC_READER_SOCKET,
            PUBLIC_READER_SYSTEMD / PUBLIC_READER_SERVICE,
            *(PUBLIC_READER_SYSTEMD / (unit(role) + '.d') / 'zzzzz-public-reader.conf'
              for role in PUBLIC_READER_CONSUMERS)]


def public_reader_render(template, sha, uid, gid):
    require(SHA.fullmatch(sha) and type(uid) is int and type(gid) is int and uid > 0 and gid > 0,
            'invalid public reader identity')
    values = {'@IMAGE@': str(STORE / sha), '@PYTHON@': str(PUBLIC_READER_PYTHON), '@SITE@': str(PUBLIC_READER_SITE),
              '@UID@': str(uid), '@GID@': str(gid), '@SHA@': sha}
    placeholders = set(re.findall(r'@[A-Z_]+@', template))
    require(placeholders and placeholders <= values.keys(), 'unexpected public reader template parameter')
    for key, value in values.items():
        template = template.replace(key, value)
    require('@' not in template, 'unexpanded public reader template')
    return template


def public_reader_identity(role):
    user, group = property_of(role, 'User'), property_of(role, 'Group')
    account = pwd.getpwuid(int(user)) if user.isdecimal() else pwd.getpwnam(user)
    gid = (int(group) if group.isdecimal() else grp.getgrnam(group).gr_gid) if group else account.pw_gid
    require(account.pw_uid > 0 and gid > 0, 'public reader consumer cannot be root')
    return account.pw_uid, gid


def public_reader_route(role):
    values = [item.split('=', 1)[1] for item in shlex.split(property_of(role, 'Environment'))
              if item.startswith('LEAD_PUBLIC_READER_SOCKET=')]
    require(len(values) <= 1, 'ambiguous public reader route')
    return values[0] if values else None


def public_reader_consumer(role):
    props = ('MainPID', 'InvocationID', 'ActiveState', 'FragmentPath', 'DropInPaths',
             'PrivateNetwork', 'JoinsNamespaceOf', 'BindReadOnlyPaths', 'BindPaths', 'NeedDaemonReload')
    result = {key: property_of(role, key) for key in props}
    require(result['ActiveState'] == 'active' and result['MainPID'].isdecimal()
            and int(result['MainPID']) > 1, 'public reader consumer unavailable')
    require(result['PrivateNetwork'] == 'yes' and result['NeedDaemonReload'] == 'no'
            and 'lead-home-net.service' in result['JoinsNamespaceOf'].split(),
            'public reader consumer isolation differs')
    files = [executor_path(result['FragmentPath']),
             *map(executor_path, result['DropInPaths'].split())]
    result['unit_files'] = {str(path): executor_digest(executor_host_file(path)) for path in files}
    result['uid'], result['gid'] = public_reader_identity(role)
    result['route'] = public_reader_route(role)
    return result


def public_reader_bindings(value):
    result = set()
    for item in value.split():
        parts = item.split(':')
        require(len(parts) in (2, 3) and (len(parts) == 2 or parts[2] == 'rbind'),
                'unsupported public reader consumer binding')
        # systemctl prefixes an optional source with one ignore-ENOENT '-'.
        # Unit preimages and the fixed append bytes remain checked separately.
        source = parts[0][1:] if parts[0].startswith('-') else parts[0]
        result.add((str(executor_path(source)), str(executor_path(parts[1]))))
    return result


def public_reader_assert_consumer(record, role, *, activated):
    before = record['previous']['consumers'][role]
    now = public_reader_consumer(role)
    append = public_reader_paths()[2 + PUBLIC_READER_CONSUMERS.index(role)]
    files = {**before['unit_files'], str(append): record['installed'][str(append)]}
    require(now['unit_files'] == files
            and all(now.get(key) == before.get(key) for key in
                    ('uid', 'gid', 'PrivateNetwork', 'JoinsNamespaceOf', 'BindPaths', 'FragmentPath'))
            and public_reader_bindings(now['BindReadOnlyPaths']) ==
                public_reader_bindings(before['BindReadOnlyPaths']) | {(str(PUBLIC_READER_RUNTIME), str(PUBLIC_READER_RUNTIME))}
            and now['route'] == str(PUBLIC_READER_RUNTIME / 'reader.sock'),
            'public reader consumer configuration changed from approved preimage')
    require((now['InvocationID'] != before['InvocationID']) if activated else
            (now['InvocationID'] == before['InvocationID'] and now['MainPID'] == before['MainPID']),
            'public reader consumer invocation changed unexpectedly')
    return now


def public_reader_journal(sha):
    require(SHA.fullmatch(sha), 'invalid public reader revision')
    return STORE / 'public-reader' / (sha + '.json')


def public_reader_load(sha):
    record = json.loads(executor_host_file(public_reader_journal(sha), private=True))
    require(record.get('version') == 1 and record.get('sha') == sha,
            'public reader journal identity differs')
    return record


def public_reader_plan(sha):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node and exact SHA required')
    public_reader_account_unused()
    consumers = {role: public_reader_consumer(role) for role in PUBLIC_READER_CONSUMERS}
    identities = {(data['uid'], data['gid']) for data in consumers.values()}
    require(len(identities) == 1, 'public reader consumers require one actual UID/GID')
    require(all(data['route'] is None for data in consumers.values()), 'public reader route already exists')
    for service in (PUBLIC_READER_SERVICE, PUBLIC_READER_SOCKET):
        require(property_of(service, 'LoadState') == 'not-found', 'public reader unit already exists')
    files = {str(path): executor_file_backup(path) for path in public_reader_paths()}
    require(all(value is None for value in files.values()) and not os.path.lexists(PUBLIC_READER_ROOT.parent)
            and not os.path.lexists(PUBLIC_READER_RUNTIME), 'public reader first-provision paths already exist')
    uid, gid = next(iter(identities))
    return dict(version=1, sha=sha, expected_app=current(), client_uid=uid, client_gid=gid,
                consumers=consumers, files=files)


def public_reader_health_result(raw, sha):
    require(type(raw) is bytes and len(raw) <= 4096 and raw.endswith(b'\n')
            and raw.count(b'\n') == 1, 'invalid public reader health frame')
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                require(key not in value, 'duplicate public reader health field')
                value[key] = item
            return value
        result = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('invalid public reader health response') from None
    require(type(result) is dict and type(result.get('v')) is int and type(result.get('protocol')) is int
            and result == {'v': 1, 'ok': True, 'protocol': 1, 'revision': sha},
            'public reader health identity differs')
    return result


def public_reader_nx_stack(raw):
    require(len(raw) >= 64 and raw[:5] == b'\x7fELF\x02' and raw[5] in (1, 2),
            'public reader requires a 64-bit ELF interpreter')
    order = '<' if raw[5] == 1 else '>'
    offset = struct.unpack_from(order + 'Q', raw, 32)[0]
    size, count = struct.unpack_from(order + 'HH', raw, 54)
    require(size >= 56 and 0 < count <= 256 and offset + size * count <= len(raw),
            'public reader ELF program headers invalid')
    stacks = [struct.unpack_from(order + 'II', raw, offset + size * index)[1]
              for index in range(count)
              if struct.unpack_from(order + 'I', raw, offset + size * index)[0] == 0x6474e551]
    require(stacks == [6], 'public reader requires an explicit non-executable GNU stack')


def public_reader_source_path(path):
    resolved = path.resolve(strict=True)
    require(resolved.is_relative_to(PUBLIC_READER_PUBLIC_USR), 'reader runtime source outside public OS files')
    executor_directory(resolved if resolved.is_dir() else resolved.parent)
    info = resolved.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o7022
            and ((stat.S_ISDIR(info.st_mode) and info.st_mode & 0o005 == 0o005)
                 or (stat.S_ISREG(info.st_mode) and info.st_mode & 0o004)),
            'untrusted reader runtime source')
    return resolved


def public_reader_python_probe(executable, prefix, version):
    program = ('import json,sys,os,ssl,asyncio,threading; '
               'print(json.dumps([list(sys.version_info[:2]),sys.prefix,sys.exec_prefix,os.__file__]))')
    result = subprocess.run([str(executable), '-I', '-S', '-B', '-c', program], env={},
                            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=20)
    value = json.loads(result.stdout)
    require(value[:3] == [[int(part) for part in version.split('.')], str(prefix), str(prefix)]
            and value[3] == str(prefix / ('lib/python' + version) / 'os.py'),
            'reader runtime version or standard library prefix differs')


def public_reader_runtime_source():
    candidates = [(version, path) for version, path in PUBLIC_READER_OS_PYTHONS if path.exists()]
    require(candidates, 'supported OS Python unavailable')
    version, binary = candidates[0]
    binary = public_reader_source_path(binary)
    public_reader_nx_stack(binary.read_bytes())
    stdlib = public_reader_source_path(PUBLIC_READER_PUBLIC_USR / ('lib/python' + version))
    entries = {'bin': {'kind': 'directory', 'mode': 0o755},
               'lib': {'kind': 'directory', 'mode': 0o755}}
    total = 0

    def visit(source, relative, ancestors=()):
        nonlocal total
        resolved = public_reader_source_path(source)
        require(resolved not in ancestors and len(entries) < 12000, 'reader runtime source cycle or size limit')
        info = resolved.lstat()
        if stat.S_ISDIR(info.st_mode):
            entries[relative] = {'kind': 'directory', 'mode': 0o755, 'source': str(resolved)}
            for child in sorted(resolved.iterdir()):
                if child.name in {'site-packages', 'dist-packages', '__pycache__', 'sitecustomize.py', 'usercustomize.py'} or child.suffix in {'.pyc', '.pyo'}:
                    continue
                visit(child, relative + '/' + child.name, (*ancestors, resolved))
        else:
            total += info.st_size
            require(info.st_size <= 128 * 1024**2 and total <= 1024**3, 'reader runtime source too large')
            entries[relative] = {'kind': 'file', 'source': str(resolved), 'size': info.st_size,
                                 'mode': stat.S_IMODE(info.st_mode),
                                 'sha256': executor_digest(resolved.read_bytes())}
    visit(binary, 'bin/python3')
    visit(stdlib, 'lib/python' + version)
    require('lib/python' + version + '/os.py' in entries, 'OS Python standard library landmark missing')
    return {'version': version, 'binary': str(binary), 'stdlib': str(stdlib), 'entries': entries}


def public_reader_runtime_plan(sha):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node and exact SHA required')
    executor_directory(PUBLIC_READER_PYTHON.parent)
    require(not os.path.lexists(PUBLIC_READER_PYTHON), 'reader runtime already exists; verify original installation')
    return {'version': 1, 'sha': sha, 'expected_app': current(), 'source': public_reader_runtime_source()}


def public_reader_runtime_tree(prefix, manifest):
    executor_directory(prefix)
    entries = manifest['source']['entries']
    require({str(path.relative_to(prefix)) for path in prefix.rglob('*')} == entries.keys() | {'runtime-manifest.json'},
            'reader runtime tree differs from manifest')
    for relative, entry in entries.items():
        require(relative and not Path(relative).is_absolute() and '..' not in Path(relative).parts,
                'reader runtime manifest path invalid')
        path = prefix / relative
        info = path.lstat()
        require(info.st_uid == 0 and info.st_gid == 0 and stat.S_IMODE(info.st_mode) == entry['mode']
                and not info.st_mode & 0o7022, 'reader runtime metadata differs')
        if entry['kind'] == 'directory':
            require(stat.S_ISDIR(info.st_mode), 'reader runtime directory redirected')
        else:
            require(entry['kind'] == 'file' and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and info.st_size == entry['size'] and executor_digest(path.read_bytes()) == entry['sha256'],
                    'reader runtime file differs')
    public_reader_nx_stack((prefix / 'bin/python3').read_bytes())
    public_reader_python_probe(prefix / 'bin/python3', prefix, manifest['source']['version'])


def verify_public_reader_runtime(sha=None):
    if sha is not None:
        require(SHA.fullmatch(sha), 'exact reader runtime revision required')
    raw = executor_host_file(PUBLIC_READER_PYTHON / 'runtime-manifest.json', private=True)
    manifest = json.loads(raw)
    require(manifest.get('version') == 1 and SHA.fullmatch(manifest.get('sha', ''))
            and manifest.get('source') == public_reader_runtime_source(), 'reader runtime source provenance differs')
    journal = json.loads(executor_host_file(STORE / 'public-reader-runtime' / (manifest['sha'] + '.json'), private=True))
    require(journal.get('sha') == manifest['sha'] and journal.get('plan_sha256') == manifest.get('plan_sha256')
            and journal.get('manifest_sha256') == executor_digest(raw)
            and journal.get('state') in ('install-requested', 'ready'), 'reader runtime original journal differs')
    public_reader_runtime_tree(PUBLIC_READER_PYTHON, manifest)
    return {'runtime_revision': manifest['sha'], 'version': manifest['source']['version'],
            'manifest_sha256': executor_digest(raw),
            'python_sha256': manifest['source']['entries']['bin/python3']['sha256']}


def public_reader_python():
    return verify_public_reader_runtime()


def provision_public_reader_runtime(sha, plan_path):
    require(os.geteuid() == 0 and SHA.fullmatch(sha) and Path(plan_path) == PUBLIC_READER_RUNTIME_PLAN,
            'trusted node, exact SHA and fixed runtime plan required')
    raw = executor_config(PUBLIC_READER_RUNTIME_PLAN)
    require(not PUBLIC_READER_RUNTIME_PLAN.lstat().st_mode & 0o077, 'reader runtime plan is not private')
    plan = json.loads(raw)
    folder = STORE / 'public-reader-runtime'
    with (STORE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        executor_directory(folder, create=True, private=True)
        journal = folder / (sha + '.json')
        pending = PUBLIC_READER_PYTHON.with_name(PUBLIC_READER_PYTHON.name + '.' + sha + '.pending')
        if os.path.lexists(journal):
            record = json.loads(executor_host_file(journal, private=True))
            require(record.get('plan_sha256') == executor_digest(raw) and record.get('state') in ('install-requested', 'ready')
                    and not os.path.lexists(pending) and current() == plan['expected_app'],
                    'reader runtime outcome unknown; inspect original journal')
            proof = verify_public_reader_runtime(sha)
            require(proof['runtime_revision'] == sha and proof['manifest_sha256'] == record.get('manifest_sha256'),
                    'reader runtime installed outcome differs')
            if record['state'] != 'ready':
                record['state'] = 'ready'
                executor_atomic(journal, executor_json(record))
            return proof
        require(public_reader_runtime_plan(sha) == plan and not os.path.lexists(pending),
                'reader runtime plan or pending target changed')
        sync_public_main(STORE / 'lead.git', sha)
        record = {'version': 1, 'sha': sha, 'plan_sha256': executor_digest(raw), 'state': 'copying'}
        executor_atomic(journal, executor_json(record))
        pending.mkdir(mode=0o755)
        pending.chmod(0o755)
        os.chown(pending, 0, 0)
        executor_sync(pending.parent)
        for relative, entry in plan['source']['entries'].items():
            destination = pending / relative
            if entry['kind'] == 'directory':
                destination.mkdir(mode=entry['mode'])
                destination.chmod(entry['mode'])
                os.chown(destination, 0, 0)
            else:
                with destination.open('xb') as stream:
                    os.fchmod(stream.fileno(), entry['mode'])
                    os.fchown(stream.fileno(), 0, 0)
                    stream.write(Path(entry['source']).read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
        manifest = {'version': 1, 'sha': sha, 'plan_sha256': executor_digest(raw), 'source': plan['source']}
        executor_atomic(pending / 'runtime-manifest.json', executor_json(manifest))
        public_reader_runtime_tree(pending, manifest)
        require(public_reader_runtime_plan(sha) == plan and executor_config(PUBLIC_READER_RUNTIME_PLAN) == raw,
                'reader runtime source or baseline changed during copy')
        for path in sorted((p for p in pending.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            executor_sync(path)
        executor_sync(pending)
        record.update(state='install-requested', manifest_sha256=executor_digest(executor_json(manifest)))
        executor_atomic(journal, executor_json(record))
        os.rename(pending, PUBLIC_READER_PYTHON)
        executor_sync(PUBLIC_READER_PYTHON.parent)
        proof = verify_public_reader_runtime(sha)
        record['state'] = 'ready'
        executor_atomic(journal, executor_json(record))
        return proof


def public_reader_account_unused():
    for lookup in (pwd.getpwnam, grp.getgrnam):
        try:
            lookup('lead-public-reader')
        except KeyError:
            continue
        raise Refused('public reader static identity already exists')


def public_reader_process_identity(status, client_uid, client_gid):
    fields = {key: value.split() for key, value in
              (line.split(':', 1) for line in status.splitlines() if ':' in line)}
    uids, gids, groups = fields.get('Uid', []), fields.get('Gid', []), fields.get('Groups', [])
    require(len(uids) == len(gids) == 4 and all(value.isdecimal() for value in uids + gids + groups),
            'public reader actual identity unavailable')
    require(len(set(uids)) == len(set(gids)) == 1 and int(uids[0]) > 0 and int(gids[0]) > 0
            and int(uids[0]) != client_uid and int(gids[0]) != client_gid
            and all(int(value) == int(gids[0]) and int(value) != client_gid for value in groups),
            'public reader actual identity overlaps a business identity')


def public_reader_dependencies(sha):
    executor_directory(PUBLIC_READER_SITE)
    lock = run('git', '--git-dir', STORE / 'lead.git', 'show', f'{sha}:requirements.lock.txt')
    expected = {}
    for name, version in re.findall(r'(?m)^([a-zA-Z0-9_.-]+)==([^\s;]+)\s*$', lock):
        name = name.lower().replace('_', '-').replace('.', '-')
        if name in PUBLIC_READER_DEPENDENCIES:
            require(name not in expected, 'ambiguous public reader dependency lock')
            expected[name] = version
    require(expected.keys() == PUBLIC_READER_DEPENDENCIES, 'public reader dependency lock incomplete')
    result = {}
    for path in PUBLIC_READER_SITE.glob('*.dist-info/METADATA'):
        name = path.parent.name.removesuffix('.dist-info').rsplit('-', 1)[0].lower().replace('_', '-').replace('.', '-')
        if name not in PUBLIC_READER_DEPENDENCIES:
            continue
        raw = executor_host_file(path)
        metadata = BytesParser().parsebytes(raw, headersonly=True)
        names, versions = metadata.get_all('Name', []), metadata.get_all('Version', [])
        require(len(names) == len(versions) == 1 and name not in result
                and names[0].lower().replace('_', '-').replace('.', '-') == name
                and versions[0] == expected[name], 'public reader dependency versions differ from candidate lock')
        result[name] = {'version': versions[0], 'metadata_sha256': executor_digest(raw)}
    require(result.keys() == PUBLIC_READER_DEPENDENCIES, 'public reader dependency metadata incomplete')
    return result


def public_reader_runtime(sha):
    runtime = public_reader_python()
    return {'python': runtime,
            'dependencies': public_reader_dependencies(sha)}


def public_reader_empty_root(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o755
            and not any(path.iterdir()), 'public reader sandbox root must be empty and root-owned')


def public_reader_health(sha, uid, gid):
    # Probe with the actual consumer identity. Root is intentionally not an
    # authorized socket peer. This operation does not read a URL or call a model.
    program = ("import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(5);"
               "s.connect(sys.argv[1]); s.sendall(b'{\"v\":1,\"action\":\"health\"}\\n');"
               "data=b''\nwhile b'\\n' not in data and len(data)<=4096:\n"
               " chunk=s.recv(4097-len(data))\n if not chunk:break\n data+=chunk\n"
               "sys.stdout.buffer.write(data)")
    raw = subprocess.run(['setpriv', '--reuid=' + str(uid), '--regid=' + str(gid), '--clear-groups',
              '/usr/bin/python3', '-IB', '-c', program, str(PUBLIC_READER_RUNTIME / 'reader.sock')],
              capture_output=True, check=True, timeout=10).stdout
    return public_reader_health_result(raw, sha)


def public_reader_existing(record, sha):
    require(record.get('sha') == sha and record.get('state') not in ('rolled-back', 'rolling-back'),
            'public reader journal cannot be replayed')
    try:
        proof = verify_public_reader(sha, consumers=False)
        if record['state'] != 'ready':
            require(record['state'] in ('prepared', 'installing', 'start-requested')
                    and current() == record['previous']['expected_app'],
                    'public reader original application changed')
            for role in PUBLIC_READER_CONSUMERS:
                public_reader_assert_consumer(record, role, activated=False)
            record['reconciled_journal_sha256'] = executor_digest(
                executor_host_file(public_reader_journal(sha), private=True))
            record['reconciled_from'] = record['state']
            public_reader_save(record, 'ready')
        return proof
    except (Refused, OSError, ValueError, subprocess.SubprocessError):
        raise Refused('public reader outcome unknown; inspect original journal without replaying') from None


def public_reader_sources(sha):
    # As in deploy(), an operator may have already promoted the original CI
    # artifact on a node without the optional downloader. Always recheck the
    # sealed image, main ancestry, runtime and exact source bytes below.
    if not (STORE / sha).is_mount():
        require((TOOLS / 'mount-image').is_file(),
                'verified release image is not mounted; prepare the original CI artifact before provisioning')
        run(TOOLS / 'mount-image', sha, timeout=1800)
    release = image(sha)
    mirror = STORE / 'lead.git'
    sync_public_main(mirror, sha)
    public_reader_python()
    for name in PUBLIC_READER_SOURCE_FILES:
        path = release / name
        require(path.resolve(strict=True) == path and path.is_file(), 'public reader image source redirected')
        require(run('git', '--git-dir', mirror, 'hash-object', '--no-filters', path) ==
                run('git', '--git-dir', mirror, 'rev-parse', f'{sha}:{name}'),
                'public reader image source differs from approved main')
    return release


def public_reader_blobs(sha):
    require(SHA.fullmatch(sha), 'invalid public reader source revision')
    result = {name: run('git', '--git-dir', STORE / 'lead.git', 'rev-parse', f'{sha}:{name}')
              for name in PUBLIC_READER_REUSE_FILES}
    require(all(SHA.fullmatch(value) for value in result.values()), 'public reader source proof unavailable')
    return result


def public_reader_for_application(sha):
    expected = public_reader_blobs(sha)
    direct = public_reader_journal(sha)
    if os.path.lexists(direct):
        record = public_reader_load(sha)
        require(record.get('state') == 'ready' and record.get('source_blobs') == expected,
                'unchanged reader proof unavailable for application')
        return record
    # A later UI/docs-only application can reuse the existing transport. Never
    # relabel the reader's own revision, create a new installation journal or
    # accept a different fetcher/template/dependency input as compatible.
    candidates = []
    for path in direct.parent.glob('*.json'):
        require(SHA.fullmatch(path.stem), 'unexpected public reader journal')
        record = public_reader_load(path.stem)
        if record.get('state') == 'ready' and record.get('source_blobs') == expected:
            candidates.append(record)
    require(len(candidates) == 1, 'unique unchanged reader proof required for application')
    return candidates[0]


def verify_public_reader_application(sha):
    record = public_reader_for_application(sha)
    proof = verify_public_reader(record['sha'], application_sha=sha)
    return {**proof, 'application_sha': sha, 'reader_sha': record['sha']}


def public_reader_before_switch(sha, old):
    require(current() == old, 'application changed before public reader preflight')
    record = public_reader_for_application(sha)
    if old == record['previous']['expected_app']:
        verify_public_reader(record['sha'], consumers=False)
        for role in PUBLIC_READER_CONSUMERS:
            public_reader_assert_consumer(record, role, activated=False)
    else:
        require(public_reader_for_application(old)['sha'] == record['sha'],
                'application reader baseline differs')
        verify_public_reader(record['sha'], application_sha=old)
    require(current() == old, 'application changed during public reader preflight')


def public_reader_save(record, state):
    record['state'] = state
    executor_atomic(public_reader_journal(record['sha']), executor_json(record))


def provision_public_reader(sha, plan_path):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node and exact SHA required')
    require(Path(plan_path) == PUBLIC_READER_PLAN, 'nonstandard public reader plan path')
    raw = executor_config(PUBLIC_READER_PLAN)
    require(not PUBLIC_READER_PLAN.lstat().st_mode & 0o077, 'public reader plan is not private')
    plan = json.loads(raw)
    require(type(plan) is dict and set(plan) == {'version', 'sha', 'expected_app', 'client_uid',
            'client_gid', 'consumers', 'files'} and plan.get('version') == 1 and plan.get('sha') == sha,
            'invalid public reader plan')
    executor_directory(STORE)
    with (STORE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = public_reader_journal(sha)
        if os.path.lexists(journal):
            record = public_reader_load(sha)
            require(record.get('plan_sha256') == executor_digest(raw), 'public reader plan differs from journal')
            return public_reader_existing(record, sha)
        require(public_reader_plan(sha) == plan, 'public reader baseline drift; no installation performed')
        release = public_reader_sources(sha)
        # Every possible target is fixed by code, never taken from a plan.
        rendered = {}
        for service in (PUBLIC_READER_SOCKET, PUBLIC_READER_SERVICE):
            source = release / 'services/public_reader/systemd' / (service + '.in')
            rendered[str(PUBLIC_READER_SYSTEMD / service)] = public_reader_render(
                source.read_text(), sha, plan['client_uid'], plan['client_gid']).encode()
        rendered.update({str(path): public_reader_dropin() for path in public_reader_paths()[2:]})
        executor_directory(journal.parent, create=True, private=True)
        record = dict(version=1, sha=sha, plan_sha256=executor_digest(raw), previous=plan,
                      source_blobs=public_reader_blobs(sha),
                      runtime=public_reader_runtime(sha),
                      installed={name: executor_digest(value) for name, value in rendered.items()})
        public_reader_save(record, 'prepared')
        require(public_reader_plan(sha) == plan and executor_config(PUBLIC_READER_PLAN) == raw,
                'public reader baseline changed before installation')
        # A killed process leaves its intent. No retry blindly starts/stops again.
        public_reader_save(record, 'installing')
        executor_directory(PUBLIC_READER_ROOT.parent, create=True)
        PUBLIC_READER_ROOT.parent.chmod(0o755)
        executor_directory(PUBLIC_READER_ROOT, create=True)
        PUBLIC_READER_ROOT.chmod(0o755)
        public_reader_empty_root(PUBLIC_READER_ROOT)
        executor_sync(PUBLIC_READER_ROOT)
        executor_sync(PUBLIC_READER_ROOT.parent)
        executor_sync(PUBLIC_READER_ROOT.parent.parent)
        for name, content in rendered.items():
            path = Path(name)
            executor_directory(path.parent, create=True)
            executor_atomic(path, content, mode=0o644)
        run('systemctl', 'daemon-reload')
        public_reader_save(record, 'start-requested')
        run('systemctl', 'enable', '--now', PUBLIC_READER_SOCKET)
        run('systemctl', 'start', PUBLIC_READER_SERVICE)
        proof = verify_public_reader(sha, consumers=False)
        # New drop-ins are deliberately pending; original application processes
        # must still be precisely the instances captured before provisioning.
        require(current() == plan['expected_app']
                and all(property_of(role, 'MainPID') == before['MainPID']
                    and property_of(role, 'InvocationID') == before['InvocationID']
                    for role, before in plan['consumers'].items()), 'application changed during reader preparation')
        for role in PUBLIC_READER_CONSUMERS:
            public_reader_assert_consumer(record, role, activated=False)
        public_reader_save(record, 'ready')
        return proof


def public_reader_mount(proc, target, source):
    entries = [line.split() for line in (proc / 'mountinfo').read_text().splitlines()]
    exact = [entry for entry in entries if entry[4] == str(target)]
    require(len(exact) == 1 and 'ro' in exact[0][5].split(',')
            and not any(entry[4].startswith(str(target) + '/') for entry in entries),
            'public reader binding missing, writable or overlaid')
    view = proc / 'root' / target.relative_to('/')
    require(view.samefile(source), 'public reader binding inode differs')


def public_reader_root_probe(proc, uid, gid, node):
    require(proc.name.isdecimal() and int(proc.name) > 1 and type(uid) is int and uid > 0
            and type(gid) is int and gid > 0 and len(node) == 2
            and all(type(value) is int and value >= 0 for value in node),
            'public reader root mask probe identity invalid')
    # Load every module/libc binding before changing namespaces/root. The child
    # never executes service code, inherits environment values, or reads data.
    program = r'''
import ctypes
import errno
import os
import sys

stage = 11
try:
    pid, uid, gid, device, inode = map(int, sys.argv[1:])
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
    libc.capset.argtypes = [ctypes.POINTER(Header), ctypes.POINTER(Data)]
    libc.capset.restype = ctypes.c_int
    header, capabilities = Header(0x20080522, 0), (Data * 2)()
    with open('/proc/sys/kernel/cap_last_cap', 'rb') as stream:
        last_cap = int(stream.read(16))
    if not 0 <= last_cap <= 63:
        os._exit(stage)
    namespace = os.open('/proc/' + str(pid) + '/ns/mnt', os.O_RDONLY | os.O_CLOEXEC)
    root = os.open('/proc/' + str(pid) + '/root', os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    os.setns(namespace, 0)
    os.fchdir(root)
    os.chroot('.')
    os.chdir('/')
    os.close(root)
    os.close(namespace)
    stage = 12
    actual = os.lstat('/root')
    if (actual.st_dev, actual.st_ino) != (device, inode):
        os._exit(stage)
    stage = 13
    # Clear ambient/bounding capabilities and keepcaps, then drop all three ID
    # slots. No host cwd or namespace/root descriptor remains accessible.
    for option, argument in ((38, 1), (47, 4), (8, 0)):
        if libc.prctl(option, argument, 0, 0, 0) != 0:
            os._exit(stage)
    for capability in range(last_cap + 1):
        if libc.prctl(24, capability, 0, 0, 0) != 0:
            os._exit(stage)
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    if libc.capset(ctypes.byref(header), capabilities) != 0:
        os._exit(stage)
    with open('/proc/self/status', 'rb') as stream:
        fields = dict(line.split(b':', 1) for line in stream.read().splitlines() if b':' in line)
    if (fields.get(b'Uid', b'').split() != [str(uid).encode()] * 4
            or fields.get(b'Gid', b'').split() != [str(gid).encode()] * 4
            or fields.get(b'Groups', b'').split()
            or fields.get(b'NoNewPrivs', b'').strip() != b'1'
            or any(int(fields.get(key, b'1').strip(), 16) != 0 for key in
                   (b'CapInh', b'CapPrm', b'CapEff', b'CapBnd', b'CapAmb'))):
        os._exit(stage)
    stage = 14
    try:
        descriptor = os.open('/root', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        if error.errno != errno.EACCES:
            os._exit(stage)
    else:
        os.close(descriptor)
        os._exit(15)
    os.write(1, b'root-inaccessible\n')
    os._exit(0)
except BaseException:
    os._exit(stage)
'''
    try:
        result = subprocess.run(['/usr/bin/python3', '-I', '-S', '-B', '-c', program,
            proc.name, str(uid), str(gid), str(node[0]), str(node[1])],
            env={}, stdin=subprocess.DEVNULL, close_fds=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise Refused('public reader root mask probe failed') from None
    require(result.returncode != 11, 'public reader root mask namespace probe failed')
    require(result.returncode != 12, 'public reader root mask probe inode differs')
    require(result.returncode != 13, 'public reader root mask identity probe failed')
    require(result.returncode != 15, 'public reader root directory unexpectedly readable')
    require(result.returncode == 0 and result.stdout == b'root-inaccessible\n' and not result.stderr,
            'public reader root mask probe failed')


def public_reader_masked_root(proc, plan):
    invocation = property_of(PUBLIC_READER_SERVICE, 'InvocationID')
    require(re.fullmatch('[a-f0-9]{32}', invocation) and process(PUBLIC_READER_SERVICE) == proc
            and property_of(PUBLIC_READER_SERVICE, 'ProtectHome') == 'yes',
            'public reader root protection differs')
    status = (proc / 'status').read_text()
    public_reader_process_identity(status, plan['client_uid'], plan['client_gid'])
    fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
    capabilities = ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')
    require(fields.get('NoNewPrivs', '').strip() == '1' and all(
        re.fullmatch('[0-9a-fA-F]{16}', fields.get(key, '').strip())
        and int(fields[key].strip(), 16) == 0 for key in capabilities),
        'public reader root mask capability differs')
    security_fields = ('Uid', 'Gid', 'Groups', 'NoNewPrivs', *capabilities)
    before_identity = {key: fields[key].split() for key in security_fields}
    uid, gid = int(fields['Uid'].split()[0]), int(fields['Gid'].split()[0])
    executor_directory(PUBLIC_READER_INACCESSIBLE.parent)
    source, target = PUBLIC_READER_INACCESSIBLE.lstat(), (proc / 'root/root').lstat()
    require(stat.S_ISDIR(source.st_mode) and source.st_uid == source.st_gid == 0
            and stat.S_IMODE(source.st_mode) == 0, 'public reader root mask source differs')
    def identity(info):
        return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid
    source_identity = identity(source)
    require(identity(target) == source_identity, 'public reader root mask inode differs')
    entries = [line.split() for line in (proc / 'mountinfo').read_text().splitlines()]
    exact = [entry for entry in entries if entry[4] == '/root']
    require(len(exact) == 1 and 'ro' in exact[0][5].split(',')
            and not any(entry[4].startswith('/root/') for entry in entries),
            'public reader root mask mount differs')
    root_mounts = [entry for entry in entries if entry[4] == '/root' or entry[4].startswith('/root/')]
    public_reader_root_probe(proc, uid, gid, (source.st_dev, source.st_ino))
    after_fields = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
    after_mounts = [line.split() for line in (proc / 'mountinfo').read_text().splitlines()]
    require(process(PUBLIC_READER_SERVICE) == proc
            and property_of(PUBLIC_READER_SERVICE, 'InvocationID') == invocation
            and property_of(PUBLIC_READER_SERVICE, 'ProtectHome') == 'yes'
            and {key: after_fields.get(key, '').split() for key in security_fields} == before_identity
            and [entry for entry in after_mounts if entry[4] == '/root' or entry[4].startswith('/root/')] == root_mounts
            and identity(PUBLIC_READER_INACCESSIBLE.lstat()) == source_identity
            and identity((proc / 'root/root').lstat()) == source_identity,
            'public reader root mask service changed')


def public_reader_private_paths(proc, plan):
    view = proc / 'root'
    require(not (view / 'srv').exists(), 'public reader private srv path visible')
    require(not (view / 'home').exists(), 'public reader private home path visible')
    require(not (view / 'run/lead-home').exists(), 'public reader private lead-home runtime visible')
    require(not (view / 'run/credentials').exists(), 'public reader private credentials runtime visible')
    require(not (view / 'etc/lead-home-release').exists(), 'public reader private release config visible')
    public_reader_masked_root(proc, plan)


def verify_public_reader(sha, *, consumers=True, application_sha=None):
    record = public_reader_load(sha)
    require(record['state'] not in ('rolled-back', 'rolling-back'), 'public reader is restored')
    plan = record['previous']
    release = image(sha)
    require(set(record['installed']) == set(map(str, public_reader_paths())), 'public reader journal file set differs')
    for name, digest in record['installed'].items():
        require(name in map(str, public_reader_paths()), 'public reader journal target differs')
        require(executor_digest(executor_host_file(Path(name))) == digest, 'public reader installed file drift')
    require(property_of(PUBLIC_READER_SOCKET, 'ActiveState') == 'active'
            and property_of(PUBLIC_READER_SOCKET, 'UnitFileState') == 'enabled', 'public reader socket unavailable')
    for service in (PUBLIC_READER_SERVICE, PUBLIC_READER_SOCKET):
        require(property_of(service, 'FragmentPath') == str(PUBLIC_READER_SYSTEMD / service)
                and not property_of(service, 'DropInPaths') and property_of(service, 'NeedDaemonReload') == 'no',
                'public reader unit source differs')
    require(property_of(PUBLIC_READER_SERVICE, 'DynamicUser') == 'yes'
            and property_of(PUBLIC_READER_SERVICE, 'RootDirectory') == str(PUBLIC_READER_ROOT)
            and property_of(PUBLIC_READER_SERVICE, 'PrivateNetwork') == 'no'
            and property_of(PUBLIC_READER_SERVICE, 'NeedDaemonReload') == 'no'
            and not property_of(PUBLIC_READER_SERVICE, 'EnvironmentFiles'), 'public reader sandbox differs')
    proc = process(PUBLIC_READER_SERVICE)
    public_reader_process_identity((proc / 'status').read_text(), plan['client_uid'], plan['client_gid'])
    require((proc / 'root').samefile(PUBLIC_READER_ROOT), 'public reader actual root differs')
    public_reader_mount(proc, Path('/app/lead_app'), release / 'services/api/lead_app')
    public_reader_mount(proc, Path('/python'), PUBLIC_READER_PYTHON)
    public_reader_mount(proc, Path('/site'), PUBLIC_READER_SITE)
    require(public_reader_runtime(sha) == record.get('runtime'), 'public reader runtime changed after provisioning')
    require((proc / 'ns/net').readlink() == Path('/proc/self/ns/net').readlink(), 'public reader not in host network')
    public_reader_private_paths(proc, plan)
    runtime = PUBLIC_READER_RUNTIME.lstat()
    sock = (PUBLIC_READER_RUNTIME / 'reader.sock').lstat()
    require(stat.S_ISDIR(runtime.st_mode) and runtime.st_uid == 0 and stat.S_IMODE(runtime.st_mode) == 0o755
            and stat.S_ISSOCK(sock.st_mode) and sock.st_uid == plan['client_uid']
            and sock.st_gid == plan['client_gid'] and stat.S_IMODE(sock.st_mode) == 0o600,
            'public reader socket ownership or permissions differ')
    health = public_reader_health(sha, plan['client_uid'], plan['client_gid'])
    if consumers:
        require(current() == (application_sha or sha), 'public reader application not activated')
        for role, before in plan['consumers'].items():
            public_reader_assert_consumer(record, role, activated=True)
            consumer = process(role)
            public_reader_mount(consumer, PUBLIC_READER_RUNTIME, PUBLIC_READER_RUNTIME)
            # Read only the non-secret selected route from the actual process;
            # never expose other environment values in evidence or diagnostics.
            route = [value for value in (consumer / 'environ').read_bytes().split(b'\0')
                     if value.startswith(b'LEAD_PUBLIC_READER_SOCKET=')]
            require(route == [('LEAD_PUBLIC_READER_SOCKET=' + str(PUBLIC_READER_RUNTIME / 'reader.sock')).encode()],
                    'public reader process route differs')
            require(process(role) == consumer, 'public reader consumer changed during verification')
    require(process(PUBLIC_READER_SERVICE) == proc, 'public reader changed during verification')
    return dict(sha=sha, protocol=1, service_pid=int(proc.name), health=health, consumers_verified=consumers)


def public_reader_restore_consumers(sha, old):
    path = public_reader_journal(sha)
    if not os.path.lexists(path):
        return False
    record = public_reader_load(sha)
    require(record['previous']['expected_app'] == old, 'public reader rollback baseline differs')
    require(record['state'] in ('ready', 'restoring-consumers'), 'public reader rollback needs known ready state')
    for path in public_reader_paths()[2:]:
        require(executor_digest(executor_host_file(path)) == record['installed'][str(path)],
                'public reader consumer file changed before restore')
    public_reader_save(record, 'restoring-consumers')
    for path in public_reader_paths()[2:]:
        executor_restore_file(path, record['previous']['files'][str(path)])
    run('systemctl', 'daemon-reload')
    return True


def verify_public_reader_prepared(sha):
    record = public_reader_load(sha)
    require(record['state'] == 'ready' and current() == record['previous']['expected_app'],
            'original application must remain active before reader deployment')
    proof = verify_public_reader(sha, consumers=False)
    require(all(property_of(role, 'MainPID') == before['MainPID']
                and property_of(role, 'InvocationID') == before['InvocationID']
                for role, before in record['previous']['consumers'].items()),
            'original application changed after reader preparation')
    for role in PUBLIC_READER_CONSUMERS:
        public_reader_assert_consumer(record, role, activated=False)
    return {**proof, 'application_sha': record['previous']['expected_app'], 'state': 'prepared'}


def public_reader_stop_restored(sha):
    record = public_reader_load(sha)
    old = record['previous']['expected_app']
    require(current() == old and record['state'] == 'restoring-consumers', 'restore application before reader stop')
    for role, before in record['previous']['consumers'].items():
        now = public_reader_consumer(role)
        require(now['route'] == before['route'] and now['BindPaths'] == before['BindPaths']
                and now['BindReadOnlyPaths'] == before['BindReadOnlyPaths']
                and now['unit_files'] == before['unit_files'], 'old consumer configuration not restored')
        proc = process(role)
        mounts = [line.split()[4] for line in (proc / 'mountinfo').read_text().splitlines()]
        require(str(PUBLIC_READER_RUNTIME) not in mounts
                and not (proc / 'root' / PUBLIC_READER_RUNTIME.relative_to('/') / 'reader.sock').exists(),
                'old consumer still sees public reader socket')
    public_reader_save(record, 'stop-requested')
    run('systemctl', 'disable', '--now', PUBLIC_READER_SOCKET)
    run('systemctl', 'stop', PUBLIC_READER_SERVICE)
    require(property_of(PUBLIC_READER_SERVICE, 'MainPID') == '0'
            and property_of(PUBLIC_READER_SERVICE, 'ActiveState') in ('inactive', 'failed'),
            'public reader did not stop; inspect original journal')
    for path in public_reader_paths()[:2]:
        require(executor_digest(executor_host_file(path)) == record['installed'][str(path)],
                'public reader unit changed before restore')
        executor_restore_file(path, record['previous']['files'][str(path)])
    run('systemctl', 'daemon-reload')
    public_reader_save(record, 'rolled-back')


def rollback_public_reader(sha):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node and exact SHA required')
    with (STORE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = public_reader_load(sha)
        old = record['previous']['expected_app']
        require(current() == old, 'restore old application before reader rollback')
        require(record['state'] == 'ready', 'public reader rollback outcome unknown; inspect original journal')
        with maintenance_window(old, old) as (fence, check):
            run('systemctl', 'start', fence)
            run(*check, old, old)
            verify(old, fenced=True)
            public_reader_restore_consumers(sha, old)
            run('systemctl', 'restart', *(unit(role) for role in PUBLIC_READER_CONSUMERS), timeout=180)
            verify(old, fenced=True)
            public_reader_stop_restored(sha)
            run(*check, old, old)
            run('systemctl', 'stop', fence)
    return {'sha': sha, 'state': 'rolled-back', 'application_sha': old}


def public_reader_inode(path):
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o7022
            and (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)), 'unsafe reader lifecycle preimage')
    return {'device': info.st_dev, 'inode': info.st_ino, 'mode': info.st_mode,
            'uid': info.st_uid, 'gid': info.st_gid}


def public_reader_group_empty():
    group = property_of(PUBLIC_READER_SERVICE, 'ControlGroup')
    require(group in ('', '/system.slice/' + PUBLIC_READER_SERVICE), 'reader control group differs')
    path = Path('/sys/fs/cgroup/system.slice') / PUBLIC_READER_SERVICE
    if path.exists():
        require(path.resolve() == path and all(not item.read_text().strip()
                for item in path.rglob('cgroup.procs')), 'reader control group still contains processes')


def public_reader_original_consumers(record, *, pending):
    require(current() == record['previous']['expected_app'], 'reader original application changed')
    old_image = image(record['previous']['expected_app'])
    require(not (old_image / PUBLIC_READER_SOURCE_FILES[0]).exists(),
            'prepared rollback requires the original application without a reader client')
    for role, before in record['previous']['consumers'].items():
        if pending:
            public_reader_assert_consumer(record, role, activated=False)
        else:
            now = public_reader_consumer(role)
            require(all(now.get(key) == before.get(key) for key in
                ('MainPID', 'InvocationID', 'route', 'BindPaths', 'BindReadOnlyPaths', 'unit_files',
                 'uid', 'gid', 'PrivateNetwork', 'JoinsNamespaceOf', 'FragmentPath')),
                'reader original consumer instance or bindings changed')
        proc = process(role)
        mounts = [line.split()[4] for line in (proc / 'mountinfo').read_text().splitlines()]
        require(not any(path == str(PUBLIC_READER_RUNTIME) or path.startswith(str(PUBLIC_READER_RUNTIME) + '/')
                        for path in mounts)
                and not any(value.startswith(b'LEAD_PUBLIC_READER_SOCKET=')
                            for value in (proc / 'environ').read_bytes().split(b'\0')),
                'prepared reader was activated in an original consumer')
        require(process(role) == proc and property_of(role, 'InvocationID') == before['InvocationID'],
                'reader consumer changed during lifecycle check')


def public_reader_lifecycle_pins(record):
    proc = process(PUBLIC_READER_SERVICE)
    require(property_of(PUBLIC_READER_SERVICE, 'RootDirectory') == str(PUBLIC_READER_ROOT)
            and (proc / 'root').samefile(PUBLIC_READER_ROOT), 'old reader root provenance differs')
    for path in public_reader_paths()[:2]:
        require(executor_digest(executor_host_file(path)) == record['installed'][str(path)],
                'old reader unit provenance differs')
        require(property_of(path.name, 'FragmentPath') == str(path)
                and not property_of(path.name, 'DropInPaths')
                and property_of(path.name, 'NeedDaemonReload') == 'no',
                'old reader loaded unit provenance differs')
    public_reader_mount(proc, Path('/app/lead_app'), image(record['sha']) / 'services/api/lead_app')
    python = [source for source, target in public_reader_bindings(property_of(PUBLIC_READER_SERVICE, 'BindReadOnlyPaths'))
              if target == '/python']
    require(len(python) == 1 and python[0] in {str(PUBLIC_READER_PYTHON), str(EXECUTOR_PYTHON_SOURCE.parents[1])},
            'old reader interpreter binding differs')
    public_reader_mount(proc, Path('/python'), Path(python[0]))
    raw = executor_config(PUBLIC_READER_PLAN)
    require(executor_digest(raw) == record['plan_sha256'] and json.loads(raw) == record['previous'],
            'old reader plan differs')
    require(stat.S_IMODE(PUBLIC_READER_PLAN.lstat().st_mode) == 0o600
            and PUBLIC_READER_PLAN.lstat().st_nlink == 1
            and property_of(PUBLIC_READER_SERVICE, 'ControlGroup') == '/system.slice/' + PUBLIC_READER_SERVICE,
            'old reader private plan or control group differs')
    return {'root_parent': public_reader_inode(PUBLIC_READER_ROOT.parent),
            'root': public_reader_inode(PUBLIC_READER_ROOT),
            'runtime': public_reader_inode(PUBLIC_READER_RUNTIME),
            'plan': public_reader_inode(PUBLIC_READER_PLAN), 'plan_sha256': executor_digest(raw),
            'service_pid': proc.name, 'invocation': property_of(PUBLIC_READER_SERVICE, 'InvocationID'),
            'control_group': property_of(PUBLIC_READER_SERVICE, 'ControlGroup')}


def public_reader_stopped_original(record):
    pins = record['prepared_rollback']
    require(property_of(PUBLIC_READER_SERVICE, 'MainPID') == '0'
            and property_of(PUBLIC_READER_SERVICE, 'ActiveState') in ('inactive', 'failed')
            and property_of(PUBLIC_READER_SOCKET, 'ActiveState') in ('inactive', 'failed')
            and not os.path.lexists(PUBLIC_READER_RUNTIME / 'reader.sock'),
            'old reader stop outcome unknown; inspect without replaying')
    invocation = property_of(PUBLIC_READER_SERVICE, 'InvocationID')
    require(invocation in ('', pins['invocation']), 'different reader invocation replaced stopped instance')
    public_reader_group_empty()
    for role in PUBLIC_READER_CONSUMERS:
        proc = process(role)
        require(not (proc / 'root' / PUBLIC_READER_RUNTIME.relative_to('/') / 'reader.sock').exists(),
                'stopped reader socket remains visible in original consumer')


def rollback_prepared_public_reader(sha):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node and exact SHA required')
    with (STORE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = public_reader_load(sha)
        if record['state'] == 'ready':
            public_reader_original_consumers(record, pending=True)
            record['prepared_rollback'] = public_reader_lifecycle_pins(record)
            public_reader_original_consumers(record, pending=True)
            public_reader_save(record, 'prepared-restore-requested')
            for path in public_reader_paths()[2:]:
                executor_restore_file(path, record['previous']['files'][str(path)])
            run('systemctl', 'daemon-reload')
        require(record['state'] in ('prepared-restore-requested', 'prepared-socket-stop-requested',
                'prepared-service-stop-requested', 'prepared-service-stop-observed', 'prepared-units-restore-requested', 'rolled-back')
                and 'prepared_rollback' in record, 'prepared reader rollback needs its original pinned journal')
        public_reader_original_consumers(record, pending=False)
        pins = record['prepared_rollback']
        require(public_reader_inode(PUBLIC_READER_ROOT.parent) == pins['root_parent']
                and public_reader_inode(PUBLIC_READER_ROOT) == pins['root']
                and public_reader_inode(PUBLIC_READER_RUNTIME) == pins['runtime']
                and public_reader_inode(PUBLIC_READER_PLAN) == pins['plan']
                and executor_digest(executor_config(PUBLIC_READER_PLAN)) == pins['plan_sha256'],
                'prepared reader lifecycle paths changed')
        if record['state'] == 'prepared-restore-requested':
            require(public_reader_lifecycle_pins(record) == pins,
                    'prepared reader instance changed before stop')
            public_reader_save(record, 'prepared-socket-stop-requested')
            run('systemctl', 'disable', '--now', PUBLIC_READER_SOCKET)
        if record['state'] == 'prepared-socket-stop-requested':
            require(property_of(PUBLIC_READER_SOCKET, 'ActiveState') in ('inactive', 'failed')
                    and not os.path.lexists(PUBLIC_READER_RUNTIME / 'reader.sock'),
                    'prepared reader socket stop unknown; inspect without replaying')
            # Requires=socket can stop the service as part of the same socket
            # job. Observe that completed result instead of sending a second stop.
            if property_of(PUBLIC_READER_SERVICE, 'MainPID') == '0':
                public_reader_stopped_original(record)
                public_reader_save(record, 'prepared-service-stop-observed')
            else:
                require(property_of(PUBLIC_READER_SERVICE, 'MainPID') == pins['service_pid']
                        and property_of(PUBLIC_READER_SERVICE, 'InvocationID') == pins['invocation'],
                        'prepared reader instance changed before service stop')
                public_reader_save(record, 'prepared-service-stop-requested')
                run('systemctl', 'stop', PUBLIC_READER_SERVICE)
        if record['state'] in ('prepared-service-stop-requested', 'prepared-service-stop-observed'):
            public_reader_stopped_original(record)
            public_reader_save(record, 'prepared-units-restore-requested')
            for path in public_reader_paths()[:2]:
                require(executor_digest(executor_host_file(path)) == record['installed'][str(path)],
                        'old reader unit changed before restore')
                executor_restore_file(path, record['previous']['files'][str(path)])
            run('systemctl', 'daemon-reload')
        public_reader_stopped_original(record)
        require(all(executor_file_backup(path) == record['previous']['files'][str(path)]
                    for path in public_reader_paths()), 'prepared reader restored file preimages differ')
        public_reader_original_consumers(record, pending=False)
        if record['state'] != 'rolled-back':
            public_reader_save(record, 'rolled-back')
        return {'sha': sha, 'state': 'rolled-back', 'consumer_restarts': 0}


def retire_public_reader(sha):
    require(os.geteuid() == 0 and SHA.fullmatch(sha), 'trusted node and exact SHA required')
    with (STORE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = public_reader_load(sha)
        require(record['state'] == 'rolled-back' and 'prepared_rollback' in record,
                'retire only a pinned prepared rollback')
        pins = record['prepared_rollback']
        archive = PUBLIC_READER_RETIRED / sha
        journal_folder = STORE / 'public-reader/retirement'
        journal_path = journal_folder / (sha + '.json')
        archived_root, archived_plan = archive / 'root-parent', archive / 'original-plan.json'
        old_journal_hash = executor_digest(executor_host_file(public_reader_journal(sha), private=True))
        if os.path.lexists(journal_path):
            retirement = json.loads(executor_host_file(journal_path, private=True))
            require(retirement.get('version') == 1 and retirement.get('sha') == sha
                    and retirement.get('reader_journal_sha256') == old_journal_hash
                    and retirement.get('pins') == pins, 'reader retirement identity differs')
            if retirement.get('state') == 'retired':
                require(public_reader_inode(archived_root) == pins['root_parent']
                        and public_reader_inode(archived_root / PUBLIC_READER_ROOT.name) == pins['root']
                        and executor_digest(executor_host_file(archived_plan, private=True)) == pins['plan_sha256'],
                        'retired reader archive differs')
                # Never inspect/mutate reused live paths after retirement completed.
                return {'sha': sha, 'state': 'retired', 'archive_verified': True}
        else:
            public_reader_original_consumers(record, pending=False)
            public_reader_stopped_original(record)
            require(public_reader_inode(PUBLIC_READER_ROOT.parent) == pins['root_parent']
                    and public_reader_inode(PUBLIC_READER_ROOT) == pins['root']
                    and public_reader_inode(PUBLIC_READER_PLAN) == pins['plan']
                    and executor_digest(executor_config(PUBLIC_READER_PLAN)) == pins['plan_sha256']
                    and not os.path.lexists(archive), 'reader retirement preimage changed')
            if os.path.lexists(PUBLIC_READER_RUNTIME):
                require(public_reader_inode(PUBLIC_READER_RUNTIME) == pins['runtime']
                        and not any(PUBLIC_READER_RUNTIME.iterdir()), 'old reader runtime is not the pinned empty directory')
            executor_directory(journal_folder, create=True, private=True)
            executor_directory(PUBLIC_READER_RETIRED, create=True, private=True)
            retirement = {'version': 1, 'sha': sha, 'reader_journal_sha256': old_journal_hash,
                          'pins': pins, 'state': 'root-move-requested'}
            executor_atomic(journal_path, executor_json(retirement))
            archive.mkdir(mode=0o700)
            executor_sync(archive.parent)
        require(retirement['state'] in ('root-move-requested', 'plan-copy-requested', 'plan-unlink-requested', 'runtime-remove-requested'),
                'reader retirement outcome unknown')
        executor_directory(archive, create=True, private=True)
        public_reader_original_consumers(record, pending=False)
        public_reader_stopped_original(record)
        # An interrupted old retirement must reject every reused live path
        # before touching any of them, even if a different pinned path remains.
        if os.path.lexists(PUBLIC_READER_ROOT.parent):
            require(not os.path.lexists(archived_root)
                    and public_reader_inode(PUBLIC_READER_ROOT.parent) == pins['root_parent']
                    and public_reader_inode(PUBLIC_READER_ROOT) == pins['root'],
                    'reader root path reused during retirement')
        else:
            require(public_reader_inode(archived_root) == pins['root_parent']
                    and public_reader_inode(archived_root / PUBLIC_READER_ROOT.name) == pins['root'],
                    'reader archived root identity differs')
        if os.path.lexists(PUBLIC_READER_PLAN):
            require(public_reader_inode(PUBLIC_READER_PLAN) == pins['plan']
                    and executor_digest(executor_config(PUBLIC_READER_PLAN)) == pins['plan_sha256'],
                    'reader plan path reused during retirement')
        else:
            require(retirement['state'] in ('plan-unlink-requested', 'runtime-remove-requested')
                    and executor_digest(executor_host_file(archived_plan, private=True)) == pins['plan_sha256'],
                    'reader plan disappeared before archival')
        if os.path.lexists(PUBLIC_READER_RUNTIME):
            require(public_reader_inode(PUBLIC_READER_RUNTIME) == pins['runtime']
                    and not any(PUBLIC_READER_RUNTIME.iterdir()), 'reader runtime path reused during retirement')
        if retirement['state'] == 'root-move-requested':
            if not os.path.lexists(archived_root):
                require(public_reader_inode(PUBLIC_READER_ROOT.parent) == pins['root_parent']
                        and public_reader_inode(PUBLIC_READER_ROOT) == pins['root'], 'reader root changed before archive')
                mounts = [line.split()[4] for line in PUBLIC_READER_MOUNTINFO.read_text().splitlines()]
                require(not any(path == str(PUBLIC_READER_ROOT.parent) or path.startswith(str(PUBLIC_READER_ROOT.parent) + '/')
                                for path in mounts), 'reader root is still mounted')
                require(PUBLIC_READER_ROOT.parent.stat().st_dev == archive.stat().st_dev,
                        'reader root archive must share the original filesystem')
                os.rename(PUBLIC_READER_ROOT.parent, archived_root)
                executor_sync(PUBLIC_READER_ROOT.parent.parent)
                executor_sync(archive)
            require(not os.path.lexists(PUBLIC_READER_ROOT.parent)
                    and public_reader_inode(archived_root) == pins['root_parent']
                    and public_reader_inode(archived_root / PUBLIC_READER_ROOT.name) == pins['root'],
                    'reader archived root identity differs')
            retirement['state'] = 'plan-copy-requested'
            executor_atomic(journal_path, executor_json(retirement))
        if retirement['state'] == 'plan-copy-requested':
            require(public_reader_inode(PUBLIC_READER_PLAN) == pins['plan'], 'reader plan inode changed before archive')
            raw = executor_config(PUBLIC_READER_PLAN)
            require(executor_digest(raw) == pins['plan_sha256'], 'reader plan bytes changed before archive')
            if not os.path.lexists(archived_plan):
                with archived_plan.open('xb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                executor_sync(archive)
            require(executor_digest(executor_host_file(archived_plan, private=True)) == pins['plan_sha256'],
                    'reader archived plan differs')
            retirement['state'] = 'plan-unlink-requested'
            executor_atomic(journal_path, executor_json(retirement))
        if retirement['state'] == 'plan-unlink-requested':
            require(executor_digest(executor_host_file(archived_plan, private=True)) == pins['plan_sha256'],
                    'reader archived plan differs')
            if os.path.lexists(PUBLIC_READER_PLAN):
                require(public_reader_inode(PUBLIC_READER_PLAN) == pins['plan']
                        and executor_digest(executor_config(PUBLIC_READER_PLAN)) == pins['plan_sha256'],
                        'reader plan replaced before removal')
                PUBLIC_READER_PLAN.unlink()
                executor_sync(PUBLIC_READER_PLAN.parent)
            retirement['state'] = 'runtime-remove-requested'
            executor_atomic(journal_path, executor_json(retirement))
        if os.path.lexists(PUBLIC_READER_RUNTIME):
            require(public_reader_inode(PUBLIC_READER_RUNTIME) == pins['runtime']
                    and not any(PUBLIC_READER_RUNTIME.iterdir()), 'reader runtime replaced or not empty')
            PUBLIC_READER_RUNTIME.rmdir()
            executor_sync(PUBLIC_READER_RUNTIME.parent)
        require(not os.path.lexists(PUBLIC_READER_ROOT.parent) and not os.path.lexists(PUBLIC_READER_PLAN),
                'reader fixed paths reused during retirement')
        retirement['state'] = 'retired'
        executor_atomic(journal_path, executor_json(retirement))
        return {'sha': sha, 'state': 'retired', 'archive_verified': True}


def public_reader_connector_tree(root, data):
    files, directories, seen, total = {}, set(), set(), 0
    require(len(data) <= 4 * 1024 * 1024, 'connector archive too large')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        require(len(members) <= 20000, 'connector archive too large')
        for member in members:
            parts = member.name.rstrip('/').split('/')
            require(member.name not in seen and parts[0] == 'package'
                    and all(part not in ('', '.', '..') for part in parts)
                    and (member.isfile() or member.isdir()) and not member.mode & 0o7000,
                    'unsafe connector artifact member')
            seen.add(member.name)
            path = Path(*parts[1:])
            name = str(path)
            if name == '.':
                require(member.isdir(), 'connector root is not a directory')
                continue
            directories.update(str(parent) for parent in path.parents if str(parent) != '.')
            if member.isdir():
                directories.add(name)
            else:
                total += member.size
                require(total <= 256 * 1024 * 1024, 'connector expanded size too large')
                files[name] = (executor_digest(archive.extractfile(member).read()),
                               0o755 if member.mode & 0o111 else 0o644)
    require(files and not files.keys() & directories, 'ambiguous connector artifact tree')
    require(stat.S_IMODE(root.lstat().st_mode) == 0o755 and not root.is_symlink(), 'unsafe connector tree root')
    require({str(path.relative_to(root)) for path in root.rglob('*')} == files.keys() | directories,
            'installed connector tree differs from original package')
    for name in directories:
        info = (root / name).lstat()
        require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o755
                and info.st_uid == root.stat().st_uid, 'unsafe installed connector directory')
    for name, (digest, mode) in files.items():
        path = root / name
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == mode
                and info.st_uid == root.stat().st_uid and executor_digest(path.read_bytes()) == digest,
                'installed connector bytes or mode differ from original package')


def public_reader_connector():
    # Read only public package/code. No identity, pairing, grant or authentication
    # file is opened. Compare the original retained artifact as well as live bytes.
    root = Path('/opt/lead-codex/connector')
    require(root.resolve(strict=True) == root and root.stat().st_uid == 0, 'installed connector path differs')
    require(executor_host_file(root / 'REVISION').decode().strip() == PUBLIC_READER_CONNECTOR_REVISION
            and executor_digest(executor_host_file(root / 'dist/runner.js')) == PUBLIC_READER_CONNECTOR_RUNNER,
            'installed connector approval repair differs')
    package = STORE / ('maintenance-' + PUBLIC_READER_CONNECTOR_REVISION) / 'connector-original.tgz'
    raw = executor_host_file(package, private=True)
    require(executor_digest(raw) == PUBLIC_READER_CONNECTOR_PACKAGE,
            'installed connector original artifact differs')
    public_reader_connector_tree(root, raw)


def reviewed_public_reader_files(mirror, sha, extra):
    paths = extra & (set(PUBLIC_READER_SOURCE_FILES) | set(PUBLIC_READER_CONNECTOR_FILES))
    if not paths:
        return set()
    for path in paths & PUBLIC_READER_CONNECTOR_FILES.keys():
        require(run('git', '--git-dir', mirror, 'rev-parse', f'{sha}:{path}') ==
                PUBLIC_READER_CONNECTOR_FILES[path], 'connector history requires separate compatibility review')
    if paths & PUBLIC_READER_CONNECTOR_FILES.keys():
        public_reader_connector()
    if paths & set(PUBLIC_READER_SOURCE_FILES):
        record = public_reader_load(sha)
        require(record['state'] == 'ready', 'public reader must be provisioned before application deployment')
        verify_public_reader(sha, consumers=False)
    return paths

def probe(role, url):
    proc = process(role)
    return run("nsenter", "-t", proc.name, "-n", "--", "curl", "--noproxy", "*",
               "--silent", "--fail", "--max-time", "4", url)


FRAME_SERVICE = "lead-frame-fast-live.service"


def verify_frames(release, *, running_web=None):
    bundle = release / "home-browser-frames"
    if not bundle.is_dir():
        return  # Earlier release images did not ship the site components.
    require(run("systemctl", "is-enabled", FRAME_SERVICE) == "enabled", "frame service not enabled")
    pid = run("systemctl", "show", FRAME_SERVICE, "-p", "MainPID", "--value")
    require(pid.isdecimal() and int(pid) > 1, "frame service unavailable")
    proc = Path("/proc", pid)
    require(bound_tree(proc, WEB).samefile(running_web or release / "web"), "stale frame dependencies")
    script = proc / "root/run/lead-home/frame.cjs"
    require(script.read_bytes() == (bundle / "frame-live.cjs").read_bytes(), "frame encoder drift")
    unit_file = Path(run("systemctl", "show", FRAME_SERVICE, "-p", "FragmentPath", "--value"))
    require(unit_file.read_bytes() == (bundle / FRAME_SERVICE).read_bytes(), "frame unit drift")
    edge = process("edge") / "root/run/lead-home/nginx.conf"
    site = " ".join(edge.read_text().split())
    for fragment in ("upstream.conf", "location.conf"):
        require(" ".join((bundle / fragment).read_text().split()) in site, "frame route drift")
    require(pid == run("systemctl", "show", FRAME_SERVICE, "-p", "MainPID", "--value"),
            "frame process changed during verification")


def verify(sha, *, fenced=False, require_idle=True):
    require(current() == sha, "running SHA mismatch")
    if (STORE / sha / PUBLIC_READER_SOURCE_FILES[0]).is_file():
        verify_public_reader_application(sha)
    verify_frames(STORE / sha)
    intake = run("systemctl", "show", "lead-home-ingress.socket", "-p", "ActiveState", "--value")
    require(intake == ("inactive" if fenced else "active"), "unexpected ingress state")
    for role in ("db", "temporal"):
        process(role)
    site = TOOLS / "home_live_maintenance.py"
    health = json.loads(run(site, "health") if site.is_file() else probe("api", "http://127.0.0.1:8088/api/health"))
    require(all(health.get(key) is True for key in ("database", "worker", "browser")),
            "API dependencies unavailable")
    if fenced and require_idle:
        require(health.get("active_profiles") == 0, "browser profiles still in use")
    probe("web", "http://127.0.0.1:3100/")
    # Site-owned read-only check of durable Temporal and the isolated synthetic receipt.
    # Bind its evidence to this worker invocation so an older success cannot be reused.
    invocation = property_of("worker", "InvocationID")
    require(re.fullmatch("[0-9a-f]{32}", invocation), "worker invocation unavailable")
    if site.is_file():
        run(site, "workflow", sha, invocation, timeout=40)
    else:
        run(TOOLS / "verify-workflow", sha, invocation)
    require(property_of("worker", "InvocationID") == invocation, "worker changed during verification")
    require(current() == sha, "release changed during verification")


def select(sha):
    with tempfile.TemporaryDirectory(dir=STORE) as scratch:
        tmp = Path(scratch) / "current"
        tmp.symlink_to(sha)
        os.replace(tmp, STORE / "current")
        fd = os.open(STORE, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def restart_and_verify(sha):
    units = [unit(role) for role in application_roles()]
    # A bind mount follows current at process startup, including when rolling back.
    if run("systemctl", "show", FRAME_SERVICE, "-p", "LoadState", "--value") == "loaded":
        units.append(FRAME_SERVICE)
    run("systemctl", "restart", *units, timeout=180)
    deadline = time.monotonic() + 90
    while True:
        try:
            verify(sha, fenced=True)
            return
        except (Refused, OSError, ValueError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise Refused("restarted release failed verification") from None
            time.sleep(2)


def replace_release(old, new):
    select(new)
    try:
        restart_and_verify(new)
    except Exception:
        reader = public_reader_restore_consumers(new, old)
        select(old)
        restart_and_verify(old)
        if reader:
            public_reader_stop_restored(new)
        raise Refused("candidate failed; previous release independently verified") from None


@contextmanager
def maintenance_window(old, sha):
    """Quiesce the live site without cancelling tasks or reissuing authorizations."""
    site = TOOLS / "home_live_maintenance.py"
    if not site.is_file():
        yield f"lead-home-release-fence@{old}-{sha}.service", [TOOLS / "check-fence"]
        return
    fence = f"lead-home-site-fence@{old}-{sha}.service"
    deadline = time.monotonic() + 3600
    while run(site, "ready", sha) != "idle":
        require(time.monotonic() < deadline, "active work still running; intake untouched")
        time.sleep(15)
    run("systemctl", "stop", "lead-home-live.path")
    try:
        run("systemctl", "stop", "lead-home-ingress.socket", "lead-home-ingress.service")
        run(site, "hold", sha)
        deadline = time.monotonic() + 150
        while json.loads(run(site, "health"))["active_profiles"]:
            require(time.monotonic() < deadline, "existing profiles did not drain")
            time.sleep(3)
        yield fence, [site, "fence"]
    finally:
        # An uncertain switch or an independently held lock needs reconciliation.
        # Never reopen intake or resume scheduling on an unverified revision.
        state = run("systemctl", "show", fence, "-p", "ActiveState", "--value")
        if state in ("inactive", "failed") and current() in (old, sha):
            intake = run("systemctl", "show", "lead-home-ingress.socket", "-p", "ActiveState", "--value")
            revision = current()
            if intake == "active":
                verify(revision)
            else:
                # During a refused/aborted drain a profile may legitimately still be open.
                verify(revision, fenced=True, require_idle=False)
            if not cutover_pending(sha):
                run(site, "restore", sha)
                run("systemctl", "start", "lead-home-live.path", "lead-home-ingress.socket")


def growth_cutover_pending(sha):
    """A root-owned upgrade journal retains the hold across executor cutovers."""
    require(SHA.fullmatch(sha),"invalid cutover revision")
    stage = STORE / ("maintenance-"+sha)
    marker = stage / "growth-cutover.pending"
    if not os.path.lexists(marker):
        return False
    executor_directory(stage,private=True)
    require(executor_host_file(marker,private=True).decode().strip()==sha,"growth cutover journal differs")
    return True


def unified_cutover_pending(sha):
    require(SHA.fullmatch(sha),"invalid cutover revision")
    stage=STORE/("maintenance-"+sha)
    marker=stage/"unified-browser-cutover.pending"
    if not os.path.lexists(marker):
        return False
    executor_directory(stage,private=True)
    require(executor_host_file(marker,private=True).decode().strip()==sha,"unified-browser cutover journal differs")
    return True


def shared_cutover_pending(sha):
    require(SHA.fullmatch(sha),"invalid cutover revision")
    stage=STORE/("maintenance-"+sha)
    marker=stage/"shared-executor-cutover.pending"
    if not os.path.lexists(marker):
        return False
    executor_directory(stage,private=True)
    require(executor_host_file(marker,private=True).decode().strip()==sha,"shared-executor cutover journal differs")
    return True


def cutover_kind(sha):
    growth=growth_cutover_pending(sha)
    unified=unified_cutover_pending(sha)
    shared=shared_cutover_pending(sha)
    require(sum((growth,unified,shared))<=1,'ambiguous cutover journals')
    return 'growth' if growth else 'unified-browser' if unified else 'shared-executor' if shared else None


def cutover_pending(sha):
    return cutover_kind(sha) is not None


# These node tools were deployed and verified independently of the application image.
# A later app commit may include their historical diff even when every blob is identical.
MAINTENANCE_REVISION = "5684637c5015a3ec1b511aafb5cd39447b90eda9"
MAINTENANCE_FILES = {
    "infra/systemd/lead-home-site-fence@.service", "scripts/home_live_maintenance.py",
    "scripts/home_release_operator.py", "scripts/home_systemd_release.py",
    "scripts/home_verify_workflow.py", "scripts/poll_home_release.py",
}


def reviewed_maintenance_files(mirror, sha, extra):
    paths = extra & MAINTENANCE_FILES
    if not paths or not (TOOLS / "home_live_maintenance.py").is_file():
        return set()
    # Preserve the separate frame compatibility check for other versions of the hook.
    changed = run("git", "--git-dir", mirror, "diff", "--no-renames", "--name-only",
                  "-z", MAINTENANCE_REVISION, sha, "--", *sorted(paths))
    historical = paths - set(changed.split("\0"))
    # These two scripts may already have been upgraded by the node operator.
    # Compare the candidate blob with the independently installed tool bytes.
    for name in (paths - historical) & {"scripts/home_systemd_release.py", "scripts/home_live_maintenance.py"}:
        installed = TOOLS / Path(name).name
        if installed.is_file() and run("git", "hash-object", "--no-filters", installed) == run(
                "git", "--git-dir", mirror, "rev-parse", f"{sha}:{name}"):
            historical.add(name)
    return historical


# Schema changes are applied separately by the node database owner.
CHANNEL_SCHEMA_FILES = {
    ".env.example": "4ea891ba1cdcd4068d80d0ca07cca6460d1d3c01",
    "infra/migrations/versions/0026_first_entry.py": "6c67420e3a578c42181ef45d69bed9ed70ec55bb",
    "infra/migrations/versions/0027_account_profiles.py": "244d0277b597d6f9f6351ff9da7920e8ed098a0c",
    "infra/migrations/versions/0028_distribution_slots.py": "ff2307da5e88017e5d8b91ef7ebcd233f1c77e01",
    "infra/migrations/versions/0029_merge_channel_restore.py": "248855860ddd20d1659f6aef85b443d265fe36b2",
    "infra/migrations/versions/0026_local_restore_proof.py": "0b9554440ea5c15417553d23ae4da8d6520b4377",
}


def reviewed_channel_schema(mirror, sha, extra):
    paths = extra.intersection(CHANNEL_SCHEMA_FILES)
    if not paths or not (TOOLS / "home_live_maintenance.py").is_file():
        return set()
    if any(run("git", "--git-dir", mirror, "rev-parse", f"{sha}:{path}") != CHANNEL_SCHEMA_FILES[path]
           for path in paths):
        return set()
    # The independently installed tool checks the live database, outside the image.
    require(run(TOOLS / "home_live_maintenance.py", "channel-schema") == "ready",
            "channel schema migration has not been verified on this node")
    return paths


# This additive upgrade is installed separately, after backup/restore rehearsal.
# Only these reviewed bytes are accepted; the application hook never runs DDL.
# Install this verifier from the successful release candidate's tool bundle before
# upgrading the database; an application image does not update node-owned tools.
FAULT_ISOLATION_FILES = {'scripts/fault_isolation_upgrade.py': '6fa2986893c3d1dbc50f84d51d85947a714f5cf2', 'infra/migrations/versions/0030_fault_isolation.py': '930763fed2ebf6eac3ab2c1238ec49adbc7e6955', 'scripts/service.py': '81d3285a63cbf0158dd2301060df33ccd2437077'}


def fault_schema_query():
    return """SELECT
        (SELECT array_agg(version_num::text) FROM public.lead_migration_version) IN
            (ARRAY['0030_fault_isolation'],ARRAY['0031_growth_runtime'],ARRAY['0033_unified_browser'],ARRAY['0034_local_executor'],ARRAY['0035_shared_executor'])
        AND (SELECT count(*)=2 AND bool_and(relrowsecurity AND relforcerowsecurity)
             FROM pg_class WHERE oid IN (to_regclass('lead.runtime_issues'),to_regclass('lead.ui_revisions')))
        AND (SELECT count(*)=4 FROM pg_policies WHERE schemaname='lead'
             AND tablename IN ('runtime_issues','ui_revisions') AND policyname IN ('scope','member_scope'))
        AND has_table_privilege('lead_runtime','lead.runtime_issues','SELECT')
        AND has_table_privilege('lead_runtime','lead.runtime_issues','INSERT')
        AND has_table_privilege('lead_runtime','lead.runtime_issues','UPDATE')
        AND has_table_privilege('lead_runtime','lead.ui_revisions','SELECT')
        AND has_table_privilege('lead_runtime','lead.ui_revisions','INSERT')
        AND has_table_privilege('lead_runtime','lead.ui_revisions','UPDATE')
        AND (SELECT count(*)=3 FROM pg_attribute WHERE attrelid='lead.admin_notifications'::regclass
             AND NOT attisdropped AND (attname,atttypid) IN
             (('muted','boolean'::regtype),('external_pending','boolean'::regtype),('external_available_at','timestamptz'::regtype)))
        AND (SELECT count(*)=11 FROM pg_trigger WHERE tgname='ui_changed' AND tgenabled='O'
             AND tgfoid=to_regprocedure('lead.touch_ui_revision()') AND tgrelid IN
             ('lead.agents'::regclass,'lead.tasks'::regclass,'lead.chat_jobs'::regclass,
              'lead.conversation_messages'::regclass,'lead.activity'::regclass,'lead.browser_runs'::regclass,
              'lead.browser_steps'::regclass,'lead.assets'::regclass,'lead.runtime_issues'::regclass,
              'lead.admin_notifications'::regclass,'lead.admin_notification_receipts'::regclass))
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.local_restore_ready()')
                   AND md5(prosrc)='f0b323f641b48d4ab81322cc4f501b42' AND prosecdef)
    """


def reviewed_fault_isolation(mirror, sha, extra):
    paths = extra.intersection(FAULT_ISOLATION_FILES)
    if not paths:
        return set()
    for path in paths:
        require(run('git', '--git-dir', mirror, 'rev-parse', f'{sha}:{path}') == FAULT_ISOLATION_FILES[path],
                'fault isolation upgrade requires compatibility review')
    site = TOOLS / 'home_live_maintenance.py'
    require(site.is_file(), 'existing node database verifier required')
    program = ('import runpy;read=runpy.run_path(' + repr(str(site)) + ')["site_sql"];print(read(' + repr(fault_schema_query()) + '))')
    require(run('/usr/bin/python3', '-IB', '-c', program) == 't', 'fault isolation schema is not ready')
    # scripts/service.py is a standalone template. Home services use their existing
    # node-owned entrypoints; installing/changing those remains a separate operation.
    return paths


GROWTH_RUNTIME_FILES = {'scripts/growth_runtime_upgrade.py': '5a98672c797361272a1a22148ba7b3539ca09aba', 'infra/migrations/versions/0031_growth_runtime.py': 'ad10651410b6ecd5641c10dc6d6a1979675d9f48', 'infra/migrations/sql/0031_growth_runtime.sql': '862552a7ca10303415da81f1808a522234bbf7dc'}


def growth_schema_query():
    return """SELECT (SELECT array_agg(version_num::text) FROM public.lead_migration_version) IN (ARRAY['0031_growth_runtime'],ARRAY['0033_unified_browser'],ARRAY['0034_local_executor'],ARRAY['0035_shared_executor'])
        AND (SELECT count(*)=3 AND bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE oid IN
            (to_regclass('lead.growth_experiments'),to_regclass('lead.account_operating_intents'),to_regclass('lead.profile_execution_routes')))
        AND (SELECT count(*)=6 FROM pg_policies WHERE schemaname='lead'
            AND tablename IN ('growth_experiments','account_operating_intents','profile_execution_routes')
            AND policyname IN ('agent_scope','member_scope'))
        AND has_table_privilege('lead_runtime','lead.growth_experiments','INSERT')
        AND NOT has_table_privilege('lead_runtime','lead.account_operating_intents','INSERT')
        AND NOT has_table_privilege('lead_runtime','lead.account_operating_intents','UPDATE')
        AND NOT has_table_privilege('lead_runtime','lead.profile_execution_routes','UPDATE')
        AND has_function_privilege('lead_auth','lead.growth_set_intent(uuid,uuid,text,text,text,bigint,jsonb)','EXECUTE')
        AND NOT has_function_privilege('lead_runtime','lead.growth_set_intent(uuid,uuid,text,text,text,bigint,jsonb)','EXECUTE')
        AND has_function_privilege('lead_runtime','lead.growth_receipt_valid(uuid)','EXECUTE')
        AND has_function_privilege('lead_runtime','lead.growth_confirm_publication(uuid)','EXECUTE')
        AND (SELECT count(*)=4 FROM pg_trigger WHERE tgenabled='O'
            AND tgname IN ('growth_task_owner','growth_action_owner','content_target_before_action','growth_disconnect_intent'))
        AND EXISTS(SELECT FROM pg_constraint WHERE conrelid='lead.tasks'::regclass AND conname='task_growth_experiment')
        AND EXISTS(SELECT FROM pg_constraint WHERE conrelid='lead.browser_runs'::regclass AND conname='growth_research_profile')
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_content_target' AND md5(prosrc)='56d7b2d54aa0235a22d6fe7f769c2edc')
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_read_intent' AND md5(prosrc)='edf9680628a7b005069dbc45a42892e6')
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_read_route' AND md5(prosrc)='ae42619cd4089f6f2bbfc6f70e9d343d')
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_set_intent' AND md5(prosrc)='5cfb3ef37809d4c947cfbf4a30d856e3')
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_disconnect_intent' AND md5(prosrc)='b568fc706e1f0bfca2e071224facd5a3')
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_complete_connection' AND md5(prosrc) IN ('cab9778a5c090dd092b03c2982327ba4','fe6c839637f14db267c2b0dae12b6eef'))
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_receipt_valid' AND md5(prosrc) IN ('a4bca05077668d2f9a28a7806ffbecc1','f868f71708e1cdcd167aac21b3a43630'))
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_confirm_publication' AND md5(prosrc) IN ('1be562501250d3092463dadc05414241','4e4b9df37d438d1232deba642c577e93'))
        AND EXISTS(SELECT FROM pg_proc WHERE pronamespace='lead'::regnamespace AND proname='growth_owner_fence' AND md5(prosrc)='5ac78ca4773b17f90ef940d253b885dd')
    """


def reviewed_growth_runtime(mirror,sha,extra):
    paths = extra.intersection(GROWTH_RUNTIME_FILES)
    if not paths:
        return set()
    for path in paths:
        require(run('git','--git-dir',mirror,'rev-parse',f'{sha}:{path}')==GROWTH_RUNTIME_FILES[path],
                'growth runtime upgrade requires compatibility review')
    site = TOOLS/'home_live_maintenance.py'
    require(site.is_file(),'existing node database verifier required')
    program = ('import runpy;read=runpy.run_path('+repr(str(site))+')["site_sql"];print(read('+repr(growth_schema_query())+'))')
    require(run('/usr/bin/python3','-IB','-c',program)=='t','growth runtime schema is not ready')
    require(growth_cutover_pending(sha),'growth runtime cutover hold is missing')
    return paths


def complete_growth_cutover(sha):
    """Keep the original deployment in progress until actual consumers agree."""
    require(growth_cutover_pending(sha),'growth runtime cutover hold is missing')
    stage=STORE/('maintenance-'+sha)
    journal=json.loads(executor_host_file(stage/'growth-migration.json',private=True))
    require(journal.get('sha')==sha and SHA.fullmatch(journal.get('old','')),'growth migration journal differs')
    helper=stage/'source/scripts/growth_runtime_upgrade.py'
    executor_host_file(helper)
    expected=GROWTH_RUNTIME_FILES['scripts/growth_runtime_upgrade.py']
    require(run('git','hash-object','--no-filters',helper)==expected,'growth finalizer source differs')
    # Both consumers need this revision's actual provisioning proof. Following
    # main-current updates Reddit's code mount, but does not create its journal.
    for channel in ('telegram','reddit'):
        provision_executor(sha,EXECUTOR_REGISTRY.parent/(channel+'-provision.json'))
    register_executors(sha)
    run(EXECUTOR_PYTHON_SOURCE,'-I','-B',helper,'finish','--sha',sha,'--old',journal['old'],timeout=180)
    require(not growth_cutover_pending(sha),'growth finalizer did not release its journal')


# Exact blobs are frozen only after core/schema/runtime packaging has passed its
# targeted checks. Unfrozen values deliberately cannot admit a release.
UNIFIED_BROWSER_FILES = {
    'apps/web/next.config.ts': '36f582242ca36fa5274008ef981aec32580a8590',
    'infra/migrations/sql/0032_research_memory.sql': '1cd03ca94dae3bdd3bcd11124c92bd224a9c6e15',
    'infra/migrations/sql/0033_unified_browser.sql': '2328c5a253438f1b157d24cf71f81b18075485b8',
    'infra/migrations/sql/0034_local_executor.sql': '77be5f0e80251e67020427e4b4f9947079196753',
    'infra/migrations/versions/0032_research_memory.py': '6f1423fefdd2fa3aba498b37d9f7fad9159ced65',
    'infra/migrations/versions/0033_unified_browser.py': '8f75f097ad91baac8295537e6d5a52b49b503693',
    'infra/migrations/versions/0034_local_executor.py': '8902def3c7daa0578de3e9e3bebf9674992d1411',
    'requirements.browser-use.lock.txt': '9105c7748d6aa830f312114e4f9d3e1431e4f911',
    'requirements.lock.txt': 'dcf3273dc078a66007e8db836ecefa2518834bbf',
    'scripts/browser_namespace.py': '7ecb47f233761b910317126d9d15e8edfb9fe21b',
    'scripts/browser_use_runner.py': '7abc6429011cd5087ca5413b1916ab24969116df',
    'scripts/build_browser_runtime.py': 'cbc4d3d3521a2fe50c589813cf0d3956289ca94a',
    'scripts/build_home_image.sh': '77023d7fa2a3ab679d3556c25936a278d0641cc8',
    'scripts/unified_browser_upgrade.py': '64ba9808dd6a667338b0af6e09b2f73abb5a3f2a',
    'services/local-executor/.gitignore': '72bdd5d427f474fb9b7835d3226d921ef7a8bd6f',
    'services/local-executor/README.md': 'a14c1164203cadf8aa3ede18c649c536aa73e5f8',
    'services/local-executor/package-lock.json': 'cc1e5486a892bed0fdb59f70c6c0cc083659bbdc',
    'services/local-executor/package.json': 'e7f3c2fc9606e76c01dcdd91dab71650fa1202cf',
    'services/local-executor/src/capabilities.ts': 'e49690e0145c8b1354273db0751f7d6e75a91fe7',
    'services/local-executor/src/cli.ts': 'b759a15663023ff9861b4a7fdce8134c95abebab',
    'services/local-executor/src/daemon.ts': 'e9c49fa5297b64e5da72d5e38ee35122cbba3ccb',
    'services/local-executor/src/launchd.ts': '1e77964b223df26ec81407c9c94cd7061e280d69',
    'services/local-executor/src/mcp.ts': '6567311ef9d318ba0e1793085155128ad3dc85d8',
    'services/local-executor/src/rpc.ts': '7f6929242c23086ef5126ce6eaa951939c07250f',
    'services/local-executor/src/runner.ts': 'b72b3ccbe60c4ebb1a91dcaab32ce6149f08e41b',
    'services/local-executor/src/store.ts': '8e28f5b83f01ad366994707fc57e2fe582df6a7f',
    'services/local-executor/src/transport.ts': '67420728f073acd762e15484aa485df30cd67994',
    'services/local-executor/src/types.ts': '7dedb73166381d0409232a12d83b5bcfd9f03b39',
    'services/local-executor/test/cli.test.ts': '1298a9fc3b82b7a9136076f5d51dd385d8447926',
    'services/local-executor/test/consumer-api.py': '409e9c530464f9e2da19469a40316498ed2fa836',
    'services/local-executor/test/consumer-compact.ts': 'd58c5832684a7709ce20bfd219194e4bb45f7670',
    'services/local-executor/test/consumer-runner.ts': 'ffa1dfed3a0a2d04572790a3d8b92d5daa14b1fb',
    'services/local-executor/test/integration-api.py': '3e89fb92c88d7dff046573599452c66bb14c4a21',
    'services/local-executor/test/integration-control.ts': '4b2e278a428b5424c748443220f5f494d0a2dfcd',
    'services/local-executor/test/integration.ts': '69d2cc0244096085227945f7543dba96b628d077',
    'services/local-executor/test/mcp.test.ts': '40c634be81ee3731a91c390bb689ede4d2fff8e4',
    'services/local-executor/test/runtime.test.ts': 'd363bed2b4e1297e93ecd4e356f000c94ede27ed',
    'services/local-executor/test/subagent-api.py': '2d143bc1608954fc729a12751c889961375fd3cf',
    'services/local-executor/test/subagent-metadata.ts': '610a0e379c23d928785f552d3cbe88a2451f88b5',
    'services/local-executor/test/transport.test.ts': '290aadf7abc7a7ed1e1a68eda2b43a73ee310e3a',
    'services/local-executor/test/verify_onboarding_ui.py': 'ccff869c5a5e4abd8719c6ba8dc80b1f4d1b3fdb',
    'services/local-executor/tsconfig.json': '513e6691417731913efca310e2b6d7f0142ca98e',
}


LOCAL_EXECUTOR_FILES = frozenset((
    'services/local-executor/.gitignore',
    'services/local-executor/README.md',
    'services/local-executor/package-lock.json',
    'services/local-executor/package.json',
    'services/local-executor/src/capabilities.ts',
    'services/local-executor/src/cli.ts',
    'services/local-executor/src/daemon.ts',
    'services/local-executor/src/launchd.ts',
    'services/local-executor/src/mcp.ts',
    'services/local-executor/src/rpc.ts',
    'services/local-executor/src/runner.ts',
    'services/local-executor/src/store.ts',
    'services/local-executor/src/transport.ts',
    'services/local-executor/src/types.ts',
    'services/local-executor/test/cli.test.ts',
    'services/local-executor/test/consumer-api.py',
    'services/local-executor/test/consumer-compact.ts',
    'services/local-executor/test/consumer-runner.ts',
    'services/local-executor/test/integration-api.py',
    'services/local-executor/test/integration-control.ts',
    'services/local-executor/test/integration.ts',
    'services/local-executor/test/mcp.test.ts',
    'services/local-executor/test/runtime.test.ts',
    'services/local-executor/test/subagent-api.py',
    'services/local-executor/test/subagent-metadata.ts',
    'services/local-executor/test/transport.test.ts',
    'services/local-executor/test/verify_onboarding_ui.py',
    'services/local-executor/tsconfig.json',
))


def browser_schema_query():
    return "SELECT ("+growth_schema_query().removeprefix("SELECT ")+""" )
        AND (SELECT array_agg(version_num::text) FROM public.lead_migration_version) IN (ARRAY['0033_unified_browser'],ARRAY['0034_local_executor'],ARRAY['0035_shared_executor'])
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.growth_complete_connection(uuid,uuid,uuid,bigint,bigint,bigint,jsonb,text,jsonb)') AND prosecdef AND md5(prosrc)='fe6c839637f14db267c2b0dae12b6eef')
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.growth_receipt_valid(uuid)') AND prosecdef AND md5(prosrc)='f868f71708e1cdcd167aac21b3a43630')
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.growth_confirm_publication(uuid)') AND prosecdef AND md5(prosrc)='4e4b9df37d438d1232deba642c577e93')
        AND (SELECT count(*)=2 AND bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE oid IN
            (to_regclass('lead.experiences'),to_regclass('lead.research_allowances')))
        AND (SELECT count(*)=3 FROM pg_policies WHERE schemaname='lead'
            AND (tablename,policyname) IN (('experiences','agent_scope'),('experiences','member_scope'),('research_allowances','tenant_scope')))
        AND has_table_privilege('lead_runtime','lead.experiences','SELECT')
        AND has_table_privilege('lead_runtime','lead.experiences','INSERT')
        AND has_table_privilege('lead_runtime','lead.research_allowances','UPDATE')
        AND has_function_privilege('lead_runtime','lead.public_experiences(uuid)','EXECUTE')
        AND NOT EXISTS(SELECT FROM pg_proc p,aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid=to_regprocedure('lead.public_experiences(uuid)') AND a.grantee=0 AND a.privilege_type='EXECUTE')
        AND EXISTS(SELECT FROM pg_trigger WHERE tgrelid='lead.experiences'::regclass
            AND tgname='experience_projection' AND tgenabled='O' AND tgfoid=to_regprocedure('lead.check_experience_projection()'))
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.public_experiences(uuid)') AND prosecdef AND md5(prosrc)='bc25a6e8dbac3fcd198153cbc9723b66')
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.check_experience_projection()') AND md5(prosrc)='134fb9fdb3ee79a63ba3fd411c6c8f8c')
        AND (SELECT count(*)=5 FROM pg_constraint WHERE convalidated AND
            (conrelid,conname,md5(pg_get_constraintdef(oid))) IN (
            ('lead.takeover_sessions'::regclass,'connection_scoped_account','0075da41f4dee854c2f4f69d61bd1838'),
            ('lead.takeover_sessions'::regclass,'takeover_sessions_automatic_attempts_check','bdadc4dcd889555ef5d133595e2c23eb'),
            ('lead.takeover_sessions'::regclass,'takeover_sessions_challenge_version_check','d4e4efd7f90a999728a087f7df9d272e'),
            ('lead.takeover_sessions'::regclass,'takeover_sessions_control_mode_check','9090f486dcd4bf6c44e1b59e343edf12'),
            ('lead.social_accounts'::regclass,'web_connection_origin_required','d046c50b7d4dfff2a1753f8acc18ca8b')))
        AND (SELECT count(*)=3 FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid
            WHERE x.relnamespace='lead'::regnamespace AND i.indisunique AND i.indisvalid AND i.indisready
            AND (x.relname,md5(pg_get_indexdef(i.indexrelid))) IN (
            ('browser_request_original_action','eb4fcee9f141150500a90664e355d933'),
            ('one_account_controller','6ec20aed23f164cfd84f75f917554342'),
            ('one_live_takeover','7d8070d98dff321128fb72c9adc5cf18')))
        AND (SELECT count(*)=2 FROM pg_trigger WHERE tgenabled='O' AND
            (tgrelid,tgname,tgfoid) IN (
            ('lead.takeover_sessions'::regclass,'connection_login_lifetime',to_regprocedure('lead.clear_connection_login()')),
            ('lead.agents'::regclass,'expire_login_control',to_regprocedure('lead.expire_login_on_control_change()'))))
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.clear_connection_login()') AND md5(prosrc)='6a9c4c62dd7a1a54c805ed43f8850842')
        AND EXISTS(SELECT FROM pg_proc WHERE oid=to_regprocedure('lead.expire_login_on_control_change()') AND md5(prosrc)='fe200520acc3d2ca82f22e36dd616a7c')
        AND has_function_privilege('lead_auth','lead.growth_complete_connection(uuid,uuid,uuid,bigint,bigint,bigint,jsonb,text,jsonb)','EXECUTE')
        AND NOT has_function_privilege('lead_runtime','lead.growth_complete_connection(uuid,uuid,uuid,bigint,bigint,bigint,jsonb,text,jsonb)','EXECUTE')
    """


def local_executor_schema_query(*, shared=False):
    """Exact additive transport keys and RLS; no production facts are sampled."""
    version='0035_shared_executor' if shared else '0034_local_executor'
    original_policies="polname IN ('agent_scope','member_scope') AND " if shared else ''
    return f"""SELECT
        (SELECT array_agg(version_num::text) FROM public.lead_migration_version)=ARRAY['{version}']
        AND (SELECT count(*)=5 AND bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class
            WHERE relnamespace='lead'::regnamespace AND relname IN ('local_executor_devices','local_executor_projects',
                'local_executor_requests','local_executor_commands','local_executor_events'))
        AND (SELECT count(*)=10 AND bool_and(polcmd='*' AND polroles=ARRAY[0::oid] AND
            CASE polname
              WHEN 'agent_scope' THEN polpermissive AND md5(pg_get_expr(polqual,polrelid))='3e0c5a9d5892e92b7636fe01c2340832'
                AND md5(pg_get_expr(polwithcheck,polrelid))='3e0c5a9d5892e92b7636fe01c2340832'
              WHEN 'member_scope' THEN NOT polpermissive AND md5(pg_get_expr(polqual,polrelid))='ba49c86007a3518c764dfc9790c67b50'
                AND md5(pg_get_expr(polwithcheck,polrelid))='ba49c86007a3518c764dfc9790c67b50'
              ELSE false END) FROM pg_policy
            WHERE {original_policies}polrelid IN (to_regclass('lead.local_executor_devices'),to_regclass('lead.local_executor_projects'),
                to_regclass('lead.local_executor_requests'),to_regclass('lead.local_executor_commands'),to_regclass('lead.local_executor_events')))
        AND (SELECT count(*)=22 FROM pg_constraint WHERE convalidated
            AND (conindid=0 OR EXISTS(SELECT FROM pg_index WHERE indexrelid=conindid AND indisunique AND indisvalid AND indisready))
            AND (conrelid,conname,md5(pg_get_constraintdef(oid))) IN (
            ('lead.local_executor_commands'::regclass,'local_executor_commands_kind_check','7704c55ae6424857a322dd140d4c0b0c'),
            ('lead.local_executor_commands'::regclass,'local_executor_commands_pkey','4c6419b3704337bbfe50f018842a9ad3'),
            ('lead.local_executor_commands'::regclass,'local_executor_commands_tenant_id_agent_id_request_id_comma_key','c23f1b0177643c131288608b0515bfa8'),
            ('lead.local_executor_commands'::regclass,'local_executor_commands_tenant_id_agent_id_request_id_fkey','f8bd293d1a3855cb11c2b2c9ebe4b053'),
            ('lead.local_executor_devices'::regclass,'local_executor_devices_epoch_check','542480a8da1ec524fa52b99f57b19ee8'),
            ('lead.local_executor_devices'::regclass,'local_executor_devices_pkey','4c6419b3704337bbfe50f018842a9ad3'),
            ('lead.local_executor_devices'::regclass,'local_executor_devices_state_check','8047b4af23b864dd84d9c178112b1c05'),
            ('lead.local_executor_devices'::regclass,'local_executor_devices_tenant_id_agent_id_fkey','03af99f687a8794aaae4a68c0b96058b'),
            ('lead.local_executor_devices'::regclass,'local_executor_devices_tenant_id_agent_id_id_key','6309755b4f041a54136c3df39beb5874'),
            ('lead.local_executor_events'::regclass,'local_executor_events_pkey','d21c800fdf017e9de072dd72b98ea446'),
            ('lead.local_executor_events'::regclass,'local_executor_events_tenant_id_agent_id_request_id_fkey','f8bd293d1a3855cb11c2b2c9ebe4b053'),
            ('lead.local_executor_projects'::regclass,'local_executor_projects_mode_check','601fb6282b6d3e461e0f30e3c7448f04'),
            ('lead.local_executor_projects'::regclass,'local_executor_projects_pkey','f34326dabef8749613ed9f8abb497019'),
            ('lead.local_executor_projects'::regclass,'local_executor_projects_tenant_id_agent_id_device_id_fkey','b6920ea1bc091fd7bec91856e4910a8f'),
            ('lead.local_executor_projects'::regclass,'local_executor_projects_tenant_id_agent_id_fkey','03af99f687a8794aaae4a68c0b96058b'),
            ('lead.local_executor_requests'::regclass,'local_executor_requests_pkey','4c6419b3704337bbfe50f018842a9ad3'),
            ('lead.local_executor_requests'::regclass,'local_executor_requests_state_check','fd2d500fc858b1c1402e822d3389e28c'),
            ('lead.local_executor_requests'::regclass,'local_executor_requests_tenant_id_agent_id_device_id_fkey','b6920ea1bc091fd7bec91856e4910a8f'),
            ('lead.local_executor_requests'::regclass,'local_executor_requests_tenant_id_agent_id_fkey','03af99f687a8794aaae4a68c0b96058b'),
            ('lead.local_executor_requests'::regclass,'local_executor_requests_tenant_id_agent_id_id_key','6309755b4f041a54136c3df39beb5874'),
            ('lead.local_executor_requests'::regclass,'local_executor_requests_tenant_id_agent_id_request_key_key','5bf052683cf55aff8667edc520c5d5d1'),
            ('lead.local_executor_requests'::regclass,'local_executor_requests_tenant_id_agent_id_task_id_fkey','262a2148e4eef17c00764e401acdc4c7')))
        AND NOT EXISTS(SELECT FROM pg_trigger t JOIN pg_constraint c ON c.oid=t.tgconstraint
            WHERE c.contype='f' AND t.tgenabled<>'O' AND c.conrelid IN
                (to_regclass('lead.local_executor_devices'),to_regclass('lead.local_executor_projects'),
                to_regclass('lead.local_executor_requests'),to_regclass('lead.local_executor_commands'),to_regclass('lead.local_executor_events')))
        AND (SELECT bool_and(has_table_privilege('lead_runtime',oid,'SELECT')
            AND has_table_privilege('lead_runtime',oid,'INSERT') AND has_table_privilege('lead_runtime',oid,'UPDATE')
            AND NOT has_table_privilege('lead_runtime',oid,'DELETE'))
            FROM pg_class WHERE relnamespace='lead'::regnamespace AND relname IN ('local_executor_devices','local_executor_projects',
                'local_executor_requests','local_executor_commands','local_executor_events'))
    """


def unified_schema_query():
    return "SELECT ("+browser_schema_query().removeprefix("SELECT").strip()+") AND ("+local_executor_schema_query().removeprefix("SELECT").strip()+")"


# Public source blobs are reviewed separately from the catalog of an actual
# PostgreSQL 18 rehearsal. A missing catalog attestation must never admit DDL.
SHARED_EXECUTOR_FILES = {
    'apps/web/next.config.ts': '36f582242ca36fa5274008ef981aec32580a8590',
    'infra/local-executor/egress.py': '0e710ad696dcd3b801397037c1192a313911ffa0',
    'infra/local-executor/lead-codex-egress.conf': '42e731ae9d0ff5b45c68dae0b0f62ff770060493',
    'infra/local-executor/lead-codex-egress.service': 'e2199a737732bba1e18471d3e11920ad350c42d2',
    'infra/local-executor/lead-codex-node.service': '3456e0d0103ea4a4faef176ac3ba30a72a12c513',
    'infra/local-executor/lead-codex-node.socket': '1c925357993bcb6e6b6fb3ca1400006edcd4b347',
    'infra/local-executor/lead-codex-worker.service': '44df0a853be5f2dc0a217484fc89e3f156f886df',
    'infra/local-executor/lead-codex@.service': '2574edb43a06ccbbd4866b0342a4e8f69be492a6',
    'infra/local-executor/node_broker.py': '71f516f86a2f43090495315a8e48e446ef197030',
    'infra/migrations/sql/0032_research_memory.sql': '1cd03ca94dae3bdd3bcd11124c92bd224a9c6e15',
    'infra/migrations/sql/0033_unified_browser.sql': '2328c5a253438f1b157d24cf71f81b18075485b8',
    'infra/migrations/sql/0034_local_executor.sql': '77be5f0e80251e67020427e4b4f9947079196753',
    'infra/migrations/sql/0035_shared_executor.sql': 'fe714092acb0b7cc3377342f1b7c1ff5be5de08d',
    'infra/migrations/versions/0032_research_memory.py': '6f1423fefdd2fa3aba498b37d9f7fad9159ced65',
    'infra/migrations/versions/0033_unified_browser.py': '8f75f097ad91baac8295537e6d5a52b49b503693',
    'infra/migrations/versions/0034_local_executor.py': '8902def3c7daa0578de3e9e3bebf9674992d1411',
    'infra/migrations/versions/0035_shared_executor.py': '44ed281a355e4532e242967004f24af84983f6d0',
    'requirements.browser-use.lock.txt': '9105c7748d6aa830f312114e4f9d3e1431e4f911',
    'requirements.lock.txt': 'dcf3273dc078a66007e8db836ecefa2518834bbf',
    'scripts/browser_namespace.py': '7ecb47f233761b910317126d9d15e8edfb9fe21b',
    'scripts/browser_use_runner.py': '7abc6429011cd5087ca5413b1916ab24969116df',
    'scripts/build_browser_runtime.py': 'cbc4d3d3521a2fe50c589813cf0d3956289ca94a',
    'scripts/build_home_image.sh': '2f90521b4f04fcdb3b90179958decd4fbffc5827',
    'scripts/growth_runtime_upgrade.py': '4e0cb2f4a63ece38be37a3ef3ac8e2a05f14fe7c',
    'scripts/shared_executor_upgrade.py': 'efd0a3907f70297e455989af608f1fad8906f264',
    'scripts/unified_browser_upgrade.py': '8d6c4e9fc060b2cc907c9849838e6483cb9d196d',
    'services/local-executor/.gitignore': '72bdd5d427f474fb9b7835d3226d921ef7a8bd6f',
    'services/local-executor/README.md': 'a14c1164203cadf8aa3ede18c649c536aa73e5f8',
    'services/local-executor/package-lock.json': 'cc1e5486a892bed0fdb59f70c6c0cc083659bbdc',
    'services/local-executor/package.json': 'e7f3c2fc9606e76c01dcdd91dab71650fa1202cf',
    'services/local-executor/src/broker-client.ts': 'a348cef2b138854f1e3b3d1f02fb9ac997abdc51',
    'services/local-executor/src/capabilities.ts': 'e49690e0145c8b1354273db0751f7d6e75a91fe7',
    'services/local-executor/src/cli.ts': '97c3efb31282f65048e301132c5ac34978b360c3',
    'services/local-executor/src/daemon.ts': '93cb3da3d9ae828c958c5b6ca517299825c0dcea',
    'services/local-executor/src/launchd.ts': '1e77964b223df26ec81407c9c94cd7061e280d69',
    'services/local-executor/src/mcp.ts': '432118740c6839772d71e7efcd1a3a54c07854cf',
    'services/local-executor/src/request-bridge.ts': 'c19a5229ea2a5d5ebd5ba163b71b5aac2f19a12e',
    'services/local-executor/src/rpc.ts': '3f38f34f49b37f3a31756366e9ebde1646075a8f',
    'services/local-executor/src/runner.ts': '741dbc978555ca2231639611dc5c73ed9b7726e3',
    'services/local-executor/src/server-binding.ts': '02bc1cc9ec4504750f7c3fd512b8b30aaec775d5',
    'services/local-executor/src/server-worker.ts': '122afb64e2a1ed475f42f38d958e69cc2c1553a7',
    'services/local-executor/src/shared-runtime.ts': '367fff19f56b93b4d8e7a6da985e73254e26c5b4',
    'services/local-executor/src/store.ts': '8e69365e411894e42dab3900d337e0da1a95188f',
    'services/local-executor/src/transport.ts': '67420728f073acd762e15484aa485df30cd67994',
    'services/local-executor/src/types.ts': 'e02fb8fe26ae84b68284af5b60014de08db0dd8d',
    'services/local-executor/test/cli.test.ts': '1298a9fc3b82b7a9136076f5d51dd385d8447926',
    'services/local-executor/test/consumer-api.py': '409e9c530464f9e2da19469a40316498ed2fa836',
    'services/local-executor/test/consumer-compact.ts': 'd58c5832684a7709ce20bfd219194e4bb45f7670',
    'services/local-executor/test/consumer-runner.ts': 'ffa1dfed3a0a2d04572790a3d8b92d5daa14b1fb',
    'services/local-executor/test/integration-api.py': '3e89fb92c88d7dff046573599452c66bb14c4a21',
    'services/local-executor/test/integration-control.ts': '4b2e278a428b5424c748443220f5f494d0a2dfcd',
    'services/local-executor/test/integration.ts': '69d2cc0244096085227945f7543dba96b628d077',
    'services/local-executor/test/mcp.test.ts': '40c634be81ee3731a91c390bb689ede4d2fff8e4',
    'services/local-executor/test/request-bridge.test.ts': 'f3143b65f7621d50378c704d69ead871b0530c6b',
    'services/local-executor/test/runtime.test.ts': 'd363bed2b4e1297e93ecd4e356f000c94ede27ed',
    'services/local-executor/test/server-worker.test.ts': 'df2e40ca328d4469c79b42ed2c027062f53a12fc',
    'services/local-executor/test/subagent-api.py': '2d143bc1608954fc729a12751c889961375fd3cf',
    'services/local-executor/test/subagent-metadata.ts': '610a0e379c23d928785f552d3cbe88a2451f88b5',
    'services/local-executor/test/transport.test.ts': '290aadf7abc7a7ed1e1a68eda2b43a73ee310e3a',
    'services/local-executor/test/verify_onboarding_ui.py': 'ccff869c5a5e4abd8719c6ba8dc80b1f4d1b3fdb',
    'services/local-executor/tsconfig.json': '513e6691417731913efca310e2b6d7f0142ca98e',
}
# PostgreSQL 18 rehearsal: seven permission/key/policy mutations rejected.
# Evidence SHA256: 273ca6daff28687df90bb91c8c06000cf2fe740a4f2ecd2d8c670ed277dc2cc5
SHARED_EXECUTOR_CATALOG_MD5 = '5bb5c1b655606cf163aef47b5d57ab82'
# PostgreSQL 18.6 registered-admin backup clone: two migrations, nine mutations rejected.
# Catalog SHA256: 8d207c035353958495aa56e9a5b5f68d031ecf083ca6abccb1e816a715d67063
# Proof SHA256: b71b527eafa9b4bae701a73655dc0bf83652c3e03861ac92a6aa123015c6b9c1
SHARED_EXECUTOR_SITE_CATALOG_MD5 = '64239613f49e124deb187afa68d32a91'


def shared_executor_catalog_query():
    """Deterministic catalog only: no credentials or application rows sampled.

    Names replace installation-specific OIDs. Original transport tables and
    the execution-default config constraint remain in scope. Freeze only the
    actual rehearsed query result; no live application rows are inspected.
    """
    return """WITH tables AS (
        SELECT c.* FROM pg_class c WHERE c.relnamespace='lead'::regnamespace AND c.relname IN
        ('local_executor_devices','local_executor_projects','local_executor_requests','local_executor_commands',
         'local_executor_events','executor_nodes','executor_node_grants','executor_studio_requests',
         'executor_studio_commands','executor_studio_events','admin_configs')),
    funcs AS (SELECT p.* FROM pg_proc p WHERE p.pronamespace='lead'::regnamespace
        AND (p.proname LIKE 'executor\\_%' ESCAPE '\\' OR p.proname LIKE 'ctx\\_executor\\_%' ESCAPE '\\')),
    roles AS (SELECT * FROM pg_roles WHERE rolname IN ('lead_runtime','lead_auth','lead_identity_owner')
        OR oid IN (SELECT relowner FROM tables UNION SELECT proowner FROM funcs))
    SELECT jsonb_build_object(
      'tables',(SELECT jsonb_agg(jsonb_build_array(c.relname,pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity,
        (SELECT jsonb_agg(jsonb_build_array(coalesce(r.rolname,'PUBLIC'),a.privilege_type,a.is_grantable)
          ORDER BY coalesce(r.rolname,'PUBLIC'),a.privilege_type,a.is_grantable)
         FROM aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a LEFT JOIN pg_roles r ON r.oid=a.grantee))
        ORDER BY c.relname) FROM tables c),
      'columns',(SELECT jsonb_agg(jsonb_build_array(c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
        pg_get_expr(d.adbin,d.adrelid),
        has_column_privilege('lead_runtime',c.oid,a.attnum,'SELECT'),
        has_column_privilege('lead_runtime',c.oid,a.attnum,'INSERT'),
        has_column_privilege('lead_runtime',c.oid,a.attnum,'UPDATE'),
        (SELECT jsonb_agg(jsonb_build_array(coalesce(r.rolname,'PUBLIC'),x.privilege_type,x.is_grantable)
          ORDER BY coalesce(r.rolname,'PUBLIC'),x.privilege_type,x.is_grantable)
         FROM aclexplode(a.attacl) x LEFT JOIN pg_roles r ON r.oid=x.grantee)) ORDER BY c.relname,a.attnum)
        FROM tables c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum),
      'constraints',(SELECT jsonb_agg(jsonb_build_array(c.relname,k.conname,k.contype,k.convalidated,
        k.condeferrable,k.condeferred,pg_get_constraintdef(k.oid)) ORDER BY c.relname,k.conname)
        FROM tables c JOIN pg_constraint k ON k.conrelid=c.oid),
      'indexes',(SELECT jsonb_agg(jsonb_build_array(c.relname,x.relname,i.indisunique,i.indisvalid,i.indisready,
        pg_get_indexdef(i.indexrelid)) ORDER BY c.relname,x.relname)
        FROM tables c JOIN pg_index i ON i.indrelid=c.oid JOIN pg_class x ON x.oid=i.indexrelid),
      'policies',(SELECT jsonb_agg(jsonb_build_array(c.relname,p.polname,p.polcmd,p.polpermissive,
        (SELECT jsonb_agg(coalesce(r.rolname,'PUBLIC') ORDER BY coalesce(r.rolname,'PUBLIC'))
         FROM unnest(p.polroles) u(oid) LEFT JOIN pg_roles r ON r.oid=u.oid),
        pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid)) ORDER BY c.relname,p.polname)
        FROM tables c JOIN pg_policy p ON p.polrelid=c.oid),
      'triggers',(SELECT jsonb_agg(jsonb_build_array(c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid))
        ORDER BY c.relname,t.tgname) FROM tables c JOIN pg_trigger t ON t.tgrelid=c.oid WHERE NOT t.tgisinternal),
      'foreign_keys_enabled',NOT EXISTS(SELECT FROM tables c JOIN pg_constraint k ON k.conrelid=c.oid
        JOIN pg_trigger t ON t.tgconstraint=k.oid WHERE k.contype='f' AND t.tgenabled<>'O'),
      'functions',(SELECT jsonb_agg(jsonb_build_array(p.proname,pg_get_function_identity_arguments(p.oid),
        pg_get_function_result(p.oid),p.prosrc,p.prosecdef,p.proconfig,pg_get_userbyid(p.proowner),
        l.lanname,p.provolatile,p.proisstrict,p.proleakproof,p.proparallel,
        has_function_privilege('lead_runtime',p.oid,'EXECUTE'),has_function_privilege('lead_auth',p.oid,'EXECUTE'),
        (SELECT jsonb_agg(jsonb_build_array(coalesce(r.rolname,'PUBLIC'),a.privilege_type,a.is_grantable)
          ORDER BY coalesce(r.rolname,'PUBLIC'),a.privilege_type,a.is_grantable)
         FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a LEFT JOIN pg_roles r ON r.oid=a.grantee))
        ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)) FROM funcs p JOIN pg_language l ON l.oid=p.prolang),
      'roles',(SELECT jsonb_agg(jsonb_build_array(rolname,rolsuper,rolbypassrls,rolinherit,rolcreaterole,rolcreatedb,
        rolreplication,rolcanlogin) ORDER BY rolname) FROM roles),
      'memberships',(SELECT jsonb_agg(jsonb_build_array(parent.rolname,child.rolname,m.admin_option,m.inherit_option,m.set_option)
        ORDER BY parent.rolname,child.rolname) FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid
        JOIN pg_roles child ON child.oid=m.member WHERE m.member IN (SELECT oid FROM roles))
    )"""


def shared_executor_schema_query():
    require(re.fullmatch('[a-f0-9]{32}',SHARED_EXECUTOR_CATALOG_MD5),
            'shared-executor catalog is not frozen')
    digests = [SHARED_EXECUTOR_CATALOG_MD5]
    if SHARED_EXECUTOR_SITE_CATALOG_MD5 is not None:
        require(type(SHARED_EXECUTOR_SITE_CATALOG_MD5) is str
                and re.fullmatch('[a-f0-9]{32}',SHARED_EXECUTOR_SITE_CATALOG_MD5),
                'shared-executor site catalog is not frozen')
        digests.append(SHARED_EXECUTOR_SITE_CATALOG_MD5)
    exact_catalogs = ','.join("'"+value+"'" for value in digests)
    return ('SELECT ('+browser_schema_query().removeprefix('SELECT').strip()+') AND ('+
            local_executor_schema_query(shared=True).removeprefix('SELECT').strip()+
            ") AND (SELECT count(*)=5 AND bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class "
            "WHERE relnamespace='lead'::regnamespace AND relname IN ('executor_nodes','executor_node_grants',"
            "'executor_studio_requests','executor_studio_commands','executor_studio_events')) AND "
            "(SELECT count(*)=2 AND bool_and(NOT rolsuper AND NOT rolbypassrls) FROM pg_roles "
            "WHERE rolname IN ('lead_runtime','lead_auth')) AND "
            "md5(("+shared_executor_catalog_query()+")::text) IN ("+exact_catalogs+")")


# Only the reviewed v2 forwarding addition; no new DDL or cutover journal.
SHARED_EXECUTOR_PROXY_BLOB = '8b07fed2ae460ee3b4404df64e91ebff38a225f6'


def reviewed_shared_executor_proxy(mirror, sha, extra):
    path = 'apps/web/next.config.ts'
    if path not in extra:
        return set()
    require(run('git', '--git-dir', mirror, 'rev-parse', f'{sha}:{path}') == SHARED_EXECUTOR_PROXY_BLOB,
            'shared-executor proxy requires compatibility review')
    site = TOOLS / 'home_live_maintenance.py'
    require(site.is_file(), 'existing node database verifier required')
    query = shared_executor_schema_query()
    program = ('import runpy;read=runpy.run_path(' + repr(str(site)) + ')["site_sql"];print(read(' + repr(query) + '))')
    require(run('/usr/bin/python3', '-IB', '-c', program) == 't', 'shared-executor schema is not ready')
    verify_local_executor_package(sha)
    return {path}


def reviewed_shared_executor(mirror,sha,extra):
    paths=extra.intersection(SHARED_EXECUTOR_FILES)
    if not paths:
        return set()
    if not shared_cutover_pending(sha):
        return set()  # Old exact-blob admission remains authoritative for 0034.
    require(SHARED_EXECUTOR_FILES and all(SHA.fullmatch(v) for v in SHARED_EXECUTOR_FILES.values()),
            'shared-executor release blobs are not frozen')
    for path in paths:
        require(run('git','--git-dir',mirror,'rev-parse',f'{sha}:{path}')==SHARED_EXECUTOR_FILES[path],
                'shared-executor files require compatibility review')
    query=shared_executor_schema_query()
    site=TOOLS/'home_live_maintenance.py'
    require(site.is_file(),'existing node database verifier required')
    program=('import runpy;read=runpy.run_path('+repr(str(site))+')["site_sql"];print(read('+repr(query)+'))')
    require(run('/usr/bin/python3','-IB','-c',program)=='t','shared-executor schema is not ready')
    require(shared_cutover_pending(sha),'shared-executor cutover hold is missing')
    verify_local_executor_package(sha)
    return paths


def verify_local_executor_package(sha):
    """Inspect the same immutable image's package without extracting or running it."""
    root = image(sha)
    package = root/'services/local-executor/lead-local-executor.tgz'
    require(package.is_file() and package.resolve() == package, 'local executor package unavailable or redirected')
    with tarfile.open(package, 'r:gz') as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        require(len(names) == len(set(names)), 'duplicate local executor package member')
        for member in members:
            path = Path(member.name)
            require(not path.is_absolute() and '..' not in path.parts and path.parts and path.parts[0] == 'package'
                    and (member.isfile() or member.isdir()), 'unsafe local executor package member')
        for name in ('package/REVISION','package/package.json','package/dist/cli.js'):
            require(name in names and archive.getmember(name).isfile(), 'local executor package member missing')
        require(archive.extractfile('package/REVISION').read().decode().strip() == sha, 'local executor package revision differs')
        metadata = json.load(archive.extractfile('package/package.json'))
        require(metadata.get('name') == '@lead/local-executor' and metadata.get('bin',{}).get('lead-executor') == './dist/cli.js',
                'local executor package identity differs')


def reviewed_unified_browser(mirror,sha,extra):
    paths=extra.intersection(UNIFIED_BROWSER_FILES)
    if not paths:
        return set()
    require(all(SHA.fullmatch(value) for value in UNIFIED_BROWSER_FILES.values()),'unified-browser release blobs are not frozen')
    for path in paths:
        require(run('git','--git-dir',mirror,'rev-parse',f'{sha}:{path}')==UNIFIED_BROWSER_FILES[path],
                'unified-browser files require compatibility review')
    site=TOOLS/'home_live_maintenance.py'
    require(site.is_file(),'existing node database verifier required')
    shared=shared_cutover_pending(sha)
    query=shared_executor_schema_query() if shared else unified_schema_query()
    program=('import runpy;read=runpy.run_path('+repr(str(site))+')["site_sql"];print(read('+repr(query)+'))')
    require(run('/usr/bin/python3','-IB','-c',program)=='t','unified-browser schema is not ready')
    require(shared or unified_cutover_pending(sha),'unified-browser cutover hold is missing')
    if paths.intersection(LOCAL_EXECUTOR_FILES):
        verify_local_executor_package(sha)
    return paths


def verify_unified_cutover(sha, *, shared=False):
    pending=shared_cutover_pending if shared else unified_cutover_pending
    require(pending(sha),'schema cutover pending marker missing')
    require(current()==sha,'cutover current revision differs')
    intake=run('systemctl','show','lead-home-ingress.socket','-p','ActiveState','--value')
    require(intake in ('active','inactive'),'cutover ingress state unavailable')
    verify(sha,fenced=intake=='inactive',require_idle=False)
    verify_executors(sha)
    site=TOOLS/'home_live_maintenance.py'
    require(site.is_file(),'existing node database verifier required')
    query=shared_executor_schema_query() if shared else unified_schema_query()
    program=('import runpy;read=runpy.run_path('+repr(str(site))+')["site_sql"];print(read('+repr(query)+'))')
    require(run('/usr/bin/python3','-IB','-c',program)=='t','unified-browser schema is not ready')
    verify_local_executor_package(sha)
    require(current()==sha and pending(sha),'cutover changed during verification')


def complete_unified_cutover(sha, *, shared=False):
    pending=shared_cutover_pending if shared else unified_cutover_pending
    prefix='shared-executor' if shared else 'unified-browser'
    helper_name='shared_executor_upgrade.py' if shared else 'unified_browser_upgrade.py'
    files=SHARED_EXECUTOR_FILES if shared else UNIFIED_BROWSER_FILES
    require(pending(sha),'schema cutover hold is missing')
    stage=STORE/('maintenance-'+sha)
    journal=json.loads(executor_host_file(stage/(prefix+'-migration.json'),private=True))
    require(journal.get('sha')==sha and SHA.fullmatch(journal.get('old','')),'unified-browser migration journal differs')
    helper=stage/'source/scripts'/helper_name
    for name in ({helper_name,'unified_browser_upgrade.py'} if shared else {helper_name}):
        path=stage/'source/scripts'/name
        executor_host_file(path)
        expected=files['scripts/'+name]
        require(SHA.fullmatch(expected) and run('git','hash-object','--no-filters',path)==expected,'schema finalizer source differs')
    for channel in ('telegram','reddit'):
        provision_executor(sha,EXECUTOR_REGISTRY.parent/(channel+'-provision.json'))
    register_executors(sha)
    run(EXECUTOR_PYTHON_SOURCE,'-I','-B',helper,'finish','--sha',sha,'--old',journal['old'],timeout=360)
    require(not pending(sha),'schema finalizer did not release its journal')


def complete_shared_cutover(sha):
    complete_unified_cutover(sha,shared=True)


def release_preflight(sha):
    live=current()
    kind=cutover_kind(sha)
    if kind:
        journal=json.loads(executor_host_file(STORE/('maintenance-'+sha)/(kind+'-migration.json'),private=True))
        require(journal.get('sha')==sha and live in (journal.get('old'),sha),'cutover preflight revision differs')
    verify(live,fenced=bool(kind),require_idle=not kind)
    return live


def finalize_cutover(sha):
    kind=cutover_kind(sha)
    if kind:
        require(current()==sha,'application cutover has not completed; inspect original request')
        {'growth':complete_growth_cutover,'unified-browser':complete_unified_cutover,
         'shared-executor':complete_shared_cutover}[kind](sha)


# These site units are installed and verified separately; only these reviewed blobs
# are compatible with the home application's release. Future revisions need review.
REDDIT_SITE_FILES = {
    # Independently installed on Tokyo under PR #19, before the next application release.
    "infra/home-reddit-login/private-listener.conf": "7b4cdf5479577735b99df998cf93fc7a3858d41a",
    "infra/home-reddit-login/browser.conf": "e80d38a2cb8a020280ba6a2f1cc5f350fe358c59",
    "infra/home-reddit-login/lead-reddit-guard.service": "61031ac0195c3abcc0ae38de9a06ecf3cc688f1d",
    "infra/home-reddit-login/lead-reddit-link.service": "cbeb23de298f2da604630ed2746fbe761b823ac6",
    "infra/home-reddit-login/tokyo-guard.nft": "899f0ca6f4945c69e9ca0039c0da7746d155ff4e",
    "infra/home-reddit-login/tokyo-sshd.conf": "2fb5148544188492b8fab3818f315f5b926ddbaf",
}


def reviewed_reddit_site(mirror, sha, extra):
    paths = extra.intersection(REDDIT_SITE_FILES)
    if not paths or not (TOOLS / "home_live_maintenance.py").is_file():
        return set()
    for path in paths:
        require(run("git", "--git-dir", mirror, "rev-parse", f"{sha}:{path}") == REDDIT_SITE_FILES[path],
                "Reddit site files require separate compatibility review")
    for name, installed in (
        ("browser.conf", "/etc/systemd/system/lead-home-browser.service.d/98-reddit-login.conf"),
        ("lead-reddit-link.service", "/etc/systemd/system/lead-reddit-link.service"),
    ):
        require(run("git", "hash-object", "--no-filters", installed) == REDDIT_SITE_FILES["infra/home-reddit-login/" + name],
                "Reddit browser unit differs from the reviewed version")
    require(run("systemctl", "is-active", "lead-reddit-link.service") == "active", "Reddit tunnel unavailable")
    return paths


# The model-route dependency was independently provisioned on the home node.
# These are exact reviewed manifests/tools, never a directory-wide exception.
RUNTIME_COMPATIBILITY_FILES = {
    "scripts/browser_use_runner.py": {'08cd818b33cbb6a484d181a436572fef19d13158'},
    "pyproject.toml": {"46a2c7c01a2866bcebcf9e4b4bb2a6570d80461f"},
    "scripts/verify_account_browser.py": {
        "69daf9b5065322206da080a4c64b66a27d74d179",  # PR #28
        "aa243338dd22dc3becf10eaf0100ecf7858d00b6",  # PR #29
    },
}

# These exact paths are maintained outside the application image. App upgrades
# neither install nor execute their repository copies. Tools have a separate CI
# proof, bundle, explicit install operation, backup and readback.
OPERATOR_ONLY_FILES = {
    "scripts/ci_scope.py", "scripts/ci_gate.py", "scripts/build_tool_bundle.py",
    "scripts/tool_release.py", "scripts/tool_install.py",
    "scripts/request_home_release.py", "scripts/poll_home_release.py",
    "scripts/prepare_home_image.py", "scripts/home_release_operator.py",
    "scripts/home_systemd_release.py",
}

# Local verification sources stay outside tool bundles and application images.
LOCAL_VERIFICATION_FILES = {"scripts/verify_cadence_ui.py","scripts/verify_growth_runtime.py",
    "scripts/evaluate_native_browser.py","scripts/native_eval_seed.py",
    "scripts/verify_growth_ui.py","scripts/verify_connection_ui.py","scripts/verify_growth_executor.py","scripts/evaluate_growth_model.py","scripts/render_operating_playbooks.py",
    "scripts/verify_operating_board_model.cjs","scripts/verify_operating_ui.py"}


def verify_model_runtime():
    # Use the existing, root-provisioned interpreter, no candidate program and no
    # network request or credential environment. Do not install anything here.
    program = (
        "import sys;sys.path.insert(0,'/run/lead-home/bin/site');"
        "import asyncio,importlib.metadata as m,httpx2;"
        "assert m.version('httpx2')=='2.16.0';"
        "transport=httpx2.AsyncHTTPTransport(uds='/run/lead-model-runtime-probe.sock',"
        "verify=True,trust_env=False);"
        "asyncio.run(transport.aclose());print('model-runtime-ready')"
    )
    for role in ("api", "worker", "browser"):
        proc = process(role)
        result = run("nsenter", "-t", proc.name, "-m", "-n", "--", "setpriv",
                     "--reuid=lead-r-app", "--regid=lead-r-app", "--clear-groups",
                     BIN / "py/bin/python3.12", "-I", "-c", program)
        require(result == "model-runtime-ready", "model runtime dependency unavailable")
        require(process(role) == proc, "model runtime changed during compatibility check")


def reviewed_runtime_files(mirror, sha, extra):
    paths = extra.intersection(RUNTIME_COMPATIBILITY_FILES)
    if not paths or not (TOOLS / "home_live_maintenance.py").is_file():
        return set()
    for path in paths:
        require(run("git", "--git-dir", mirror, "rev-parse", f"{sha}:{path}")
                in RUNTIME_COMPATIBILITY_FILES[path], "runtime files require separate compatibility review")
    if "pyproject.toml" in paths:
        verify_model_runtime()
    return paths


def deploy(sha):
    require(os.geteuid() == 0, "trusted node dispatcher required")
    safe_layout()
    require(run("findmnt", "-n", "-M", VAULT, "-o", "TARGET") == str(VAULT), "vault unavailable")
    # Administrative store, including ancestry mirror, images and selector. Never candidate-writable.
    require(STORE.resolve() == STORE and STORE.stat().st_uid == 0 and not STORE.stat().st_mode & 0o022,
            "untrusted release store")
    with (STORE / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = current()
        verify(old,fenced=cutover_pending(sha),require_idle=False)
        if old == sha:
            return
        if not (STORE / sha).is_mount():
            run(TOOLS / "mount-image", sha, timeout=1800)
        image(sha)
        mirror = STORE / "lead.git"
        sync_public_main(mirror, sha)
        run("git", "--git-dir", mirror, "merge-base", "--is-ancestor", old, sha)
        # Reject schema, dependency, service configuration and runtime changes without a separate plan.
        changes = run("git", "--git-dir", mirror, "diff", "--no-renames", "--name-only", "-z", old, sha)
        allowed = ("services/api/lead_app/", "apps/web/app/", "apps/web/public/", "docs/", "tests/", ".github/")
        # This public adapter is application source in the same verified services image.
        # Adding it never installs node configuration or widens the script/infra exceptions.
        allowed_files = {"README.md", "AGENTS.md", "services/channel_adapters/reddit/entry.py",
                         "services/channel_adapters/telegram/entry.py","services/channel_adapters/systemd/executor.conf.in"}
        extra = {name for name in changes.split("\0") if name and not name.startswith(allowed)
                 and name not in allowed_files}
        extra -= OPERATOR_ONLY_FILES
        extra -= LOCAL_VERIFICATION_FILES
        extra -= reviewed_public_reader_files(mirror, sha, extra)
        extra -= reviewed_maintenance_files(mirror, sha, extra)
        extra -= reviewed_channel_schema(mirror, sha, extra)
        extra -= reviewed_shared_executor_proxy(mirror, sha, extra)
        extra -= reviewed_shared_executor(mirror, sha, extra)
        extra -= reviewed_unified_browser(mirror, sha, extra)
        extra -= reviewed_fault_isolation(mirror, sha, extra)
        extra -= reviewed_growth_runtime(mirror,sha,extra)
        extra -= reviewed_reddit_site(mirror, sha, extra)
        extra -= reviewed_runtime_files(mirror, sha, extra)
        extra -= reviewed_executor_image_files(mirror, sha, extra)
        if extra:
            # The frame subsystem was already installed from this reviewed commit.
            # Only accept its EXACT reviewed blobs, and attest the installed components.
            reviewed = "c8013909ab22a0ea3da15b5bfae7c70443fdca6a"
            frame_files = {"infra/home-browser-frames/" + name for name in
                           ("frame-live.cjs", FRAME_SERVICE, "location.conf", "upstream.conf")}
            compatible = frame_files | {"scripts/build_home_image.sh", "scripts/home_systemd_release.py",
                                        "scripts/request_home_release.py"}
            require(extra <= compatible and (STORE / sha / "home-browser-frames").is_dir(),
                    "release requires separate compatibility review")
            run("git", "--git-dir", mirror, "diff", "--exit-code", reviewed, sha, "--", *sorted(extra))
            verify_frames(image(sha), running_web=image(old) / "web")
        if (STORE / sha / PUBLIC_READER_SOURCE_FILES[0]).is_file():
            public_reader_before_switch(sha, old)
        # The node's maintenance fence outlives this process. It must remain held across recovery.
        with maintenance_window(old, sha) as (fence, check):
            run("systemctl", "start", fence)
            run(*check, old, sha)
            # Stop both the listener and existing accepted connections. Health probes use loopback.
            # On interruption the independent guard stays up and intake stays closed.
            run("systemctl", "stop", "lead-home-ingress.socket", "lead-home-ingress.service")
            try:
                verify(old, fenced=True)
                run("systemctl", "start", "lead-pc-local-backup.service", timeout=1800)
                run(*check, old, sha)
                require(current() == old, "baseline changed before activation")
                if (STORE / sha / PUBLIC_READER_SOURCE_FILES[0]).is_file():
                    public_reader_before_switch(sha, old)
                replace_release(old, sha)
            except Exception:
                # Only a positively reverified baseline can take traffic again automatically.
                verify(old, fenced=True)
                run(*check, old, sha)
                run("systemctl", "stop", fence)
                if not (TOOLS / "home_live_maintenance.py").is_file():
                    run("systemctl", "start", "lead-home-ingress.socket")
                raise
            run(*check, old, sha)
            run("systemctl", "stop", fence)
            if not (TOOLS / "home_live_maintenance.py").is_file():
                run("systemctl", "start", "lead-home-ingress.socket")
        verify(sha,fenced=cutover_pending(sha))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("current", "deploy", "verify", "preflight", "finalize-cutover", "verify-cutover", "executors", "provision-executor", "register-executors", "public-reader-plan", "provision-public-reader", "verify-public-reader", "verify-public-reader-prepared", "rollback-public-reader", "public-reader-runtime-plan", "provision-public-reader-runtime", "verify-public-reader-runtime", "rollback-prepared-public-reader", "retire-public-reader"))
    parser.add_argument("sha", nargs="?")
    parser.add_argument("plan", nargs="?")
    args = parser.parse_args()
    if (args.action == "current" and args.sha is not None) or (
        args.action != "current" and not SHA.fullmatch(args.sha or "")
    ):
        parser.error("current takes no arguments; deploy, verify and executors require a full SHA")
    if (args.action in ('provision-executor', 'provision-public-reader', 'provision-public-reader-runtime')) != (args.plan is not None):
        parser.error('only provision operations require a private plan path')
    try:
        if args.action == "current":
            print(current())
        elif args.action == 'preflight':
            print(release_preflight(args.sha))
        elif args.action == 'finalize-cutover':
            finalize_cutover(args.sha)
        elif args.action == "verify-cutover":
            verify_unified_cutover(args.sha,shared=cutover_kind(args.sha)=='shared-executor')
        elif args.action == "verify":
            verify(args.sha)
            # Final release acceptance covers provisioned independent consumers.
            # A dangling or redirected registry must fail, never disable acceptance.
            if os.path.lexists(EXECUTOR_REGISTRY):
                verify_executors(args.sha)
        elif args.action == 'register-executors':
            print(json.dumps(register_executors(args.sha), sort_keys=True))
        elif args.action == 'provision-executor':
            print(json.dumps(provision_executor(args.sha, args.plan), sort_keys=True))
        elif args.action == "executors":
            print(json.dumps(verify_executors(args.sha), sort_keys=True))
        elif args.action == 'public-reader-plan':
            print(json.dumps(public_reader_plan(args.sha), sort_keys=True))
        elif args.action == 'provision-public-reader':
            print(json.dumps(provision_public_reader(args.sha, args.plan), sort_keys=True))
        elif args.action == 'verify-public-reader':
            print(json.dumps(verify_public_reader_application(args.sha), sort_keys=True))
        elif args.action == 'verify-public-reader-prepared':
            print(json.dumps(verify_public_reader_prepared(args.sha), sort_keys=True))
        elif args.action == 'rollback-public-reader':
            print(json.dumps(rollback_public_reader(args.sha), sort_keys=True))
        elif args.action == 'public-reader-runtime-plan':
            print(json.dumps(public_reader_runtime_plan(args.sha), sort_keys=True))
        elif args.action == 'provision-public-reader-runtime':
            print(json.dumps(provision_public_reader_runtime(args.sha, args.plan), sort_keys=True))
        elif args.action == 'verify-public-reader-runtime':
            print(json.dumps(verify_public_reader_runtime(args.sha), sort_keys=True))
        elif args.action == 'rollback-prepared-public-reader':
            print(json.dumps(rollback_prepared_public_reader(args.sha), sort_keys=True))
        elif args.action == 'retire-public-reader':
            print(json.dumps(retire_public_reader(args.sha), sort_keys=True))
        else:
            deploy(args.sha)
            kind=cutover_kind(args.sha)
            if kind:
                {'growth':complete_growth_cutover,'unified-browser':complete_unified_cutover,
                 'shared-executor':complete_shared_cutover}[kind](args.sha)
    except Refused as error:
        sys.exit(str(error))
    except (OSError, ValueError, subprocess.SubprocessError):
        sys.exit("node evidence or operation unavailable; inspect locally")


if __name__ == "__main__":
    main()

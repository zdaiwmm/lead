#!/usr/bin/python3 -IB
"""Trusted, node-local hook for poll_home_release.py. Install outside release images."""

import argparse
import base64
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
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


def unit(role):
    return role if role.endswith(".service") else f"lead-home-{role}.service"


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
        require(path.name <= dropin.name, 'later executor dropin would override provisioning')
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
        run('git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null', '--git-dir', mirror,
            'fetch', '--no-tags', 'https://github.com/zdaiwmm/lead.git',
            '+refs/heads/main:refs/heads/main', timeout=120)
        run('git', '--git-dir', mirror, 'merge-base', '--is-ancestor', sha, 'refs/heads/main')
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
        select(old)
        restart_and_verify(old)
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


def cutover_kind(sha):
    growth=growth_cutover_pending(sha)
    unified=unified_cutover_pending(sha)
    require(not (growth and unified),'ambiguous cutover journals')
    return 'growth' if growth else 'unified-browser' if unified else None


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
            (ARRAY['0030_fault_isolation'],ARRAY['0031_growth_runtime'],ARRAY['0033_unified_browser'])
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
    return """SELECT (SELECT array_agg(version_num::text) FROM public.lead_migration_version) IN (ARRAY['0031_growth_runtime'],ARRAY['0033_unified_browser'])
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
    'scripts/unified_browser_upgrade.py': '634006d550e09babc44d529126bbe5933800431e',
    'infra/migrations/versions/0032_research_memory.py': '6f1423fefdd2fa3aba498b37d9f7fad9159ced65',
    'infra/migrations/sql/0032_research_memory.sql': '1cd03ca94dae3bdd3bcd11124c92bd224a9c6e15',
    'infra/migrations/versions/0033_unified_browser.py': '8f75f097ad91baac8295537e6d5a52b49b503693',
    'infra/migrations/sql/0033_unified_browser.sql': '2328c5a253438f1b157d24cf71f81b18075485b8',
    'scripts/browser_namespace.py': '7ecb47f233761b910317126d9d15e8edfb9fe21b',
    'scripts/browser_use_runner.py': 'bb4a76b6d101d6291c3af03b35c2f443b3a8495b',
    'scripts/build_home_image.sh': '45e639192d96135a2f8eaf38077670740a29e0a7',
    'scripts/build_browser_runtime.py': 'cbc4d3d3521a2fe50c589813cf0d3956289ca94a',
    'requirements.browser-use.lock.txt': '9105c7748d6aa830f312114e4f9d3e1431e4f911',
    'requirements.lock.txt': 'dcf3273dc078a66007e8db836ecefa2518834bbf',
}


def unified_schema_query():
    return "SELECT ("+growth_schema_query().removeprefix("SELECT ")+""" )
        AND (SELECT array_agg(version_num::text) FROM public.lead_migration_version)=ARRAY['0033_unified_browser']
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
    program=('import runpy;read=runpy.run_path('+repr(str(site))+')["site_sql"];print(read('+repr(unified_schema_query())+'))')
    require(run('/usr/bin/python3','-IB','-c',program)=='t','unified-browser schema is not ready')
    require(unified_cutover_pending(sha),'unified-browser cutover hold is missing')
    return paths


def verify_unified_cutover(sha):
    require(unified_cutover_pending(sha),'unified-browser cutover pending marker missing')
    require(current()==sha,'cutover current revision differs')
    intake=run('systemctl','show','lead-home-ingress.socket','-p','ActiveState','--value')
    require(intake in ('active','inactive'),'cutover ingress state unavailable')
    verify(sha,fenced=intake=='inactive',require_idle=False)
    verify_executors(sha)
    site=TOOLS/'home_live_maintenance.py'
    require(site.is_file(),'existing node database verifier required')
    program=('import runpy;read=runpy.run_path('+repr(str(site))+')["site_sql"];print(read('+repr(unified_schema_query())+'))')
    require(run('/usr/bin/python3','-IB','-c',program)=='t','unified-browser schema is not ready')
    require(current()==sha and unified_cutover_pending(sha),'cutover changed during verification')


def complete_unified_cutover(sha):
    require(unified_cutover_pending(sha),'unified-browser cutover hold is missing')
    stage=STORE/('maintenance-'+sha)
    journal=json.loads(executor_host_file(stage/'unified-browser-migration.json',private=True))
    require(journal.get('sha')==sha and SHA.fullmatch(journal.get('old','')),'unified-browser migration journal differs')
    helper=stage/'source/scripts/unified_browser_upgrade.py'
    executor_host_file(helper)
    expected=UNIFIED_BROWSER_FILES['scripts/unified_browser_upgrade.py']
    require(SHA.fullmatch(expected) and run('git','hash-object','--no-filters',helper)==expected,'unified-browser finalizer source differs')
    for channel in ('telegram','reddit'):
        provision_executor(sha,EXECUTOR_REGISTRY.parent/(channel+'-provision.json'))
    register_executors(sha)
    run(EXECUTOR_PYTHON_SOURCE,'-I','-B',helper,'finish','--sha',sha,'--old',journal['old'],timeout=360)
    require(not unified_cutover_pending(sha),'unified-browser finalizer did not release its journal')


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
        (complete_growth_cutover if kind=='growth' else complete_unified_cutover)(sha)


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
        run("git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", "--git-dir", mirror,
            "fetch", "--no-tags", "https://github.com/zdaiwmm/lead.git",
            "+refs/heads/main:refs/heads/main", timeout=120)
        run("git", "--git-dir", mirror, "merge-base", "--is-ancestor", old, sha)
        run("git", "--git-dir", mirror, "merge-base", "--is-ancestor", sha, "refs/heads/main")
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
        extra -= reviewed_maintenance_files(mirror, sha, extra)
        extra -= reviewed_channel_schema(mirror, sha, extra)
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
    parser.add_argument("action", choices=("current", "deploy", "verify", "preflight", "finalize-cutover", "verify-cutover", "executors", "provision-executor", "register-executors"))
    parser.add_argument("sha", nargs="?")
    parser.add_argument("plan", nargs="?")
    args = parser.parse_args()
    if (args.action == "current" and args.sha is not None) or (
        args.action != "current" and not SHA.fullmatch(args.sha or "")
    ):
        parser.error("current takes no arguments; deploy, verify and executors require a full SHA")
    if (args.action == 'provision-executor') != (args.plan is not None):
        parser.error('only provision-executor requires a private plan path')
    try:
        if args.action == "current":
            print(current())
        elif args.action == 'preflight':
            print(release_preflight(args.sha))
        elif args.action == 'finalize-cutover':
            finalize_cutover(args.sha)
        elif args.action == "verify-cutover":
            verify_unified_cutover(args.sha)
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
        else:
            deploy(args.sha)
            kind=cutover_kind(args.sha)
            if kind:
                (complete_growth_cutover if kind=='growth' else complete_unified_cutover)(args.sha)
    except Refused as error:
        sys.exit(str(error))
    except (OSError, ValueError, subprocess.SubprocessError):
        sys.exit("node evidence or operation unavailable; inspect locally")


if __name__ == "__main__":
    main()

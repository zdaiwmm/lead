"""Independent executor configuration and boot evidence shared by all channels.

Operator-owned bindings contain deployment identity, never a social account ID.
The adapter resolves the current account from its profile before each task. Code
is mounted read-only by the host; a receipt alone is not a mount attestation.
"""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class Binding:
    version: int
    channel: str
    executor_id: str
    agent_id: str
    profile_slot: str
    release: str
    adapter_root: str
    entrypoint: str
    adapter_files: dict[str, str]
    receipt: str

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError('invalid executor binding fields')
        if type(data['version']) is not int or data['version'] != 1:
            raise ValueError('unsupported executor binding version')
        if data['channel'] not in ('telegram', 'reddit'):
            raise ValueError('unsupported executor channel')
        for key in ('executor_id', 'profile_slot'):
            if not isinstance(data[key], str) or not re.fullmatch(r'[a-z0-9][a-z0-9:_-]{0,95}', data[key]):
                raise ValueError('invalid executor ' + key)
        if not isinstance(data['agent_id'], str) or str(UUID(data['agent_id'])) != data['agent_id']:
            raise ValueError('invalid executor agent')
        if not isinstance(data['entrypoint'], str) or not re.fullmatch(r'[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*:[a-z_][a-z0-9_]*', data['entrypoint']):
            raise ValueError('invalid adapter entrypoint')
        for key in ('release', 'adapter_root', 'receipt'):
            if not isinstance(data[key], str) or not Path(data[key]).is_absolute() or '..' in Path(data[key]).parts:
                raise ValueError('invalid executor ' + key)
        files = data['adapter_files']
        if not isinstance(files, dict) or not files:
            raise ValueError('missing adapter files')
        for name, digest in files.items():
            if (not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_./-]+\.py', name)
                    or Path(name).is_absolute() or any(p in ('.', '..') for p in name.split('/'))
                    or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)):
                raise ValueError('invalid adapter file')
        entry = data['entrypoint'].split(':')[0].replace('.', '/') + '.py'
        if entry not in files:
            raise ValueError('missing adapter entrypoint file')
        return cls(**{**data, 'adapter_files': dict(files)})

    def as_dict(self):
        return asdict(self)


def binding_digest(binding):
    return hashlib.sha256(json.dumps(binding.as_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def source_hashes(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('missing or redirected source tree')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('source symlink is not permitted')
        if path.suffix == '.py':
            if not path.is_file():
                raise ValueError('invalid source file')
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise ValueError('empty source tree')
    return result


def snapshot(binding, *, invocation, pid):
    if not isinstance(invocation, str) or not re.fullmatch(r'[a-f0-9]{32}', invocation):
        raise ValueError('missing systemd invocation')
    if type(pid) is not int or pid < 2:
        raise ValueError('invalid executor process')
    release = Path(binding.release)
    revision = (release / 'REVISION').read_text().strip()
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('invalid release revision')
    adapters = source_hashes(binding.adapter_root)
    supplied = {str(p.relative_to(binding.adapter_root)) for p in Path(binding.adapter_root).rglob('*') if p.is_file()}
    if adapters != binding.adapter_files or supplied != set(binding.adapter_files):
        raise ValueError('adapter source drift')
    return {
        'version': 1, 'channel': binding.channel, 'executor_id': binding.executor_id,
        'revision': revision, 'pid': pid, 'invocation': invocation,
        'boot_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'binding_sha256': binding_digest(binding),
        'core_files': source_hashes(release / 'services/api/lead_app'),
        'adapter_files': adapters,
    }


def write_receipt(binding, proof):
    target = Path(binding.receipt)
    # The host provisions the private writable directory; do not create new paths.
    fd, name = tempfile.mkstemp(prefix='.executor-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(proof, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_receipt(binding, proof, *, invocation, pid):
    if not isinstance(proof, dict) or proof.get('invocation') != invocation:
        raise ValueError('executor invocation changed')
    if proof.get('pid') != pid:
        raise ValueError('executor process changed')
    if proof != snapshot(binding, invocation=invocation, pid=pid):
        raise ValueError('executor source or binding changed after boot')
    return proof


def verify_imports(binding, modules):
    core = (Path(binding.release) / 'services/api/lead_app').resolve(strict=True)
    for name, module in list(modules.items()):
        if name != 'lead_app' and not name.startswith('lead_app.'):
            continue
        source = getattr(module, '__file__', None)
        if source is None:
            raise ValueError('common module has no source: ' + name)
        path = Path(source).resolve(strict=True)
        if not path.is_relative_to(core) or path.suffix != '.py':
            raise ValueError('common module outside release: ' + name)


def load_entrypoint(binding):
    import importlib
    import sys
    import types

    namespace = 'lead_channel_adapter'
    if namespace in sys.modules:
        raise ValueError('adapter already initialized in this process')
    package = types.ModuleType(namespace)
    package.__path__ = [binding.adapter_root]
    package.__package__ = namespace
    sys.modules[namespace] = package
    module_name, function = binding.entrypoint.split(':')
    try:
        module = importlib.import_module(namespace + '.' + module_name)
        if getattr(module, 'CHANNEL', None) != binding.channel:
            raise ValueError('adapter channel does not match binding')
        entry = getattr(module, function)
        import inspect
        if not inspect.iscoroutinefunction(entry):
            raise ValueError('adapter entrypoint must be asynchronous')
        return entry
    except BaseException:
        for key in list(sys.modules):
            if key == namespace or key.startswith(namespace + '.'):
                del sys.modules[key]
        raise


def main(config_path):
    import asyncio
    import sys

    binding = Binding.parse(json.loads(Path(config_path).read_text()))
    invocation = os.environ.get('INVOCATION_ID', '')
    before = snapshot(binding, invocation=invocation, pid=os.getpid())
    # Called by absolute filename with -I -B. Only the selected immutable core
    # is added; the adapter receives its own namespace, never a PYTHONPATH overlay.
    # Existing home nodes keep their verified, read-only Python dependencies
    # outside the application image. -I deliberately ignores PYTHONPATH.
    dependencies = Path('/run/lead-home/bin/site')
    if dependencies.is_dir():
        sys.path.append(str(dependencies))
    sys.path.insert(0, str(Path(binding.release) / 'services/api'))
    os.environ['LEAD_EXECUTOR_BINDING'] = str(Path(config_path).resolve(strict=True))
    entry = load_entrypoint(binding)
    verify_imports(binding, sys.modules)
    after = snapshot(binding, invocation=invocation, pid=os.getpid())
    if before != after:
        raise ValueError('executor source changed during initialization')
    write_receipt(binding, after)
    print(json.dumps({'event': 'executor_boot', 'executor_id': binding.executor_id,
                      'revision': after['revision'], 'binding_sha256': after['binding_sha256'],
                      'invocation': invocation}), flush=True)
    asyncio.run(entry(binding))


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        raise SystemExit('usage: channel_executor.py /absolute/operator-binding.json')
    main(sys.argv[1])

"""An executor receipt proves a particular boot, not a selector or stale file."""
import hashlib
import json
from pathlib import Path

import pytest
from lead_app import channel_executor as runtime


@pytest.fixture(autouse=True)
def clean_adapter_namespace():
    import sys
    yield
    for name in list(sys.modules):
        if name == 'lead_channel_adapter' or name.startswith('lead_channel_adapter.'):
            del sys.modules[name]


SHA = 'a' * 40


def bundle(tmp_path):
    release = tmp_path / SHA
    core = release / 'services/api/lead_app'
    core.mkdir(parents=True)
    (release / 'REVISION').write_text(SHA + '\n')
    (core / 'browser.py').write_text('VERSION = 1\n')
    adapter = tmp_path / 'adapter'
    adapter.mkdir()
    (adapter / 'entry.py').write_text("CHANNEL = 'telegram'\nasync def run(binding):\n    return binding.channel\n")
    config = {
        'version': 1, 'channel': 'telegram', 'executor_id': 'telegram-primary',
        'agent_id': '00000000-0000-0000-0000-000000000001', 'profile_slot': 'default',
        'release': str(release), 'adapter_root': str(adapter), 'entrypoint': 'entry:run',
        'adapter_files': {'entry.py': hashlib.sha256((adapter / 'entry.py').read_bytes()).hexdigest()},
        'receipt': str(tmp_path / 'receipt.json'),
    }
    return config, core, adapter


def test_independent_binding_and_account_configuration(tmp_path):
    config, _, _ = bundle(tmp_path)
    binding = runtime.Binding.parse(config)
    assert binding.channel == 'telegram'
    assert binding.profile_slot == 'default'
    assert 'external_account_id' not in binding.as_dict()
    other = {**config, 'channel': 'reddit', 'executor_id': 'reddit-primary', 'profile_slot': 'reddit:new'}
    assert runtime.Binding.parse(other).executor_id != binding.executor_id
    assert runtime.Binding.parse(other).profile_slot != binding.profile_slot


@pytest.mark.parametrize('change', [
    {'channel': 'unknown'}, {'version': 2}, {'agent_id': 'not-a-uuid'},
    {'entrypoint': 'entry:run()'}, {'profile_slot': '../other'},
    {'adapter_files': {'../entry.py': 'a'*64}},
    {'adapter_files': {'entry.py': 'invalid'}}, {'external_account_id': 'secret'},
])
def test_invalid_binding_fails_closed(tmp_path, change):
    config, _, _ = bundle(tmp_path)
    with pytest.raises(ValueError):
        runtime.Binding.parse({**config, **change})


def test_receipt_binds_core_adapter_and_process_invocation(tmp_path):
    config, _, _ = bundle(tmp_path)
    binding = runtime.Binding.parse(config)
    proof = runtime.snapshot(binding, invocation='b'*32, pid=123)
    assert proof['revision'] == SHA
    assert proof['core_files']['browser.py'] == hashlib.sha256(b'VERSION = 1\n').hexdigest()
    assert proof['adapter_files'] == config['adapter_files']
    assert proof['invocation'] == 'b'*32 and proof['pid'] == 123
    assert 'agent_id' not in proof
    assert 'profile_slot' not in proof
    assert proof['binding_sha256'] == runtime.binding_digest(binding)
    runtime.write_receipt(binding, proof)
    assert json.loads(Path(config['receipt']).read_text()) == proof
    assert Path(config['receipt']).stat().st_mode & 0o777 == 0o600
    runtime.verify_receipt(binding, proof, invocation='b'*32, pid=123)
    with pytest.raises(ValueError, match='invocation'):
        runtime.verify_receipt(binding, proof, invocation='c'*32, pid=123)
    with pytest.raises(ValueError, match='process'):
        runtime.verify_receipt(binding, proof, invocation='b'*32, pid=124)


@pytest.mark.parametrize('component', ['core', 'adapter', 'binding'])
def test_drift_does_not_reuse_previous_boot_proof(tmp_path, component):
    config, core, adapter = bundle(tmp_path)
    binding = runtime.Binding.parse(config)
    proof = runtime.snapshot(binding, invocation='b'*32, pid=123)
    if component == 'core':
        (core / 'browser.py').write_text('VERSION = 2\n')
    elif component == 'adapter':
        (adapter / 'entry.py').write_text('async def run(binding): pass\n')
    else:
        binding = runtime.Binding.parse({**config, 'profile_slot': 'new-slot'})
    with pytest.raises(ValueError):
        runtime.verify_receipt(binding, proof, invocation='b'*32, pid=123)


def test_unlisted_adapter_code_or_symlink_is_rejected(tmp_path):
    config, _, adapter = bundle(tmp_path)
    binding = runtime.Binding.parse(config)
    extra = adapter / 'hidden.py'
    extra.write_text('pass\n')
    with pytest.raises(ValueError, match='adapter'):
        runtime.snapshot(binding, invocation='b'*32, pid=123)
    extra.unlink()
    outside = tmp_path / 'outside.py'
    outside.write_text('pass\n')
    extra.symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        runtime.snapshot(binding, invocation='b'*32, pid=123)


def test_imported_common_module_cannot_come_from_old_private_copy(tmp_path):
    import types
    config, core, adapter = bundle(tmp_path)
    binding = runtime.Binding.parse(config)
    good = types.ModuleType('lead_app.browser')
    good.__file__ = str(core / 'browser.py')
    runtime.verify_imports(binding, {'lead_app.browser': good})
    (adapter / 'browser.py').write_text('VERSION = 0\n')
    bad = types.ModuleType('lead_app.browser')
    bad.__file__ = str(adapter / 'browser.py')
    with pytest.raises(ValueError, match='outside release'):
        runtime.verify_imports(binding, {'lead_app.browser': bad})


def test_adapter_entry_cannot_replace_common_namespace(tmp_path):
    config, _, adapter = bundle(tmp_path)
    # Even an otherwise valid adapter must live in a distinct module namespace.
    (adapter / 'entry.py').write_text("CHANNEL = 'telegram'\nasync def run(binding):\n    return binding.channel\n")
    config['adapter_files']['entry.py'] = hashlib.sha256((adapter / 'entry.py').read_bytes()).hexdigest()
    binding = runtime.Binding.parse(config)
    entry = runtime.load_entrypoint(binding)
    assert entry.__module__ == 'lead_channel_adapter.entry'
    assert entry.__name__ == 'run'


def test_native_adapter_payload_is_not_hidden_from_manifest(tmp_path):
    config, _, adapter = bundle(tmp_path)
    (adapter / 'unlisted.so').write_bytes(b'untrusted native payload')
    with pytest.raises(ValueError, match='adapter'):
        runtime.snapshot(runtime.Binding.parse(config), invocation='b'*32, pid=123)


def test_adapter_cannot_claim_another_channel(tmp_path):
    config, _, _ = bundle(tmp_path)
    binding = runtime.Binding.parse({**config, 'channel': 'reddit'})
    with pytest.raises(ValueError, match='channel'):
        runtime.load_entrypoint(binding)

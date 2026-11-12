"""Exact-source, ABI and integrity contracts for the isolated SDK artifact."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_browser_runtime.py'
spec = importlib.util.spec_from_file_location('browser_runtime_builder', SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_locked_linux_dependencies_preserve_sdk_and_playwright_versions():
    root = SCRIPT.parents[1]
    packages = builder.linux_requirements((root/'requirements.browser-use.lock.txt').read_text(), (root/'requirements.lock.txt').read_text())
    assert packages['browser-use'] == '0.13.14'
    assert packages['cdp-use'] == '1.4.5'
    assert packages['websockets'] == '15.0.1'
    assert packages['playwright'] == '1.62.0'
    assert packages['greenlet'] == '3.5.6'
    assert packages['pyee'] == '13.0.1'
    assert 'pyobjc-core' not in packages


@pytest.mark.parametrize('line', ['browser-use>=0.13', '-r another.lock', 'browser-use @ https://example.com/pkg', 'bad==1; python_version > "3"'])
def test_unpinned_or_unreviewed_lock_syntax_is_rejected(line):
    with pytest.raises(ValueError):
        builder.parse_lock(line)


def test_manifest_inventory_rejects_symlinks_and_detects_changed_bytes(tmp_path):
    (tmp_path/'site-packages').mkdir()
    module = tmp_path/'site-packages/library.py'
    module.write_text('original\n')
    original = builder.inventory(tmp_path)
    module.write_text('changed\n')
    assert builder.inventory(tmp_path) != original
    module.unlink()
    module.symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='Regular'):
        builder.inventory(tmp_path)


def test_wrong_runtime_abi_and_platform_refused():
    with pytest.raises(ValueError, match='Python 3.12'):
        builder.require_build_runtime((3, 14), 'Linux', 'x86_64')
    with pytest.raises(ValueError, match='Linux x86_64'):
        builder.require_build_runtime((3, 12), 'Darwin', 'arm64')
    builder.require_build_runtime((3, 12), 'Linux', 'x86_64')


@pytest.fixture
def runtime_fixture(tmp_path):
    import subprocess
    root = tmp_path/'checkout'
    root.mkdir()
    for name in builder.SOURCE_FILES:
        target = root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SCRIPT.parents[1]/name).read_bytes())
    subprocess.run(['git','init','-q',str(root)], check=True)
    subprocess.run(['git','add','.'], cwd=root, check=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.test',
                    '-c','commit.gpgsign=false','commit','-qm','fixture'], cwd=root, check=True)
    sha = builder.git(root,'rev-parse','HEAD').decode().strip()
    folder = tmp_path/'runtime'
    (folder/'site-packages').mkdir(parents=True)
    (folder/'site-packages/synthetic.py').write_text('fixture only\n')
    executable = folder/builder.CHROME
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    original = builder.sources(root,sha)
    for name in builder.SOURCE_FILES[2:]:
        (folder/Path(name).name).write_bytes(original[name])
    packages = builder.linux_requirements(*(original[name].decode() for name in builder.SOURCE_FILES[:2]))
    builder.write_manifest(folder,sha,original,packages,
        {'python':{'required_minor':'3.12','cache_tag':'cpython-312'},
         'chromium_version':'fixture','native_smoke':True},'fixture')
    return root,sha,folder


def test_verifier_rejects_byte_changes_after_native_receipt(runtime_fixture):
    root,sha,folder = runtime_fixture
    builder.verify(root,sha,folder)
    (folder/'site-packages/synthetic.py').write_text('changed after test\n')
    with pytest.raises(ValueError, match='digest mismatch'):
        builder.verify(root,sha,folder)


def test_verifier_rejects_helper_not_from_exact_sha_even_with_new_inventory(runtime_fixture):
    import json
    root,sha,folder = runtime_fixture
    (folder/'browser_namespace.py').write_text('other source\n')
    manifest = json.loads((folder/'manifest.json').read_text())
    manifest['files'] = builder.inventory(folder)
    (folder/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='sole Git source'):
        builder.verify(root,sha,folder)


def test_verifier_rejects_another_abi_or_sha(runtime_fixture):
    import json
    root,sha,folder = runtime_fixture
    path = folder/'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['python']['cache_tag'] = 'cpython-314'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='ABI'):
        builder.verify(root,sha,folder)
    manifest['python']['cache_tag'] = 'cpython-312'
    manifest['sha'] = 'a'*40
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='SHA'):
        builder.verify(root,sha,folder)

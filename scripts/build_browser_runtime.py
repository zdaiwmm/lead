"""Build once in Python 3.12 Linux CI, then verify and copy the same SDK payload.

No application interpreter or deployment environment is modified. SDK dependencies
are a closed, pinned target directory. Helpers come only from the exact Git SHA.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_FILES = ('requirements.browser-use.lock.txt', 'requirements.lock.txt',
                'scripts/browser_namespace.py', 'scripts/browser_use_runner.py')
CRITICAL = {'browser-use': '0.13.14', 'cdp-use': '1.4.5', 'websockets': '15.0.1', 'playwright': '1.62.0'}
CHROME = 'chromium/chrome-linux64/chrome'


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def parse_lock(text):
    packages = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'''([\w.-]+)==([\w.+!*-]+)(?:\s*;\s*sys_platform\s*==\s*["'](darwin|linux)["'])?''', line)
        if not match or '*' in match[2]:
            raise ValueError('Only exact pins and reviewed platform markers are supported')
        if match[3] == 'darwin':
            continue
        name = normalize(match[1])
        if name in packages and packages[name] != match[2]:
            raise ValueError('Conflicting locked versions')
        packages[name] = match[2]
    return packages


def linux_requirements(browser_lock, application_lock):
    packages = parse_lock(browser_lock)
    application = parse_lock(application_lock)
    for name in ('playwright', 'greenlet', 'pyee'):
        if name in packages and packages[name] != application[name]:
            raise ValueError('Conflicting Playwright runtime versions')
        packages[name] = application[name]
    if any(packages.get(name) != version for name, version in CRITICAL.items()):
        raise ValueError('Reviewed SDK/browser versions must not change implicitly')
    return packages


def require_build_runtime(version=None, system=None, machine=None):
    if tuple(version or sys.version_info[:2]) != (3, 12):
        raise ValueError('Browser runtime must be built with Python 3.12')
    if (system or platform.system()) != 'Linux' or (machine or platform.machine()) != 'x86_64':
        raise ValueError('Browser runtime requires Linux x86_64')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def sources(root, sha):
    if not re.fullmatch('[a-f0-9]{40}', sha) or git(root, 'rev-parse', 'HEAD').decode().strip() != sha:
        raise ValueError('Exact checked-out commit required')
    subprocess.run(['git', '-C', str(root), 'diff', '--quiet', sha, '--', *SOURCE_FILES, 'services/api'], check=True)
    result = {}
    for name in SOURCE_FILES:
        entry = git(root, 'ls-tree', sha, '--', name).decode().split()
        if not entry or entry[0] not in ('100644', '100755'):
            raise ValueError('Regular exact Git sources required')
        result[name] = git(root, 'show', f'{sha}:{name}')
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(folder):
    entries = []
    for path in sorted(folder.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('Regular runtime files required; symlinks are not accepted')
        relative = path.relative_to(folder).as_posix()
        if relative == 'manifest.json':
            continue
        entries.append({'path': relative, 'sha256': file_digest(path), 'size': path.stat().st_size,
                        'mode': stat.S_IMODE(mode)})
    return entries


def isolated_env(folder, application=None):
    values = {key:value for key,value in os.environ.items() if not key.startswith(('PYTHON', 'LEAD_', 'BROWSER_USE_'))
              and not key.endswith(('_API_KEY', '_API_TOKEN'))}
    values.update(PYTHONPATH=os.pathsep.join((str(folder/'site-packages'), str(application or folder.parent/'api'))), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                  ANONYMIZED_TELEMETRY='false', BROWSER_USE_DISABLE_TELEMETRY='true')
    return values


SMOKE = r'''
import asyncio, importlib.metadata, json, platform, runpy, sys, sysconfig
from pathlib import Path
from browser_use import Agent, Tools
from cdp_use import CDPClient
from websockets.asyncio.connection import Connection
from playwright.async_api import async_playwright
root = Path(sys.argv[1])
for name in ('browser_namespace.py', 'browser_use_runner.py'):
    runpy.run_path(str(root/name), run_name='lead_runtime_smoke')
async def main():
    async with async_playwright() as browser_tools:
        browser = await browser_tools.chromium.launch(executable_path=str(root/'chromium/chrome-linux64/chrome'), headless=True)
        try:
            page = await browser.new_page()
            await page.goto('data:text/html,<title>Lead runtime smoke</title><p>fixture only</p>')
            assert await page.title() == 'Lead runtime smoke'
            version = browser.version
        finally:
            await browser.close()
    print(json.dumps({'python':{'version':platform.python_version(), 'implementation':platform.python_implementation(),
        'cache_tag':sys.implementation.cache_tag, 'soabi':sysconfig.get_config_var('SOABI'), 'required_minor':'3.12', 'minimum_runtime':'3.12.3'},
        'chromium_version':version, 'native_smoke':True}))
asyncio.run(main())
'''


def smoke(folder, application=None):
    require_build_runtime()
    with tempfile.TemporaryDirectory(prefix='lead-sdk-smoke-') as private_home:
        values = isolated_env(folder, application)
        values.update(HOME=private_home, XDG_CACHE_HOME=str(Path(private_home)/'cache'),
                      XDG_CONFIG_HOME=str(Path(private_home)/'config'), BROWSER_USE_CLOUD_SYNC='false')
        result = subprocess.run([sys.executable, '-B', '-S', '-c', SMOKE, str(folder)],
                                env=values, text=True, capture_output=True, check=True, timeout=90)
    # SDK startup diagnostics may precede the single machine-readable receipt.
    return json.loads(result.stdout.strip().splitlines()[-1])


def write_manifest(folder, sha, source_bytes, packages, receipt, chromium_revision):
    manifest = {'schema':1, 'sha':sha, 'python':receipt['python'], 'dependencies':packages,
        'chromium':{'version':receipt['chromium_version'], 'revision':chromium_revision, 'executable':CHROME},
        'native_smoke':receipt['native_smoke'], 'source_sha256':{name:digest(data) for name,data in source_bytes.items()},
        'files':inventory(folder)}
    (folder/'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2)+'\n')
    (folder/'manifest.json').chmod(0o644)
    return manifest


def verify(root, sha, folder):
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('A regular runtime directory is required')
    source_bytes = sources(root, sha)
    if (folder/'manifest.json').is_symlink():
        raise ValueError('Regular manifest required')
    manifest = json.loads((folder/'manifest.json').read_text())
    packages = linux_requirements(*(source_bytes[name].decode() for name in SOURCE_FILES[:2]))
    if (manifest.get('schema') != 1 or manifest.get('sha') != sha or manifest.get('native_smoke') is not True
            or manifest.get('dependencies') != packages or manifest.get('python',{}).get('required_minor') != '3.12'
            or manifest.get('python',{}).get('cache_tag') != 'cpython-312'
            or manifest.get('chromium',{}).get('executable') != CHROME):
        raise ValueError('Runtime SHA, ABI, locked dependencies or native verification mismatch')
    if manifest.get('source_sha256') != {name:digest(data) for name,data in source_bytes.items()}:
        raise ValueError('Runtime sources do not match the exact Git SHA')
    for name in SOURCE_FILES[2:]:
        if (folder/Path(name).name).read_bytes() != source_bytes[name]:
            raise ValueError('Runtime helper differs from the sole Git source')
    if not (folder/CHROME).is_file() or not os.access(folder/CHROME, os.X_OK):
        raise ValueError('Executable Chromium payload missing')
    if manifest.get('files') != inventory(folder):
        raise ValueError('Runtime inventory or file digest mismatch')
    return manifest


def build(root, sha, output, with_system_deps=False):
    require_build_runtime()
    if output.exists() or output.is_symlink():
        raise ValueError('A new runtime output directory is required')
    source_bytes = sources(root, sha)
    packages = linux_requirements(*(source_bytes[name].decode() for name in SOURCE_FILES[:2]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lead-sdk-build-', dir=output.parent) as temp:
        work = Path(temp)
        payload = work/'payload'
        payload.mkdir(mode=0o755)
        requirements = work/'runtime.lock'
        requirements.write_text(''.join(f'{name}=={version}\n' for name,version in sorted(packages.items())))
        subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
            '--only-binary=:all:', '--python-version', '3.12.3', '--no-compile', '--target', str(payload/'site-packages'),
            '--report', str(work/'install.json'), '--timeout', '30', '--retries', '2', '-r', str(requirements)], check=True)
        installed = {normalize(dist.metadata['Name']):dist.version for dist in importlib.metadata.distributions(path=[str(payload/'site-packages')])}
        if installed != packages:
            raise ValueError('Dependency resolution is not the complete reviewed lock')
        values = isolated_env(payload)
        values['PLAYWRIGHT_BROWSERS_PATH'] = str(work/'browsers')
        command = [sys.executable, '-B', '-S', '-m', 'playwright', 'install', '--no-shell']
        if with_system_deps:
            command.append('--with-deps')
        subprocess.run([*command, 'chromium'], env=values, check=True, timeout=600)
        script = 'from playwright.sync_api import sync_playwright\nwith sync_playwright() as p: print(p.chromium.executable_path)'
        executable = Path(subprocess.check_output([sys.executable, '-B', '-S', '-c', script], env=values, text=True).strip())
        if executable.name != 'chrome' or executable.parent.name != 'chrome-linux64' or not executable.is_relative_to(work/'browsers'):
            raise ValueError('Unexpected fixed Playwright Chromium layout')
        shutil.copytree(executable.parent, payload/'chromium/chrome-linux64')
        for name in SOURCE_FILES[2:]:
            target = payload/Path(name).name
            target.write_bytes(source_bytes[name])
            target.chmod(0o755)
        # Drop host umask/private or setuid bits; the immutable payload must be
        # readable by existing unprivileged browser/executor service identities.
        for path in payload.rglob('*'):
            if path.is_symlink():
                raise ValueError('Unexpected runtime symlink')
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        receipt = smoke(payload, root/'services/api')
        descriptor = json.loads((payload/'site-packages/playwright/driver/package/browsers.json').read_text())
        chromium = next(item for item in descriptor['browsers'] if item['name'] == 'chromium')
        if chromium.get('browserVersion') != receipt['chromium_version']:
            raise ValueError('Launched Chromium version does not match fixed Playwright metadata')
        write_manifest(payload, sha, source_bytes, packages, receipt, chromium['revision'])
        verify(root, sha, payload)
        payload.rename(output)
    return verify(root, sha, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sha')
    parser.add_argument('directory', type=Path)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--with-system-deps', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = args.directory.resolve()
    manifest = verify(root, args.sha, folder) if args.verify else build(root, args.sha, folder, args.with_system_deps)
    if args.smoke:
        receipt = smoke(folder, root/'services/api')
        if receipt['chromium_version'] != manifest['chromium']['version']:
            raise ValueError('Mounted runtime version differs from the tested artifact')
    print(json.dumps({'sha':manifest['sha'], 'verified_files':len(manifest['files']),
                      'python':manifest['python'], 'chromium':manifest['chromium'], 'native_smoke':manifest['native_smoke']}))


if __name__ == '__main__':
    main()

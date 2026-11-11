"""Disposable Linux runner: real systemd reset/append semantics and non-root data writes."""

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import home_systemd_release as hook  # noqa: E402


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    if os.geteuid() or os.environ.get('LEAD_DISPOSABLE_RUNNER') != '1' or sys.platform != 'linux':
        raise SystemExit('disposable Linux systemd runtime required')
    # A real unit + drop-in is essential: hand-made mounts cannot reproduce the
    # shared BindPaths/BindReadOnlyPaths list reset implemented by systemd.
    unit = 'lead-executor-fixture-' + uuid.uuid4().hex + '.service'
    unit_file = Path('/run/systemd/system') / unit
    dropin_dir = unit_file.with_name(unit + '.d')
    dropin = dropin_dir / '90-executor.conf'
    assert not unit_file.exists() and not dropin_dir.exists()
    with tempfile.TemporaryDirectory(prefix='lead-executor-drill-', dir='/run') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        core = root / 'core'
        launcher = core / 'services/api/lead_app/channel_executor.py'
        launcher.parent.mkdir(parents=True)
        (core / 'REVISION').write_text('fixture-revision\n')
        # This would be writable by the test service without the read-only bind.
        (core / 'REVISION').chmod(0o666)
        launcher.write_text('''import errno
import json
import os
import tempfile
from pathlib import Path
assert os.getuid() == 65534 and os.getgid() == 65534
with tempfile.NamedTemporaryFile() as temporary:
    assert temporary.name.startswith('/run/lead-home/actor-home/tmp/')
    temporary.write(b'private scratch probe')
Path('/run/lead-home/actor-home/home-proof').write_text('home preserved')
Path('/run/lead-home/state/state-proof').write_text('state preserved')
assert not Path('/run/lead-home/obsolete-code').exists()
try:
    Path('/run/lead-executor/release/REVISION').write_text('must not change')
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError('common code bind became writable')
Path('/run/lead-executor/receipt/proof.json').write_text(json.dumps({'uid': os.getuid(), 'writable': True}))
''')
        adapter = root / 'adapter'
        adapter.mkdir()
        binding = root / 'binding.json'
        binding.write_text('{}\n')
        environment = root / 'operator.env'
        environment.write_text('# fixture only\n')
        obsolete = root / 'obsolete.py'
        obsolete.write_text('# must be removed from the process namespace\n')
        home, state, receipt = (root / name for name in ('home', 'state', 'receipt'))
        for directory in (home, state, receipt):
            directory.mkdir(mode=0o700)
            os.chown(directory, 65534, 65534)
        (home / 'tmp').mkdir(mode=0o700)
        os.chown(home / 'tmp', 65534, 65534)
        original = {(str(home), '/run/lead-home/actor-home'), (str(state), '/run/lead-home/state')}
        text = ('[Service]\nType=oneshot\nRemainAfterExit=yes\nUser=65534\nGroup=65534\n'
                'ProtectSystem=strict\nTemporaryFileSystem=/run/lead-home:ro /tmp:ro /var/tmp:ro\n'
                'Environment=TMPDIR=/run/lead-home/actor-home/tmp\nExecStart=/usr/bin/true\n'
                f'BindReadOnlyPaths={obsolete}:/run/lead-home/obsolete-code\n')
        text += ''.join('BindPaths=' + source + ':' + target + ':rbind\n' for source, target in sorted(original))
        try:
            unit_file.write_text(text)
            run('systemctl', 'daemon-reload')
            inherited = hook.executor_existing_bindings(unit, 'BindPaths')
            assert inherited == original
            template = (Path(__file__).resolve().parents[1] / hook.EXECUTOR_TEMPLATE).read_text()
            values = {
                '@BINDING@': str(binding),
                '@PRESERVED_READONLY_BINDINGS@': str(Path(sys.executable).resolve()) + ':' + str(hook.EXECUTOR_PYTHON),
                '@CORE_SOURCE@': str(core), '@ADAPTER_SOURCE@': str(adapter),
                '@RECEIPT_SOURCE@': str(receipt), '@ENV_SOURCE@': str(environment),
            }
            dropin_dir.mkdir()
            # Deliberately reproduce the old bug first, in a disposable service.
            dropin.write_text(hook.executor_render_template(template, values, []))
            run('systemctl', 'daemon-reload')
            assert hook.executor_existing_bindings(unit, 'BindPaths') == {(str(receipt), '/run/lead-executor/receipt')}
            broken = subprocess.run(['systemctl', 'start', unit], capture_output=True, text=True)
            assert broken.returncode != 0, 'reset unexpectedly retained original writable mounts'
            assert not (receipt / 'proof.json').exists()
            assert not (home / 'home-proof').exists() and not (state / 'state-proof').exists()
            run('systemctl', 'reset-failed', unit)
            # Apply the same complete rendering used by the trusted provisioning tool.
            dropin.write_text(hook.executor_render_template(template, values, inherited))
            run('systemctl', 'daemon-reload')
            assert hook.executor_existing_bindings(unit, 'BindPaths') == original | {(str(receipt), '/run/lead-executor/receipt')}
            run('systemctl', 'start', unit)
            assert (home / 'home-proof').read_text() == 'home preserved'
            assert (state / 'state-proof').read_text() == 'state preserved'
            assert (receipt / 'proof.json').is_file()
            assert (core / 'REVISION').read_text() == 'fixture-revision\n'
            print('Real systemd drop-in reset reproduced; original writable mounts and read-only code verified as UID 65534')
        finally:
            subprocess.run(['systemctl', 'stop', unit], check=False, capture_output=True)
            dropin.unlink(missing_ok=True)
            if dropin_dir.exists():
                dropin_dir.rmdir()
            unit_file.unlink(missing_ok=True)
            run('systemctl', 'daemon-reload')


if __name__ == '__main__':
    main()

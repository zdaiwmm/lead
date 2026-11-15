"""Disposable Linux only: real first provisioning, private-network UDS and rollback.

Uses the exact service/template/fetch sources with a local synthetic public-IP
HTTP fixture. No model, business database, account, pairing or auth file is used.
"""

import contextlib
import http.server
import importlib.util
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def command(*args, timeout=30):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def main():
    if (os.geteuid() or sys.platform != 'linux' or os.environ.get('LEAD_DISPOSABLE_RUNNER') != '1'
            or Path('/srv/lead-prod-vault').exists()):
        raise SystemExit('disposable Linux systemd runtime required')
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('reader_release', repo / 'scripts/home_systemd_release.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    prefix = Path(os.environ['LEAD_PUBLIC_READER_PYTHON'])
    hook.PUBLIC_READER_PYTHON = prefix
    hook.PUBLIC_READER_SITE = prefix / 'lib/python3.12/site-packages'
    try:
        hook.public_reader_python()
    except hook.Refused:
        print(json.dumps({'runtime_ancestors': [
            {'path': str(path), 'uid': path.lstat().st_uid, 'mode': oct(path.lstat().st_mode & 0o777)}
            for path in (prefix, prefix / 'bin', *prefix.parents)
        ]}), file=sys.stderr)
        raise
    units = ('lead-home-net.service', 'lead-home-api.service', 'lead-home-worker.service',
             hook.PUBLIC_READER_SERVICE, hook.PUBLIC_READER_SOCKET, 'lead-public-reader-drill-fence.service')
    for name in units:
        assert command('systemctl', 'show', name, '-p', 'LoadState', '--value') == 'not-found', name
    protected = [Path('/var/lib/lead-public-reader'), hook.PUBLIC_READER_RUNTIME]
    assert all(not path.exists() and not path.is_symlink() for path in protected)
    for name in units:
        assert not Path('/run/systemd/system', name).exists()
        assert not Path('/run/systemd/system', name + '.d').exists()
    # This globally classified address belongs only to lo on this disposable
    # machine. The test never sends a packet to its actual Internet owner.
    address = '93.184.216.34'
    assert address not in command('ip', '-j', 'address', 'show')
    added_address, server = False, None
    account = pwd.getpwnam('nobody')
    old, sha = 'a' * 40, 'b' * 40
    selected = {'sha': old}
    originals = {}
    try:
        with tempfile.TemporaryDirectory(prefix='lead-public-reader-drill-', dir='/run') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            hook.STORE = root / 'store'
            hook.STORE.mkdir(mode=0o755)
            release = hook.STORE / sha
            source = release / 'services/api/lead_app'
            source.mkdir(parents=True, mode=0o755)
            for name in ('public_reader.py', 'fetch.py', '__init__.py'):
                shutil.copyfile(repo / 'services/api/lead_app' / name, source / name)
                (source / name).chmod(0o644)
            templates = release / 'services/public_reader/systemd'
            templates.mkdir(parents=True)
            for name in (hook.PUBLIC_READER_SERVICE, hook.PUBLIC_READER_SOCKET):
                shutil.copyfile(repo / 'services/public_reader/systemd' / (name + '.in'), templates / (name + '.in'))
            (release / 'REVISION').write_text(sha + '\n')
            config = root / 'config'
            config.mkdir(mode=0o700)
            hook.EXECUTOR_REGISTRY = config / 'executors.json'
            hook.PUBLIC_READER_PLAN = config / 'public-reader-provision.json'
            hook.PUBLIC_READER_SYSTEMD = Path('/run/systemd/system')
            # Only artifact promotion/DB-backed application selection are replaced
            # by the fixture. All unit writes, journals, roots, actual process
            # mounts, Unix peer credentials and rollback below are real.
            hook.current = lambda: selected['sha']
            hook.image = lambda revision: release if revision == sha else root / old
            hook.public_reader_sources = lambda revision: release
            hook.public_reader_blobs = lambda revision: {name: 'd' * 40 for name in hook.PUBLIC_READER_REUSE_FILES}
            original_run = hook.run
            def fixture_run(*args, **kwargs):
                if args[0] == 'git' and args[-1] == sha + ':requirements.lock.txt':
                    return (repo / 'requirements.lock.txt').read_text()
                return original_run(*args, **kwargs)
            hook.run = fixture_run
            net = ('[Service]\nType=simple\nPrivateNetwork=yes\n'
                   'ExecStart=/usr/bin/sleep infinity\n')
            Path('/run/systemd/system/lead-home-net.service').write_text(net)
            consumer_root = root / 'consumer-root'
            consumer_root.mkdir(mode=0o755)
            base = ('[Unit]\nRequires=lead-home-net.service\nAfter=lead-home-net.service\n'
                    'JoinsNamespaceOf=lead-home-net.service\n[Service]\nType=simple\n'
                    f'User={account.pw_uid}\nGroup={account.pw_gid}\n'
                    f'RootDirectory={consumer_root}\nMountAPIVFS=yes\n'
                    'PrivateNetwork=yes\nProtectSystem=strict\n'
                    'BindReadOnlyPaths=/usr:/usr\nBindReadOnlyPaths=-/lib:/lib -/lib64:/lib64\n'
                    'ExecStart=/usr/bin/sleep infinity\n')
            for role in hook.PUBLIC_READER_CONSUMERS:
                Path('/run/systemd/system', hook.unit(role)).write_text(base)
            # Reproduce the actual worker's later reset and require the new
            # fixed append to survive it, while an original mount stays intact.
            worker_dropin = Path('/run/systemd/system/lead-home-worker.service.d')
            worker_dropin.mkdir()
            worker_reset = worker_dropin / 'zzzz-lead-executor.conf'
            worker_reset.write_text('[Service]\nBindReadOnlyPaths=\n'
                                    'BindReadOnlyPaths=/usr:/usr -/lib:/lib -/lib64:/lib64\n')
            fence = Path('/run/systemd/system/lead-public-reader-drill-fence.service')
            fence.write_text('[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n')
            command('systemctl', 'daemon-reload')
            command('systemctl', 'start', 'lead-home-api.service', 'lead-home-worker.service')
            originals = {role: hook.public_reader_consumer(role) for role in hook.PUBLIC_READER_CONSUMERS}
            plan = hook.public_reader_plan(sha)
            hook.PUBLIC_READER_PLAN.write_text(json.dumps(plan))
            hook.PUBLIC_READER_PLAN.chmod(0o600)
            actual_run = hook.run
            def lost_start_reply(*args, **kwargs):
                result = actual_run(*args, **kwargs)
                if args == ('systemctl', 'start', hook.PUBLIC_READER_SERVICE):
                    raise TimeoutError('synthetic delivered start reply lost')
                return result
            hook.run = lost_start_reply
            try:
                hook.provision_public_reader(sha, hook.PUBLIC_READER_PLAN)
            except TimeoutError:
                pass
            else:
                raise AssertionError('lost start response was not injected')
            original_intent = hook.public_reader_journal(sha).read_bytes()
            assert hook.public_reader_load(sha)['state'] == 'start-requested'
            invocation = hook.property_of(hook.PUBLIC_READER_SERVICE, 'InvocationID')
            mutations = []
            def readback_run(*args, **kwargs):
                if args[0] == 'systemctl' and args[1] in ('start', 'stop', 'restart', 'enable', 'disable', 'daemon-reload'):
                    mutations.append(args)
                return actual_run(*args, **kwargs)
            hook.run = readback_run
            prepared = hook.provision_public_reader(sha, hook.PUBLIC_READER_PLAN)
            assert mutations == []
            assert hook.property_of(hook.PUBLIC_READER_SERVICE, 'InvocationID') == invocation
            assert hook.public_reader_load(sha)['reconciled_journal_sha256'] == hook.executor_digest(original_intent)
            hook.run = actual_run
            assert prepared['consumers_verified'] is False
            assert all(hook.property_of(role, 'MainPID') == before['MainPID'] for role, before in originals.items())
            assert hook.verify_public_reader_prepared(sha)['application_sha'] == old
            first_journal = hook.public_reader_journal(sha).read_bytes()
            # A successful retry is readback, never another service start.
            assert hook.provision_public_reader(sha, hook.PUBLIC_READER_PLAN)['health']['revision'] == sha
            assert hook.public_reader_journal(sha).read_bytes() == first_journal
            command('ip', 'address', 'add', address + '/32', 'dev', 'lo')
            added_address = True

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    body = b'<html><title>Local public fixture</title><p>synthetic source only</p></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args):
                    pass

            server = http.server.ThreadingHTTPServer((address, 80), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            command('systemctl', 'restart', 'lead-home-api.service', 'lead-home-worker.service')
            selected['sha'] = sha
            activated = hook.verify_public_reader(sha)
            assert activated['consumers_verified'] is True
            selected['sha'] = 'c' * 40
            reused = hook.verify_public_reader_application('c' * 40)
            assert reused['reader_sha'] == sha and reused['application_sha'] == 'c' * 40
            selected['sha'] = sha
            pid = hook.property_of('api', 'MainPID')
            probe = '''import json,socket,sys
try:
 s=socket.create_connection((sys.argv[1],80),timeout=1);s.close()
except OSError:pass
else:raise AssertionError('private consumer acquired direct Internet route')
def call(value):
 s=socket.socket(socket.AF_UNIX);s.settimeout(40);s.connect('/run/lead-public-reader/reader.sock')
 s.sendall(json.dumps(value).encode()+b'\\n');raw=b''
 while b'\\n' not in raw:
  chunk=s.recv(65536)
  assert chunk
  raw+=chunk
 s.close();return json.loads(raw)
assert call({'v':1,'action':'health'})['revision']==sys.argv[2]
r=call({'v':1,'action':'read','url':'http://'+sys.argv[1]+'/',
        'discover_links':False,'external_links':False})
assert r['ok'] is True,r
assert 'synthetic source only' in r['observed']['text'],r
print('private-network-direct-denied-unix-read-ok')
'''
            def consumer_probe():
                return command('nsenter', '-t', pid, '-m', '-n', '--root', '--wd=/',
                    '--', '/usr/bin/setpriv', '--reuid=' + str(account.pw_uid),
                    '--regid=' + str(account.pw_gid), '--clear-groups',
                    '/usr/bin/python3', '-I', '-B', '-c', probe, address, sha, timeout=50)
            assert consumer_probe() == 'private-network-direct-denied-unix-read-ok'
            # Root can bypass filesystem permissions, but must still be denied
            # by SO_PEERCRED before any frame is processed.
            with socket.socket(socket.AF_UNIX) as wrong_peer:
                wrong_peer.settimeout(3)
                wrong_peer.connect(str(hook.PUBLIC_READER_RUNTIME / 'reader.sock'))
                try:
                    wrong_peer.sendall(b'{"v":1,"action":"health"}\n')
                    assert wrong_peer.recv(4096) == b''
                except (ConnectionResetError, BrokenPipeError):
                    pass
            for name in ('private-business-canary', 'private-auth-canary'):
                path = root / name
                path.write_text('synthetic private input')
                path.chmod(0o644)
                assert not (Path('/proc', str(activated['service_pid']), 'root') / path.relative_to('/')).exists()
            directory_inode = hook.PUBLIC_READER_RUNTIME.stat().st_ino
            command('systemctl', 'stop', hook.PUBLIC_READER_SERVICE, hook.PUBLIC_READER_SOCKET)
            command('systemctl', 'start', hook.PUBLIC_READER_SOCKET, hook.PUBLIC_READER_SERVICE)
            assert hook.PUBLIC_READER_RUNTIME.stat().st_ino == directory_inode
            assert consumer_probe() == 'private-network-direct-denied-unix-read-ok'
            hook.verify_public_reader(sha)
            # Model a separately restored old application, then exercise the
            # formal guarded consumer restore -> restart -> reader stop order.
            selected['sha'] = old
            @contextlib.contextmanager
            def fixture_maintenance(_old, _new):
                yield 'lead-public-reader-drill-fence.service', ['/usr/bin/true']
            hook.maintenance_window = fixture_maintenance
            hook.verify = lambda revision, **_kwargs: hook.require(selected['sha'] == revision, 'fixture app differs')
            restored = hook.rollback_public_reader(sha)
            assert restored['state'] == 'rolled-back'
            assert hook.property_of(hook.PUBLIC_READER_SERVICE, 'MainPID') == '0'
            assert hook.public_reader_load(sha)['state'] == 'rolled-back'
            for role, before in originals.items():
                now = hook.public_reader_consumer(role)
                assert now['unit_files'] == before['unit_files']
                assert now['BindReadOnlyPaths'] == before['BindReadOnlyPaths'] and now['route'] is None
            try:
                hook.provision_public_reader(sha, hook.PUBLIC_READER_PLAN)
            except hook.Refused:
                pass
            else:
                raise AssertionError('rolled-back intent replayed installation')
            print(json.dumps({'real_systemd_provision': True, 'uid_before_frame': True,
                'consumer_isolation_retained': True, 'local_public_source_read': True,
                'private_canaries_hidden': True, 'socket_recreation': True,
                'old_bindings_restored_before_reader_stop': True, 'unknown_start_readback_only': True,
                'replay_refused': True}))
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if added_address:
            subprocess.run(['ip', 'address', 'del', address + '/32', 'dev', 'lo'], check=True)
        subprocess.run(['systemctl', 'disable', '--now', hook.PUBLIC_READER_SOCKET], capture_output=True)
        subprocess.run(['systemctl', 'stop', *units], capture_output=True)
        for name in units:
            Path('/run/systemd/system', name).unlink(missing_ok=True)
            folder = Path('/run/systemd/system', name + '.d')
            if folder.exists():
                shutil.rmtree(folder)
        for path in protected:
            if path.exists():
                shutil.rmtree(path)
        command('systemctl', 'daemon-reload')


if __name__ == '__main__':
    main()

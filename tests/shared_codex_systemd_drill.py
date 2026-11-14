"""Disposable runner only: actual credential mounts, Unix peer PID and FD isolation."""
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

UNITS = ("lead-codex-worker.service", "lead-codex-node.service", "lead-codex-node.socket")
USERS = ("lead-codex", "lead-codex-supervisor")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    if (os.geteuid() or sys.platform != "linux" or os.environ.get("LEAD_DISPOSABLE_RUNNER") != "1"
            or Path("/srv/lead-prod-vault").exists()):
        raise SystemExit("disposable Linux systemd runtime required")
    source = Path(__file__).resolve().parents[1] / "infra/local-executor"
    prefix, config = Path("/opt/lead-codex-node"), Path("/etc/lead-codex-node")
    runtime = Path("/run/lead-codex-node")
    dropin = Path("/run/systemd/system/lead-codex-worker.service.d")
    targets = [Path("/run/systemd/system") / unit for unit in UNITS]
    fresh_paths = [prefix, config, dropin, runtime, *targets, *(Path("/var/lib") / name for name in USERS)]
    assert all(not path.exists() and not path.is_symlink() for path in fresh_paths)
    for unit in UNITS:
        assert run("systemctl", "show", "--property=LoadState", "--value", unit) == "not-found"
    for name in USERS:
        try:
            pwd.getpwnam(name)
        except KeyError:
            pass
        else:
            raise AssertionError("fixture identity already exists")
    created_users, created_units, created_directories = [], [], []
    created_runtime = False
    try:
        runtime.mkdir(mode=0o755)
        created_runtime = True
        for name in USERS:
            run("useradd", "--system", "--user-group", "--create-home", "--home-dir",
                "/var/lib/" + name, "--shell", "/usr/sbin/nologin", name)
            created_users.append(name)
        worker = pwd.getpwnam(USERS[0])
        supervisor = pwd.getpwnam(USERS[1])
        assert worker.pw_uid != supervisor.pw_uid and not os.getgrouplist(USERS[0], worker.pw_gid)[1:]
        prefix.mkdir(mode=0o755)
        created_directories.append(prefix)
        shutil.copyfile(source / "node_broker.py", prefix / "node_broker.py")
        (prefix / "node_broker.py").chmod(0o644)
        config.mkdir(mode=0o700)
        created_directories.append(config)
        credential = {"base_url": "https://example.invalid", "node_id": str(uuid.uuid4()),
            "node_token": "synthetic-credential-" + uuid.uuid4().hex,
            "node_epoch": 1, "instance_id": str(uuid.uuid4()), "credential_generation": 1}
        (config / "node.json").write_text(json.dumps(credential))
        (config / "node.json").chmod(0o600)
        with tempfile.TemporaryDirectory(prefix="lead-shared-ipc-", dir="/run") as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            receipt = root / "receipt"
            receipt.mkdir(mode=0o700)
            os.chown(receipt, worker.pw_uid, worker.pw_gid)
            fixture = root / "worker.py"
            fixture.write_text('''import json,os,socket,subprocess,sys,time,uuid
from pathlib import Path
path='/run/lead-codex-node/broker.sock'
deadline=time.monotonic()+12
while True:
 try:
  client=socket.socket(socket.AF_UNIX);client.settimeout(3);client.connect(path)
  client.sendall((json.dumps({'v':1,'id':str(uuid.uuid4()),'method':'worker.hello','params':{'instance_id':str(uuid.uuid4()),'capabilities':{}}})+'\\n').encode())
  response=json.loads(client.recv(4096))
  assert response['result']['worker_generation'].startswith(str(os.getpid())+':')
  break
 except Exception:
  client.close()
  if time.monotonic()>deadline:raise
  time.sleep(.1)
node_pid=subprocess.check_output(['/usr/bin/systemctl','show','--property=MainPID','--value','lead-codex-node.service'],text=True).strip()
def readable(path):
 try:Path(path).read_bytes();return True
 except OSError:return False
assert not readable('/etc/lead-codex-node/node.json')
assert not readable('/run/credentials/lead-codex-node.service/node.json')
assert not readable('/proc/'+node_pid+'/environ')
child_code="""import os,socket,sys
try:os.fstat(int(sys.argv[2]));inherited=True
except OSError:inherited=False
assert not inherited
s=socket.socket(socket.AF_UNIX);s.settimeout(3);s.connect(sys.argv[1])
try:s.sendall(b'{}\\\\n');assert not s.recv(4096)
except (ConnectionResetError,BrokenPipeError):pass
print('child_denied',flush=True)
"""
child=subprocess.run([sys.executable,'-I','-c',child_code,path,str(client.fileno())],close_fds=True,capture_output=True,text=True,timeout=5,check=True)
assert child.stdout.strip()=='child_denied'
proof=Path(sys.argv[1]);pending=proof.with_suffix('.pending')
pending.write_text(json.dumps({'main_pid_authenticated':True,'same_uid_child_denied':True,'broker_fd_not_inherited':True,'master_credential_unreadable':True,'supervisor_proc_unreadable':True}))
pending.replace(proof)
time.sleep(120)
''')
            fixture.chmod(0o644)
            for unit in UNITS:
                target = Path("/run/systemd/system") / unit
                text = (source / unit).read_text()
                if unit == "lead-codex-worker.service":
                    # Dependencies cannot be removed in a systemd drop-in.
                    # This is a synthetic worker, never the official CLI or egress.
                    assert "Requires=lead-codex-node.socket lead-codex-egress.service" in text
                    text = text.replace("Requires=lead-codex-node.socket lead-codex-egress.service",
                        "Requires=lead-codex-node.socket").replace(
                        "After=lead-codex-node.socket lead-codex-egress.service network-online.target",
                        "After=lead-codex-node.socket network-online.target")
                data = text.encode()
                with target.open("xb") as output:
                    created_units.append(target)
                    output.write(data)
            dropin.mkdir(mode=0o755)
            created_directories.append(dropin)
            (dropin / "fixture.conf").write_text("[Service]\nEnvironmentFile=\nExecStartPre=\nExecStart=\n"
                f"ExecStart=/usr/bin/python3 -I -B {fixture} {receipt / 'proof.json'}\n"
                f"ReadWritePaths=\nReadWritePaths={receipt}\n")
            run("systemctl", "daemon-reload")
            run("systemd-analyze", "verify", "--man=no", *map(str, created_units))
            run("systemctl", "start", "lead-codex-worker.service")
            deadline = time.monotonic() + 15
            while not (receipt / "proof.json").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert (receipt / "proof.json").exists(), "real broker IPC proof absent"
            result = json.loads((receipt / "proof.json").read_text())
            assert len(result) == 5 and all(value is True for value in result.values())
            assert run("systemctl", "show", "--property=User", "--value", "lead-codex-node.service") == USERS[1]
            print(json.dumps(result))
    finally:
        owned_units = {path.name for path in created_units}
        for unit in UNITS:
            if unit not in owned_units:
                continue
            subprocess.run(["systemctl", "stop", unit], check=False, capture_output=True)
        for path in created_units:
            path.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        for unit in owned_units:
            subprocess.run(["systemctl", "reset-failed", unit], check=False, capture_output=True)
        for path in reversed(created_directories):
            shutil.rmtree(path)
        for name in reversed(created_users):
            subprocess.run(["userdel", "--remove", name], check=True, capture_output=True)
        assert not Path("/run/lead-codex-node/broker.sock").exists()
        if created_runtime:
            runtime.rmdir()


if __name__ == "__main__":
    main()

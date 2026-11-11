"""Root-only disposable Linux test: kernel lock, real swap/rollback and killed installer recovery."""

import fcntl
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tool_install as tool  # noqa: E402


def main():
    if os.geteuid() or os.environ.get("LEAD_DISPOSABLE_RUNNER") != "1":
        raise SystemExit("disposable root runner required")
    with tempfile.TemporaryDirectory(dir="/run") as temp:
        root = Path(temp)
        tools, store = root / "tools", root / "store"
        tools.mkdir(mode=0o700)
        store.mkdir(mode=0o700)
        target = tools / "home_systemd_release.py"
        old, new, sha, app = b"VALUE=1\n", b"VALUE=2\n", "a" * 40, "b" * 40
        target.write_bytes(old)
        target.chmod(0o755)
        def install(current=lambda: app):
            return tool.install(tools, store, target.name, new, sha, tool.digest(old), app,
                                current=current, git_source=lambda *_: new)
        try:
            install(current=lambda: app if target.read_bytes() == old else "c" * 40)
        except ValueError:
            pass
        else:
            raise AssertionError("identity failure accepted")
        assert target.read_bytes() == old
        program = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import tool_install as tool
root=Path(sys.argv[2]);target=root/'tools/home_systemd_release.py'
original=tool.atomic_write
def interrupted(path,data,mode):
 original(path,data,mode)
 if path==target:os._exit(77)
tool.atomic_write=interrupted
tool.install(root/'tools',root/'store',target.name,b'VALUE=2\\n','a'*40,tool.digest(b'VALUE=1\\n'),'b'*40,current=lambda:'b'*40,git_source=lambda *_:b'VALUE=2\\n')
"""
        command = [sys.executable, "-I", "-B", "-c", program,
                   str(Path(__file__).resolve().parents[1] / "scripts"), str(root)]
        with (store / "deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = subprocess.run(command, check=False, capture_output=True, text=True)
            assert "BlockingIOError" in blocked.stderr and target.read_bytes() == old
        result = subprocess.run(command, check=False)
        assert result.returncode == 77 and target.read_bytes() == new
        proof = install()
        assert Path(proof["backup"]).read_bytes() == old
        assert proof["application_switch"] is False and proof["service_restarts"] is False
        assert target.lstat().st_uid == 0 and target.stat().st_mode & 0o777 == 0o755
        print("Root tool swap, rollback, killed-process resume and backup identity passed")


if __name__ == "__main__":
    main()

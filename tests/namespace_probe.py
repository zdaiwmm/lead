"""Stdlib-only OS-boundary probe, executed inside a disposable Linux container."""

import json
import os
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

CLIENT = r"""
import json, os, socket, sys
from pathlib import Path
port, token, index = int(sys.argv[1]), sys.argv[2], sys.argv[3]
assert os.read(3,64) == b"pipe-three"
os.write(4,b"pipe-four")
def blocked(family, kind, destination):
    try:
        with socket.socket(family, kind) as client:
            client.settimeout(1)
            if kind == socket.SOCK_STREAM: client.connect(destination)
            else: client.sendto(b"probe", destination)
        return False
    except OSError: return True
with socket.create_connection(("127.0.0.1",port),timeout=2) as client:
    client.sendall(token.encode())
    response=client.recv(128).decode()
assert response == "profile:" + index
results = {
    "proxy_bridge":True,
    "control_pipes":True,
    "direct_https_blocked":blocked(socket.AF_INET,socket.SOCK_STREAM,("1.1.1.1",443)),
    "direct_dns_blocked":blocked(socket.AF_INET,socket.SOCK_DGRAM,("8.8.8.8",53)),
    "direct_ipv6_blocked":blocked(socket.AF_INET6,socket.SOCK_DGRAM,("2606:4700:4700::1111",53)),
    "host_loopback_blocked":blocked(socket.AF_INET,socket.SOCK_STREAM,("127.0.0.1",int(sys.argv[4]))),
    "private_files_hidden":not Path(sys.argv[5]).exists(),
    "operator_env_absent":not any(k.startswith("LEAD_") for k in os.environ),
}
try:
    Path(sys.argv[6]).write_text("forbidden")
    results["assets_read_only"]=False
except OSError: results["assets_read_only"]=True
assert all(results.values()),results
results["network_namespace"]=os.readlink("/proc/self/ns/net")
print(json.dumps(results),flush=True)
"""


def serve(server, index, token):
    while True:
        connection, _ = server.accept()
        with connection:
            value = connection.recv(128).decode()
            if value == token:
                connection.sendall(("profile:" + str(index)).encode())


def main():
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        secret = root / "operator.key"
        secret.write_text("private-operator-key")
        host = socket.socket()
        host.bind(("127.0.0.1", 0))
        host.listen()
        processes = []
        for index in range(3):
            profile = root / ("profile-" + str(index))
            profile.mkdir()
            assets = root / ("assets-" + str(index))
            assets.mkdir()
            probe = profile / "probe.py"
            probe.write_text(CLIENT)
            uds = root / (str(index) + ".sock")
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(uds))
            server.listen()
            token = "test-private-" + str(index)
            threading.Thread(
                target=serve, args=(server, index, token), daemon=True
            ).start()
            values = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LEAD_REAL_BROWSER": "/usr/bin/python3",
                "LEAD_BROWSER_PROFILE": str(profile),
                "LEAD_BROWSER_ASSETS": str(assets),
                "LEAD_BROWSER_SOCKET": str(uds),
                "LEAD_BROWSER_PROXY_PORT": "31415",
            }
            read_three, write_three = os.pipe()
            read_four, write_four = os.pipe()
            os.write(write_three, b"pipe-three")
            os.close(write_three)
            helper = "import os,sys;os.dup2(int(sys.argv[1]),3);os.dup2(int(sys.argv[2]),4);os.execv('/usr/bin/python3',['/usr/bin/python3',*sys.argv[3:]])"
            process = subprocess.Popen(
                [
                    "/usr/bin/python3",
                    "-c",
                    helper,
                    str(read_three),
                    str(write_four),
                    "/opt/lead/browser_namespace.py",
                    str(probe),
                    "31415",
                    token,
                    str(index),
                    str(host.getsockname()[1]),
                    str(secret),
                    str(assets / "write-test"),
                ],
                env=values,
                pass_fds=(read_three, write_four),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(read_three)
            os.close(write_four)
            processes.append((process, read_four))
        reports = []
        for process, read_four in processes:
            output, errors = process.communicate(timeout=30)
            if process.returncode:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "namespace_unavailable",
                            "diagnostic": errors[-600:],
                        }
                    )
                )
                return 1
            assert os.read(read_four, 64) == b"pipe-four"
            os.close(read_four)
            reports.append(json.loads(output))
        assert len({r["network_namespace"] for r in reports}) == 3
        print(
            json.dumps(
                {
                    "ok": True,
                    "scope": "linux_os_and_proxy_transport",
                    "native_browser_included": False,
                    "profiles": reports,
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

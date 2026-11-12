"""Run an attended release with separate signing and application hosts over existing SSH."""
import argparse
import json
import re
import subprocess

TOOLS = "/usr/local/lib/lead-home-release/"
JOURNAL = "/srv/lead-prod-vault/lead-git-releases/requests"


def serve(stream, replies, host, sha):
    for line in stream:
        request = json.loads(line)
        op = request.get("operation")
        if request.get("sha") != sha or op not in ("current", "deploy", "verify"):
            raise ValueError("unexpected production command")
        command = [*host, "sudo", "-n", TOOLS + "home_systemd_release.py"]
        if op == "current":
            live = subprocess.check_output([*command, "current"], text=True).strip()
            if not re.fullmatch("[a-f0-9]{40}", live):
                raise ValueError("invalid live revision")
            checked = subprocess.check_output([*command, "preflight", sha], text=True).strip()
            if checked != live:
                raise ValueError("production revision changed during preflight")
            response = {"ok": True, "current": live}
        else:
            if op == 'verify':
                # Resume only the staged executor/finalization journal of this
                # same request, then launch a separate read-only verification.
                # An interruption leaves no terminal result to falsely cache.
                subprocess.run([*command, 'finalize-cutover', sha], check=True)
            result = subprocess.run([*command, op, sha], check=False)
            # A lost SSH session cannot establish a negative deployment verification.
            if result.returncode == 255:
                raise ConnectionError("production connection interrupted")
            response = {"ok": result.returncode == 0}
        replies.write(json.dumps({**request, **response}) + "\n")
        replies.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-config", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("sha")
    args = parser.parse_args()
    if not re.fullmatch("[a-f0-9]{40}", args.sha) or not all(re.fullmatch("[a-zA-Z0-9][a-zA-Z0-9.-]*", h)
                                                            for h in (args.host, args.node)):
        parser.error("expected a full revision and SSH host aliases")
    ssh = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-F", args.ssh_config]
    node = [*ssh, args.node, "sudo", "-n", "env", "LEAD_NODE_GITHUB=1", "/usr/bin/python3", "-EsB",
            TOOLS + "poll_home_release.py", "--state-dir", JOURNAL, "--attended-host", args.sha]
    with subprocess.Popen(node, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as proc:
        try:
            serve(proc.stdout, proc.stdin, [*ssh, args.host], args.sha)
        finally:
            proc.stdin.close()
        if proc.wait():
            raise SystemExit("release node did not confirm completion")

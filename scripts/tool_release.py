"""Inspect/install one tool from a tested main bundle through the existing authenticated SSH."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

from ci_scope import TOOL_FILES
from request_home_release import REPO, api, verified_run
from tool_install import INSTALLABLE, atomic_write, trusted


def unpack(raw, sha):
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Tool bundle too large")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(set(names)) != len(names) or set(names) != TOOL_FILES | {"manifest.json"}:
            raise ValueError("Unexpected tool bundle contents")
        if any(not m.isfile() or m.size > 1024 * 1024 or m.mode & 0o7022 for m in members):
            raise ValueError("Unsafe tool bundle member")
        files = {m.name: archive.extractfile(m).read() for m in members}
    manifest = json.loads(files.pop("manifest.json"))
    if manifest.get("schema") != 1 or manifest.get("sha") != sha:
        raise ValueError("Tool manifest belongs to another revision")
    entries = manifest.get("files", [])
    if len(entries) != len(files) or {e["path"] for e in entries} != set(files):
        raise ValueError("Tool manifest is incomplete")
    for entry in entries:
        if (entry["mode"] not in {"100644", "100755"}
                or hashlib.sha256(files[entry["path"]]).hexdigest() != entry["sha256"]):
            raise ValueError("Tool source digest mismatch")
    return files


def fetch(sha, *, get=api, download=None):
    ci = verified_run(sha, get=get, kind="tools")
    name = f"lead-tools-{sha}-{ci['run_attempt']}"
    listing = get(f"actions/runs/{ci['id']}/artifacts?name={name}&per_page=2")
    artifacts = listing["artifacts"]
    if listing["total_count"] != 1 or len(artifacts) != 1:
        raise ValueError("Unique tested tool artifact required")
    item = artifacts[0]
    if (item["name"] != name or item["expired"] or not 0 < item["size_in_bytes"] < 16 * 1024 * 1024
            or item["workflow_run"]["head_sha"] != sha or item["workflow_run"]["id"] != ci["id"]
            or not re.fullmatch("sha256:[a-f0-9]{64}", item.get("digest", ""))):
        raise ValueError("Tool artifact provenance unavailable")
    if download is None:
        def download(ident):
            return subprocess.check_output(
                ["gh", "api", f"repos/{REPO}/actions/artifacts/{ident}/zip"], timeout=120)
    raw = download(item["id"])
    if len(raw) != item["size_in_bytes"] or "sha256:" + hashlib.sha256(raw).hexdigest() != item["digest"]:
        raise ValueError("Tool transport digest mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != "lead-tools.tar" or members[0].file_size > 8 * 1024 * 1024:
            raise ValueError("Unexpected tool transport archive")
        files = unpack(archive.read(members[0]), sha)
    latest = verified_run(sha, get=get, kind="tools")
    if (latest["id"], latest["run_attempt"]) != (ci["id"], ci["run_attempt"]):
        raise ValueError("Tool CI changed while fetching")
    # Do not return a now-expired/replaced artifact after a transfer.
    current = get(f"actions/runs/{ci['id']}/artifacts?name={name}&per_page=2")
    if current["total_count"] != 1 or len(current["artifacts"]) != 1:
        raise ValueError("Tool artifact changed while fetching")
    final = current["artifacts"][0]
    if final["expired"] or any(final[k] != item[k] for k in ("id", "digest", "size_in_bytes", "workflow_run", "name")):
        raise ValueError("Tool artifact changed while fetching")
    return files, {"sha": sha, "ci_run": ci["id"], "ci_attempt": ci["run_attempt"], "artifact": item["id"]}


def ssh_command(config, host):
    if not re.fullmatch("[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
        raise ValueError("Expected an SSH host alias")
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-F", str(config), host,
            "sudo -n python3 -IB -"]


def run_remote(ssh, program):
    return json.loads(subprocess.check_output(ssh, input=program, text=True, timeout=180))


def inspect(ssh, name):
    if name not in INSTALLABLE:
        raise ValueError("Tool outside installation scope")
    return run_remote(ssh, """import hashlib,json,subprocess
from pathlib import Path
tools=Path('/usr/local/lib/lead-home-release')
p=tools/NAME
assert p.is_file() and not p.is_symlink()
app=subprocess.check_output([str(tools/'home_systemd_release.py'),'current'],text=True).strip()
print(json.dumps({'old':hashlib.sha256(p.read_bytes()).hexdigest(),'app':app}))
""".replace("NAME", repr(name)))


def remote_program(files, sha, name, before):
    # The installer itself is a source in the same authenticated, tested bundle.
    module = base64.b64encode(files["scripts/tool_install.py"]).decode()
    data = base64.b64encode(files["scripts/" + name]).decode()
    program = """import base64,json,os,subprocess
from pathlib import Path
assert os.geteuid()==0
exec(compile(base64.b64decode(__MODULE__),'reviewed-tool-installer','exec'))
store=Path('/srv/lead-prod-vault/lead-git-releases')
tools=Path('/usr/local/lib/lead-home-release')
def current():
 return subprocess.check_output([str(tools/'home_systemd_release.py'),'current'],text=True,timeout=40).strip()
def source(sha,name):
 mirror=store/'lead.git'
 subprocess.run(['git','-c','credential.helper=','-c','core.hooksPath=/dev/null','--git-dir',str(mirror),'fetch','--no-tags','https://github.com/zdaiwmm/lead.git','+refs/heads/main:refs/heads/main'],check=True,timeout=120,stdout=subprocess.DEVNULL)
 subprocess.run(['git','--git-dir',str(mirror),'merge-base','--is-ancestor',sha,'refs/heads/main'],check=True)
 return subprocess.check_output(['git','--git-dir',str(mirror),'show',sha+':'+name])
result=install(tools,store,__NAME__,base64.b64decode(__DATA__),__SHA__,__OLD__,__APP__,current=current,git_source=source)
print(json.dumps(result))
"""
    for key, value in {"MODULE": module, "DATA": data, "NAME": name, "SHA": sha,
                       "OLD": before["old"], "APP": before["app"]}.items():
        program = program.replace("__" + key + "__", repr(value))
    return program


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha")
    parser.add_argument("--tool", choices=sorted(INSTALLABLE), required=True)
    parser.add_argument("--ssh-config", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--install", action="store_true", help="Execute the already authorized scoped installation")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    files, receipt = fetch(args.sha)
    receipt.update(tool=args.tool, sha256=hashlib.sha256(files["scripts/" + args.tool]).hexdigest())
    if args.install:
        if not args.ssh_config or not args.host or not args.receipt:
            parser.error("Installation requires existing SSH and a local evidence file")
        ssh = ssh_command(args.ssh_config, args.host)
        receipt.update(host=args.host, ssh_config=str(args.ssh_config.resolve()))
        args.receipt.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        trusted(args.receipt.parent, True)
        if args.receipt.exists() or args.receipt.is_symlink():
            trusted(args.receipt)
            saved = json.loads(args.receipt.read_text())
            if any(saved.get(k) != v for k, v in receipt.items()):
                raise ValueError("Evidence belongs to another candidate, CI or destination")
            before = saved["before"]
        else:
            before = inspect(ssh, args.tool)
            # Save identity before any write so a lost SSH reply can be resumed.
            saved = {**receipt, "before": before, "state": "INSTALLATION_UNRESOLVED"}
            with args.receipt.open("x") as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(json.dumps(saved) + "\n")
                out.flush()
                os.fsync(out.fileno())
        receipt["before"] = before
        receipt["installation"] = run_remote(ssh, remote_program(files, args.sha, args.tool, before))
        # A separate SSH/Python process attests bytes and live application independently.
        after = inspect(ssh, args.tool)
        if after != {"old": receipt["sha256"], "app": before["app"]}:
            raise ValueError("Independent tool readback failed; inspect the saved installation")
        receipt["readback"] = "TOOL_READBACK_OK"
        atomic_write(args.receipt, (json.dumps(receipt, indent=2) + "\n").encode(), 0o600)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()

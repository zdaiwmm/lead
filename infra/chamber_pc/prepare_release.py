"""Prepare a verified GitHub push for the existing Chamber deployment tools."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = "zdaiwmm/lead"
ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "release.py": "infra/chamber_pc/release.py",
    "activate.py": "infra/chamber_pc/workspace/activate.py",
    "modules/resident.py": "infra/chamber_pc/resident/pc_resident_agent.py",
    "modules/community.py": "services/api/lead_app/community_runtime.py",
    "modules/selection.py": "services/api/lead_app/community_selection.py",
    "modules/telegram.py": "services/api/lead_app/telegram.py",
    "modules/model.py": "infra/chamber_pc/resident/target_model.py",
    "modules/broker.py": "infra/chamber_pc/resident/target_business.py",
    "modules/business.py": "infra/chamber_pc/resident/business.py",
}


def prepare(revision, output_parent, *, workspace=False):
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        cwd=ROOT, text=True,
    ).strip()
    # A PR's synthetic merge commit and an earlier green branch tip are not
    # evidence that this exact push passed the repository's workflow.
    runs = json.loads(subprocess.check_output([
        "gh", "run", "list", "--repo", REPO, "--workflow", "ci.yml", "--event", "push",
        "--commit", sha, "--limit", "1", "--json", "databaseId,headSha,status,conclusion,url",
    ], text=True))
    if not runs or runs[0]["headSha"] != sha:
        raise ValueError(f"GitHub 上尚未找到提交 {sha} 对应的 push CI")
    run = runs[0]
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise ValueError(f"此提交的 CI 状态为 {run['conclusion'] or run['status']}，详见 {run['url']}")

    output_parent.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=f"chamber-{sha[:12]}-", dir=output_parent))
    try:
        hashes = {}
        if workspace:
            subprocess.run([
                "gh", "run", "download", str(run["databaseId"]), "--repo", REPO,
                "--name", f"chamber-workspace-{sha}", "--dir", str(dest),
            ], check=True)
            if (dest / "REVISION").read_text().strip() != sha:
                raise ValueError("制品的提交号与所选提交不一致")
            expected = (dest / "policy.SHA256").read_text().strip()
            if (not re.fullmatch(r"[0-9a-f]{64}", expected)
                    or hashlib.sha256((dest / "policy.tar.gz").read_bytes()).hexdigest() != expected):
                raise ValueError("工作台安装包的 SHA-256 校验失败")
            hashes["policy.tar.gz"] = expected
        for name, path in SOURCES.items():
            data = subprocess.check_output(["git", "show", f"{sha}:{path}"], cwd=ROOT)
            target = dest / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        (dest / "release.json").write_text(json.dumps(
            {"commit": sha, "ci": run["url"], "sha256": hashes}, indent=2,
        ) + "\n")
        return dest
    except BaseException:
        shutil.rmtree(dest)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", help="需要交付的完整 Git SHA 或本地可解析的版本")
    parser.add_argument("--workspace", action="store_true", help="同时下载并校验该次 CI 构建的工作台安装包")
    parser.add_argument("--output-parent", type=Path, default=ROOT / ".state" / "releases")
    args = parser.parse_args()
    try:
        directory = prepare(args.revision, args.output_parent.resolve(), workspace=args.workspace)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"发布准备失败：{exc}\n")
    print(directory)

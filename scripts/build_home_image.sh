#!/usr/bin/env bash
# Run in a clean Linux CI checkout immediately after building apps/web.
set -euo pipefail
sha=${1:?full commit required}
output=${2:?new output file required}
browser_runtime=${3:?same-run tested browser runtime required}
local_executor=${4:?same-run tested local executor package required}
[[ $sha =~ ^[a-f0-9]{40}$ && ! -e $output ]]
root=$(git rev-parse --show-toplevel)
cd "$root"
[[ $(git rev-parse HEAD) == "$sha" ]]
runtime_paths=(
  scripts/browser_namespace.py
  scripts/browser_use_runner.py
  scripts/backup.py
  infra/alembic.ini
  infra/compose.yaml
  infra/migrations
)
git diff --quiet "$sha" -- services apps/web infra/home-browser-frames "${runtime_paths[@]}"
test -s apps/web/.next/BUILD_ID
test -d apps/web/node_modules/next
# This consumes the same run/attempt's verified package; it never installs or builds.
python3 -IB scripts/build_browser_runtime.py "$sha" "$browser_runtime" --verify
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
# SquashFS preserves the staging root mode; channel executors traverse it as non-root.
chmod 0755 "$stage"
# Runtime helpers remain under ROOT; operator tools and local credentials are excluded.
git archive "$sha" services apps/web infra/home-browser-frames "${runtime_paths[@]}" | tar -x -C "$stage"
test ! -e "$stage/services/browser-runtime"
cp -a "$browser_runtime" "$stage/services/browser-runtime"
python3 -IB scripts/build_browser_runtime.py "$sha" "$stage/services/browser-runtime" --verify
test -s "$local_executor"
python3 -IB - "$local_executor" "$sha" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    members = archive.getmembers()
    required = ("package/dist/cli.js", "package/REVISION")
    entries = {}
    for name in required:
        matches = [member for member in members if member.name == name]
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError("Executor package requires one regular CLI and REVISION")
        entries[name] = matches[0]
    expected = (sys.argv[2] + "\n").encode("ascii")
    revision = entries["package/REVISION"]
    if revision.size != len(expected) or archive.extractfile(revision).read() != expected:
        raise ValueError("Executor package belongs to another revision")
PY
cp "$local_executor" "$stage/services/local-executor/lead-local-executor.tgz"
mv "$stage/apps/web" "$stage/web"
mv "$stage/infra/home-browser-frames" "$stage/home-browser-frames"
rmdir "$stage/apps"
cp -a apps/web/.next apps/web/node_modules "$stage/web/"
rm -rf -- "$stage/web/.next/cache"
mkdir -p "$stage/web/.next/cache"
printf '%s\n' "$sha" > "$stage/REVISION"
mksquashfs "$stage" "$output" -noappend -all-root -no-progress -comp zstd

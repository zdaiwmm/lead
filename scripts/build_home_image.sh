#!/usr/bin/env bash
# Run in a clean Linux CI checkout immediately after building apps/web.
set -euo pipefail
sha=${1:?full commit required}
output=${2:?new output file required}
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
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
# Runtime helpers remain under ROOT; operator tools and local credentials are excluded.
git archive "$sha" services apps/web infra/home-browser-frames "${runtime_paths[@]}" | tar -x -C "$stage"
mv "$stage/apps/web" "$stage/web"
mv "$stage/infra/home-browser-frames" "$stage/home-browser-frames"
rmdir "$stage/apps"
cp -a apps/web/.next apps/web/node_modules "$stage/web/"
rm -rf -- "$stage/web/.next/cache"
mkdir -p "$stage/web/.next/cache"
printf '%s\n' "$sha" > "$stage/REVISION"
mksquashfs "$stage" "$output" -noappend -all-root -no-progress -comp zstd

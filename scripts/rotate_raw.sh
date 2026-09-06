#!/usr/bin/env bash
# Move raw files older than the retention windows (hourly 10 d, daily 90 d) into monthly
# tarballs on the orphan branch `data-archive`, then delete them from main. Idempotent: a
# month's tarball is appended to (tar --concatenate is avoided; we re-create from the union).
set -euo pipefail
HOURLY_KEEP="${HOURLY_KEEP_DAYS:-10}"
DAILY_KEEP="${DAILY_KEEP_DAYS:-90}"
ROOT="data/raw"
[ -d "$ROOT" ] || exit 0
now=$(date -u +%s)
tmp=$(mktemp -d)
moved=0
while IFS= read -r f; do
  rel=${f#"$ROOT"/}
  y=${rel%%/*}; rest=${rel#*/}; m=${rest%%/*}; rest=${rest#*/}; d=${rest%%/*}
  day=$(date -u -d "$y-$m-$d" +%s 2>/dev/null || date -u -j -f "%Y-%m-%d" "$y-$m-$d" +%s)
  age=$(( (now - day) / 86400 ))
  keep=$DAILY_KEEP
  case "$f" in *_0000.json.gz) ;; *) keep=$HOURLY_KEEP ;; esac
  if [ "$age" -gt "$keep" ]; then
    mkdir -p "$tmp/$y-$m/$y/$m/$d"
    mv "$f" "$tmp/$y-$m/$y/$m/$d/"
    moved=$((moved + 1))
  fi
done < <(find "$ROOT" -type f -name '*.json.gz')
echo "raw files to rotate: $moved"
[ "$moved" -gt 0 ] || exit 0
git fetch origin data-archive:data-archive 2>/dev/null || true
wt=$(mktemp -d)
if git show-ref --verify --quiet refs/heads/data-archive; then
  git worktree add "$wt" data-archive
else
  git worktree add --detach "$wt"
  (cd "$wt" && git checkout --orphan data-archive && git rm -rfq . && echo "# raw archive (monthly tarballs)" > README.md && git add README.md && git -c user.name=crypto-monitor-bot -c user.email=crypto-monitor-bot@users.noreply.github.com commit -qm "init data-archive")
fi
for month in "$tmp"/*; do
  mm=$(basename "$month")
  tarball="$wt/raw-$mm.tar"
  if [ -f "$tarball" ]; then
    (cd "$month" && tar -rf "$tarball" .)
  else
    (cd "$month" && tar -cf "$tarball" .)
  fi
done
(cd "$wt" && git add -A && git -c user.name=crypto-monitor-bot -c user.email=crypto-monitor-bot@users.noreply.github.com commit -qm "rotate raw files $(date -u +%Y-%m-%d)" && git push -q origin data-archive)
git worktree remove --force "$wt"
find "$ROOT" -type d -empty -delete
echo "rotated $moved files into data-archive"

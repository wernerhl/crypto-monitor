#!/usr/bin/env bash
# Move raw files older than the retention windows (hourly 10 d, daily 90 d) onto the orphan branch
# `data-archive`, then delete them from main.
#
# Layout on data-archive (2026-11-04): each rotation writes NEW tar parts per month,
#   raw-YYYY-MM/rot-YYYYMMDD-NN.tar      each at most MAX_PART_MB (default 90; GitHub rejects > 100 MB)
# and never rewrites an existing tar. The earlier layout appended every week to one raw-YYYY-MM.tar:
# raw-2026-09.tar reached 104.56 MB on 27 Sep (just under GitHub's 104.86 MB limit), the next append
# was rejected (pre-receive hook, GH001), and the weekly job failed for five weeks — and because the
# commit step came after this one, five weeks of weekly outputs were discarded with the runner. Old
# raw-YYYY-MM.tar files stay as they are. Restore everything with:
#   for t in raw-*.tar raw-*/*.tar; do tar -xf "$t" -C data/raw; done
#
# Failure-safe: if anything fails before the archive push succeeds, every moved file is put back
# under data/raw, so the caller's commit step never records deletions of unarchived files.
set -euo pipefail
HOURLY_KEEP="${HOURLY_KEEP_DAYS:-10}"
DAILY_KEEP="${DAILY_KEEP_DAYS:-90}"
MAX_PART_BYTES=$(( ${MAX_PART_MB:-90} * 1024 * 1024 ))
ROOT="data/raw"
PUSH="${ROTATE_PUSH:-1}"      # tests set 0: build the parts in the worktree, do not push
[ -d "$ROOT" ] || exit 0
now=$(date -u +%s)
stamp=$(date -u +%Y%m%d)
tmp=$(mktemp -d)
wt=""
done_ok=0

restore() {
  if [ "$done_ok" != 1 ] && [ -d "$tmp" ]; then
    n=0
    for month in "$tmp"/*/; do
      [ -d "$month" ] || continue
      cp -a "$month/." "$ROOT/" && n=$((n + 1))
    done
    [ "$n" -gt 0 ] && echo "::warning::raw rotation did not complete; moved files were restored under $ROOT" >&2
  fi
  if [ -n "$wt" ] && [ -d "$wt" ]; then git worktree remove --force "$wt" 2>/dev/null || true; fi
  rm -rf "$tmp"
}
trap restore EXIT

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
if [ "$moved" -eq 0 ]; then done_ok=1; exit 0; fi

git fetch origin data-archive:data-archive 2>/dev/null || true
wt=$(mktemp -d)
if git show-ref --verify --quiet refs/heads/data-archive; then
  git worktree add -q "$wt" data-archive
else
  git worktree add -q --detach "$wt"
  (cd "$wt" && git checkout -q --orphan data-archive && git rm -rfq . && echo "# raw archive (monthly tar parts)" > README.md && git add README.md && git -c user.name=crypto-monitor-bot -c user.email=crypto-monitor-bot@users.noreply.github.com commit -qm "init data-archive")
fi

# one month at a time: split its files into parts whose size (tar headers and 512-byte padding
# included, plus the end-of-archive record) stays under MAX_PART_BYTES
for month in "$tmp"/*/; do
  mm=$(basename "$month")
  mkdir -p "$wt/raw-$mm"
  part=0; size=0; list=""
  flush() {
    [ -n "$list" ] || return 0
    part=$((part + 1))
    out="$wt/raw-$mm/rot-$stamp-$(printf '%02d' "$part").tar"
    while [ -e "$out" ]; do part=$((part + 1)); out="$wt/raw-$mm/rot-$stamp-$(printf '%02d' "$part").tar"; done
    printf '%s\n' "$list" | sed '/^$/d' > "$tmp/.list"
    (cd "$month" && tar -cf "$out" -T "$tmp/.list")
    bytes=$(stat -c %s "$out" 2>/dev/null || stat -f %z "$out")
    if [ "$bytes" -gt "$MAX_PART_BYTES" ]; then
      echo "::error::$out is $bytes bytes, over the $MAX_PART_BYTES-byte part limit" >&2; exit 1
    fi
    echo "  raw-$mm/$(basename "$out"): $(wc -l < "$tmp/.list") files, $((bytes / 1048576)) MB"
    list=""; size=0
  }
  while IFS= read -r rel; do
    fbytes=$(stat -c %s "$month/$rel" 2>/dev/null || stat -f %z "$month/$rel")
    cost=$(( 512 + (fbytes + 511) / 512 * 512 ))
    if [ -n "$list" ] && [ $(( size + cost + 20480 )) -gt "$MAX_PART_BYTES" ]; then flush; fi
    list="$list$rel"$'\n'; size=$(( size + cost ))
  done < <(cd "$month" && find . -type f -name '*.json.gz' | sed 's|^\./||' | sort)
  flush
done

(cd "$wt" && git add -A && git -c user.name=crypto-monitor-bot -c user.email=crypto-monitor-bot@users.noreply.github.com commit -qm "rotate raw files $(date -u +%Y-%m-%d)")
if [ "$PUSH" = 1 ]; then
  (cd "$wt" && git push -q origin data-archive)
fi
done_ok=1
find "$ROOT" -type d -empty -delete
echo "rotated $moved files into data-archive"

#!/usr/bin/env bash
# Back up Gluster "Projects" tree to local /SANDIEGO-BAK/Projects using rsync,
# honouring each directory's .gitignore (plus .git/info/exclude where present).
#
# Uses hostname to pick the Gluster mount:
#   MARVIN -> /mnt/MARVIN-SANDIEGO/Projects
#   EDDIE  -> /mnt/EDDIE-SANDIEGO/Projects
#
# Usage (same command on both hosts):
#   ./scripts/backup_projects_sandiego_bak.sh              # live run
#   DRY_RUN=1 ./scripts/backup_projects_sandiego_bak.sh    # dry-run
#   DELETE=1 ./scripts/backup_projects_sandiego_bak.sh     # mirror (delete extras on backup)
#
# Requires rsync >= 3.2 (dir-merge). Limitations: rsync's gitignore parsing is close but not
# identical to git for exotic patterns (rare negation edge cases).
#
set -euo pipefail

HOSTSHORT="$(hostname -s)"
HOSTTAG="${HOSTSHORT^^}"
SOURCE="/mnt/${HOSTTAG}-SANDIEGO/Projects"
DEST_ROOT="${DEST_ROOT:-/SANDIEGO-BAK}"
DEST="${DEST:-${DEST_ROOT}/Projects}"

DRY_RUN="${DRY_RUN:-0}"
DELETE="${DELETE:-0}"

if [[ ! -d "$SOURCE" ]]; then
  echo "error: source missing: $SOURCE (hostname short: $HOSTSHORT / tag: $HOSTTAG)" >&2
  exit 1
fi

mkdir -p "$DEST"
if [[ ! -d "$DEST" ]]; then
  echo "error: cannot create or use destination: $DEST" >&2
  exit 1
fi

RSYNC_OPTS=(
  -aHAX
  --numeric-ids
  --human-readable
  --info=progress2
)

if [[ "$DRY_RUN" != "0" ]]; then
  RSYNC_OPTS+=(--dry-run)
fi

if [[ "$DELETE" != "0" ]]; then
  # Delay deletes until end of run (slightly safer than --delete during transfer).
  RSYNC_OPTS+=(--delete-delay --partial-dir=.rsync-partial)
fi

# Per-directory git excludes (gitignore + local excludes inside each git checkout).
FILTER=(
  --filter='dir-merge /.gitignore'
  --filter='dir-merge /.git/info/exclude'
)

echo "Host:     $HOSTTAG ($HOSTSHORT)"
echo "Source:   $SOURCE/"
echo "Dest:     $DEST/"
echo "Dry-run:  $DRY_RUN   Delete extras on dest: $DELETE"
echo ""

exec rsync "${RSYNC_OPTS[@]}" "${FILTER[@]}" "${SOURCE}/" "${DEST}/"

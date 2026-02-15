#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Stage A (Telegram context) candidate search.

Usage:
  stage_a.sh --chat-id <id> [--from-id <id>] [--kw <regex>] [--root <dir>] [--n <N>] [--out <dir>]

Options:
  --chat-id    Required. Telegram chat.id
  --from-id    Optional. Sender id (defaults to chat-id)
  --kw         Optional. Regex for extra narrowing (e.g. 'stage A|neighborhood.py')
  --root       Optional. Memories root (default: ~/memories/records)
  --n          Optional. Lines kept per route (default: 10)
  --out        Optional. Output dir (default: /tmp/tg_ctx)

Output:
  Prints a de-duped, path-sorted list of matches (newest at bottom).
  Also writes intermediate files: by_chat.txt, by_user.txt, by_kw.txt.

Exit behavior:
  If a route has no matches, it is treated as empty output (not a hard failure).
USAGE
}

CHAT_ID=""
FROM_ID=""
KW=""
ROOT="$HOME/memories/records"
N=7
OUT=/tmp/tg_ctx

while [[ $# -gt 0 ]]; do
  case "$1" in
    --chat-id) CHAT_ID="$2"; shift 2;;
    --from-id) FROM_ID="$2"; shift 2;;
    --kw) KW="$2"; shift 2;;
    --root) ROOT="$2"; shift 2;;
    --n) N="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$CHAT_ID" ]]; then
  echo "--chat-id is required" >&2
  usage
  exit 2
fi
if [[ -z "$FROM_ID" ]]; then
  FROM_ID="$CHAT_ID"
fi

RG_MATCH_FLAGS=(--no-line-number --with-filename)

mkdir -p "$OUT"

# Route A1: by chat.id (and kind=telegram)
KIND_RE='"kind"[[:space:]]*:[[:space:]]*"telegram"'
CHAT_RE='\\*"chat\\*"[[:space:]]*:[[:space:]]*\{[[:space:]]*\\*"id\\*"[[:space:]]*:[[:space:]]*'"${CHAT_ID}"
FROM_RE='\\*"from\\*"[[:space:]]*:[[:space:]]*\{[[:space:]]*\\*"id\\*"[[:space:]]*:[[:space:]]*'"${FROM_ID}"

{
  rg --null -l --sort path -g '*.core.json' -e "$KIND_RE" "$ROOT"     | xargs -0 -r rg "${RG_MATCH_FLAGS[@]}" --sort path -e "$CHAT_RE"     | tail -n "$N" > "$OUT/by_chat.txt"
} || true &

# Route A2: by from.id (and kind=telegram)
{
  rg --null -l --sort path -g '*.core.json' -e "$KIND_RE" "$ROOT"     | xargs -0 -r rg "${RG_MATCH_FLAGS[@]}" --sort path -e "$FROM_RE"     | tail -n "$N" > "$OUT/by_user.txt"
} || true &

# Route A3: keywords constrained to same chat (and kind=telegram)
if [[ -n "$KW" ]]; then
  {
    rg --null -l --sort path -g '*.core.json' -e "$KIND_RE" "$ROOT"       | xargs -0 -r rg --null -l --sort path -e "$CHAT_RE"       | xargs -0 -r rg "${RG_MATCH_FLAGS[@]}" --sort path "$KW"       | tail -n "$N" > "$OUT/by_kw.txt"
  } || true &
else
  : > "$OUT/by_kw.txt"
fi

# Route A4: Preferences
# Always load global preference, then chat-specific preference
KIND="telegram"
GLOBAL_PREF_FILE="$HOME/preferences/$KIND/preferences.md"
CHAT_PREF_FILE="$HOME/preferences/$KIND/by_chat/${CHAT_ID}.md"
USER_PREF_FILE="$HOME/preferences/$KIND/by_user/${FROM_ID}.md"

: > "$OUT/all_preferences.txt"

if [[ -f "$GLOBAL_PREF_FILE" ]]; then
  {
    echo "Global Preference ($KIND):"
    cat "$GLOBAL_PREF_FILE"
    echo "---"
  } >> "$OUT/all_preferences.txt"
fi

if [[ -f "$CHAT_PREF_FILE" ]]; then
  {
    echo "Chat-specific Preference (chat_id: $CHAT_ID):"
    cat "$CHAT_PREF_FILE"
    echo "---"
  } >> "$OUT/all_preferences.txt"
fi

if [[ -f "$USER_PREF_FILE" ]]; then
  {
    echo "User-specific Preference (from_id: $FROM_ID):"
    cat "$USER_PREF_FILE"
    echo "---"
  } >> "$OUT/all_preferences.txt"
fi

wait

# Output Combined Results
cat "$OUT/all_preferences.txt"

cat "$OUT/by_chat.txt" "$OUT/by_user.txt" "$OUT/by_kw.txt" \
  | awk -F: '!seen[$1]++' \
  | sort -t: -k1,1

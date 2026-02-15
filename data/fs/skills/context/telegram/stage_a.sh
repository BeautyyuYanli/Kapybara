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
  --kw         Optional. Regex for extra narrowing (e.g. 'stage A|retrieve-memory')
  --root       Optional. Memories root (default: ~/memories/records)
  --n          Optional. Lines kept per route (default: 10)
  --out        Optional. Output dir (default: /tmp/tg_ctx)

Output:
  Prints a de-duped, id-sorted list of matches (newest at bottom).
  Each match is returned as:
    <id>\troutes\t<matched_detailed_line>
  - `routes` is a comma-separated list of routes that matched (chat/user/kw)
  - `matched_detailed_line` is only populated when `--kw` is provided (the kw route)
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

core_to_detailed_files_for_kind() {
  # Start from core files (which contain metadata like kind) and map to detailed
  # files (which contain raw input and ModelResponse lines).
  rg --null -l --sort path -g '*.core.json' -e "$KIND_RE" "$ROOT" \
    | while IFS= read -r -d '' core; do
        detailed_jsonl="${core%.core.json}.detailed.jsonl"
        if [[ -f "$detailed_jsonl" ]]; then
          printf '%s\0' "$detailed_jsonl"
        fi
      done
}

emit_record_tsv() {
  # Emit a single TSV line that clearly ties core + detailed to the same memory id.
  #
  # Args:
  #   1) route: chat|user|kw
  #   2) detailed_path: full path to *.detailed.jsonl
  #   3) matched_line: optional (only for kw)
  local route="$1"
  local detailed_path="$2"
  local matched_line="${3:-}"

  [[ "$detailed_path" == *.detailed.jsonl ]] || return 0
  local core_path="${detailed_path%.detailed.jsonl}.core.json"
  [[ -f "$core_path" ]] || return 0

  local base
  base="$(basename "$core_path")"
  local id="${base%.core.json}"
  local core_json
  core_json="$(cat "$core_path")"

  if [[ -n "$matched_line" ]]; then
    printf '%s\t%s\t%s\t%s\n' "$id" "$route" "$matched_line"
  else
    printf '%s\t%s\t%s\n' "$id" "$route"
  fi
}

# Route A1: by chat.id (and kind=telegram)
KIND_RE='"kind"[[:space:]]*:[[:space:]]*"telegram"'
CHAT_RE='\\*"chat\\*"[[:space:]]*:[[:space:]]*\{[[:space:]]*\\*"id\\*"[[:space:]]*:[[:space:]]*'"${CHAT_ID}"
FROM_RE='\\*"from\\*"[[:space:]]*:[[:space:]]*\{[[:space:]]*\\*"id\\*"[[:space:]]*:[[:space:]]*'"${FROM_ID}"

{
  core_to_detailed_files_for_kind \
    | xargs -0 -r rg --null -l --sort path -e "$CHAT_RE" \
    | tr '\0' '\n' \
    | tail -n "$N" \
    | while IFS= read -r detailed_path; do
        [[ -n "$detailed_path" ]] || continue
        emit_record_tsv "chat" "$detailed_path"
      done \
    > "$OUT/by_chat.txt"
} || true &

# Route A2: by from.id (and kind=telegram)
{
  core_to_detailed_files_for_kind \
    | xargs -0 -r rg --null -l --sort path -e "$FROM_RE" \
    | tr '\0' '\n' \
    | tail -n "$N" \
    | while IFS= read -r detailed_path; do
        [[ -n "$detailed_path" ]] || continue
        emit_record_tsv "user" "$detailed_path"
      done \
    > "$OUT/by_user.txt"
} || true &

# Route A3: keywords constrained to same chat (and kind=telegram)
if [[ -n "$KW" ]]; then
  {
    core_to_detailed_files_for_kind \
      | xargs -0 -r rg --null -l --sort path -e "$CHAT_RE" \
      | xargs -0 -r rg "${RG_MATCH_FLAGS[@]}" --max-count 1 --sort path "$KW" \
      | tail -n "$N" \
      | while IFS= read -r line; do
          [[ -n "$line" ]] || continue
          detailed_path="${line%%:*}"
          matched="${line#*:}"
          emit_record_tsv "kw" "$detailed_path" "$matched"
        done \
      > "$OUT/by_kw.txt"
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

# Combined output: 1 line per memory id, with routes merged and kw match preserved when present.
echo -e "# id\troutes\tmatched_detailed_line"
cat "$OUT/by_chat.txt" "$OUT/by_user.txt" "$OUT/by_kw.txt" \
  | awk -F$'\t' '
    BEGIN { OFS = "\t" }
    NF >= 3 {
      id = $1
      route = $2
      core_json = $3
      matched = (NF >= 4 ? $4 : "")

      if (!(id in routes)) {
        routes[id] = route
        core_jsons[id] = core_json
        matches[id] = matched
      } else {
        if (index("," routes[id] ",", "," route ",") == 0) {
          routes[id] = routes[id] "," route
        }
        if (matched != "") {
          matches[id] = matched
        }
      }
    }
    END {
      for (id in core_jsons) {
        print id, routes[id], core_jsons[id], matches[id]
      }
    }
  ' \
  | sort -t$'\t' -k1,1

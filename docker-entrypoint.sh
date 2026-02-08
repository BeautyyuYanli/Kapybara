#!/usr/bin/env bash
set -euo pipefail

# This entrypoint aligns runtime identity with PUID/PGID (creating or updating
# passwd/group records as needed) and then runs supercronic in the foreground
# as that non-root numeric identity.

group_name_for_gid() {
  local gid="$1"
  getent group "${gid}" | cut -d: -f1 || true
}

user_name_for_uid() {
  local uid="$1"
  getent passwd "${uid}" | cut -d: -f1 || true
}

ensure_group_for_pgid() {
  local group_with_target_gid

  group_with_target_gid="$(group_name_for_gid "${PGID}")"

  if [[ -n "${group_with_target_gid}" ]]; then
    return
  fi

  groupadd --gid "${PGID}" "k"
}

ensure_user_for_puid() {
  local user_with_target_uid

  user_with_target_uid="$(user_name_for_uid "${PUID}")"

  if [[ -n "${user_with_target_uid}" ]]; then
    usermod \
      --gid "${PGID}" \
      --home "${WORKSPACE}" \
      --shell /bin/bash \
      "${user_with_target_uid}"
    return
  fi

  useradd \
    --uid "${PUID}" \
    --gid "${PGID}" \
    --home-dir "${WORKSPACE}" \
    --shell /bin/bash \
    --no-create-home \
    "k"
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: entrypoint must start as root to create runtime user/group." >&2
  echo "remove --user and pass -e PUID/-e PGID instead." >&2
  exit 1
fi

if [[ -z "${PUID:-}" || -z "${PGID:-}" ]]; then
  echo "error: PUID and PGID must be set when starting the container." >&2
  echo "example: docker run -e PUID=\$(id -u) -e PGID=\$(id -g) -v \$PWD:/home/k <image>" >&2
  exit 1
fi

if ! [[ "${PUID}" =~ ^[0-9]+$ && "${PGID}" =~ ^[0-9]+$ ]]; then
  echo "error: PUID and PGID must be numeric values." >&2
  exit 1
fi

if [[ "${PUID}" -eq 0 || "${PGID}" -eq 0 ]]; then
  echo "error: root uid/gid is not allowed. Use a non-root PUID/PGID." >&2
  exit 1
fi

export WORKSPACE="/home/k"
export HOME="/home/k"
export CRON_SCHEDULE_FILE="/home/k/.crontab"

if [[ ! -d "${WORKSPACE}" ]]; then
  echo "error: ${WORKSPACE} does not exist; mount your workspace at ${WORKSPACE}." >&2
  exit 1
fi

if ! awk -v mount_path="${WORKSPACE}" '$5 == mount_path {found=1} END {exit !found}' /proc/self/mountinfo; then
  echo "error: ${WORKSPACE} is not a mountpoint; start container with -v <host_path>:${WORKSPACE}." >&2
  exit 1
fi

ensure_group_for_pgid
ensure_user_for_puid

if [[ -e "${CRON_SCHEDULE_FILE}" && ! -f "${CRON_SCHEDULE_FILE}" ]]; then
  echo "error: ${CRON_SCHEDULE_FILE} exists but is not a regular file." >&2
  exit 1
fi

if [[ ! -f "${CRON_SCHEDULE_FILE}" ]]; then
  gosu "${PUID}:${PGID}" bash -c "printf '# managed by container\n' > \"${CRON_SCHEDULE_FILE}\""
fi

echo "supercronic: loading schedule file ${CRON_SCHEDULE_FILE}"
exec gosu "${PUID}:${PGID}" /usr/local/bin/supercronic "${CRON_SCHEDULE_FILE}"

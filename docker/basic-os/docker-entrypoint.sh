#!/usr/bin/env bash
set -euo pipefail

# This entrypoint aligns runtime identity with PUID/PGID (creating or updating
# passwd/group records as needed) and then hands off to Supervisor, which keeps
# long-running services (currently: sshd and cron) in the foreground. It also
# ensures SSH keys exist for both the SSH daemon (host keys) and the runtime
# user (~/.ssh/id_ed25519), authorizes that user key for SSH login, and repairs
# the Debian crontab spool when it is bind-mounted from the host so `crontab -l`
# works against the mounted files.

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
    echo "error: uid ${PUID} is already in use by user ${user_with_target_uid}." >&2
    exit 1
  fi

  useradd \
    --uid "${PUID}" \
    --gid "${PGID}" \
    --home-dir "${WORKSPACE}" \
    --shell /bin/bash \
    --no-create-home \
    "k"
}

ensure_user_ssh_keypair() {
  local ssh_dir private_key public_key
  ssh_dir="${HOME}/.ssh"
  private_key="${ssh_dir}/id_ed25519"
  public_key="${private_key}.pub"

  if [[ -e "${ssh_dir}" && ! -d "${ssh_dir}" ]]; then
    echo "error: ${ssh_dir} exists but is not a directory." >&2
    exit 1
  fi

  mkdir -p "${ssh_dir}"
  chown "${PUID}:${PGID}" "${ssh_dir}"
  chmod 0700 "${ssh_dir}"

  if [[ ! -f "${private_key}" && -f "${public_key}" ]]; then
    echo "error: found ${public_key} without ${private_key}; cannot reconstruct private key." >&2
    exit 1
  fi

  if [[ -f "${private_key}" && ! -f "${public_key}" ]]; then
    echo "error: found ${private_key} without ${public_key}; please restore or remove the incomplete keypair." >&2
    exit 1
  fi

  if [[ ! -f "${private_key}" ]]; then
    ssh-keygen -q -t ed25519 -N '' -f "${private_key}" -C "k@container"
  fi

  chown "${PUID}:${PGID}" "${private_key}" "${public_key}"
  chmod 0600 "${private_key}"
  chmod 0644 "${public_key}"
}

ensure_user_authorized_key() {
  local ssh_dir public_key authorized_keys
  ssh_dir="${HOME}/.ssh"
  public_key="${ssh_dir}/id_ed25519.pub"
  authorized_keys="${ssh_dir}/authorized_keys"

  if [[ -e "${authorized_keys}" && ! -f "${authorized_keys}" ]]; then
    echo "error: ${authorized_keys} exists but is not a regular file." >&2
    exit 1
  fi

  touch "${authorized_keys}"

  if ! grep -qxF "$(cat "${public_key}")" "${authorized_keys}"; then
    cat "${public_key}" >> "${authorized_keys}"
    echo >> "${authorized_keys}"
  fi

  chown "${PUID}:${PGID}" "${authorized_keys}"
  chmod 0600 "${authorized_keys}"
}

ensure_crontab_spool() {
  local spool_dir template
  spool_dir="${CRONTAB_SPOOL_DIR}"
  template="${CRONTAB_TEMPLATE_DIR}/root"

  if ! getent group crontab >/dev/null; then
    echo "error: missing crontab group; cron package is not installed correctly." >&2
    exit 1
  fi

  if [[ ! -f "${template}" ]]; then
    echo "error: missing root crontab template: ${template}" >&2
    exit 1
  fi

  mkdir -p "${spool_dir}"

  # Debian cron expects a very specific spool mode. Bind mounts inherit host
  # permissions, so repair the directory before using `crontab` to seed or read
  # mounted jobs.
  chown root:crontab "${spool_dir}"
  chmod 1730 "${spool_dir}"

  if [[ ! -f "${spool_dir}/root" ]]; then
    crontab -u root "${template}"
  else
    chown root:crontab "${spool_dir}/root"
    chmod 0600 "${spool_dir}/root"
  fi
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
export SUPERVISORD_CONFIG="/etc/supervisor/conf.d/supervisord.conf"
export CRONTAB_SPOOL_DIR="/var/spool/cron/crontabs"
export CRONTAB_TEMPLATE_DIR="/usr/local/share/kapybara/crontabs"

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
ensure_user_ssh_keypair
ensure_user_authorized_key
ensure_crontab_spool

if [[ ! -f "${SUPERVISORD_CONFIG}" ]]; then
  echo "error: missing supervisor config: ${SUPERVISORD_CONFIG}" >&2
  exit 1
fi

mkdir -p /run/sshd /var/run/sshd
ssh-keygen -A >/dev/null 2>&1

echo "supervisor: starting with ${SUPERVISORD_CONFIG}"
exec /usr/bin/supervisord -n -c "${SUPERVISORD_CONFIG}"

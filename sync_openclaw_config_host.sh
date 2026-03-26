#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_USER=openclaw
CONFIG_STAGING_PATH=/tmp/openclaw-config-staging
CONFIG_ROOT=
CONFIG_FILE=openclaw.json

usage() {
  cat <<'EOF'
Usage: sudo ./sync_openclaw_config_host.sh [options]

Install a synced OpenClaw config repo onto the host, repoint the service user to
that config, validate it, and refresh the user service if present.

Options:
  --openclaw-user <user>         Service user that owns and runs OpenClaw. Default: openclaw
  --config-staging-path <path>   Staging path already populated on the host. Default: /tmp/openclaw-config-staging
  --config-root <path>           Final config repo location on the host. Default: <service-home>/openclaw-config
  --config-file <path>           Config file inside the repo root. Default: openclaw.json
  -h, --help                     Show this help text.
EOF
}

log() {
  printf '[openclaw-config-sync] %s\n' "$*"
}

die() {
  printf '[openclaw-config-sync] ERROR: %s\n' "$*" >&2
  exit 1
}

systemctl_user() {
  systemctl --machine="${OPENCLAW_USER}@.host" --user "$@"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "run this script as root or via sudo"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --openclaw-user)
        OPENCLAW_USER="${2:-}"
        shift 2
        ;;
      --config-staging-path)
        CONFIG_STAGING_PATH="${2:-}"
        shift 2
        ;;
      --config-root)
        CONFIG_ROOT="${2:-}"
        shift 2
        ;;
      --config-file)
        CONFIG_FILE="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done

  [[ -n "${OPENCLAW_USER}" ]] || die "OpenClaw user is required"
  [[ -d "${CONFIG_STAGING_PATH}" ]] || die "Config staging path does not exist: ${CONFIG_STAGING_PATH}"
  [[ -n "${CONFIG_FILE}" ]] || die "Config file is required"
}

resolve_context() {
  USER_ENTRY=$(getent passwd "${OPENCLAW_USER}") || die "user does not exist: ${OPENCLAW_USER}"
  USER_HOME=$(cut -d: -f6 <<<"${USER_ENTRY}")
  USER_GROUP=$(id -gn "${OPENCLAW_USER}")

  if [[ -z "${CONFIG_ROOT}" ]]; then
    CONFIG_ROOT="${USER_HOME}/openclaw-config"
  fi

  CONFIG_TARGET_FILE="${CONFIG_ROOT}/${CONFIG_FILE}"
  OPENCLAW_HOME_DIR="${USER_HOME}/.openclaw"
  OPENCLAW_DEFAULT_CONFIG="${OPENCLAW_HOME_DIR}/openclaw.json"
  USER_ENV_DIR="${USER_HOME}/.config/environment.d"
  USER_ENV_FILE="${USER_ENV_DIR}/openclaw.conf"
  USER_SERVICE_DROPIN_DIR="${USER_HOME}/.config/systemd/user/openclaw-gateway.service.d"
  USER_SERVICE_DROPIN_FILE="${USER_SERVICE_DROPIN_DIR}/10-config-path.conf"
}

install_repo() {
  [[ -f "${CONFIG_STAGING_PATH}/${CONFIG_FILE}" ]] || {
    die "Config file ${CONFIG_FILE} was not found under staging path ${CONFIG_STAGING_PATH}"
  }

  local tmp_root
  tmp_root="${CONFIG_ROOT}.tmp.$$"
  rm -rf "${tmp_root}"
  mkdir -p "$(dirname "${CONFIG_ROOT}")"
  mkdir -p "${tmp_root}"
  cp -a "${CONFIG_STAGING_PATH}/." "${tmp_root}/"
  chown -R "${OPENCLAW_USER}:${USER_GROUP}" "${tmp_root}"
  rm -rf "${CONFIG_ROOT}"
  mv "${tmp_root}" "${CONFIG_ROOT}"
  chown -R "${OPENCLAW_USER}:${USER_GROUP}" "${CONFIG_ROOT}"
}

write_config_path_overrides() {
  install -d -o "${OPENCLAW_USER}" -g "${USER_GROUP}" -m 0755 "${OPENCLAW_HOME_DIR}"
  install -d -o "${OPENCLAW_USER}" -g "${USER_GROUP}" -m 0755 "${USER_ENV_DIR}"
  install -d -o "${OPENCLAW_USER}" -g "${USER_GROUP}" -m 0755 "${USER_SERVICE_DROPIN_DIR}"

  cat >"${USER_ENV_FILE}" <<EOF
OPENCLAW_CONFIG_PATH=${CONFIG_TARGET_FILE}
EOF

  cat >"${USER_SERVICE_DROPIN_FILE}" <<EOF
[Service]
Environment=OPENCLAW_CONFIG_PATH=${CONFIG_TARGET_FILE}
EOF

  ln -sfn "${CONFIG_TARGET_FILE}" "${OPENCLAW_DEFAULT_CONFIG}"
  chown -h "${OPENCLAW_USER}:${USER_GROUP}" "${OPENCLAW_DEFAULT_CONFIG}"
  chown "${OPENCLAW_USER}:${USER_GROUP}" "${USER_ENV_FILE}" "${USER_SERVICE_DROPIN_FILE}"
}

validate_config() {
  if ! command -v openclaw >/dev/null 2>&1; then
    die "openclaw CLI is not installed on the host"
  fi

  su - "${OPENCLAW_USER}" -c \
    "$(printf "OPENCLAW_CONFIG_PATH=%q openclaw config validate" "${CONFIG_TARGET_FILE}")"
}

refresh_gateway_service() {
  if ! systemctl_user list-unit-files | grep -q '^openclaw-gateway.service'; then
    log "gateway service is not installed yet; config path was updated without restarting anything"
    return
  fi

  systemctl_user daemon-reload

  if systemctl_user is-active --quiet openclaw-gateway.service; then
    systemctl_user restart openclaw-gateway.service
    log "restarted openclaw-gateway.service"
  else
    log "gateway service exists but is not active; config path was updated"
  fi
}

main() {
  require_root
  parse_args "$@"
  resolve_context
  install_repo
  write_config_path_overrides
  validate_config
  refresh_gateway_service
  log "config repo installed at ${CONFIG_ROOT}"
  log "active config path is ${CONFIG_TARGET_FILE}"
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

LOCAL_ROOT=
REMOTE_HOST=
REMOTE_ROOT=
SSH_USER=ubuntu
SSH_PORT=22
SSH_KEY_PATH=
CONFIG_FILE=openclaw.json
AUTO_APPROVE=0

usage() {
  cat <<'EOF'
Usage: ./pull_openclaw_config_local.sh [options]

Fetch a remote OpenClaw directory into a temporary local staging directory,
show a diff against the local directory, then optionally overwrite the local
directory with the remote contents.

Options:
  --local-root <path>       Local directory to overwrite after confirmation.
  --remote-host <host>      Remote SSH host or IP.
  --remote-root <path>      Remote directory to fetch.
  --ssh-user <user>         SSH user used to connect to the host. Default: ubuntu
  --ssh-port <port>         SSH port. Default: 22
  --ssh-key-path <path>     SSH private key path.
  --config-file <path>      Config file expected in both trees. Default: openclaw.json
  --yes                     Overwrite local files without prompting after diff.
  -h, --help                Show this help text.
EOF
}

log() {
  printf '[openclaw-config-pull] %s\n' "$*"
}

die() {
  printf '[openclaw-config-pull] ERROR: %s\n' "$*" >&2
  exit 1
}

expand_local_path() {
  case "$1" in
    "~")
      printf '%s\n' "${HOME}"
      ;;
    "~/"*)
      printf '%s\n' "${HOME}/${1#~/}"
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --local-root)
        LOCAL_ROOT="${2:-}"
        shift 2
        ;;
      --remote-host)
        REMOTE_HOST="${2:-}"
        shift 2
        ;;
      --remote-root)
        REMOTE_ROOT="${2:-}"
        shift 2
        ;;
      --ssh-user)
        SSH_USER="${2:-}"
        shift 2
        ;;
      --ssh-port)
        SSH_PORT="${2:-}"
        shift 2
        ;;
      --ssh-key-path)
        SSH_KEY_PATH="${2:-}"
        shift 2
        ;;
      --config-file)
        CONFIG_FILE="${2:-}"
        shift 2
        ;;
      --yes)
        AUTO_APPROVE=1
        shift
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

  LOCAL_ROOT=$(expand_local_path "${LOCAL_ROOT}")
  if [[ -n "${SSH_KEY_PATH}" ]]; then
    SSH_KEY_PATH=$(expand_local_path "${SSH_KEY_PATH}")
  fi

  [[ -n "${LOCAL_ROOT}" ]] || die "Local root is required"
  [[ -n "${REMOTE_HOST}" ]] || die "Remote host is required"
  [[ -n "${REMOTE_ROOT}" ]] || die "Remote root is required"
  [[ -d "${LOCAL_ROOT}" ]] || die "Local root does not exist: ${LOCAL_ROOT}"
  [[ -n "${CONFIG_FILE}" ]] || die "Config file is required"
}

build_rsync_rsh() {
  RSYNC_RSH="ssh -p ${SSH_PORT}"
  if [[ -n "${SSH_KEY_PATH}" ]]; then
    RSYNC_RSH="${RSYNC_RSH} -i ${SSH_KEY_PATH}"
  fi
}

fetch_remote_tree() {
  STAGING_DIR=$(mktemp -d /tmp/openclaw-config-pull.XXXXXX)
  trap 'rm -rf "${STAGING_DIR}"' EXIT

  REMOTE_STAGE_DIR="${STAGING_DIR}/remote"
  mkdir -p "${REMOTE_STAGE_DIR}"

  rsync -az --delete --no-owner --no-group \
    --exclude='.git' --exclude='.DS_Store' \
    --rsync-path="sudo rsync" \
    -e "${RSYNC_RSH}" \
    "${SSH_USER}@${REMOTE_HOST}:${REMOTE_ROOT}/" "${REMOTE_STAGE_DIR}/"

  [[ -f "${REMOTE_STAGE_DIR}/${CONFIG_FILE}" ]] || {
    die "Config file ${CONFIG_FILE} was not found in fetched remote tree"
  }
}

show_diff() {
  local diff_status

  if git --no-pager diff --no-index --no-ext-diff -- "${LOCAL_ROOT}" "${REMOTE_STAGE_DIR}"; then
    log "no differences found"
    return 1
  fi

  diff_status=$?
  if [[ "${diff_status}" -eq 1 ]]; then
    return 0
  fi

  die "failed to render diff"
}

confirm_sync() {
  [[ "${AUTO_APPROVE}" -eq 1 ]] && return 0

  local reply
  printf 'Overwrite %s with remote contents from %s:%s? [y/N] ' \
    "${LOCAL_ROOT}" "${REMOTE_HOST}" "${REMOTE_ROOT}"
  read -r reply || return 1
  [[ "${reply}" =~ ^[Yy]([Ee][Ss])?$ ]]
}

overwrite_local_tree() {
  rsync -az --delete --no-owner --no-group \
    --exclude='.git' --exclude='.DS_Store' \
    "${REMOTE_STAGE_DIR}/" "${LOCAL_ROOT}/"
}

main() {
  parse_args "$@"
  build_rsync_rsh
  fetch_remote_tree

  if ! show_diff; then
    exit 0
  fi

  if ! confirm_sync; then
    log "aborted without changing local files"
    exit 1
  fi

  overwrite_local_tree
  log "local directory updated from ${REMOTE_HOST}:${REMOTE_ROOT}"
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_USER=openclaw
OPENCLAW_PREFIX=
OPENCLAW_VERSION=latest
BOOTSTRAP_SCRIPT_PATH=/tmp/bootstrap_openclaw_host.sh
LEGACY_OPENCLAW_USER=ubuntu
REVIEW_ONLY=0

PASS_ITEMS=()
WARN_ITEMS=()
FIX_ITEMS=()
FAIL_ITEMS=()

usage() {
  cat <<'EOF'
Usage: sudo ./review_openclaw_host.sh [options]

Review the OpenClaw host setup, repair common fixable issues, then print a
summary of what was fixed and what still needs attention.

Options:
  --openclaw-user <user>          Dedicated service user that should own OpenClaw. Default: openclaw
  --openclaw-prefix <path>        Install prefix for OpenClaw. Default: <user-home>/.openclaw
  --openclaw-version <value>      Version or channel passed to bootstrap script. Default: latest
  --bootstrap-script-path <path>  Path to bootstrap_openclaw_host.sh on the host. Default: /tmp/bootstrap_openclaw_host.sh
  --review-only                   Do not apply fixes; report findings only.
  -h, --help                      Show this help text.
EOF
}

pass() {
  PASS_ITEMS+=("$*")
}

warn() {
  WARN_ITEMS+=("$*")
}

fix_item() {
  FIX_ITEMS+=("$*")
}

fail() {
  FAIL_ITEMS+=("$*")
}

systemctl_user() {
  local user_name=$1
  shift
  systemctl --machine="${user_name}@.host" --user "$@"
}

print_items() {
  local title=$1
  shift
  local items=("$@")
  printf '%s: %s\n' "$title" "${#items[@]}"
  local item
  for item in "${items[@]}"; do
    printf -- '- %s\n' "$item"
  done
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "[openclaw-review] ERROR: run this script as root or via sudo" >&2
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --openclaw-user)
        OPENCLAW_USER="${2:-}"
        shift 2
        ;;
      --openclaw-prefix)
        OPENCLAW_PREFIX="${2:-}"
        shift 2
        ;;
      --openclaw-version)
        OPENCLAW_VERSION="${2:-}"
        shift 2
        ;;
      --bootstrap-script-path)
        BOOTSTRAP_SCRIPT_PATH="${2:-}"
        shift 2
        ;;
      --review-only)
        REVIEW_ONLY=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "[openclaw-review] ERROR: unknown argument: $1" >&2
        exit 1
        ;;
    esac
  done

  [[ -n "${OPENCLAW_USER}" ]] || {
    echo "[openclaw-review] ERROR: OpenClaw user is required" >&2
    exit 1
  }
}

resolve_openclaw_context() {
  OPENCLAW_ENTRY=$(getent passwd "${OPENCLAW_USER}" || true)
  OPENCLAW_HOME=
  OPENCLAW_GROUP=
  if [[ -n "${OPENCLAW_ENTRY}" ]]; then
    OPENCLAW_HOME=$(cut -d: -f6 <<<"${OPENCLAW_ENTRY}")
    OPENCLAW_GROUP=$(id -gn "${OPENCLAW_USER}")
  fi

  if [[ -z "${OPENCLAW_PREFIX}" ]]; then
    if [[ -n "${OPENCLAW_HOME}" ]]; then
      OPENCLAW_PREFIX="${OPENCLAW_HOME}/.openclaw"
    else
      OPENCLAW_PREFIX="/home/${OPENCLAW_USER}/.openclaw"
    fi
  fi

  OPENCLAW_BIN="${OPENCLAW_PREFIX}/bin/openclaw"
}

reset_findings() {
  PASS_ITEMS=()
  WARN_ITEMS=()
  FIX_ITEMS=()
  FAIL_ITEMS=()
}

detect_issues() {
  local symlink_target linger_value legacy_entry legacy_home

  NEED_BOOTSTRAP=0
  NEED_LEGACY_CLEANUP=0

  if [[ -n "${OPENCLAW_ENTRY}" ]]; then
    pass "Service user ${OPENCLAW_USER} exists"
  else
    warn "Service user ${OPENCLAW_USER} does not exist"
    NEED_BOOTSTRAP=1
  fi

  if [[ -x "${OPENCLAW_BIN}" ]]; then
    pass "OpenClaw CLI exists at ${OPENCLAW_BIN}"
  else
    warn "OpenClaw CLI is missing at ${OPENCLAW_BIN}"
    NEED_BOOTSTRAP=1
  fi

  symlink_target=$(readlink -f /usr/local/bin/openclaw 2>/dev/null || true)
  if [[ "${symlink_target}" == "${OPENCLAW_BIN}" ]]; then
    pass "/usr/local/bin/openclaw points to ${OPENCLAW_BIN}"
  else
    warn "/usr/local/bin/openclaw does not point to ${OPENCLAW_BIN}"
    NEED_BOOTSTRAP=1
  fi

  if [[ -n "${OPENCLAW_ENTRY}" && -d "${OPENCLAW_PREFIX}" ]]; then
    if [[ "$(stat -c '%U:%G' "${OPENCLAW_PREFIX}")" == "${OPENCLAW_USER}:${OPENCLAW_GROUP}" ]]; then
      pass "OpenClaw prefix ownership matches ${OPENCLAW_USER}:${OPENCLAW_GROUP}"
    else
      warn "OpenClaw prefix ownership does not match ${OPENCLAW_USER}:${OPENCLAW_GROUP}"
      NEED_BOOTSTRAP=1
    fi
  fi

  linger_value=$(loginctl show-user "${OPENCLAW_USER}" -p Linger --value 2>/dev/null || true)
  if [[ "${linger_value}" == "yes" ]]; then
    pass "linger is enabled for ${OPENCLAW_USER}"
  else
    warn "linger is not enabled for ${OPENCLAW_USER}"
    NEED_BOOTSTRAP=1
  fi

  legacy_entry=$(getent passwd "${LEGACY_OPENCLAW_USER}" || true)
  if [[ -n "${legacy_entry}" && "${OPENCLAW_USER}" != "${LEGACY_OPENCLAW_USER}" ]]; then
    legacy_home=$(cut -d: -f6 <<<"${legacy_entry}")
    if [[ -d "${legacy_home}/.openclaw" ]]; then
      warn "Legacy OpenClaw install still exists under ${LEGACY_OPENCLAW_USER}"
      NEED_LEGACY_CLEANUP=1
    else
      pass "No legacy OpenClaw install remains under ${LEGACY_OPENCLAW_USER}"
    fi
  fi

  if [[ -x "${OPENCLAW_BIN}" ]]; then
    if su - "${OPENCLAW_USER}" -c "$(printf "%q --version" "${OPENCLAW_BIN}")" >/dev/null 2>&1; then
      pass "OpenClaw CLI runs as ${OPENCLAW_USER}"
    else
      warn "OpenClaw CLI does not run cleanly as ${OPENCLAW_USER}"
      NEED_BOOTSTRAP=1
    fi
  fi
}

cleanup_legacy_install() {
  local legacy_entry legacy_home legacy_cli

  legacy_entry=$(getent passwd "${LEGACY_OPENCLAW_USER}" || true)
  if [[ -z "${legacy_entry}" ]]; then
    return
  fi

  legacy_home=$(cut -d: -f6 <<<"${legacy_entry}")
  legacy_cli="${legacy_home}/.openclaw/bin/openclaw"

  if [[ -x "${legacy_cli}" ]]; then
    su - "${LEGACY_OPENCLAW_USER}" -c "$(printf "%q uninstall --all --yes --non-interactive || true" "${legacy_cli}")"
  fi

  systemctl_user "${LEGACY_OPENCLAW_USER}" disable --now openclaw-gateway.service >/dev/null 2>&1 || true
  rm -f "${legacy_home}/.config/systemd/user/openclaw-gateway.service"
  rm -f "${legacy_home}"/.config/systemd/user/openclaw-gateway-*.service
  systemctl_user "${LEGACY_OPENCLAW_USER}" daemon-reload >/dev/null 2>&1 || true

  if [[ "$(readlink -f /usr/local/bin/openclaw 2>/dev/null || true)" == "${legacy_cli}" ]]; then
    rm -f /usr/local/bin/openclaw
  fi

  rm -rf "${legacy_home}/.openclaw" "${legacy_home}"/.openclaw-* \
    "${legacy_home}/.config/openclaw" "${legacy_home}/.cache/openclaw" \
    "${legacy_home}/.local/state/openclaw"

  fix_item "Removed legacy OpenClaw install under ${LEGACY_OPENCLAW_USER}"
}

repair_if_needed() {
  if (( REVIEW_ONLY == 1 )); then
    return
  fi

  if (( NEED_LEGACY_CLEANUP == 1 )); then
    cleanup_legacy_install
  fi

  if (( NEED_BOOTSTRAP == 1 )); then
    [[ -x "${BOOTSTRAP_SCRIPT_PATH}" ]] || {
      fail "Bootstrap script not found or not executable: ${BOOTSTRAP_SCRIPT_PATH}"
      return
    }
    "${BOOTSTRAP_SCRIPT_PATH}" \
      --openclaw-user "${OPENCLAW_USER}" \
      --openclaw-prefix "${OPENCLAW_PREFIX}" \
      --openclaw-version "${OPENCLAW_VERSION}"
    fix_item "Re-ran bootstrap for ${OPENCLAW_USER} using ${BOOTSTRAP_SCRIPT_PATH}"
  fi
}

main() {
  require_root
  parse_args "$@"

  reset_findings
  resolve_openclaw_context
  detect_issues

  if (( NEED_BOOTSTRAP == 0 && NEED_LEGACY_CLEANUP == 0 )); then
    print_items "PASS" "${PASS_ITEMS[@]}"
    print_items "WARN" "${WARN_ITEMS[@]}"
    exit 0
  fi

  repair_if_needed

  local pre_fix_warnings=("${WARN_ITEMS[@]}")
  local fix_actions=("${FIX_ITEMS[@]}")
  local pre_fix_failures=("${FAIL_ITEMS[@]}")

  reset_findings
  resolve_openclaw_context
  detect_issues

  print_items "FIXED" "${fix_actions[@]}"
  print_items "PASS" "${PASS_ITEMS[@]}"
  print_items "WARN" "${WARN_ITEMS[@]}"
  print_items "FAIL" "${pre_fix_failures[@]}" "${FAIL_ITEMS[@]}"

  if [[ "${#pre_fix_warnings[@]}" -gt 0 ]]; then
    print_items "INITIAL WARNINGS" "${pre_fix_warnings[@]}"
  fi

  if [[ "${#FAIL_ITEMS[@]}" -gt 0 ]]; then
    exit 1
  fi
}

main "$@"

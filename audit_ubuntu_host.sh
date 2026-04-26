#!/usr/bin/env bash
set -euo pipefail

EXPECTED_OPEN_PORTS=()
VERBOSE=0

PASS_ITEMS=()
WARN_ITEMS=()
FAIL_ITEMS=()

usage() {
  cat <<'EOF'
Usage: sudo ./audit_ubuntu_host.sh [options]

Collect a read-only security and provisioning snapshot from an Ubuntu OCI host,
then print a summary of what looks good and what still needs attention.

Options:
  --expect-open-port <port>   TCP port expected to be publicly reachable on the host.
                              Repeatable. Defaults to the SSH daemon port only.
  --verbose                   Print the full raw command output sections.
  -h, --help                  Show this help text.
EOF
}

section() {
  printf '\n[%s]\n' "$1"
}

pass() {
  PASS_ITEMS+=("$*")
}

warn() {
  WARN_ITEMS+=("$*")
}

fail() {
  FAIL_ITEMS+=("$*")
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
    echo "[audit] ERROR: run this script as root or via sudo" >&2
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --expect-open-port)
        EXPECTED_OPEN_PORTS+=("${2:-}")
        shift 2
        ;;
      --verbose)
        VERBOSE=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "[audit] ERROR: unknown argument: $1" >&2
        exit 1
        ;;
    esac
  done

  local port
  for port in "${EXPECTED_OPEN_PORTS[@]+"${EXPECTED_OPEN_PORTS[@]}"}"; do
    [[ "${port}" =~ ^[0-9]+$ ]] || {
      echo "[audit] ERROR: expected port must be numeric: ${port}" >&2
      exit 1
    }
    (( port >= 1 && port <= 65535 )) || {
      echo "[audit] ERROR: expected port must be in 1..65535: ${port}" >&2
      exit 1
    }
  done
}

service_enabled() {
  systemctl is-enabled "$1" 2>/dev/null || true
}

service_active() {
  systemctl is-active "$1" 2>/dev/null || true
}

get_sshd_value() {
  awk -v key="$1" '$1 == key {print $2}' <<<"${SSHD_T_OUTPUT}" | tail -n1
}

has_expected_port() {
  local wanted=$1
  local port
  for port in "${EXPECTED_OPEN_PORTS[@]}"; do
    if [[ "${port}" == "${wanted}" ]]; then
      return 0
    fi
  done
  return 1
}

record_listener_findings() {
  local unexpected=0
  while IFS='|' read -r proto address port process; do
    [[ -n "${proto}" ]] || continue
    if has_expected_port "${port}"; then
      continue
    fi
    if [[ "${address}" == "127.0.0.53" && "${port}" == "53" ]]; then
      continue
    fi
    if [[ "${address}" == "<REDACTED-PRIVATE-IP>" && "${port}" == "68" ]]; then
      continue
    fi
    if [[ "${address}" == "127.0.0.1" || "${address}" == "::1" ]]; then
      continue
    fi
    warn "Unexpected listener: ${proto} ${address}:${port} (${process})"
    unexpected=1
  done <<<"${PUBLIC_LISTENERS}"

  if (( unexpected == 0 )); then
    pass "No unexpected non-local listeners were detected"
  fi
}

record_ufw_findings() {
  if grep -q '^Status: active' <<<"${UFW_STATUS}"; then
    pass "UFW is active"
  else
    fail "UFW is not active"
    return
  fi

  local unexpected_rules=0
  while IFS= read -r rule_port; do
    [[ -n "${rule_port}" ]] || continue
    if has_expected_port "${rule_port}"; then
      continue
    fi
    warn "UFW allows unexpected inbound TCP port ${rule_port}"
    unexpected_rules=1
  done < <(
    awk '/ALLOW IN/ && $1 ~ /\/tcp$/ {sub(/\/tcp$/, "", $1); print $1}' <<<"${UFW_STATUS}" | sort -u
  )

  if (( unexpected_rules == 0 )); then
    pass "UFW inbound rules match the expected TCP ports: ${EXPECTED_OPEN_PORTS[*]}"
  fi
}

record_service_findings() {
  [[ "${CLOUD_INIT_STATUS}" == *"status: done"* ]] \
    && pass "cloud-init completed successfully" \
    || warn "cloud-init is not reporting status: done"

  [[ "${FAIL2BAN_ACTIVE}" == "active" ]] \
    && pass "fail2ban is active" \
    || warn "fail2ban is not active"

  [[ "${UNATTENDED_ENABLED}" == "enabled" && "${UNATTENDED_ACTIVE}" == "active" ]] \
    && pass "unattended-upgrades is enabled and active" \
    || warn "unattended-upgrades is not fully enabled/active"

  [[ "${AUDITD_ACTIVE}" == "active" ]] \
    && pass "auditd is active" \
    || warn "auditd is not active"

  if [[ "${RPCBIND_ACTIVE}" == "active" || "${RPCBIND_SOCKET_ACTIVE}" == "active" ]]; then
    fail "rpcbind is active and should usually be disabled on an internet-facing VM"
  else
    pass "rpcbind is disabled"
  fi
}

record_sshd_findings() {
  [[ "${SSHD_PASSWORD_AUTH}" == "no" ]] \
    && pass "SSH password authentication is disabled" \
    || fail "SSH password authentication is enabled"

  [[ "${SSHD_KBDINT_AUTH}" == "no" ]] \
    && pass "SSH keyboard-interactive authentication is disabled" \
    || warn "SSH keyboard-interactive authentication is not disabled"

  [[ "${SSHD_ROOT_LOGIN}" == "no" ]] \
    && pass "SSH root login is disabled" \
    || fail "SSH root login is not disabled"

  [[ "${SSHD_PUBKEY_AUTH}" == "yes" ]] \
    && pass "SSH public-key authentication is enabled" \
    || fail "SSH public-key authentication is not enabled"

  [[ "${SSHD_X11}" == "no" ]] \
    && pass "SSH X11 forwarding is disabled" \
    || warn "SSH X11 forwarding is enabled"

  [[ "${SSHD_AGENT_FWD}" == "no" ]] \
    && pass "SSH agent forwarding is disabled" \
    || warn "SSH agent forwarding is enabled"

  if [[ "${SSHD_MAX_AUTH_TRIES}" =~ ^[0-9]+$ ]] && (( SSHD_MAX_AUTH_TRIES <= 3 )); then
    pass "SSH MaxAuthTries is ${SSHD_MAX_AUTH_TRIES}"
  else
    warn "SSH MaxAuthTries is ${SSHD_MAX_AUTH_TRIES:-unknown}"
  fi
}

record_provisioning_findings() {
  if [[ -f /etc/ssh/sshd_config.d/60-oci-toolbox-hardening.conf ]]; then
    pass "OCI toolbox SSH hardening drop-in is installed"
  else
    warn "OCI toolbox SSH hardening drop-in is missing"
  fi

  if [[ -f /etc/sysctl.d/60-oci-toolbox-hardening.conf ]]; then
    pass "OCI toolbox sysctl hardening file is installed"
  else
    warn "OCI toolbox sysctl hardening file is missing"
  fi
}

print_summary() {
  local overall="PASS"
  if ((${#FAIL_ITEMS[@]} > 0)); then
    overall="FAIL"
  elif ((${#WARN_ITEMS[@]} > 0)); then
    overall="WARN"
  fi

  section summary
  printf 'overall=%s\n' "${overall}"
  printf 'expected_open_tcp_ports=%s\n' "${EXPECTED_OPEN_PORTS[*]}"
  print_items "pass" "${PASS_ITEMS[@]}"
  print_items "warn" "${WARN_ITEMS[@]}"
  print_items "fail" "${FAIL_ITEMS[@]}"
}

capture_raw_state() {
  OS_PRETTY="$(lsb_release -ds 2>/dev/null || { . /etc/os-release && printf '%s %s\n' "${NAME}" "${VERSION}"; })"
  KERNEL_INFO="$(uname -a)"
  CLOUD_INIT_STATUS="$(cloud-init status 2>/dev/null || true)"

  SSH_ENABLED="$(service_enabled ssh)"
  SSH_ACTIVE="$(service_active ssh)"
  FAIL2BAN_ENABLED="$(service_enabled fail2ban)"
  FAIL2BAN_ACTIVE="$(service_active fail2ban)"
  UFW_ENABLED="$(service_enabled ufw)"
  UFW_ACTIVE="$(service_active ufw)"
  UNATTENDED_ENABLED="$(service_enabled unattended-upgrades)"
  UNATTENDED_ACTIVE="$(service_active unattended-upgrades)"
  AUDITD_ENABLED="$(service_enabled auditd)"
  AUDITD_ACTIVE="$(service_active auditd)"
  DOCKER_ENABLED="$(service_enabled docker)"
  DOCKER_ACTIVE="$(service_active docker)"
  RPCBIND_ENABLED="$(service_enabled rpcbind)"
  RPCBIND_ACTIVE="$(service_active rpcbind)"
  RPCBIND_SOCKET_ENABLED="$(service_enabled rpcbind.socket)"
  RPCBIND_SOCKET_ACTIVE="$(service_active rpcbind.socket)"

  UFW_STATUS="$(ufw status verbose || true)"
  FAIL2BAN_STATUS="$(fail2ban-client status sshd || true)"
  SSHD_T_OUTPUT="$(sshd -T 2>/dev/null || true)"
  LISTENERS="$(ss -H -tulpn 2>/dev/null || true)"
  UPDATES_OUTPUT="$(grep -R 'APT::Periodic\|Unattended-Upgrade::' /etc/apt/apt.conf.d 2>/dev/null || true)"
  SSH_DROPIN_OUTPUT="$(test -f /etc/ssh/sshd_config.d/60-oci-toolbox-hardening.conf && cat /etc/ssh/sshd_config.d/60-oci-toolbox-hardening.conf || true)"
  SYSCTL_OUTPUT="$(test -f /etc/sysctl.d/60-oci-toolbox-hardening.conf && cat /etc/sysctl.d/60-oci-toolbox-hardening.conf || true)"
  BOOTSTRAP_NOTES_OUTPUT="$(test -f /var/lib/oci-toolbox/bootstrap-notes.txt && cat /var/lib/oci-toolbox/bootstrap-notes.txt || true)"

  SSHD_PORT="$(get_sshd_value port)"
  SSHD_PASSWORD_AUTH="$(get_sshd_value passwordauthentication)"
  SSHD_KBDINT_AUTH="$(get_sshd_value kbdinteractiveauthentication)"
  SSHD_ROOT_LOGIN="$(get_sshd_value permitrootlogin)"
  SSHD_PUBKEY_AUTH="$(get_sshd_value pubkeyauthentication)"
  SSHD_X11="$(get_sshd_value x11forwarding)"
  SSHD_AGENT_FWD="$(get_sshd_value allowagentforwarding)"
  SSHD_MAX_AUTH_TRIES="$(get_sshd_value maxauthtries)"

  if ((${#EXPECTED_OPEN_PORTS[@]} == 0)); then
    if [[ -n "${SSHD_PORT}" ]]; then
      EXPECTED_OPEN_PORTS=("${SSHD_PORT}")
    else
      EXPECTED_OPEN_PORTS=("22")
    fi
  fi

  PUBLIC_LISTENERS="$(
    awk '
      $1 ~ /^(tcp|udp)$/ {
        split($5, parts, ":")
        port=parts[length(parts)]
        address=$5
        sub(/:[^:]+$/, "", address)
        gsub(/^\[|\]$/, "", address)
        if (address == "*" || address == "") {
          address="0.0.0.0"
        }
        if (address ~ /^(0\.0\.0\.0|\*)$/ || address == "::") {
          process=$NF
          print $1 "|" address "|" port "|" process
        }
      }
    ' <<<"${LISTENERS}"
  )"
}

print_raw_sections() {
  section os
  printf '%s\n' "${OS_PRETTY}"
  printf '%s\n' "${KERNEL_INFO}"

  section cloud_init
  printf '%s\n' "${CLOUD_INIT_STATUS}"

  section services
  printf 'ssh enabled=%s active=%s\n' "${SSH_ENABLED}" "${SSH_ACTIVE}"
  printf 'fail2ban enabled=%s active=%s\n' "${FAIL2BAN_ENABLED}" "${FAIL2BAN_ACTIVE}"
  printf 'ufw enabled=%s active=%s\n' "${UFW_ENABLED}" "${UFW_ACTIVE}"
  printf 'unattended-upgrades enabled=%s active=%s\n' "${UNATTENDED_ENABLED}" "${UNATTENDED_ACTIVE}"
  printf 'auditd enabled=%s active=%s\n' "${AUDITD_ENABLED}" "${AUDITD_ACTIVE}"
  printf 'docker enabled=%s active=%s\n' "${DOCKER_ENABLED}" "${DOCKER_ACTIVE}"
  printf 'rpcbind enabled=%s active=%s\n' "${RPCBIND_ENABLED}" "${RPCBIND_ACTIVE}"
  printf 'rpcbind.socket enabled=%s active=%s\n' "${RPCBIND_SOCKET_ENABLED}" "${RPCBIND_SOCKET_ACTIVE}"

  section ufw
  printf '%s\n' "${UFW_STATUS}"

  section fail2ban
  printf '%s\n' "${FAIL2BAN_STATUS}"

  section sshd_effective
  printf '%s\n' "${SSHD_T_OUTPUT}" | egrep '^(port|passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|permitrootlogin|pubkeyauthentication|x11forwarding|allowagentforwarding|allowtcpforwarding|maxauthtries|logingracetime|clientaliveinterval|clientalivecountmax) ' || true

  section listeners
  printf '%s\n' "${LISTENERS}"

  section unattended_upgrades
  printf '%s\n' "${UPDATES_OUTPUT}"

  section ssh_dropin
  printf '%s\n' "${SSH_DROPIN_OUTPUT}"

  section sysctl_hardening
  printf '%s\n' "${SYSCTL_OUTPUT}"

  section bootstrap_notes
  printf '%s\n' "${BOOTSTRAP_NOTES_OUTPUT}"
}

main() {
  parse_args "$@"
  require_root
  capture_raw_state
  record_service_findings
  record_sshd_findings
  record_ufw_findings
  record_listener_findings
  record_provisioning_findings
  print_summary
  if (( VERBOSE == 1 )); then
    print_raw_sections
  fi
}

main "$@"

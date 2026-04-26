#!/usr/bin/env bash
set -euo pipefail

SSH_PORT=22
ALLOW_PORTS=()
RUN_DIST_UPGRADE=1
INSTALL_AUDITD=1

usage() {
  cat <<'EOF'
Usage: sudo ./harden_ubuntu_host.sh [options]

Options:
  --ssh-port <port>       SSH port to keep open in UFW and fail2ban. Default: 22
  --allow-port <port>     Additional TCP port to allow through UFW. Repeatable.
  --skip-dist-upgrade     Skip apt-get dist-upgrade.
  --skip-auditd           Do not install and enable auditd.
  -h, --help              Show this help text.

Example:
  sudo ./harden_ubuntu_host.sh --ssh-port 22
EOF
}

log() {
  printf '[hardening] %s\n' "$*"
}

die() {
  printf '[hardening] ERROR: %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "run this script as root or via sudo"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ssh-port)
        SSH_PORT="${2:-}"
        shift 2
        ;;
      --allow-port)
        ALLOW_PORTS+=("${2:-}")
        shift 2
        ;;
      --skip-dist-upgrade)
        RUN_DIST_UPGRADE=0
        shift
        ;;
      --skip-auditd)
        INSTALL_AUDITD=0
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

  [[ "${SSH_PORT}" =~ ^[0-9]+$ ]] || die "SSH port must be numeric"
  (( SSH_PORT >= 1 && SSH_PORT <= 65535 )) || die "SSH port must be in 1..65535"

  local port
  for port in "${ALLOW_PORTS[@]+"${ALLOW_PORTS[@]}"}"; do
    [[ "${port}" =~ ^[0-9]+$ ]] || die "allowed port must be numeric: ${port}"
    (( port >= 1 && port <= 65535 )) || die "allowed port must be in 1..65535: ${port}"
  done
}

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  wait_for_cloud_init
  wait_for_apt_lock
  apt-get update -o Acquire::Retries=5
  apt-get install -y \
    -o Acquire::Retries=5 \
    unattended-upgrades \
    apt-listchanges \
    fail2ban \
    ufw \
    curl \
    ca-certificates \
    jq \
    vim-tiny
  if (( INSTALL_AUDITD == 1 )); then
    apt-get install -y -o Acquire::Retries=5 auditd
  fi
  if (( RUN_DIST_UPGRADE == 1 )); then
    apt-get dist-upgrade -y -o Acquire::Retries=5
  fi
  apt-get autoremove -y
}

wait_for_cloud_init() {
  if command -v cloud-init >/dev/null 2>&1; then
    log "waiting for cloud-init to finish"
    cloud-init status --wait >/dev/null 2>&1 || true
  fi
}

wait_for_apt_lock() {
  local waited=0
  local timeout=600
  local lock_files=(
    /var/lib/dpkg/lock-frontend
    /var/lib/dpkg/lock
    /var/lib/apt/lists/lock
    /var/cache/apt/archives/lock
  )

  while apt_lock_is_held "${lock_files[@]}" || apt_systemd_units_active; do
    if (( waited == 0 )); then
      log "waiting for apt/dpkg lock to clear"
    fi
    if (( waited >= timeout )); then
      log "apt/dpkg still appears busy after ${timeout}s"
      print_apt_lock_diagnostics "${lock_files[@]}"
      die "timed out waiting for apt/dpkg lock"
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

apt_lock_is_held() {
  local lock_file
  for lock_file in "$@"; do
    if [[ -e "${lock_file}" ]] && fuser "${lock_file}" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

apt_systemd_units_active() {
  if ! command -v systemctl >/dev/null 2>&1; then
    return 1
  fi

  local unit
  for unit in apt-daily.service apt-daily-upgrade.service; do
    if systemctl is-active --quiet "${unit}"; then
      return 0
    fi
  done
  return 1
}

print_apt_lock_diagnostics() {
  local lock_file
  for lock_file in "$@"; do
    if [[ -e "${lock_file}" ]]; then
      log "diagnostic: holders for ${lock_file}"
      fuser -v "${lock_file}" || true
      lsof "${lock_file}" || true
    fi
  done

  if command -v systemctl >/dev/null 2>&1; then
    log "diagnostic: apt systemd units"
    systemctl --no-pager --full status apt-daily.service apt-daily-upgrade.service || true
  fi
}

write_sshd_dropin() {
  install -d -m 0755 /etc/ssh/sshd_config.d
  cat >/etc/ssh/sshd_config.d/60-oci-toolbox-hardening.conf <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding yes
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF

  if sshd -t; then
    systemctl reload ssh || systemctl reload sshd
  else
    die "sshd configuration test failed; refusing to reload SSH"
  fi
}

write_unattended_upgrades() {
  cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

  cat >/etc/apt/apt.conf.d/52oci-toolbox-unattended-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF

  systemctl enable --now unattended-upgrades
}

write_fail2ban() {
  install -d -m 0755 /etc/fail2ban/jail.d
  cat >/etc/fail2ban/jail.d/oci-toolbox.local <<EOF
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port = ${SSH_PORT}
backend = systemd
EOF

  systemctl enable --now fail2ban
  systemctl restart fail2ban
}

configure_ufw() {
  ufw --force reset
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow "${SSH_PORT}/tcp" comment 'ssh'

  local port
  for port in "${ALLOW_PORTS[@]+"${ALLOW_PORTS[@]}"}"; do
    ufw allow "${port}/tcp"
  done

  ufw --force enable
}

write_sysctl_hardening() {
  cat >/etc/sysctl.d/60-oci-toolbox-hardening.conf <<'EOF'
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.conf.default.log_martians = 1
net.ipv4.tcp_syncookies = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
EOF

  sysctl --system >/dev/null
}

disable_core_dumps() {
  cat >/etc/security/limits.d/99-oci-toolbox-hardening.conf <<'EOF'
* hard core 0
* soft core 0
EOF
}

enable_auditd_if_requested() {
  if (( INSTALL_AUDITD == 1 )); then
    systemctl enable --now auditd
  fi
}

disable_unneeded_services() {
  if systemctl list-unit-files rpcbind.service >/dev/null 2>&1; then
    systemctl disable --now rpcbind rpcbind.socket || true
  fi
}

print_summary() {
  log "hardening complete"
  log "ssh port: ${SSH_PORT}"
  if ((${#ALLOW_PORTS[@]} > 0)); then
    log "additional allowed ports: ${ALLOW_PORTS[*]}"
  else
    log "additional allowed ports: none"
  fi
  log "ufw status:"
  ufw status verbose || true
  log "fail2ban status:"
  fail2ban-client status sshd || true
}

main() {
  parse_args "$@"
  require_root

  log "installing packages"
  apt_install

  log "configuring SSH hardening"
  write_sshd_dropin

  log "configuring unattended upgrades"
  write_unattended_upgrades

  log "configuring fail2ban"
  write_fail2ban

  log "configuring firewall"
  configure_ufw

  log "applying sysctl hardening"
  write_sysctl_hardening

  log "disabling core dumps"
  disable_core_dumps

  log "enabling auditd"
  enable_auditd_if_requested

  log "disabling unneeded services"
  disable_unneeded_services

  print_summary
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_USER=openclaw
OPENCLAW_PREFIX=
OPENCLAW_VERSION=latest
HOMEBREW_PREFIX=

usage() {
  cat <<'EOF'
Usage: sudo ./bootstrap_openclaw_host.sh [options]

Options:
  --openclaw-user <user>       User that should own and run OpenClaw. Default: openclaw
  --openclaw-prefix <path>     Install prefix for OpenClaw. Default: <user-home>/.openclaw
  --openclaw-version <value>   Version or channel passed to install-cli.sh. Default: latest
  -h, --help                   Show this help text.
EOF
}

log() {
  printf '[openclaw-bootstrap] %s\n' "$*"
}

die() {
  printf '[openclaw-bootstrap] ERROR: %s\n' "$*" >&2
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
  [[ -n "${OPENCLAW_VERSION}" ]] || die "OpenClaw version is required"
}

ensure_service_user() {
  if getent passwd "${OPENCLAW_USER}" >/dev/null 2>&1; then
    return
  fi

  log "creating service user ${OPENCLAW_USER}"
  useradd --create-home --shell /bin/bash --user-group "${OPENCLAW_USER}"
}

resolve_user_context() {
  ensure_service_user
  USER_ENTRY=$(getent passwd "${OPENCLAW_USER}") || die "user does not exist: ${OPENCLAW_USER}"
  USER_HOME=$(cut -d: -f6 <<<"${USER_ENTRY}")
  USER_GROUP=$(id -gn "${OPENCLAW_USER}")

  if [[ -z "${OPENCLAW_PREFIX}" ]]; then
    OPENCLAW_PREFIX="${USER_HOME}/.openclaw"
  fi

  HOMEBREW_PREFIX="${USER_HOME}/.linuxbrew"
  OPENCLAW_BIN="${OPENCLAW_PREFIX}/bin/openclaw"
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -o Acquire::Retries=5
  apt-get install -y \
    -o Acquire::Retries=5 \
    build-essential \
    ca-certificates \
    curl \
    file \
    git \
    jq \
    procps \
    unzip \
    python3 \
    python3-pip
}

install_homebrew() {
  if [[ -x "${HOMEBREW_PREFIX}/bin/brew" ]]; then
    return
  fi

  log "installing Linuxbrew for ${OPENCLAW_USER}"
  su - "${OPENCLAW_USER}" -c \
    'NONINTERACTIVE=1 CI=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'

  [[ -x "${HOMEBREW_PREFIX}/bin/brew" ]] || die "Homebrew installation did not produce ${HOMEBREW_PREFIX}/bin/brew"
}

write_homebrew_shell_env() {
  local env_snippet profile_file
  env_snippet="${USER_HOME}/.config/openclaw-homebrew.sh"
  profile_file="${USER_HOME}/.profile"

  install -d -o "${OPENCLAW_USER}" -g "${USER_GROUP}" -m 0755 "${USER_HOME}/.config"
  cat >"${env_snippet}" <<EOF
export HOMEBREW_PREFIX="${HOMEBREW_PREFIX}"
export HOMEBREW_CELLAR="${HOMEBREW_PREFIX}/Cellar"
export HOMEBREW_REPOSITORY="${HOMEBREW_PREFIX}/Homebrew"
export PATH="${HOMEBREW_PREFIX}/bin:${HOMEBREW_PREFIX}/sbin:\$PATH"
export MANPATH="${HOMEBREW_PREFIX}/share/man:\${MANPATH:-}"
export INFOPATH="${HOMEBREW_PREFIX}/share/info:\${INFOPATH:-}"
EOF
  chown "${OPENCLAW_USER}:${USER_GROUP}" "${env_snippet}"
  chmod 0644 "${env_snippet}"

  if [[ ! -f "${profile_file}" ]] || ! grep -Fq '.config/openclaw-homebrew.sh' "${profile_file}"; then
    cat >>"${profile_file}" <<'EOF'
if [ -f "$HOME/.config/openclaw-homebrew.sh" ]; then
  . "$HOME/.config/openclaw-homebrew.sh"
fi
EOF
    chown "${OPENCLAW_USER}:${USER_GROUP}" "${profile_file}"
  fi
}

write_homebrew_systemd_env() {
  local env_dir env_file
  env_dir="${USER_HOME}/.config/environment.d"
  env_file="${env_dir}/10-homebrew.conf"

  install -d -o "${OPENCLAW_USER}" -g "${USER_GROUP}" -m 0755 "${env_dir}"
  cat >"${env_file}" <<EOF
HOMEBREW_PREFIX=${HOMEBREW_PREFIX}
HOMEBREW_CELLAR=${HOMEBREW_PREFIX}/Cellar
HOMEBREW_REPOSITORY=${HOMEBREW_PREFIX}/Homebrew
PATH=${HOMEBREW_PREFIX}/bin:${HOMEBREW_PREFIX}/sbin:${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
MANPATH=${HOMEBREW_PREFIX}/share/man:/usr/share/man
INFOPATH=${HOMEBREW_PREFIX}/share/info:/usr/share/info
EOF
  chown "${OPENCLAW_USER}:${USER_GROUP}" "${env_file}"
  chmod 0644 "${env_file}"

  su - "${OPENCLAW_USER}" -c 'systemctl --user daemon-reexec >/dev/null 2>&1 || true'
}

install_openclaw() {
  install -d -o "${OPENCLAW_USER}" -g "${USER_GROUP}" -m 0755 "${OPENCLAW_PREFIX}"
  loginctl enable-linger "${OPENCLAW_USER}" || true

  local install_command
  install_command=$(
    printf "curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh | bash -s -- --prefix %q --version %q" \
      "${OPENCLAW_PREFIX}" \
      "${OPENCLAW_VERSION}"
  )

  su - "${OPENCLAW_USER}" -c "${install_command}"
  ln -sf "${OPENCLAW_BIN}" /usr/local/bin/openclaw
  chown -R "${OPENCLAW_USER}:${USER_GROUP}" "${OPENCLAW_PREFIX}"
  su - "${OPENCLAW_USER}" -c "$(printf "%q doctor --non-interactive || true" "${OPENCLAW_BIN}")"
}

main() {
  require_root
  parse_args "$@"
  resolve_user_context

  log "installing OpenClaw for ${OPENCLAW_USER} into ${OPENCLAW_PREFIX}"
  install_packages
  install_homebrew
  write_homebrew_shell_env
  write_homebrew_systemd_env
  install_openclaw
  log "bootstrap complete"
}

main "$@"

#!/usr/bin/env bash
# MegaPBX -> Telegram installer for Debian/Ubuntu.
# The script deliberately copies an explicit manifest and never copies .env,
# virtual environments, session files or historical backups from the source.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_NAME="megapbx-tg installer"
REPO_URL="${MEGAPBX_TG_REPO_URL:-https://github.com/IndeecDen/megapbx-tg}"
RELEASE_TAG="${MEGAPBX_TG_RELEASE_TAG:-megapbx-tg-v0.1.1}"
INSTALL_ROOT="${MEGAPBX_TG_INSTALL_DIR:-/opt/megapbx-tg}"
SERVICE_NAME="${MEGAPBX_TG_SERVICE_NAME:-megapbx-tg}"
SERVICE_USER="${MEGAPBX_TG_SERVICE_USER:-megapbx}"
CONFIG_FILE="${MEGAPBX_TG_CONFIG_FILE:-/etc/megapbx-tg.env}"
BACKUP_ROOT="${MEGAPBX_TG_BACKUP_DIR:-/var/backups/megapbx-tg}"
BACKEND_HOST="127.0.0.1"
BIND_HOST="$BACKEND_HOST"
BACKEND_PORT="${MEGAPBX_TG_BACKEND_PORT:-8000}"
PUBLIC_BIND_HOST="${MEGAPBX_TG_PUBLIC_BIND:-127.0.0.1}"
PYTHON_BIN="${MEGAPBX_TG_PYTHON:-}"
PIP_INDEX_URL="${MEGAPBX_TG_PIP_INDEX_URL:-https://pypi.org/simple}"
SOURCE_DIR="${MEGAPBX_TG_SOURCE_DIR:-}"
ENV_FILE=""
DOMAIN="${MEGAPBX_TG_DOMAIN:-}"
TLS_EMAIL="${MEGAPBX_TG_TLS_EMAIL:-}"
INSTALL_NGINX="${MEGAPBX_TG_INSTALL_NGINX:-0}"
ENABLE_TLS="${MEGAPBX_TG_ENABLE_TLS:-0}"
ALLOW_ALL_DESTINATIONS="${MEGAPBX_TG_ALLOW_ALL_DESTINATIONS:-0}"
ALLOW_HTTP_API="${MEGAPBX_TG_ALLOW_HTTP_API:-0}"
NON_INTERACTIVE=0
DRY_RUN=0
NO_START=0
REPLACE_CONFIG=0
REPLACE_NGINX=0
ASSUME_YES=0
TRANSACTION_ACTIVE=0
STAGE_DIR=""
FINAL_DIR=""
TX_DIR=""
CURRENT_TARGET=""
SERVICE_WAS_ACTIVE=0
SERVICE_WAS_ENABLED=0
NGINX_WAS_ACTIVE=0
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
NGINX_LINK="/etc/nginx/sites-enabled/${SERVICE_NAME}"

log() {
    printf '[%s] %s\n' "$SCRIPT_NAME" "$*"
}

warn() {
    printf '[%s] WARNING: %s\n' "$SCRIPT_NAME" "$*" >&2
}

die() {
    printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: sudo bash install.sh [options]

Options:
  --version TAG              pinned release tag (default: megapbx-tg-v0.1.1)
  --env-file FILE            load a simple KEY=VALUE file without executing it
  --non-interactive          never prompt; all required values must be supplied
  --with-nginx               install/configure Nginx reverse proxy
  --no-nginx                 do not install/configure Nginx (default)
  --domain DOMAIN            Nginx server_name
  --enable-tls               enable Let's Encrypt certificate with --with-nginx
  --tls-email EMAIL          Let's Encrypt registration email
  --replace-config           replace /etc/megapbx-tg.env after a backup
  --replace-nginx            replace an existing Nginx site after a backup
  --no-start                 install and enable, but do not start the service
  --dry-run                  validate and show the plan without changing the system
  --yes                      accept the displayed installation plan
  -h, --help                 show this help

For a first interactive installation, run:
  sudo bash install.sh

For unattended installation, provide values through a protected env file:
  sudo bash install.sh --non-interactive --env-file /root/megapbx-tg.env --no-nginx
EOF
}

is_yes() {
    case "${1,,}" in
        1|y|yes|true|on) return 0 ;;
        *) return 1 ;;
    esac
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "Run this installer as root (for example: sudo bash install.sh)"
}

check_os() {
    [[ -r /etc/os-release ]] || die "Cannot detect the operating system"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        debian|ubuntu) ;;
        *) die "Only Debian and Ubuntu are supported (detected: ${ID:-unknown})" ;;
    esac
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    [[ -d /run/systemd/system ]] || die "The system must be booted with systemd"
    command -v curl >/dev/null 2>&1 || die "curl is required to download the release"
    command -v apt-get >/dev/null 2>&1 || die "apt-get is required"
}

check_tty() {
    if (( NON_INTERACTIVE == 0 )) && [[ ! -t 0 || ! -t 1 ]]; then
        die "Interactive installation needs a TTY. Use --non-interactive --env-file FILE for automation."
    fi
}

acquire_lock() {
    command -v flock >/dev/null 2>&1 || die "flock is required (install util-linux)"
    mkdir -p /run/lock
    exec 9>/run/lock/megapbx-tg-install.lock
    flock -n 9 || die "Another megapbx-tg installation is already running"
}

parse_args() {
    while (($#)); do
        case "$1" in
            --version)
                [[ $# -ge 2 ]] || die "--version requires a tag"
                RELEASE_TAG="$2"
                shift 2
                ;;
            --env-file)
                [[ $# -ge 2 ]] || die "--env-file requires a path"
                ENV_FILE="$2"
                shift 2
                ;;
            --domain)
                [[ $# -ge 2 ]] || die "--domain requires a hostname"
                DOMAIN="$2"
                shift 2
                ;;
            --tls-email)
                [[ $# -ge 2 ]] || die "--tls-email requires an email"
                TLS_EMAIL="$2"
                shift 2
                ;;
            --non-interactive|--unattended)
                NON_INTERACTIVE=1
                shift
                ;;
            --with-nginx)
                INSTALL_NGINX=1
                shift
                ;;
            --no-nginx)
                INSTALL_NGINX=0
                shift
                ;;
            --enable-tls)
                ENABLE_TLS=1
                shift
                ;;
            --replace-config)
                REPLACE_CONFIG=1
                shift
                ;;
            --replace-nginx)
                REPLACE_NGINX=1
                shift
                ;;
            --no-start)
                NO_START=1
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --yes)
                ASSUME_YES=1
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown option: $1 (use --help)"
                ;;
        esac
    done
}

# Reads only simple KEY=VALUE lines. It intentionally does not source or eval files.
load_env_file() {
    local file="$1"
    [[ -f "$file" ]] || die "Environment file not found: $file"
    [[ -r "$file" ]] || die "Environment file is not readable: $file"

    local line key raw
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "${line//[[:space:]]/}" ]] && continue
        [[ "${line:0:1}" == "#" ]] && continue
        if [[ ! "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            die "Unsupported line in $file; only KEY=VALUE is allowed"
        fi
        key="${BASH_REMATCH[1]}"
        raw="${BASH_REMATCH[2]}"
        case "$key" in
            TG_BOT_TOKEN|TG_CHAT_ID|TG_DELETE_WEBHOOK_ON_START|TG_API_MAX_RETRIES| \
            TG_API_RETRY_BASE_SEC|TG_API_RETRY_MAX_SEC|MAX_WEBHOOK_BODY_BYTES| \
            MEGAPBX_CRM_TOKEN|MEGAPBX_ALLOWED_GROUP|MEGAPBX_ALLOWED_DID| \
            MEGAPBX_DID_NAMES|MEGAPBX_API_BASE|MEGAPBX_API_TOKEN| \
            MEGAPBX_ALLOW_QUERY_TOKEN|TZ_OFFSET_HOURS|MISSED_MAX_AGE_SEC| \
            MISSED_CLEANUP_INTERVAL_SEC|MISSED_DEDUP_TTL_SEC| \
            MEGAPBX_TG_RELEASE_TAG|MEGAPBX_TG_INSTALL_DIR|MEGAPBX_TG_SERVICE_NAME| \
            MEGAPBX_TG_SERVICE_USER|MEGAPBX_TG_CONFIG_FILE|MEGAPBX_TG_BACKUP_DIR| \
            MEGAPBX_TG_BACKEND_PORT|MEGAPBX_TG_PUBLIC_BIND|MEGAPBX_TG_PYTHON| \
            MEGAPBX_TG_PIP_INDEX_URL|MEGAPBX_TG_SOURCE_DIR|MEGAPBX_TG_DOMAIN| \
            MEGAPBX_TG_TLS_EMAIL|MEGAPBX_TG_INSTALL_NGINX|MEGAPBX_TG_ENABLE_TLS| \
            MEGAPBX_TG_ALLOW_ALL_DESTINATIONS|MEGAPBX_TG_ALLOW_HTTP_API) ;;
            *) die "Unsupported key '$key' in $file" ;;
        esac

        if (( ${#raw} >= 2 )) && [[ "${raw:0:1}" == '"' && "${raw: -1}" == '"' ]]; then
            raw="${raw:1:${#raw}-2}"
            raw="${raw//\\\\/\\}"
            raw="${raw//\\\"/\"}"
            raw="${raw//\\$/\$}"
        elif (( ${#raw} >= 2 )) && [[ "${raw:0:1}" == "'" && "${raw: -1}" == "'" ]]; then
            raw="${raw:1:${#raw}-2}"
        fi
        [[ "$raw" != *$'\n'* && "$raw" != *$'\r'* ]] || die "Multiline value for $key is not supported"
        printf -v "$key" '%s' "$raw"
    done < "$file"
}

set_defaults() {
    TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
    TG_CHAT_ID="${TG_CHAT_ID:-}"
    TG_DELETE_WEBHOOK_ON_START="${TG_DELETE_WEBHOOK_ON_START:-1}"
    TG_API_MAX_RETRIES="${TG_API_MAX_RETRIES:-2}"
    TG_API_RETRY_BASE_SEC="${TG_API_RETRY_BASE_SEC:-0.5}"
    TG_API_RETRY_MAX_SEC="${TG_API_RETRY_MAX_SEC:-8}"
    MAX_WEBHOOK_BODY_BYTES="${MAX_WEBHOOK_BODY_BYTES:-1048576}"
    MEGAPBX_CRM_TOKEN="${MEGAPBX_CRM_TOKEN:-}"
    MEGAPBX_ALLOWED_GROUP="${MEGAPBX_ALLOWED_GROUP:-}"
    MEGAPBX_ALLOWED_DID="${MEGAPBX_ALLOWED_DID:-}"
    MEGAPBX_DID_NAMES="${MEGAPBX_DID_NAMES:-}"
    MEGAPBX_API_BASE="${MEGAPBX_API_BASE:-}"
    MEGAPBX_API_TOKEN="${MEGAPBX_API_TOKEN:-}"
    TZ_OFFSET_HOURS="${TZ_OFFSET_HOURS:-3}"
    MISSED_MAX_AGE_SEC="${MISSED_MAX_AGE_SEC:-3600}"
    MISSED_CLEANUP_INTERVAL_SEC="${MISSED_CLEANUP_INTERVAL_SEC:-3600}"
    MISSED_DEDUP_TTL_SEC="${MISSED_DEDUP_TTL_SEC:-86400}"
}

read_secret() {
    local var_name="$1"
    local prompt="$2"
    local value="${!var_name-}"
    local confirmation
    if [[ -z "$value" ]]; then
        (( NON_INTERACTIVE == 0 )) || die "$var_name is required in non-interactive mode"
        read -r -s -p "$prompt" value
        printf '\n'
        [[ -n "$value" ]] || die "$var_name cannot be empty"
        read -r -s -p "Repeat $var_name: " confirmation
        printf '\n'
        [[ "$value" == "$confirmation" ]] || die "$var_name values do not match"
    fi
    printf -v "$var_name" '%s' "$value"
}

read_value() {
    local var_name="$1"
    local prompt="$2"
    local default="${3:-}"
    local value="${!var_name-}"
    if [[ -z "$value" ]]; then
        value="$default"
    fi
    if [[ -z "$value" ]]; then
        (( NON_INTERACTIVE == 0 )) || die "$var_name is required in non-interactive mode"
        read -r -p "$prompt" value
    fi
    printf -v "$var_name" '%s' "$value"
}

read_optional() {
    local var_name="$1"
    local prompt="$2"
    local default="${3:-}"
    local value="${!var_name-}"
    [[ -n "$value" ]] || value="$default"
    if (( NON_INTERACTIVE == 0 )) && [[ -z "${!var_name-}" ]]; then
        read -r -p "$prompt" value
    fi
    printf -v "$var_name" '%s' "$value"
}

ask_yes_no() {
    local prompt="$1"
    local default="${2:-y}"
    local answer="$default"
    if (( NON_INTERACTIVE == 0 )); then
        read -r -p "$prompt [y/n]: " answer || true
        [[ -n "$answer" ]] || answer="$default"
    fi
    is_yes "$answer"
}

validate_scalar_values() {
    [[ "$RELEASE_TAG" =~ ^[A-Za-z0-9._-]+$ ]] || die "Invalid release tag"
    [[ "$SERVICE_NAME" =~ ^[a-zA-Z0-9_.-]+$ ]] || die "Invalid service name"
    [[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "Invalid service user"
    [[ "$INSTALL_ROOT" == /* && "$INSTALL_ROOT" != *$'\n'* ]] || die "Install directory must be an absolute path"
    [[ "$CONFIG_FILE" == /* && "$CONFIG_FILE" != *$'\n'* ]] || die "Config path must be absolute"
    [[ "$BACKUP_ROOT" == /* && "$BACKUP_ROOT" != *$'\n'* ]] || die "Backup path must be absolute"
    [[ "$BACKEND_PORT" =~ ^[0-9]+$ ]] || die "BACKEND_PORT must be numeric"
    (( BACKEND_PORT >= 1 && BACKEND_PORT <= 65535 )) || die "BACKEND_PORT is out of range"
    [[ "$PUBLIC_BIND_HOST" =~ ^[A-Za-z0-9.:-]+$ ]] || die "Invalid public bind host"
    [[ "$PUBLIC_BIND_HOST" == "127.0.0.1" || "$PUBLIC_BIND_HOST" == "0.0.0.0" || "$PUBLIC_BIND_HOST" == "::" ]] || die "PUBLIC_BIND_HOST must be 127.0.0.1, 0.0.0.0 or ::"
    [[ "$TG_CHAT_ID" =~ ^-?[0-9]+$ ]] || die "TG_CHAT_ID must be an integer"
    [[ "$TG_CHAT_ID" != "0" ]] || die "TG_CHAT_ID cannot be zero"
    [[ "$TZ_OFFSET_HOURS" =~ ^-?[0-9]+$ ]] || die "TZ_OFFSET_HOURS must be an integer"
    (( TZ_OFFSET_HOURS >= -23 && TZ_OFFSET_HOURS <= 23 )) || die "TZ_OFFSET_HOURS must be between -23 and 23"
    [[ "$MAX_WEBHOOK_BODY_BYTES" =~ ^[0-9]+$ ]] || die "MAX_WEBHOOK_BODY_BYTES must be numeric"
    (( MAX_WEBHOOK_BODY_BYTES >= 1024 && MAX_WEBHOOK_BODY_BYTES <= 10485760 )) || die "MAX_WEBHOOK_BODY_BYTES must be between 1 KiB and 10 MiB"
    for name in TG_BOT_TOKEN MEGAPBX_CRM_TOKEN MEGAPBX_ALLOWED_GROUP MEGAPBX_ALLOWED_DID \
        MEGAPBX_DID_NAMES MEGAPBX_API_BASE MEGAPBX_API_TOKEN; do
        value="${!name-}"
        [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "$name must not contain newlines"
    done
    if [[ -n "$MEGAPBX_API_BASE" && "$MEGAPBX_API_BASE" != https://* ]] && ! is_yes "$ALLOW_HTTP_API"; then
        die "MEGAPBX_API_BASE must use HTTPS (or explicitly set MEGAPBX_TG_ALLOW_HTTP_API=1 for a trusted LAN)"
    fi
    if [[ -n "$MEGAPBX_API_BASE" && -z "$MEGAPBX_API_TOKEN" ]]; then
        die "MEGAPBX_API_TOKEN is required when MEGAPBX_API_BASE is set"
    fi
    if [[ -z "$MEGAPBX_API_BASE" && -n "$MEGAPBX_API_TOKEN" ]]; then
        die "MEGAPBX_API_TOKEN requires MEGAPBX_API_BASE"
    fi
    if [[ -z "$MEGAPBX_ALLOWED_GROUP" && -z "$MEGAPBX_ALLOWED_DID" ]] && ! is_yes "$ALLOW_ALL_DESTINATIONS"; then
        die "Set MEGAPBX_ALLOWED_GROUP or MEGAPBX_ALLOWED_DID; refusing fail-open all-destinations mode"
    fi
    if is_yes "$ENABLE_TLS"; then
        is_yes "$INSTALL_NGINX" || die "--enable-tls requires --with-nginx"
        [[ -n "$DOMAIN" ]] || die "A domain is required for TLS"
        [[ "$TLS_EMAIL" == *@* ]] || die "A valid TLS email is required"
    fi
    if is_yes "$INSTALL_NGINX"; then
        [[ -n "$DOMAIN" ]] || die "--with-nginx requires --domain"
        [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "Invalid domain"
    fi
}

write_env_line() {
    local key="$1"
    local value="$2"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\\\$}"
    printf '%s="%s"\n' "$key" "$value" >> "$CONFIG_FILE"
}

write_config() {
    local tmp
    tmp="$(mktemp "${CONFIG_FILE}.tmp.XXXXXX")"
    : > "$tmp"
    write_env_line TG_BOT_TOKEN "$TG_BOT_TOKEN"
    write_env_line TG_CHAT_ID "$TG_CHAT_ID"
    write_env_line TG_DELETE_WEBHOOK_ON_START "$TG_DELETE_WEBHOOK_ON_START"
    write_env_line MEGAPBX_CRM_TOKEN "$MEGAPBX_CRM_TOKEN"
    write_env_line MEGAPBX_ALLOWED_GROUP "$MEGAPBX_ALLOWED_GROUP"
    write_env_line MEGAPBX_ALLOWED_DID "$MEGAPBX_ALLOWED_DID"
    write_env_line MEGAPBX_DID_NAMES "$MEGAPBX_DID_NAMES"
    write_env_line MEGAPBX_API_BASE "$MEGAPBX_API_BASE"
    write_env_line MEGAPBX_API_TOKEN "$MEGAPBX_API_TOKEN"
    write_env_line MEGAPBX_ALLOW_QUERY_TOKEN 0
    write_env_line TZ_OFFSET_HOURS "$TZ_OFFSET_HOURS"
    write_env_line MISSED_MAX_AGE_SEC "$MISSED_MAX_AGE_SEC"
    write_env_line MISSED_CLEANUP_INTERVAL_SEC "$MISSED_CLEANUP_INTERVAL_SEC"
    write_env_line MISSED_DEDUP_TTL_SEC "$MISSED_DEDUP_TTL_SEC"
    write_env_line TG_API_MAX_RETRIES "$TG_API_MAX_RETRIES"
    write_env_line TG_API_RETRY_BASE_SEC "$TG_API_RETRY_BASE_SEC"
    write_env_line TG_API_RETRY_MAX_SEC "$TG_API_RETRY_MAX_SEC"
    write_env_line MAX_WEBHOOK_BODY_BYTES "$MAX_WEBHOOK_BODY_BYTES"
    chown root:root "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$CONFIG_FILE"
}

collect_config() {
    if [[ -n "$ENV_FILE" ]]; then
        load_env_file "$ENV_FILE"
    elif [[ -f "$CONFIG_FILE" && "$REPLACE_CONFIG" -eq 0 ]]; then
        load_env_file "$CONFIG_FILE"
        log "Existing configuration will be reused: $CONFIG_FILE"
    fi

    set_defaults
    read_secret TG_BOT_TOKEN "Telegram bot token: "
    read_value TG_CHAT_ID "Telegram chat ID (supergroup IDs are negative): "
    read_secret MEGAPBX_CRM_TOKEN "MegaPBX CRM token: "
    read_optional MEGAPBX_ALLOWED_GROUP "Allowed MegaPBX group(s), comma-separated (optional): " ""
    read_optional MEGAPBX_ALLOWED_DID "Allowed DID numbers, comma-separated (optional): " ""
    read_optional MEGAPBX_DID_NAMES "DID=name mapping, comma-separated (optional): " ""
    read_optional MEGAPBX_API_BASE "MegaPBX API base URL (optional): " ""
    if [[ -n "$MEGAPBX_API_BASE" ]]; then
        read_secret MEGAPBX_API_TOKEN "MegaPBX API token: "
    fi
    read_value TZ_OFFSET_HOURS "Timezone offset from UTC (default 3): " "3"
    read_value MISSED_MAX_AGE_SEC "Missed-call correlation TTL in seconds: " "3600"
    read_value MISSED_CLEANUP_INTERVAL_SEC "Cleanup interval in seconds: " "3600"
    read_value MISSED_DEDUP_TTL_SEC "Deduplication TTL in seconds: " "86400"
    read_value MAX_WEBHOOK_BODY_BYTES "Maximum webhook body size in bytes: " "1048576"
    TG_DELETE_WEBHOOK_ON_START="${TG_DELETE_WEBHOOK_ON_START:-1}"

    if (( NON_INTERACTIVE == 0 )); then
        if [[ -z "$MEGAPBX_ALLOWED_GROUP" && -z "$MEGAPBX_ALLOWED_DID" ]]; then
            if ask_yes_no "No group/DID allowlist configured. Allow all destinations?" "n"; then
                ALLOW_ALL_DESTINATIONS=1
            else
                die "Configure at least one allowed group or DID"
            fi
        fi
        if [[ -z "$DOMAIN" ]]; then
            read -r -p "Public domain for Nginx/TLS (leave empty to skip Nginx): " DOMAIN || true
        fi
        if [[ -n "$DOMAIN" ]]; then
            if ask_yes_no "Install/configure Nginx for $DOMAIN?" "y"; then
                INSTALL_NGINX=1
            else
                INSTALL_NGINX=0
            fi
        fi
        if is_yes "$INSTALL_NGINX" && [[ -z "$TLS_EMAIL" ]] && ask_yes_no "Enable Let's Encrypt TLS?" "n"; then
            ENABLE_TLS=1
        fi
        if is_yes "$ENABLE_TLS"; then
            read -r -p "Let's Encrypt email: " TLS_EMAIL
        fi
    fi
    validate_scalar_values
}

show_plan() {
    log "Release: $RELEASE_TAG"
    log "Install directory: $INSTALL_ROOT"
    log "Service: $SERVICE_NAME ($SERVICE_USER)"
    log "Config: $CONFIG_FILE (mode 0600)"
    log "Backend bind: $BIND_HOST:$BACKEND_PORT"
    if is_yes "$INSTALL_NGINX"; then
        log "Nginx: enabled for https://$DOMAIN/"
        if is_yes "$ENABLE_TLS"; then
            log "TLS: enabled"
        else
            warn "TLS is disabled; use only for a trusted test network"
        fi
    else
        log "Nginx: disabled; application is local-only unless PUBLIC_BIND_HOST is changed"
    fi
    if (( DRY_RUN == 1 )); then
        log "Dry run: no system changes will be made"
        return
    fi
    if (( ASSUME_YES == 0 )); then
        ask_yes_no "Continue with installation?" "y" || die "Installation cancelled"
    fi
}

install_packages() {
    (( DRY_RUN == 0 )) || return
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    local packages=(python3 python3-venv python3-pip ca-certificates curl tar util-linux)
    if is_yes "$INSTALL_NGINX"; then
        packages+=(nginx)
        if is_yes "$ENABLE_TLS"; then
            packages+=(certbot python3-certbot-nginx)
        fi
    fi
    apt-get install -y --no-install-recommends "${packages[@]}"
}

select_python() {
    if [[ -n "$PYTHON_BIN" ]]; then
        command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "Python binary not found: $PYTHON_BIN"
    else
        local candidate
        for candidate in python3.11 python3.10 python3.9 python3; do
            if command -v "$candidate" >/dev/null 2>&1; then
                if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
                    PYTHON_BIN="$candidate"
                    break
                fi
            fi
        done
    fi
    [[ -n "$PYTHON_BIN" ]] || die "Python 3.9+ is required; install python3.11 or set MEGAPBX_TG_PYTHON"
    "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9+ is required"
    log "Python: $($PYTHON_BIN --version 2>&1)"
}

ensure_service_user() {
    (( DRY_RUN == 0 )) || return
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin --user-group "$SERVICE_USER"
    fi
    SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
    install -d -o root -g "$SERVICE_GROUP" -m 750 "$INSTALL_ROOT"
    install -d -o root -g "$SERVICE_GROUP" -m 750 "$INSTALL_ROOT/releases"
    install -d -o root -g "$SERVICE_GROUP" -m 750 "$BACKUP_ROOT"
    chmod 700 "$BACKUP_ROOT"
}

download_release() {
    local base_url="$REPO_URL/raw/$RELEASE_TAG"
    STAGE_DIR="$(mktemp -d "$INSTALL_ROOT/releases/.${RELEASE_TAG}.stage.XXXXXX")"
    if [[ -n "$SOURCE_DIR" ]]; then
        [[ -d "$SOURCE_DIR" ]] || die "SOURCE_DIR does not exist: $SOURCE_DIR"
        cp -- "$SOURCE_DIR/app.py" "$STAGE_DIR/app.py"
        cp -- "$SOURCE_DIR/requirements.txt" "$STAGE_DIR/requirements.txt"
        cp -- "$SOURCE_DIR/requirements.lock" "$STAGE_DIR/requirements.lock"
        [[ -f "$SOURCE_DIR/LICENSE" ]] && cp -- "$SOURCE_DIR/LICENSE" "$STAGE_DIR/LICENSE"
    else
        local file
        for file in app.py requirements.txt requirements.lock LICENSE; do
            if ! curl --fail --silent --show-error --location --retry 3 --connect-timeout 20 \
                --proto '=https' --tlsv1.2 "$base_url/$file" -o "$STAGE_DIR/$file"; then
                warn "Optional release file is unavailable: $file"
                rm -f -- "$STAGE_DIR/$file"
            fi
        done
        if curl --fail --silent --show-error --location --retry 3 --connect-timeout 20 \
            --proto '=https' --tlsv1.2 "$base_url/SHA256SUMS" -o "$STAGE_DIR/SHA256SUMS"; then
            (cd "$STAGE_DIR" && sha256sum -c SHA256SUMS --ignore-missing) || die "Release checksum verification failed"
        else
            warn "SHA256SUMS is unavailable; continuing with pinned HTTPS tag"
        fi
    fi
    [[ -s "$STAGE_DIR/app.py" ]] || die "app.py was not downloaded"
    [[ -s "$STAGE_DIR/requirements.txt" ]] || die "requirements.txt was not downloaded"
    [[ -s "$STAGE_DIR/requirements.lock" ]] || die "requirements.lock was not downloaded"
    "$PYTHON_BIN" -m py_compile "$STAGE_DIR/app.py"
    rm -rf -- "$STAGE_DIR/__pycache__"
}

begin_transaction() {
    (( DRY_RUN == 0 )) || return
    TX_DIR="$(mktemp -d "$BACKUP_ROOT/transaction.XXXXXX")"
    chmod 700 "$TX_DIR"
    if [[ -L "$INSTALL_ROOT/current" ]]; then
        CURRENT_TARGET="$(readlink "$INSTALL_ROOT/current")"
        printf '%s\n' "$CURRENT_TARGET" > "$TX_DIR/current-target"
    fi
    if [[ -f "$UNIT_PATH" ]]; then
        cp -p -- "$UNIT_PATH" "$TX_DIR/systemd-unit"
        touch "$TX_DIR/unit-existed"
    fi
    if [[ -f "$NGINX_SITE" ]]; then
        cp -p -- "$NGINX_SITE" "$TX_DIR/nginx-site"
        touch "$TX_DIR/nginx-existed"
    fi
    if [[ -e "$NGINX_LINK" || -L "$NGINX_LINK" ]]; then
        readlink "$NGINX_LINK" > "$TX_DIR/nginx-link" 2>/dev/null || true
        touch "$TX_DIR/nginx-link-existed"
    fi
    if [[ "$REPLACE_CONFIG" -eq 1 && -f "$CONFIG_FILE" ]]; then
        cp -p -- "$CONFIG_FILE" "$TX_DIR/config"
        touch "$TX_DIR/config-existed"
    fi
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        SERVICE_WAS_ACTIVE=1
    fi
    if systemctl is-enabled --quiet "$SERVICE_NAME"; then
        SERVICE_WAS_ENABLED=1
    fi
    if [[ -x "$(command -v nginx 2>/dev/null || true)" ]]; then
        if systemctl is-active --quiet nginx; then
            NGINX_WAS_ACTIVE=1
        fi
    fi
    TRANSACTION_ACTIVE=1
    log "Transaction backup: $TX_DIR"
}

restore_file() {
    local saved="$1"
    local target="$2"
    if [[ -f "$saved" ]]; then
        cp -p -- "$saved" "$target"
    else
        rm -f -- "$target"
    fi
}

rollback_transaction() {
    (( TRANSACTION_ACTIVE == 1 )) || return 0
    TRANSACTION_ACTIVE=0
    warn "Installation failed; restoring the previous installation"
    set +e
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    if [[ -f "$TX_DIR/current-target" ]]; then
        rm -f -- "$INSTALL_ROOT/current"
        ln -s "$(cat "$TX_DIR/current-target")" "$INSTALL_ROOT/current"
    else
        rm -f -- "$INSTALL_ROOT/current"
    fi
    if [[ -f "$TX_DIR/unit-existed" ]]; then
        restore_file "$TX_DIR/systemd-unit" "$UNIT_PATH"
    else
        rm -f -- "$UNIT_PATH"
    fi
    if [[ -f "$TX_DIR/nginx-existed" ]]; then
        restore_file "$TX_DIR/nginx-site" "$NGINX_SITE"
    else
        rm -f -- "$NGINX_SITE" "$NGINX_LINK"
    fi
    if [[ -f "$TX_DIR/nginx-link-existed" ]]; then
        rm -f -- "$NGINX_LINK"
        ln -s "$(cat "$TX_DIR/nginx-link")" "$NGINX_LINK"
    fi
    if [[ -f "$TX_DIR/config-existed" ]]; then
        restore_file "$TX_DIR/config" "$CONFIG_FILE"
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [[ "$SERVICE_WAS_ENABLED" -eq 1 ]]; then
        systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    else
        systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
        systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$NGINX_WAS_ACTIVE" -eq 1 ]]; then
        systemctl reload nginx >/dev/null 2>&1 || systemctl restart nginx >/dev/null 2>&1 || true
    fi
    set -e
}

on_exit() {
    local status=$?
    trap - EXIT
    if (( status != 0 && TRANSACTION_ACTIVE == 1 )); then
        rollback_transaction || true
    fi
    if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
        rm -rf -- "$STAGE_DIR"
    fi
    exit "$status"
}
trap on_exit EXIT

render_unit() {
    local unit_tmp
    unit_tmp="$(mktemp "/tmp/${SERVICE_NAME}.service.XXXXXX")"
    cat > "$unit_tmp" <<EOF
[Unit]
Description=MegaPBX Telegram missed-call notifier
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_ROOT/current
EnvironmentFile=$CONFIG_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$INSTALL_ROOT/current/.venv/bin/python -m uvicorn app:app --host $BIND_HOST --port $BACKEND_PORT --workers 1 --no-access-log --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
KillSignal=SIGINT
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "$unit_tmp" >/dev/null
    fi
    install -o root -g root -m 644 "$unit_tmp" "$UNIT_PATH"
    rm -f -- "$unit_tmp"
}

render_nginx() {
    is_yes "$INSTALL_NGINX" || return 0
    if [[ "$REPLACE_NGINX" -eq 0 && -e "$NGINX_SITE" ]]; then
        die "Nginx site already exists: $NGINX_SITE (use --replace-nginx after reviewing it)"
    fi
    if [[ -e "$NGINX_LINK" && ! -L "$NGINX_LINK" && "$REPLACE_NGINX" -eq 0 ]]; then
        die "Nginx link exists and is not a symlink; use --replace-nginx after reviewing it"
    fi
    if [[ "$REPLACE_NGINX" -eq 0 ]]; then
        local conflict
        conflict="$(grep -R -l --include='*.conf' --include='*' "server_name[[:space:]].*${DOMAIN}" \
            /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | grep -v -Fx "$NGINX_LINK" || true)"
        [[ -z "$conflict" ]] || die "Domain is already used by another Nginx config: $conflict"
    fi
    local body_limit_mb=$((MAX_WEBHOOK_BODY_BYTES / 1024 / 1024))
    (( body_limit_mb < 1 )) && body_limit_mb=1
    cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location = / {
        return 404;
    }

    location = /megapbx/webhook {
        limit_except POST { deny all; }
        access_log off;
        client_max_body_size ${body_limit_mb}m;

        proxy_pass http://127.0.0.1:$BACKEND_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-CRM-Token \$http_x_crm_token;
        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
    }
}
EOF
    chown root:root "$NGINX_SITE"
    chmod 644 "$NGINX_SITE"
    ln -sfn "$NGINX_SITE" "$NGINX_LINK"
    nginx -t
    systemctl enable --now nginx
    systemctl reload nginx
    if is_yes "$ENABLE_TLS"; then
        certbot --nginx --non-interactive --agree-tos --email "$TLS_EMAIL" --redirect -d "$DOMAIN"
        nginx -t
        systemctl reload nginx
    fi
}

health_check() {
    (( NO_START == 0 )) || return 0
    systemctl is-active --quiet "$SERVICE_NAME" || die "systemd service is not active"
    local attempt
    for ((attempt = 1; attempt <= 20; attempt++)); do
        if curl --fail --silent --show-error --max-time 3 "http://127.0.0.1:${BACKEND_PORT}/" >/dev/null; then
            log "Health check passed"
            return 0
        fi
        sleep 1
    done
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager >&2 || true
    die "Application health check failed"
}

print_result() {
    log "Installation completed"
    log "Service: systemctl status $SERVICE_NAME"
    log "Config: $CONFIG_FILE"
    log "Local health: http://127.0.0.1:${BACKEND_PORT}/"
    if is_yes "$INSTALL_NGINX"; then
        if is_yes "$ENABLE_TLS"; then
            log "Webhook: https://${DOMAIN}/megapbx/webhook"
        else
            log "Webhook: http://${DOMAIN}/megapbx/webhook (TLS not enabled)"
        fi
        log "Use the X-CRM-Token header; query-string token authentication is disabled."
    else
        log "No public reverse proxy was configured."
        log "Set up HTTPS and point it to 127.0.0.1:${BACKEND_PORT} before exposing the webhook."
    fi
    log "Application logs: journalctl -u $SERVICE_NAME -f"
}

main() {
    parse_args "$@"
    require_root
    check_os
    check_tty
    acquire_lock

    if [[ -n "$ENV_FILE" ]]; then
        [[ -f "$ENV_FILE" ]] || die "Environment file not found: $ENV_FILE"
    fi
    collect_config
    validate_scalar_values
    if is_yes "$INSTALL_NGINX"; then
        BIND_HOST="$BACKEND_HOST"
    else
        BIND_HOST="$PUBLIC_BIND_HOST"
    fi
    show_plan
    if (( DRY_RUN == 1 )); then
        return 0
    fi

    begin_transaction
    install_packages
    select_python
    ensure_service_user
    download_release

    local release_id
    release_id="${RELEASE_TAG}-$(date -u +%Y%m%d%H%M%S)-$$"
    FINAL_DIR="$INSTALL_ROOT/releases/$release_id"
    mv -- "$STAGE_DIR" "$FINAL_DIR"
    STAGE_DIR=""

    "$PYTHON_BIN" -m venv "$FINAL_DIR/.venv"
    export PIP_INDEX_URL
    "$FINAL_DIR/.venv/bin/python" -m pip install \
        --disable-pip-version-check --no-cache-dir --index-url "$PIP_INDEX_URL" --upgrade pip
    local requirements_file="$FINAL_DIR/requirements.txt"
    [[ -f "$FINAL_DIR/requirements.lock" ]] && requirements_file="$FINAL_DIR/requirements.lock"
    "$FINAL_DIR/.venv/bin/python" -m pip install \
        --disable-pip-version-check --no-cache-dir --index-url "$PIP_INDEX_URL" -r "$requirements_file"
    (cd "$FINAL_DIR" && "$FINAL_DIR/.venv/bin/python" -c 'import fastapi, httpx, uvicorn, app')

    chown -R root:"$SERVICE_GROUP" "$FINAL_DIR"
    chmod -R u=rwX,g=rX,o= "$FINAL_DIR"

    if [[ "$REPLACE_CONFIG" -eq 1 || ! -f "$CONFIG_FILE" ]]; then
        write_config
    fi
    render_unit
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"

    rm -f -- "$INSTALL_ROOT/current"
    ln -s "$FINAL_DIR" "$INSTALL_ROOT/current"

    if (( NO_START == 0 )); then
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            systemctl restart "$SERVICE_NAME"
        else
            systemctl start "$SERVICE_NAME"
        fi
    fi
    render_nginx
    health_check
    TRANSACTION_ACTIVE=0
    print_result
}

main "$@"

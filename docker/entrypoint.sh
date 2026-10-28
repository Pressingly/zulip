#!/bin/bash
# Adapted from zulip/docker-zulip's entrypoint.sh
# (https://github.com/zulip/docker-zulip), Copyright the Zulip contributors,
# Apache License 2.0. Trimmed to what the Moneta FOSS bundle runs: HTTP-only
# behind Traefik, external PostgreSQL, RabbitMQ, Redis and memcached,
# configuration from SETTING_*, SECRETS_* and CONFIG_* environment variables.

if [ "${DEBUG:-}" = "true" ] || [ "${DEBUG:-}" = "True" ]; then
    set -x
fi
set -eu
shopt -s extglob

DEPLOY_ROOT=/home/zulip/deployments/current
ZULIP_CONF=/etc/zulip/zulip.conf
ZULIP_SETTINGS=/etc/zulip/settings.py
ZULIP_SECRETS=/etc/zulip/zulip-secrets.conf

BOOT_STARTED_MS="$(date +%s%3N)"
PHASE_STARTED_MS="$BOOT_STARTED_MS"

phaseDone() {
    local now_ms
    now_ms="$(date +%s%3N)"
    echo "[zulip-boot] phase=$1 took=$((now_ms - PHASE_STARTED_MS))ms total=$((now_ms - BOOT_STARTED_MS))ms"
    PHASE_STARTED_MS="$now_ms"
}

normalizeBool() {
    local varname="$1"
    local raw_value="${!varname:-}"
    local default="${2-False}"
    case "${raw_value,,}" in
        true | enable | enabled | yes | y | 1 | on) echo "True" ;;
        false | disable | disabled | no | n | 0 | off) echo "False" ;;
        "") echo "$default" ;;
        none) echo "None" ;;
        *)
            echo "WARNING: Invalid boolean ('$raw_value') for '$varname'; defaulting to $default" >&2
            echo "$default"
            ;;
    esac
}

runAsZulip() {
    su zulip -c "$*"
}

prepareDirectories() {
    mkdir -p "$DATA_DIR" "$DATA_DIR/uploads"
    rm -rf /home/zulip/uploads
    ln -sfT "$DATA_DIR/uploads" /home/zulip/uploads
    chown zulip:zulip -R "$DATA_DIR/uploads"
}

applyConfigEnvironment() {
    crudini --set "$ZULIP_CONF" application_server http_only true
    if [ -n "${LOADBALANCER_IPS:-}" ]; then
        crudini --set "$ZULIP_CONF" loadbalancer ips "$LOADBALANCER_IPS"
    fi
    local key
    for key in "${!CONFIG_@}"; do
        [[ "$key" =~ ^CONFIG_([a-z0-9_]+?)__([a-z0-9_]+)$ ]] || continue
        local section="${BASH_REMATCH[1]}"
        local name="${BASH_REMATCH[2]}"
        local value="${!key}"
        if [ -z "$value" ]; then
            echo "WARNING: Empty value for config \"$key\", skipping."
            continue
        fi
        echo "Setting zulip.conf $section.$name = $value"
        crudini --set "$ZULIP_CONF" "$section" "$name" "$value"
    done
}

puppetConfiguration() {
    echo "Executing puppet configuration ..."
    "$DEPLOY_ROOT/scripts/zulip-puppet-apply" -f
}

setConfigurationValue() {
    local key="$1"
    local value="$2"
    local type="${3:-}"
    if [ -z "$type" ]; then
        case "$(echo -e "$value" | tr -d '[:space:]')" in
            [Tt][Rr][Uu][Ee] | [Ff][Aa][Ll][Ss][Ee] | [Nn]one) type="bool" ;;
            +([0-9])) type="integer" ;;
            \[*\] | \(*\) | \{*\}) type="array" ;;
            *) type="string" ;;
        esac
    fi
    local line
    case "$type" in
        literal) line="$key" ;;
        bool)
            # shellcheck disable=SC2034
            local PROVIDED_SETTING="$value"
            line="$key = $(normalizeBool PROVIDED_SETTING False)"
            ;;
        integer | array) line="$key = $value" ;;
        *)
            value="${value//\\/\\\\}"
            value="${value//\'/\\\'}"
            line="$key = '$value'"
            ;;
    esac
    echo "$line" >>"$ZULIP_SETTINGS"
    echo "Setting key \"$key\", type \"$type\"."
}

secretsConfiguration() {
    echo "Setting Zulip secrets ..."
    if [ ! -e "$DATA_DIR/zulip-secrets.conf" ]; then
        touch "$DATA_DIR/zulip-secrets.conf"
    fi
    ln -nsf "$DATA_DIR/zulip-secrets.conf" "$ZULIP_SECRETS"
    chown root:zulip "$DATA_DIR/zulip-secrets.conf"
    chmod 640 "$DATA_DIR/zulip-secrets.conf"
    /root/zulip/scripts/setup/generate_secrets.py --production
    local key
    for key in "${!SECRETS_@}"; do
        [[ "$key" == SECRETS_*([0-9A-Z_a-z-]) ]] || continue
        local name="${key#SECRETS_}"
        local value="${!key}"
        if [ -z "$value" ]; then
            echo "Empty secret for key \"$name\"."
        elif [[ "$value" =~ $'\n' ]]; then
            echo "ERROR: Secret \"$name\" contains a newline!"
            exit 1
        fi
        echo "Setting $name from environment variable $key"
        crudini --set "$ZULIP_SECRETS" secrets "$name" "$value"
    done
}

authenticationBackends() {
    local backends
    IFS=, read -r -a backends <<<"${ZULIP_AUTH_BACKENDS:-EmailAuthBackend}"
    setConfigurationValue "AUTHENTICATION_BACKENDS" "()" "array"
    local backend
    for backend in "${backends[@]}"; do
        setConfigurationValue "AUTHENTICATION_BACKENDS += ('zproject.backends.${backend}',)" "" "literal"
    done
}

zulipConfiguration() {
    cp -a "$DEPLOY_ROOT/zproject/prod_settings_template.py" "$ZULIP_SETTINGS"
    authenticationBackends
    if [ -n "${ZULIP_CUSTOM_SETTINGS:-}" ]; then
        echo -e "\n$ZULIP_CUSTOM_SETTINGS" >>"$ZULIP_SETTINGS"
    fi
    local key
    for key in "${!SETTING_@}"; do
        [[ "$key" == SETTING_*([0-9A-Za-z_]) ]] || continue
        local value="${!key}"
        if [ -z "$value" ]; then
            echo "WARNING: Empty var for key \"${key#SETTING_}\", skipping."
            continue
        fi
        setConfigurationValue "${key#SETTING_}" "$value"
    done
}

checkConfiguration() {
    if ! runAsZulip "$DEPLOY_ROOT/manage.py check"; then
        echo "Error in the Zulip configuration. Exiting."
        exit 1
    fi
}

waitForDatabase() {
    local timeout=60
    local db_host db_port
    db_host="$(runAsZulip "$DEPLOY_ROOT/scripts/get-django-setting REMOTE_POSTGRES_HOST")"
    db_port="$(runAsZulip "$DEPLOY_ROOT/scripts/get-django-setting REMOTE_POSTGRES_PORT")"
    echo "Waiting for database server to allow connections ..."
    while ! /usr/bin/pg_isready -h "$db_host" -p "${db_port:-5432}" -t 1 >/dev/null 2>&1; do
        if ! ((timeout--)); then
            echo "Could not connect to database server. Exiting."
            exit 1
        fi
        sleep 1
    done
}

migrateDatabase() {
    echo "Running database migrations ..."
    runAsZulip "$DEPLOY_ROOT/manage.py migrate --noinput"
    runAsZulip "$DEPLOY_ROOT/manage.py createcachetable third_party_api_results"
}

initialConfiguration() {
    prepareDirectories
    phaseDone prepare
    applyConfigEnvironment
    puppetConfiguration
    phaseDone puppet
    zulipConfiguration
    secretsConfiguration
    phaseDone settings
    checkConfiguration
    phaseDone check
    waitForDatabase
    phaseDone wait-db
    migrateDatabase
    phaseDone migrate
}

appRun() {
    initialConfiguration
    echo "Starting Zulip under supervisord ..."
    unset HOME
    tail -F /var/log/zulip/errors.log &
    exec supervisord -n -c /etc/supervisor/supervisord.conf -u root
}

appManagePy() {
    local command="${1:-shell}"
    shift || true
    exec su zulip -c "$DEPLOY_ROOT/manage.py $(printf '%q ' "$command" "$@")"
}

case "${1:-}" in
    app:run) appRun ;;
    app:init) initialConfiguration ;;
    app:managepy)
        shift
        appManagePy "$@"
        ;;
    *) exec "$@" ;;
esac

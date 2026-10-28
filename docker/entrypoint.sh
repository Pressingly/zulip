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
BAKED_CONF=/etc/zulip/zulip.conf.baked
APPLIED_CONF=/etc/zulip/zulip.conf.applied
BUILD_ID="$(cat "$DEPLOY_ROOT/build_id")"

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
    cp "$BAKED_CONF" "$ZULIP_CONF"
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

canonicalConf() {
    crudini --get --format=lines "$1" | grep -v '^\[ application_server \] nameserver ' | sort
}

confMatchesApplied() {
    [ -f "$APPLIED_CONF" ] && [ "$(canonicalConf "$ZULIP_CONF")" = "$(canonicalConf "$APPLIED_CONF")" ]
}

puppetConfiguration() {
    if confMatchesApplied; then
        echo "zulip.conf matches the applied configuration; skipping Puppet."
        return
    fi
    echo "Executing puppet configuration ..."
    "$DEPLOY_ROOT/scripts/zulip-puppet-apply" -f
    cp "$ZULIP_CONF" "$APPLIED_CONF"
}

runtimeNameserver() {
    local configured="${CONFIG_application_server__nameserver:-}"
    if [ -n "$configured" ]; then
        echo "$configured"
        return
    fi
    awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf
}

# Puppet renders two host-specific values; patching them here keeps the
# baked configuration valid on every host without a Puppet run.
patchHostSpecificFiles() {
    local nameserver
    nameserver="$(runtimeNameserver)"
    if [[ "$nameserver" == *:* && "$nameserver" != \[* ]]; then
        nameserver="[$nameserver]"
    fi
    sed -i -E "s|^resolver [^ ]+ |resolver $nameserver |" /etc/nginx/zulip-include/s3-cache
    local root_uri="https://${SETTING_EXTERNAL_HOST:-localhost}"
    sed -i -E "s|(--user-agent \"Zulip-Server/[^ ]+ \()[^)]*\)|\1$root_uri/)|" \
        /etc/supervisor/conf.d/zulip/go-camo.conf
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
    local marker="$DATA_DIR/.secrets-generated-build-id"
    if [ "$(cat "$marker" 2>/dev/null)" != "$BUILD_ID" ]; then
        /root/zulip/scripts/setup/generate_secrets.py --production
        echo "$BUILD_ID" >"$marker"
    fi
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

configurationHash() {
    cat "$ZULIP_CONF" "$ZULIP_SETTINGS" "$ZULIP_SECRETS" <(echo "$BUILD_ID") | sha256sum | cut -d' ' -f1
}

checkConfiguration() {
    local marker="$DATA_DIR/.checked-configuration"
    local current
    current="$(configurationHash)"
    if [ "$(cat "$marker" 2>/dev/null)" = "$current" ]; then
        echo "Configuration unchanged since the last successful check; skipping manage.py check."
        return
    fi
    if ! runAsZulip "$DEPLOY_ROOT/manage.py check"; then
        echo "Error in the Zulip configuration. Exiting."
        exit 1
    fi
    echo "$current" >"$marker"
}

waitForDatabase() {
    local timeout=60
    local db_host="${SETTING_REMOTE_POSTGRES_HOST:-127.0.0.1}"
    local db_port="${SETTING_REMOTE_POSTGRES_PORT:-5432}"
    echo "Waiting for database server to allow connections ..."
    while ! /usr/bin/pg_isready -h "$db_host" -p "$db_port" -t 1 >/dev/null 2>&1; do
        if ! ((timeout--)); then
            echo "Could not connect to database server. Exiting."
            exit 1
        fi
        sleep 1
    done
}

# Provisioning runs these explicitly; at boot they only run when this
# image has not migrated the database yet.
migrateDatabase() {
    echo "Running database migrations ..."
    runAsZulip "$DEPLOY_ROOT/manage.py migrate --noinput"
    runAsZulip "$DEPLOY_ROOT/manage.py createcachetable third_party_api_results"
    echo "$BUILD_ID" >"$DATA_DIR/.migrated-build-id"
}

migrateIfNewImage() {
    if [ "$(cat "$DATA_DIR/.migrated-build-id" 2>/dev/null)" = "$BUILD_ID" ]; then
        echo "Database already migrated by this image; skipping migrations."
        return
    fi
    migrateDatabase
}

initialConfiguration() {
    prepareDirectories
    phaseDone prepare
    applyConfigEnvironment
    puppetConfiguration
    patchHostSpecificFiles
    phaseDone puppet
    zulipConfiguration
    secretsConfiguration
    phaseDone settings
    checkConfiguration
    phaseDone check
    waitForDatabase
    phaseDone wait-db
    migrateIfNewImage
    phaseDone migrate
}

appRun() {
    initialConfiguration
    echo "Starting Zulip under supervisord ..."
    unset HOME "${!SECRETS_@}"
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
    app:migrate) migrateDatabase ;;
    app:managepy)
        shift
        appManagePy "$@"
        ;;
    *) exec "$@" ;;
esac

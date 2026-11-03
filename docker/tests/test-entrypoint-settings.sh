#!/usr/bin/env bash
# Checks how docker/entrypoint.sh writes SETTING_* values into settings.py.
# Run: bash docker/tests/test-entrypoint-settings.sh (needs bash >= 4 and python3).

# shellcheck source=docker/entrypoint.sh
source "$(dirname "${BASH_SOURCE[0]}")/../entrypoint.sh"
set +e

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
failures=0

writeSetting() {
    ZULIP_SETTINGS="$scratch/settings.py"
    : >"$ZULIP_SETTINGS"
    setConfigurationValue "$1" "$2" "$(declaredSettingType "$1")" >/dev/null
}

expectLine() {
    local key="$1" value="$2" expected="$3" actual
    writeSetting "$key" "$value"
    actual="$(cat "$ZULIP_SETTINGS")"
    if [ "$actual" != "$expected" ]; then
        echo "FAIL: $key=$(printf '%q' "$value") wrote [$actual], expected [$expected]"
        failures=$((failures + 1))
        return
    fi
    if ! python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$ZULIP_SETTINGS"; then
        echo "FAIL: $key=$(printf '%q' "$value") wrote [$actual], which is not valid Python"
        failures=$((failures + 1))
    fi
}

expectRefused() {
    local key="$1" value="$2"
    if (writeSetting "$key" "$value") >/dev/null 2>&1; then
        echo "FAIL: $key=$(printf '%q' "$value") was accepted, expected a refusal"
        failures=$((failures + 1))
    fi
}

expectLine MPASS_SMB_CORPORATE_ID 1234 "MPASS_SMB_CORPORATE_ID = '1234'"
expectLine MPASS_SMB_CORPORATE_ID 0123 "MPASS_SMB_CORPORATE_ID = '0123'"
expectLine MPASS_SMB_CORPORATE_ID $'1234\n' "MPASS_SMB_CORPORATE_ID = '1234'"
expectLine MPASS_SMB_CORPORATE_ID $'0123\r\n' "MPASS_SMB_CORPORATE_ID = '0123'"
expectLine MPASS_SMB_CORPORATE_ID acme "MPASS_SMB_CORPORATE_ID = 'acme'"

expectLine REMOTE_POSTGRES_PORT 5432 "REMOTE_POSTGRES_PORT = 5432"
expectLine SESSION_COOKIE_AGE 432000 "SESSION_COOKIE_AGE = 432000"
expectLine SOME_COUNT 0 "SOME_COUNT = 0"
expectLine SOME_ID 007 "SOME_ID = '007'"

expectLine MPASS_SSO True "MPASS_SSO = True"
expectLine LOCAL_UPLOADS_DIR None "LOCAL_UPLOADS_DIR = None"
expectLine SOME_TUPLE "('a', 'b')" "SOME_TUPLE = ('a', 'b')"
expectLine SOME_TEXT "it's a\\b" "SOME_TEXT = 'it\\'s a\\\\b'"

expectRefused SOME_TEXT $'a\nb'
expectRefused SOME_TEXT $'a\rb'

if [ "$failures" -ne 0 ]; then
    echo "$failures failure(s)"
    exit 1
fi
echo "entrypoint settings: all checks passed"

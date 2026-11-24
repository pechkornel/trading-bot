#!/bin/sh
# Prepares the gateway's own TLS key, keystore and config, then starts it.
# The password is read from a Docker secret. It is never printed, never put on
# a command line (openssl reads it with -passin file: and -passout file:), and
# this script never enables shell tracing.
set -eu
umask 077

PWD_FILE=/run/secrets/gateway_ssl_password
BASE=/opt/ibkr-gateway
TLS="$BASE/tls"
RUN="$BASE/run"
TEMPLATE="$BASE/conf.template.yaml"

fail() {
    echo "entrypoint: $1" >&2
    exit 1
}

[ -r "$PWD_FILE" ] || fail "cannot read $PWD_FILE (the secret must be readable by uid 10001)"
[ -r "$TEMPLATE" ] || fail "cannot read $TEMPLATE"

PW=$(cat "$PWD_FILE")
case "$PW" in
    *[!0-9A-Za-z]*) fail "gateway_ssl_password must contain only letters and digits" ;;
esac
[ "${#PW}" -ge 32 ] && [ "${#PW}" -le 128 ] ||
    fail "gateway_ssl_password must be 32 to 128 characters"

if [ ! -f "$TLS/keystore.p12" ]; then
    echo "entrypoint: generating the gateway TLS key and keystore"
    # The private key lives only on the tmpfs and is removed after the export.
    openssl req -x509 -newkey rsa:3072 -nodes \
        -keyout "$RUN/key.pem" -out "$RUN/cert.pem" -days 825 \
        -subj "/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
        -addext "extendedKeyUsage=serverAuth"
    openssl pkcs12 -export -inkey "$RUN/key.pem" -in "$RUN/cert.pem" \
        -name vertx -passout file:"$PWD_FILE" -out "$TLS/keystore.p12.new"
    rm -f "$RUN/key.pem"
    cp "$RUN/cert.pem" "$TLS/cert.pem.new"
    chmod 0644 "$TLS/cert.pem.new"
    mv "$TLS/cert.pem.new" "$TLS/cert.pem"
    mv "$TLS/keystore.p12.new" "$TLS/keystore.p12"
    rm -f "$RUN/cert.pem"
else
    openssl pkcs12 -in "$TLS/keystore.p12" -passin file:"$PWD_FILE" -noout >/dev/null 2>&1 ||
        fail "password does not match the keystore, remove the ibkr_gateway_tls volume to regenerate"
fi

cp "$TLS/keystore.p12" "$RUN/vertx.jks"
# The password goes through the environment of awk only, not its arguments.
PW="$PW" awk '{ gsub(/__SSL_PWD__/, ENVIRON["PW"]); print }' "$TEMPLATE" >"$RUN/conf.yaml"
chmod 0600 "$RUN/conf.yaml" "$RUN/vertx.jks"
unset PW

exec ./ibkr-gateway \
    -Dvertx.disableDnsResolver=true \
    -Djava.net.preferIPv4Stack=true \
    -Dvertx.logger-delegate-factory-class-name=io.vertx.core.logging.SLF4JLogDelegateFactory \
    -Dnologback.statusListenerClass=ch.qos.logback.core.status.OnConsoleStatusListener \
    -Dnolog4j.debug=true -Dnolog4j2.debug=true \
    --conf ../run/conf.yaml

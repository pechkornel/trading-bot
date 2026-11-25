#!/bin/sh
# Prepares the gateway's own TLS key and keystore, then starts it.
# The password is read from a Docker secret. It is never printed, never put on
# a command line (openssl reads it with -passin file: and -passout file:), and
# this script never enables shell tracing.
set -eu
umask 077

PWD_FILE=/run/secrets/gateway_ssl_password
BASE=/opt/ibkr-gateway
TLS="$BASE/tls"
RUN="$BASE/run"

fail() {
    echo "entrypoint: $1" >&2
    exit 1
}

[ -r "$PWD_FILE" ] || fail "cannot read $PWD_FILE (the secret must be readable by uid 10001)"

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

unset PW

# The native build ignores --conf and always uses the conf.yaml compiled into it
# (IBKR's default): sslCert "vertx.jks" in the working directory, sslPwd "mywebapi",
# and the allow list 192.*, 131.216.*, 127.0.0.1 (ADR 0004). So the gateway gets a
# copy of our own key under that public password. The password protects nothing; what
# matters is that the key is ours and not IBKR's public one. The copy is mode 600 and
# owned by this uid. The unencrypted key exists only briefly on the tmpfs.
openssl pkcs12 -in "$TLS/keystore.p12" -passin file:"$PWD_FILE" -nodes -out "$RUN/bundle.pem"
openssl pkcs12 -export -in "$RUN/bundle.pem" -name vertx -passout pass:mywebapi \
    -out "$BASE/vertx.jks.new"
rm -f "$RUN/bundle.pem"
chmod 0600 "$BASE/vertx.jks.new"
mv "$BASE/vertx.jks.new" "$BASE/vertx.jks"

exec ./ibkr-gateway \
    -Dvertx.disableDnsResolver=true \
    -Djava.net.preferIPv4Stack=true \
    -Dvertx.logger-delegate-factory-class-name=io.vertx.core.logging.SLF4JLogDelegateFactory \
    -Dnologback.statusListenerClass=ch.qos.logback.core.status.OnConsoleStatusListener \
    -Dnolog4j.debug=true -Dnolog4j2.debug=true

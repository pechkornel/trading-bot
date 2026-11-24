# IBKR Client Portal Gateway as a pinned native executable (third-party build).
# Build context is docker/gateway, so no bot sources are sent to the daemon.
# The release asset is verified against a pinned SHA-256 before it is unpacked.
# Base image pinned by digest (multi-arch index for ubuntu:24.04, checked 2026-11-24).
# Refresh: docker buildx imagetools inspect ubuntu:24.04, then update both FROM lines.
FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b AS fetch
ARG GATEWAY_VERSION=v1.0.0
ARG GATEWAY_ASSET=ibkr-gateway-linux-amd64.zip
ARG GATEWAY_SHA256=01e3b095c9040abcfe16c79af6b667b9c40be52eb0638bc05e6d2aa964e0fe5d
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/gw.zip \
      "https://github.com/petdomaa100/ibkr-client-portal-api-gateway-native-exe/releases/download/${GATEWAY_VERSION}/${GATEWAY_ASSET}" \
    && echo "${GATEWAY_SHA256}  /tmp/gw.zip" | sha256sum -c - \
    && mkdir /out && unzip -q /tmp/gw.zip -d /out \
    && test -f /out/ibkr-gateway && test -f /out/vertx.jks

FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 gateway
WORKDIR /opt/ibkr-gateway
COPY --from=fetch /out/ /opt/ibkr-gateway/
# chmod: the zip is made on Windows and may not keep the exec bit.
RUN chmod 0755 ibkr-gateway && mkdir -p root logs && cp vertx.jks root/vertx.jks \
    && chown -R gateway:gateway /opt/ibkr-gateway
USER gateway
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sk -o /dev/null https://127.0.0.1:5000/ || exit 1
ENTRYPOINT ["./ibkr-gateway", "-Dvertx.disableDnsResolver=true", "-Djava.net.preferIPv4Stack=true", "-Dvertx.logger-delegate-factory-class-name=io.vertx.core.logging.SLF4JLogDelegateFactory", "-Dnologback.statusListenerClass=ch.qos.logback.core.status.OnConsoleStatusListener", "-Dnolog4j.debug=true", "-Dnolog4j2.debug=true"]
CMD ["--conf", "../root/conf.yaml"]

# IBKR Client Portal Gateway as a pinned native executable (third-party build).
# Build context is docker/gateway, so no bot sources are sent to the daemon.
# The release zip is NOT downloaded here. scripts/fetch_gateway.sh puts it in
# vendor/ibkr-gateway/ (git-ignored) and compose passes that folder as the
# "vendor" build context. The zip is verified against gateway.sha256 before it
# is unpacked.
# Base image pinned by digest (multi-arch index for ubuntu:24.04, checked 2026-11-24).
# Refresh: docker buildx imagetools inspect ubuntu:24.04, then update both FROM lines.
FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b AS fetch
ARG GATEWAY_VERSION=v1.0.0
ARG GATEWAY_ASSET=ibkr-gateway-linux-amd64.zip
RUN apt-get update && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/gw
COPY --from=vendor ${GATEWAY_ASSET} ./${GATEWAY_ASSET}
COPY gateway.sha256 ./gateway.sha256
RUN sha256sum -c gateway.sha256 \
    && mkdir /out && unzip -q ${GATEWAY_ASSET} -d /out \
    && test -f /out/ibkr-gateway

FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 gateway
WORKDIR /opt/ibkr-gateway
COPY --from=fetch /out/ /opt/ibkr-gateway/
COPY entrypoint.sh /opt/ibkr-gateway/entrypoint.sh
# Remove IBKR's public default keystore (both copies) so the gateway can never
# fall back to it. The entrypoint generates the gateway's own key and keystore.
# chmod: the zip is made on Windows and may not keep the exec bit.
RUN rm -f vertx.jks root/vertx.jks \
    && chmod 0755 ibkr-gateway entrypoint.sh \
    && mkdir -p root logs tls run \
    && chown -R gateway:gateway /opt/ibkr-gateway
USER gateway
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sk -o /dev/null https://127.0.0.1:5000/ || exit 1
ENTRYPOINT ["./entrypoint.sh"]

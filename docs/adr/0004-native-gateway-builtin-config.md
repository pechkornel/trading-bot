# 0004. Native gateway runs with its built-in config

## Status

Accepted. Replaces the config, allow list and `--conf` parts of ADR 0001 and ADR 0003.

## Context

ADR 0001 and ADR 0003 gave the native gateway our own `conf.yaml` through `--conf`, with an
exact allow list (`127.0.0.1` and `172.31.250.1`) and `sslPwd` from a Docker secret. Both ADRs
marked `--conf` as unverified.

The first start on the server on 2026-11-25 settled it:

- With `--conf ../run/conf.yaml` the binary exits at once with `Failed to load configuration
  file: ../run/conf.yaml` and `java.io.IOException: Stream closed`, in a restart loop. The
  native build looks the path up as a resource compiled into the binary, not as a file.
- The v1.0.0 binary has IBKR's default `conf.yaml` compiled in: `sslCert: "vertx.jks"`,
  `sslPwd: "mywebapi"`, `listenPort: 5000`, `listenSsl: true`, and the allow list `192.*`,
  `131.216.*`, `127.0.0.1`. IBKR's default `vertx.jks` is compiled in too.
- A one-off test on the server (no login, throwaway key, `127.0.0.1:5001`) showed that
  without `--conf` the gateway loads `vertx.jks` from its working directory on disk: the
  certificate it served had the same SHA-256 fingerprint as the throwaway one. Requests
  through Docker's proxy from the default bridge got "Access Denied", as the built-in allow
  list predicts.

## Decision

- Start the gateway without `--conf`. It uses its built-in config.
- Move the `ibkr_gateway` bridge from `172.31.250.0/24` to `192.168.250.0/24`. Docker's proxy
  then delivers connections from `192.168.250.1`, which the built-in `192.*` rule admits.
- Keep our own key. The entrypoint still generates the RSA key and the PKCS12 keystore into
  the `ibkr_gateway_tls` volume, encrypted with the `gateway_ssl_password` secret. On every
  start it writes a copy to `/opt/ibkr-gateway/vertx.jks` (mode 600, owner uid 10001) under
  the built-in password `mywebapi`, because the gateway can only open it with that password.
- Delete `docker/gateway/conf.yaml` and its mount. The binary ignored it, so it only misled.
- Tests pin the new subnet, check that its bridge address matches the built-in list, and
  check that the entrypoint has no `--conf`.

## Consequences

- The allow list is wider than before: any `192.*` address and `131.216.*`. This
  matters little in practice. The gateway's only way in is the port published on `127.0.0.1`
  (Docker 28 or newer blocks LAN access to it) and its own bridge network, where it is the
  only container. The LAN (`192.168.0.0/24` on the home server) cannot connect, and the
  laptop checks in `deploy/README.md` confirm that on every install.
- The keystore copy the gateway reads uses a public password. What protects TLS is that the
  key is our own and not IBKR's public one, so the keeper's SPKI pin still works. The copy is
  readable only by uid 10001. The copy in the volume stays encrypted with the secret.
- The subnet must not overlap the host's LAN or another Docker network. `192.168.250.0/24`
  does not overlap the home server's `192.168.0.0/24`. If it does on another host, pick
  another `192.168.x.0/24` and update the test.
- CORS and every other setting stay IBKR's defaults. Changing them needs a gateway build
  that reads a config file (built from source) or IBKR's Java gateway.

## Alternatives

- **IBKR's Java gateway on the host.** It reads `root/conf.yaml` from disk, so an exact allow
  list works. It needs Java and its own hardening and certificate, so it is a larger change.
  It stays the fallback.
- **Build the native binary from source with our `conf.yaml`.** Not done here: it needs a
  GraalVM build pipeline and replaces the pinned release.
- **Run the gateway with `network_mode: host`.** Connections would come from `127.0.0.1`, but
  the gateway listens on every interface, and the built-in `192.*` rule would admit the LAN.
  Rejected.
- **Patch the allow list bytes inside the binary.** Rejected: it breaks the pinned hash and
  is fragile.

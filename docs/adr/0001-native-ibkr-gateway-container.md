# 0001. Run the IBKR gateway as a native executable in a container

Status: Accepted (2026-11-24). Reversible, see Consequences. The config, allow list and `--conf` parts are replaced by ADR 0004.

## Context

The bot needs the IBKR Client Portal Gateway on `https://localhost:5000`. Until now that was
IBKR's Java daemon, installed by hand on the host and run under systemd
(`deploy/UBUNTU_SETUP.md`, section 3). That works, but it needs a JVM on the server and the
daemon runs unconfined on the host.

A personal GitHub repository, `petdomaa100/ibkr-client-portal-api-gateway-native-exe`,
publishes a GraalVM native build of the same gateway. Release `v1.0.0` has a Linux amd64 zip.
It starts fast, needs no JVM and fits in a container.

Constraints:

- Paper trading only. `assert_paper_mode` and `PAPER_PORTS = {5000}` do not change.
- The dashboard and the gateway must not be reachable from the LAN or the internet.
- Docker publishes ports around host firewalls such as ufw. No firewall, ufw, iptables or
  sshd change is part of this decision, so the bind address has to be the control.
- The bot containers use `network_mode: host` and reach the gateway at `localhost:5000`.

## Decision

Add a compose service `ibkr_gateway` under a new profile `gateway`.

- Image: `docker/gateway/Dockerfile`, based on `ubuntu:24.04` (the binary links glibc
  dynamically). A fetch stage downloads release `v1.0.0`, asset
  `ibkr-gateway-linux-amd64.zip`, and checks its SHA-256
  (`01e3b095c9040abcfe16c79af6b667b9c40be52eb0638bc05e6d2aa964e0fe5d`) with
  `sha256sum -c`. A changed asset fails the build.
- Runtime: non-root user, `cap_drop: [ALL]`, `no-new-privileges`, `restart: unless-stopped`.
- Network: its own bridge network `ibkr_gateway` with the pinned subnet `172.31.250.0/24`.
  The port is published as `127.0.0.1:5000:5000` only, never `5000:5000`.
- Config: `docker/gateway/conf.yaml` is IBKR's default config with only the `ips` block
  changed. It allows `127.0.0.1` and `172.31.250.*` and never `192.*` or a wildcard. Docker's
  proxy makes every local connection arrive from the bridge address `172.31.250.1`, so that
  range has to be allowed. A LAN address would be denied. The file is mounted read-only and
  passed with `--conf ../root/conf.yaml`, mirroring IBKR's own `bin/run.sh`.
- The profile is separate from `auto`, so people who run the Java gateway on the host keep
  `docker compose --profile auto up` working. Both want port 5000, so use one or the other.
- Tests in `tests/test_deploy_config.py` pin the loopback publish, the subnet and allow list
  match, the pinned hash and the absence of `0.0.0.0`.

The same work makes the dashboard bind to `127.0.0.1` by default (`DASHBOARD_HOST`) and loads
the user's login code from the git-ignored `core/ibkr_auth_private.py`. The private module is
copied into the local image by the existing `COPY . /app`. That image is built on the
server and must never be pushed or saved.

## Consequences

Risks that remain, stated plainly:

- Supply chain. The binary comes from a personal repository with no license, no checksum file,
  no build attestations and an empty release body. The tag points at a commit that is no
  longer on its `main` branch. The SHA-256 is the digest GitHub computed at upload, not one
  the author published. Pinning stops a silent change, not a bad first release. The binary
  handles the IBKR login session and can reach the internet. The mitigations (non-root,
  dropped capabilities, own network, loopback publish, paper account only) limit the damage,
  they do not remove the trust.
- `--conf` is unverified. The upstream README says the config is baked into the binary and
  only some things are configurable on the command line. If the binary ignores `--conf`, the
  baked default does not allow `172.31.250.*`, requests get "Access Denied" and nothing is
  exposed (it fails closed). The user checks this on the server. Follow-ups if it fails:
  build the binary from source with our `conf.yaml`, or move the bridge into a range the
  baked `192.*` rule admits (this loosens the allow list and needs explicit approval).
- CPU. A native image built with GraalVM's default target may need AVX2 (x86-64-v3). The
  upstream `-march=compatibility` flag was added after `v1.0.0` was built. On an older CPU
  the container exits with an illegal instruction error. The deploy guide has a preflight
  check, and the fallback is the Java gateway.
- Secrets. The private module lives inside `tradingbot:local`. Anyone who gets that image
  gets the module. Never push it, never `docker save` it, never `git add -f` the file.
- Exposure. Publishing on `0.0.0.0` instead of `127.0.0.1` would bypass ufw and open the
  gateway to the LAN. The deploy config tests fail first if someone changes it. Docker 28 or
  newer also drops direct routed packets to ports published on loopback.
- Maintenance. The pinned binary and base images are updated by hand. Dependabot for the
  base images and building the gateway from source in our own CI are follow-ups.

Reverting: remove the `ibkr_gateway` service and `docker/gateway/`, then use the Java gateway
from `deploy/UBUNTU_SETUP.md`. There is no data or schema migration.

## Alternatives

- Gateway with `network_mode: host`. Rejected: it binds every host interface and the baked
  default allows `192.*`, so the LAN could reach it. It would need a firewall rule, which
  risks locking out SSH.
- Native binary as a systemd service on the host. Rejected: same exposure, and the binary
  would run unconfined on the host.
- IBKR's Java gateway in a container. Not chosen: needs a JVM image and was not what was
  asked for. It stays available on the host.
- Allow all of `172.16.0.0/12` in `conf.yaml`. Rejected: wider than needed. The pinned /24
  matches only our bridge.
- A ufw rule for port 5000. Rejected: Docker publishes bypass ufw, and any ufw change risks
  the SSH login.
- A bind mount for the private module instead of baking it in. Rejected: Docker creates a
  root-owned directory at the host path when the file is missing, which breaks the public
  default.

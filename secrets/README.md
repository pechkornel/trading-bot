# secrets/

Real secret files live here on the host only. Git and Docker both ignore them.
Only this README and the `*.example` placeholders are tracked.

| File | Contents | Owner, mode |
|---|---|---|
| `ibkr_login` | `IBKR_USERNAME=...` and `IBKR_PASSWORD=...` lines | uid 1000, 600 |
| `gateway_ssl_password` | one line, 48 hex chars (`openssl rand -hex 24`) | uid 10001, 600 |

Create each file from its `.example`, replace the placeholders, then run
`chmod 600` on it. Never put real values in an `.example` file.

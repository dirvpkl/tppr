# AGENTS.md — tppr contributor guide

Read this before touching anything. It condenses several days of verified
behavior; every non-obvious claim below was proven with a live test, not
assumed. User docs: `README.md` (overview), `PROXY-GUIDE.md` (how to create
proxies and accounts), `scripts/README.md` (scripts and their errors).

## What this is

A Windows-hosted personal proxy stack. Docker Compose runs Mihomo (Clash.Meta)
as the core, fed by a self-built free-proxy aggregator, private paid proxies,
per-account authentication, and a random-exit gateway with expiring
credentials. Everything is localhost- or LAN-bound; there is no public surface.

## Service topology (compose project `tppr`)

| Container | Role | Ports (host → container) |
| --- | --- | --- |
| `tppr-mihomo-relay` | core proxy, auth, routing | `0.0.0.0:17893` dispatcher, `0.0.0.0:20001-20005` services, `127.0.0.1:19090` controller |
| `tppr-subconv` | subscription format normalizer (profile `pool-aggregation`) | none (internal `:8080`) |
| `tppr-pool-worker` | pool builder (profile `pool-aggregation`) | internal `:8080` |
| `tppr-hash-gate` | random-exit/TTL gateway (profile `pool-aggregation`) | `0.0.0.0:17894` all (free/custom gateways internal-only) |
| `tppr-api` | management REST API (always on) | `127.0.0.1:18080`, Bearer token |
| `tppr-speed-tester` | throughput sweeps, manages `POOL` selection | none |

`pool-aggregation` profile is auto-enabled by `reload.ps1` when
`pool-sources.toml` exists. The controller (`19090`) has an **empty secret**:
it must stay on `127.0.0.1` or anyone on the LAN gets full control.

## Port map (what clients use)

- `17893` — dispatcher (mixed HTTP/SOCKS5): one login per account, `IN-USER`
  routes each login to its own group.
- Management API on `127.0.0.1:18080` (Bearer token): `GET /status`,
  `GET|POST /accounts`, `DELETE /accounts/{name}`, `GET|DELETE /leases`.
  Mutations validate before write and report `reload_required`; only
  `reload.ps1` recreates containers. Passwords are returned once, by create.
- `17894` — gateway: every connection exits through a uniformly
  random pool node (up to 5 random retries across distinct nodes when the pick
  is dead); username is any name, password is TTL seconds (5–2592000)
  counted from the first request.
- `20001–20004` — fixed per-service ports (pool groups, no auth).
- `20005` — fixed port, `round-robin` rotation over the free pool.
- `17890` — unpublished (container still listens on `:7890`, unreachable from
  the host). Former all-everything endpoint; removed from the host on purpose,
  keep it that way. `17891/17892` were removed the same way.

## Data flow

```
10 public sources ──SubConv /provider──▶ pool-worker ──▶ mihomo/providers/pool-free-pool.yaml
                                              │                     │
                                              │                     ├─▶ Mihomo providers (SVC_* groups, POOL)
                                              │                     └─▶ hash-gate (reads the same files)
mihomo/providers/mine.yaml (paid proxies) ────┴─▶ inlined into generated config + CUSTOM pool
```

- Worker: parallel fetch (`max_workers`), canonical-fingerprint dedupe,
  deterministic pool assignment, `max_nodes: 1000`, refresh every 300 s,
  HTTP-level liveness probing of HTTP/SOCKS5 nodes (`common/probe.py`: real
  request through the tunnel, configurable method/headers/body/cookies,
  `[check.expect]` matching on status/headers/body/cookies/latency).
- Worker drops nodes Mihomo cannot parse (ss cipher allowlist incl. the
  `2022-blake3` base64 rule, UUID check for vmess/vless, server/port sanity).
  Count is visible as `dropped_nodes` in `/healthz`.
- A source that fails is reported `degraded` and skipped; total failure keeps
  serving the last good snapshot. Health: worker `/healthz` JSON.

## Config files

Tracked (commit these): `docker-compose.yml`, `.env.example`,
`mihomo/config.base.yaml`, `services.example.toml`,
`pool-sources.example.toml`, `scripts/*`, `pool-worker/*`, `hash-gate/*`,
docs, `VERSION`.

Local, gitignored, never commit: `.env`, `services.toml`, `pool-sources.toml`,
`mihomo/providers/mine.yaml`, `mihomo/generated/`,
`docker-compose.override.yml`, `mihomo/providers/*.yaml`.

`services.toml` essentials per `[[services]]`: `port` (omit for
dispatcher-only), `primary` (must exist in `mine.yaml`),
`fallback = [...]`, `subscriptions = [...]`, `username`+`password` (≥8 chars),
`lock_proxy` (forbids every fallback source), `balance = "round-robin"`
(requires subscriptions, forbids primary/fallback/lock). `[dispatcher] port`,
optional `[global_pools]` (`free_port`, `custom_port` — currently unused).

## The only supported apply path

```powershell
./scripts/reload.ps1
```

Order matters and is load-bearing: generate → compose config check → start
pool profile → wait for worker `healthy` → wait for gate `healthy` →
`mihomo -t -f /etc/mihomo/generated/config.yaml` → recreate relay + tester
only (never the pool services mid-run, or pools start empty). `generate.py` is
the sole writer of the active config; never hand-edit `mihomo/generated/`.

## Verification cheat-sheet

```powershell
python scripts/generate.py                                    # 14+ services, must print no error
docker compose run --rm --no-deps --entrypoint /mihomo tppr-mihomo-relay -t -d /etc/mihomo -f /etc/mihomo/generated/config.yaml
curl.exe -s -o NUL -m 25 --proxy socks5h://user:pass@host.docker.internal:17893 -w "%{http_code}" https://www.gstatic.com/generate_204   # want 204
curl.exe -s -m 25 --proxy socks5h://user:pass@host.docker.internal:17893 https://api.ipify.org   # exit IP
Invoke-RestMethod http://127.0.0.1:19090/providers/proxies    # per-provider node counts
Invoke-RestMethod http://127.0.0.1:19090/proxies/SVC_<name>   # .now + .all (group order!)
Invoke-RestMethod "http://127.0.0.1:19090/proxies/<node>/delay?timeout=8000&url=https://www.gstatic.com/generate_204"  # single-node probe
docker exec tppr-pool-worker python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```

`000` from curl usually means the group's current node just died; retry twice
before suspecting config. Fresh groups start at hash-order position 0, which
is often dead — first request failing then recovering is normal.

## Mihomo behaviors learned the hard way

- `mihomo -t` does NOT initialize proxies. A config whose provider parses but
  whose nodes are all poison passes `-t` and loads 0 live. Always follow with
  a live provider-count check.
- One unparseable node empties the WHOLE provider (proven: 20 good + 1 bad =
  0 loaded). Hence the worker-side node filter.
- A group entry in `proxies:` cannot reference a provider's node by name
  (proven with a probe container: `'probe-node' not found`). Pinned proxies
  must be inlined as top-level `proxies:` — that is what the generator does
  with `mine.yaml`.
- `fallback` groups stick to the current node, move on persistent failure, and
  move back up the list when the top recovers (observed over minutes, 60 s
  health interval). No manual pinning via API for fallback groups; `select`
  groups switch instantly via `PUT /proxies/{group}`.
- `PUT /providers/proxies/{name}` returns the CURRENT fetch error in its body —
  read it with `-SkipHttpErrorCheck`, it names the exact bad node.
- From this host `host.docker.internal` resolves to a LAN-ish IP, not
  loopback. Ports bound to `127.0.0.1` are unreachable through it; proxy ports
  are therefore published on `0.0.0.0`, the controller is not.
- Docker Hub anonymous-token `EOF` on pull/build is transient; retry works.
  The worker Dockerfile pins its base by digest for this reason.

## Pool quality truth

TCP-connectable ≠ working proxy. A pool can be 60/60 TCP-alive yet mostly
dead at the proxy-protocol level (open ports serving nothing proxyable). The
worker filters parseability, not liveness — that is a known gap, not a bug
(see below). When diagnosing an empty group: check the provider error body
first (poison node), then probe the mapped nodes directly, then suspect the
pool contents — in that order.

## Testing

- `python -m unittest discover -s scripts/tests` (28: generator + accounts)
- `python -m unittest discover -s pool-worker/tests` (6: dedupe, partition, isolation)
- `python -m unittest discover -s hash-gate/tests` (8: validation, TTL, echo round-trip)
- Lint/type order: `uvx black --check`, `uvx ruff format --check`,
  `uvx ruff check`, `uvx isort --check-only`, `uvx mypy` (per directory).
- black and ruff-format fight over multiline-string concatenation: avoid the
  pattern (assign to a variable first).
- PowerShell has no heredocs; use temp script files for multiline code.
- Scale facts, measured: 1000 dispatcher accounts generate in ~0.4 s,
  `mihomo -t` in ~3 s, runtime +~50 MB RAM.

## Conventions

- English for code, comments, docs, commits. Conventional Commits
  (`feat:`/`fix:`/`chore:`), commits land on `master` when the user says so —
  never push unless asked.
- Fail loud in the generator: typos must abort the run, never produce a
  silently-wrong group (unknown primary, port clash, short password, bad
  lock/balance combination).
- No hardcoded ports/values in code: service ports live in `services.toml`,
  host ports in `.env`, gate ports in compose env with `:-defaults`.
- Never read container logs without being asked; the controller API and
  `/healthz` are the observability path. Never `docker compose logs -f` into
  a report.
- Temp work goes to the platform temp dir, never into the repo. Clean up probe
  containers/files when done.

## Known limitations / next

- Pool nodes pass an HTTP-level liveness gate (`common/probe.py`): the probe
  completes a real request through each HTTP/SOCKS5 node and matches status,
  headers, body, cookies and latency. Trojan/VLESS/SOCKS4 and stranger
  protocols still pass through unchecked — extending the prober to them is
  the remaining pool-quality work.
- Hash gate reaches only HTTP/SOCKS5 upstreams (~65% of a pool). Planned, not
  built: Trojan + VLESS forwarding in `hash-gate/gate.py` (stdlib TLS and
  framing, lifts coverage to ~90%), then VMess + Shadowsocks (needs an AEAD
  package, ~98%). Reality/XTLS-vision and Hysteria2 are out of scope.
- No Telegram management bot yet (decided: aiogram 3.x when built).
- Scale-out idea (not built): one gateway port instead of N, pool chosen by
  username prefix (`free:<name>`, `batch2:<name>`). Separate paid batches
  become pool files, not new ports.
- `mihomo -t` without `-f` validates an auto-created EMPTY config — always
  pass `-f /etc/mihomo/generated/config.yaml` (same in scripts).

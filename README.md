# proxy-pool 0.10.0 — a global pool, LAN-reachable proxy ports, an optional dispatcher, pinned accounts, and local pool aggregation.

## What it does
- `proxy-pool-mihomo-relay` exposes HTTP/SOCKS inside Docker and publishes the
  proxy ports on all host interfaces (`HOST_MIXED_PORT`, 17890 in the checked-in
  example). The controller stays on `127.0.0.1` only. The
  `POOL` selector is the production route; the separate `TEST_POOL` selector
  is used only by the speed tester, so benchmarks never reroute live Telegram
  or YouTube connections.
- Health loop: only the current production node is checked every 10s. On
  failure, candidates are latency-probed in batches of 100 with 100 workers;
  failover stops after 1 good candidate; a scheduled sweep collects up to 5
  good candidates and benchmarks at most 5 total nodes including production.
- Slow loop (`proxy-pool-speed-tester`, default every 300s): downloads a test
  file through up to 5 total nodes (the current production node plus up to 4
  discovered candidates), measures throughput, then updates only `POOL` to the
  fastest healthy node. Results persist in SQLite (`tester-data` volume): nodes
  failing 3 sweeps in a row are
  skipped but retried once failures age past `HISTORY_COOLDOWN_H`; old rows are
  pruned. Mihomo's persisted production selection is kept on startup; stale or
  dead selections go through speed-tested failover. No background health checks
  run across the full provider.
- Routing: Telegram and YouTube go through `POOL`, everything else is `DIRECT`.
- Custom upstream proxies live in `mihomo/providers/mine.yaml` and are inlined
  into the generated config; the generator is the only writer of the active
  config, so `scripts/reload.ps1` is the only supported way to apply changes.

## Services and fixed ports
- Each `[[services]]` block in the gitignored `services.toml` gets a group and,
  unless the service is reachable through the dispatcher, a fixed port
  reachable from the LAN. Ports are declared in that file, not in `.env`, and are
  generated into `docker-compose.override.yml`. The controller API is the only
  thing still bound to `127.0.0.1`.
- A service selects upstream nodes in one of two ways:
  - `subscriptions = ["name", ...]` — every node of those providers;
  - `primary = "proxy-name"` — one exact proxy, optionally followed by
    `fallback = ["other-proxy"]` and the providers in `subscriptions`.
- Service groups are Mihomo `fallback` groups, so a dead node is skipped for
  the next one. `empty-fallback: REJECT` means an empty provider rejects the
  connection instead of leaking traffic direct.

## Subscriptions
- Add one `[[subscriptions]]` block per remote feed; Mihomo pulls it natively
  as a `proxy-provider`, so Clash/Mihomo YAML, URI lists, and base64
  subscriptions all work without a converter:
  ```toml
  [[subscriptions]]
  name = "my-vless"
  url = "https://example.com/subscription"
  interval = 21600
  ```
- Subscription URLs can contain tokens; keep `services.toml` local and do not commit it.

## Local pool aggregation
- Copy `pool-sources.example.toml` to `pool-sources.toml` and assign the ten sources to named pools. The file is gitignored because source URLs may contain tokens.
- The `pool-aggregation` Compose profile starts SubConv and an internal pool worker. No worker or SubConv port is published to Windows.
- The worker calls SubConv's `/provider` endpoint, accepts Clash/URI/base64 output, removes cross-source duplicates by canonical node fingerprint, and writes deterministic pool files such as `mihomo/providers/pool-free-pool.yaml`.
- Sources are fetched in parallel (`worker.max_workers`). A dead source is reported as `degraded` in `/healthz` and skipped instead of taking the other pools down; the last good pool snapshot is kept when every source fails.
- The worker also drops nodes Mihomo cannot parse. Mihomo empties an entire `proxy-provider` when a single node fails to initialize, so one free node with an unsupported shadowsocks cipher or a malformed key would otherwise take the whole pool down. The count is reported as `dropped_nodes` in `/healthz`.
- `max_nodes` caps a pool at 1000 nodes; raise `MAX_NODES` in `pool-worker/worker.py` to go higher.
- Reference pool names from `services.toml` with `subscriptions = ["free-pool"]`. A source may appear in several pools; a node is assigned to one pool by stable hash unless the pool is marked `shared = true`.
- Pools use Mihomo fallback failover. Per-pool throughput rotation is not enabled; the existing speed tester manages only the global `POOL`.
- `reload.ps1` enables the profile automatically when `pool-sources.toml` exists.

## Dispatcher
- Add one optional authenticated entry point; fixed per-service ports keep working unchanged:
  ```toml
  [dispatcher]
  port = 20000
  ```
- Add `username` and `password` to any service that should be reachable through it. A service with credentials no longer needs its own `port`, so hundreds of accounts can share the single dispatcher port:
  ```toml
  [[services]]
  name = "acc01"
  primary = "my-proxy-01"
  subscriptions = ["my-vless"]
  username = "acc01"
  password = "change-me-at-least-8"
  ```
- Mihomo authenticates the user and routes the connection with `IN-USER` to `SVC_acc01`. Use `http://acc01:change-me-at-least-8@127.0.0.1:20000` or `socks5://acc01:change-me-at-least-8@127.0.0.1:20000` in clients that support proxy authentication.
- Services without credentials remain available only on their fixed port. Passwords stay in the gitignored `services.toml`.
- `MAX_SERVICES` is 1000. Exceeding it names the constant and the file to edit.
- `scripts/new_accounts.py` writes account blocks in bulk; see `scripts/README.md`.

## Pinned accounts and fallback
- `primary` names one proxy from `mihomo/providers/mine.yaml`. Mihomo does not resolve provider proxies by name, so the generator inlines those definitions into the generated config; a name that is not in the file fails generation instead of starting a dead group.
- With `primary` plus `subscriptions`, the group is a `fallback` chain: the pinned proxy first, then the free pool. When the pinned proxy dies the account silently continues on a free node.
- `lock_proxy = true` removes every fallback source for that account. When its proxy is down the connection is rejected instead of leaving through a free node. The generator refuses to combine `lock_proxy` with `fallback`/`subscriptions`.
- `fallback = ["name"]` inserts extra named proxies between the primary and the free pool.
- `balance = "round-robin"` turns the group into a load-balance pool: every new connection exits through the next node instead of sticking to one. It needs `subscriptions` and forbids `primary`, `fallback` and `lock_proxy`. The effective variety equals the currently healthy nodes, so a free pool rotates between its survivors.

## Hash-routed expiring access
- Three extra ports serve one pool each without touching Mihomo: `17894` reads
  every pool file plus the custom proxies, `17895` reads the worker pools only,
  `17896` reads the custom proxies only. Override them with `HASH_GATE_*_PORT`.
- Authentication is also routing and expiry. The username is a hex hash that
  picks the exit node (`int(hash, 16) % nodes`); the password is a TTL in
  seconds, from 15 up to 30 days. The countdown starts at the first request and
  is stored in SQLite, so restarts do not extend it. An expired credential is
  rejected like a wrong password.
- Generate a username with `python -c "import secrets;print(secrets.token_hex(8))"`.
  The same hash always exits through the same node while the pool file is
  unchanged; pool refreshes may shift the mapping.
- Only HTTP and SOCKS5 upstreams are forwardable, so roughly two thirds of a
  pool are reachable through these ports. The rest needs the Mihomo groups.
- `reload.ps1` starts the gate with the pool profile and waits for it to report
  healthy before applying anything else.

## Global pool endpoints
- `17890` (from `HOST_MIXED_PORT`) is the `POOL` group: subscriptions, local pools, and custom proxies.
- Two optional extra endpoints split that mix:
  ```toml
  [global_pools]
  free_port = 17891    # FREE group: subscriptions and local pools only
  custom_port = 17892  # CUSTOM group: mihomo/providers/mine.yaml only
  ```
- Both share the LAN-reachable binding with every other proxy port in the stack; only the controller stays on `127.0.0.1`.
- A port that has no matching source is a generation error: `custom_port` without custom proxies, or `free_port` without any subscription or pool.

## Quickstart
```powershell
Copy-Item .env.example .env
Copy-Item services.example.toml services.toml
# ports default to 7890/9090 — change them in .env, README examples follow .env
# private upstreams are optional — free pool works alone:
# Copy-Item mihomo\providers\mine.yaml.example mihomo\providers\mine.yaml
# local pool aggregation is optional — copy pool-sources.example.toml to
# pool-sources.toml to switch the pool-aggregation profile on.
powershell -ExecutionPolicy Bypass -File .\scripts\reload.ps1
curl.exe --proxy http://127.0.0.1:7890 https://www.gstatic.com/generate_204 -o NUL -w "%{http_code}`n"
```

## Scaling ports
Add another `[[services]]` block with a fixed localhost port in `services.toml`, then run `scripts/reload.ps1`. Service ports do not need variables in `.env`.

## Manual rotation
```powershell
# list production members (not a speed ranking)
Invoke-RestMethod http://127.0.0.1:9090/proxies/POOL | Select-Object -ExpandProperty all
# pin a node
Invoke-RestMethod -Method Put http://127.0.0.1:9090/proxies/POOL -Body '{"name":"node-17"}' -ContentType 'application/json'
# pool worker health, including per-source node counts and dropped nodes
docker exec proxy-pool-pool-worker python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```
Controller and mixed ports in those examples are container ports; on the host use the values of `HOST_CONTROLLER_PORT` and `HOST_MIXED_PORT` from `.env`.

## Publishing to GitHub
```powershell
git remote add origin https://github.com/<you>/proxy-pool.git
git push -u origin master
```
`.env`, `services.toml`, `pool-sources.toml`, `mihomo/providers/mine.yaml`, and `mihomo/generated/` are gitignored — secrets never leave the machine.

## Layout
- `docker-compose.yml` / `.env.example` — topology and tunables (single source).
- `mihomo/config.base.yaml` — template: isolated production/test groups, internal speed-test listener and TG/YT rules.
- `services.example.toml` / `services.toml` — dispatcher, subscriptions, pinned proxies, and fixed-port service-to-proxy mapping; one block can be copied for each service or account, and the same proxy may appear in multiple blocks.
- `pool-sources.example.toml` / `pool-sources.toml` — remote source catalog, pool membership, caps, and shared-pool policy.
- `scripts/generate.py` — validates the mapping and generates active config/Compose override.
- `scripts/new_accounts.py` — writes dispatcher account blocks with generated passwords.
- `pool-worker/worker.py` — SubConv client, node validation, canonical dedupe, deterministic pool builder, and internal pool HTTP endpoint.
- `mihomo/providers/mine.yaml.example` — template for private upstreams.
- `mihomo/generated/config.yaml` — generated active config; never edit it by hand.
- `speed-tester/tester.py` — throughput sweeps (stdlib only, structured logs).
- `speed-tester/history.py` — SQLite sweep history and cooldown tracking.
- `scripts/README.md` — what each script does and every error it can raise.
- `scripts/smoke.ps1` — config + syntax sanity checks.
- `scripts/reload.ps1` — regenerate, wait for the pool worker, validate with Mihomo, then recreate the relay and tester.

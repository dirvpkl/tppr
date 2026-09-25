# proxy-pool 0.2.0 — one local HTTP/SOCKS endpoint backed by a pool of upstreams.

## What it does
- `proxy-pool-mihomo-relay` exposes HTTP/SOCKS on `127.0.0.1:7890`. The
  `POOL` selector is the production route; the separate `TEST_POOL` selector
  is used only by the speed tester, so benchmarks never reroute live Telegram
  or YouTube connections. Own upstreams: copy the provider example and
  uncomment its block in `config.yaml`, add `mine` to both `use` lists, and
  set `PROVIDER_NAMES=free-vless,mine`.
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

## Quickstart
```powershell
Copy-Item .env.example .env
# ports default to 7890/9090 — change them in .env, README examples follow .env
# private upstreams are optional — free pool works alone:
# Copy-Item mihomo\providers\mine.yaml.example mihomo\providers\mine.yaml
# then uncomment the mine block in mihomo\config.yaml and add `mine` to both groups
# and set PROVIDER_NAMES=free-vless,mine in .env
docker compose config
docker compose up -d
curl.exe --proxy http://127.0.0.1:7890 https://www.gstatic.com/generate_204 -o NUL -w "%{http_code}`n"
```

## Scaling ports
Publishing another port is one mapping line in `docker-compose.yml` plus one
variable in `.env` / `.env.example` — no rebuild, `docker compose up -d` only.

## Manual rotation
```powershell
# list production members (not a speed ranking)
Invoke-RestMethod http://127.0.0.1:9090/proxies/POOL | Select-Object -ExpandProperty all
# pin a node
Invoke-RestMethod -Method Put http://127.0.0.1:9090/proxies/POOL -Body '{"name":"node-17"}' -ContentType 'application/json'
```

## Publishing to GitHub
```powershell
git remote add origin https://github.com/<you>/proxy-pool.git
git push -u origin master
```
`.env` and `mihomo/providers/mine.yaml` are gitignored — secrets never leave the machine.

## Layout
- `docker-compose.yml` / `.env.example` — topology and tunables (single source).
- `mihomo/config.yaml` — isolated production/test groups, internal speed-test
  listener and TG/YT rules.
- `mihomo/providers/mine.yaml.example` — template for private upstreams.
- `speed-tester/tester.py` — throughput sweeps (stdlib only, structured logs).
- `speed-tester/history.py` — SQLite sweep history and cooldown tracking.
- `scripts/smoke.ps1` — config + syntax sanity checks.

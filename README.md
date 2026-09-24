# proxy-pool 0.1.0 — one local HTTP/SOCKS endpoint backed by a pool of upstreams.

## What it does
- `proxy-pool-mihomo-relay` listens on `127.0.0.1:7890` (mixed HTTP/SOCKS) and
  rotates the `POOL` group across the `free-vless` provider (own `mine`
  provider: copy the example and uncomment its block in `config.yaml`).
- Fast loop (mihomo `url-test`, every 10s): latency/heartbeat probe, dead nodes
  are skipped automatically. Existing connections are never interrupted
  (`interrupt_exist_connections: false`) — rotation is seamless.
- Slow loop (`proxy-pool-speed-tester`, default every 300s): takes the 25
  lowest-latency members, downloads a test file through each, measures real
  throughput (Mbps, not ping), and pins the group to the fastest healthy node
  via the Clash API. Giant free pools: shrink with the `filter` regex in
  `config.yaml`.
- Routing: Telegram and YouTube go through `POOL`, everything else is `DIRECT`.

## Quickstart
```powershell
Copy-Item .env.example .env
# ports default to 7890/9090 — change them in .env, README examples follow .env
# private upstreams are optional — free pool works alone:
# Copy-Item mihomo\providers\mine.yaml.example mihomo\providers\mine.yaml
# then uncomment the mine block in mihomo\config.yaml
docker compose config
docker compose up -d
curl.exe --proxy http://127.0.0.1:7890 https://www.gstatic.com/generate_204 -o NUL -w "%{http_code}`n"
```

## Scaling ports
Publishing another port is one mapping line in `docker-compose.yml` plus one
variable in `.env` / `.env.example` — no rebuild, `docker compose up -d` only.

## Manual rotation
```powershell
# fastest node right now (latency view)
Invoke-RestMethod http://127.0.0.1:9090/proxies/POOL | Select-Object -ExpandProperty all
# pin a node
Invoke-RestMethod -Method Put http://127.0.0.1:9090/proxies/POOL -Body '{"name":"node-17"}' -ContentType 'application/json'
```

## Publishing to GitHub
```powershell
git remote add origin https://github.com/<you>/proxy-pool.git
git push -u origin main
```
`.env` and `mihomo/providers/mine.yaml` are gitignored — secrets never leave the machine.

## Layout
- `docker-compose.yml` / `.env.example` — topology and tunables (single source).
- `mihomo/config.yaml` — pool, 10s heartbeat group, TG/YT rules.
- `mihomo/providers/mine.yaml.example` — template for private upstreams.
- `speed-tester/tester.py` — throughput sweeps (stdlib only, structured logs).
- `scripts/smoke.ps1` — config + syntax sanity checks.

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath ".\services.toml")) {
    Copy-Item -LiteralPath ".\services.example.toml" -Destination ".\services.toml"
}

$composeProfile = if (Test-Path -LiteralPath ".\pool-sources.toml") { @("--profile", "pool-aggregation") } else { @() }

python .\scripts\generate.py
if ($LASTEXITCODE -ne 0) { throw "service config generation failed" }

docker compose @composeProfile config --quiet
if ($LASTEXITCODE -ne 0) { throw "docker compose config failed" }

# Pool providers are served over the internal network, so the worker must be
# healthy before Mihomo validates or loads the generated config. The hash gate
# reads the same pool files and tolerates them missing, so it starts alongside.
if ($composeProfile.Count -gt 0) {
    docker compose @composeProfile up -d --build --force-recreate tppr-subconv tppr-pool-worker tppr-hash-gate
    if ($LASTEXITCODE -ne 0) { throw "pool-aggregation startup failed" }

    $workerReady = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        $health = docker inspect --format "{{.State.Health.Status}}" tppr-pool-worker 2>$null
        if ($health -eq "healthy") { $workerReady = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $workerReady) { throw "pool-worker did not become healthy" }

    $gateReady = $false
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        $health = docker inspect --format "{{.State.Health.Status}}" tppr-hash-gate 2>$null
        if ($health -eq "healthy") { $gateReady = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $gateReady) { throw "hash-gate did not become healthy" }
}

docker compose run --rm --no-deps --entrypoint /mihomo tppr-mihomo-relay -t -d /etc/mihomo -f /etc/mihomo/generated/config.yaml
if ($LASTEXITCODE -ne 0) { throw "Mihomo rejected the generated config" }

# Only the relay, the tester and the management API are recreated here: the
# pool services are already healthy, and recreating them again would empty
# the pools during startup.
docker compose up -d --build --force-recreate tppr-mihomo-relay tppr-speed-tester tppr-api
if ($LASTEXITCODE -ne 0) { throw "docker compose startup failed" }

Write-Host "Proxy services applied. Run 'docker compose logs -f tppr-mihomo-relay' to inspect them."

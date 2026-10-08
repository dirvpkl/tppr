param(
    # Force the slow path: rebuild images, recreate the pool services from
    # scratch (full pool refetch + probe, takes minutes) and wait for them.
    # Default (no flag) is the fast path: when the pool worker is already
    # ready and the pool snapshots are fresh, the pool services are left
    # alone and only the relay picks up the new config (seconds).
    [switch]$Full
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath ".\services.toml")) {
    Copy-Item -LiteralPath ".\services.example.toml" -Destination ".\services.toml"
}

$composeProfile = if (Test-Path -LiteralPath ".\pool-sources.toml") { @("--profile", "pool-aggregation") } else { @() }

# Pool worker readiness, read from its own /healthz. Docker's health status
# lags behind the real state (it still says "starting" when /healthz already
# reports ready:true), so check the endpoint first and fall back to docker.
function Test-PoolWorkerReady {
    $probe = "import urllib.request,urllib.error,json`ntry:`n    d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=10))`n    print('READY' if d.get('ready') else 'NOTREADY')`nexcept Exception:`n    print('NOTREADY')"
    $out = docker exec tppr-pool-worker python -c $probe 2>$null
    if ($out -eq "READY") { return $true }
    $health = docker inspect --format "{{.State.Health.Status}}" tppr-pool-worker 2>$null
    return $health -eq "healthy"
}

function Test-PoolSnapshotsFresh {
    param([int]$MaxAgeMinutes = 60)
    $files = Get-ChildItem -LiteralPath ".\mihomo\providers" -Filter "pool-*.yaml" -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -gt 0 }
    if ($files.Count -eq 0) { return $false }
    $newest = $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    return ((Get-Date) - $newest.LastWriteTime).TotalMinutes -lt $MaxAgeMinutes
}

function Wait-PoolWorker {
    param([int]$Attempts = 120)
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (Test-PoolWorkerReady) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Wait-HashGate {
    param([int]$Attempts = 24)
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $health = docker inspect --format "{{.State.Health.Status}}" tppr-hash-gate 2>$null
        if ($health -eq "healthy") { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

python .\scripts\generate.py
if ($LASTEXITCODE -ne 0) { throw "service config generation failed" }

docker compose @composeProfile config --quiet
if ($LASTEXITCODE -ne 0) { throw "docker compose config failed" }

# Pool providers are served over the internal network, so the worker must be
# ready before Mihomo validates or loads the generated config. The hash gate
# reads the same pool files and tolerates them missing, so it starts alongside.
# Fast path: worker already ready + snapshots fresh = do not touch the pool
# services (recreating them would empty the pools and force a minutes-long
# refetch). Just make sure they are running.
if ($composeProfile.Count -gt 0) {
    $workerReady = Test-PoolWorkerReady
    $poolsFresh = Test-PoolSnapshotsFresh
    if (-not $Full -and $workerReady -and $poolsFresh) {
        Write-Host "Pool worker ready, snapshots fresh - fast path, leaving pool services alone."
        docker compose @composeProfile up -d tppr-subconv tppr-pool-worker tppr-hash-gate
        if ($LASTEXITCODE -ne 0) { throw "pool services startup failed" }
    } else {
        if ($Full) { Write-Host "Full reload requested - rebuilding and refetching pools." }
        else { Write-Host "Pool worker not ready or snapshots stale - full pool startup." }
        docker compose @composeProfile up -d --build --force-recreate tppr-subconv tppr-pool-worker tppr-hash-gate
        if ($LASTEXITCODE -ne 0) { throw "pool-aggregation startup failed" }

        if (-not (Wait-PoolWorker)) { throw "pool-worker did not become healthy" }
        if (-not (Wait-HashGate)) { throw "hash-gate did not become healthy" }
    }
}

docker compose run --rm --no-deps --entrypoint /mihomo tppr-mihomo-relay -t -d /etc/mihomo -f /etc/mihomo/generated/config.yaml
if ($LASTEXITCODE -ne 0) { throw "Mihomo rejected the generated config" }

# Only the relay, the tester and the management API are recreated here: the
# pool services are already healthy, and recreating them again would empty
# the pools during startup.
docker compose up -d --force-recreate tppr-mihomo-relay tppr-speed-tester tppr-api tppr-post-prober
if ($LASTEXITCODE -ne 0) { throw "docker compose startup failed" }

Write-Host "Proxy services applied. Status: Invoke-RestMethod http://127.0.0.1:18080/status (API token required)."

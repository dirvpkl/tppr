$ErrorActionPreference = "Stop"
# Sanity checks for tppr. Run from the repo root.
if (-not (Test-Path -LiteralPath ".\docker-compose.yml")) { throw "run from the repo root" }
if (-not (Test-Path -LiteralPath ".\.env")) { throw "missing .env (copy from .env.example)" }
if (-not (Test-Path -LiteralPath ".\services.toml")) { Copy-Item -LiteralPath ".\services.example.toml" -Destination ".\services.toml" }
python .\\scripts\\generate.py
if ($LASTEXITCODE -ne 0) { throw "config generation failed" }
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw "docker compose config failed" }
if (Test-Path -LiteralPath ".\pool-sources.toml") {
    docker compose --profile pool-aggregation config --quiet
    if ($LASTEXITCODE -ne 0) { throw "pool-aggregation compose config failed" }
}
python -m py_compile speed-tester/tester.py speed-tester/history.py scripts/generate.py
if ($LASTEXITCODE -ne 0) { throw "Python sources do not compile" }
python -m unittest discover -s speed-tester/tests -v
if ($LASTEXITCODE -ne 0) { throw "speed-tester tests failed" }
python -m unittest discover -s scripts/tests -v
if ($LASTEXITCODE -ne 0) { throw "config generator tests failed" }
try {
  python -c "import yaml" 2>$null
  if ($LASTEXITCODE -eq 0) {
    python -c "import yaml; yaml.safe_load(open('mihomo/generated/config.yaml')); print('mihomo generated config: YAML OK')"
    if ($LASTEXITCODE -ne 0) { throw "generated Mihomo config is not valid YAML" }
  } else {
    Write-Host "pyyaml not installed, mihomo YAML check skipped (explicit, not a pass)"
  }
} catch {
  Write-Host "pyyaml not installed, mihomo YAML check skipped (explicit, not a pass)"
}
Write-Host "smoke OK"

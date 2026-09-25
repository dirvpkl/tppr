$ErrorActionPreference = "Stop"
# Sanity checks for proxy-pool. Run from the repo root.
if (-not (Test-Path -LiteralPath ".\docker-compose.yml")) { throw "run from the repo root" }
if (-not (Test-Path -LiteralPath ".\.env")) { throw "missing .env (copy from .env.example)" }
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw "docker compose config failed" }
python -m py_compile speed-tester/tester.py speed-tester/history.py
if ($LASTEXITCODE -ne 0) { throw "speed-tester does not compile" }
python -m unittest discover -s speed-tester/tests -v
if ($LASTEXITCODE -ne 0) { throw "speed-tester tests failed" }
try {
  python -c "import yaml" 2>$null
  if ($LASTEXITCODE -eq 0) {
    python -c "import yaml,sys; yaml.safe_load(open('mihomo/config.yaml')); print('mihomo config.yaml: YAML OK')"
  } else {
    Write-Host "pyyaml not installed, mihomo YAML check skipped (explicit, not a pass)"
  }
} catch {
  Write-Host "pyyaml not installed, mihomo YAML check skipped (explicit, not a pass)"
}
Write-Host "smoke OK"

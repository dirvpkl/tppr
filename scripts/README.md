# Scripts

Entry points for this repo. Run them from the repo root.

## `reload.ps1` — apply everything

```powershell
./scripts/reload.ps1
```

What it does, in order:

1. copies `services.example.toml` to `services.toml` if that file is missing;
2. runs `generate.py` (fails loudly on any config error);
3. validates the Compose file;
4. if `pool-sources.toml` exists, starts the `pool-aggregation` profile and waits
   for `proxy-pool-pool-worker` to report `healthy` — pool files are served over
   the internal Docker network, so Mihomo needs them before it starts;
5. validates the generated config with `mihomo -t`;
6. recreates only the relay and the speed tester, leaving the healthy pool
   services alone so the pools are never empty during startup.

A worker that never becomes healthy aborts the run instead of starting a relay
with empty pools.

## `generate.py` — build the active config

```powershell
python scripts/generate.py
python scripts/generate.py --services services.toml --pools pool-sources.toml
```

Reads `services.toml`, `pool-sources.toml` and `mihomo/providers/mine.yaml`, then
writes `mihomo/generated/config.yaml` and `docker-compose.override.yml`. Never
edit those two outputs by hand.

Validation is strict on purpose — a typo should stop the run, not produce a
service that silently fails over to a different proxy:

| Rule | Error |
| --- | --- |
| `primary` not present in `mine.yaml` | `is not defined in the custom proxy file` |
| service has neither `port` nor `username` | `requires port or dispatcher credentials` |
| `lock_proxy = true` together with a fallback source | `lock_proxy forbids ...` |
| `lock_proxy = false` without any fallback source | `requires a fallback source` |
| `balance` with `primary`, `fallback` or `lock_proxy` | `balance rotates the whole pool, so it forbids ...` |
| `balance` without `subscriptions` | `balance requires ...` |
| unknown `balance` value | `balance must be one of round-robin, consistent-hashing` |
| password shorter than 8 characters | `at least 8 characters` |
| two listeners on the same port | `duplicates another listener port` |
| more than `MAX_SERVICES` entries | names the constant and the file to edit |

`MAX_SERVICES` lives at the top of `scripts/generate.py`. 1000 accounts
generate in about 0.4 s and run in roughly 50 MB of RAM.

## `new_accounts.py` — generate dispatcher accounts

Writes account blocks into the gitignored `services.toml`. Passwords are
generated there and stored nowhere else.

```powershell
# 200 accounts named myuser, myuser1, ... myuser199, all on one dispatcher port
python scripts/new_accounts.py --count 200 --name myuser --port 17893 --provider free-pool
```

| Argument | Meaning |
| --- | --- |
| `--count` | how many accounts to append |
| `--name` | first account name; the rest get a numeric suffix |
| `--port` | dispatcher port shared by every account |
| `--provider` | free provider used as fallback; repeat for several |
| `--password-length` | password length, 8-128, default 16 |
| `--services` | target file, default `services.toml` |

Naming: the first account is the name as typed, then `name1`, `name2`, and so
on. Names already present in the file are skipped, so running the command twice
continues where it stopped instead of colliding.

The `[dispatcher]` block is created on the first run. If it already exists with
a different port, the script refuses and tells you which port is in use.

Every generated account gets `subscriptions = [...]` only, so it uses the free
pool. To pin an account to a proxy of your own, add two lines to its block:

```toml
primary = "my-proxy-01"   # a name from mihomo/providers/mine.yaml
lock_proxy = true         # only if it must never fall back to a free node
```

## `smoke.ps1` — sanity checks

```powershell
./scripts/smoke.ps1
```

Compose config check, Python syntax check and the unit tests. It does not touch
running containers.

## Hash gate ports

`proxy-pool-hash-gate` serves three ports that need no Mihomo groups at all:
`HASH_GATE_ALL_PORT` (default 17894, every pool file plus the custom proxies),
`HASH_GATE_FREE_PORT` (default 17895, worker pools only) and
`HASH_GATE_CUSTOM_PORT` (default 17896, custom proxies only).

Connect with a hex username and a TTL-seconds password:

```powershell
curl.exe --proxy socks5h://a1b2c3d4e5f60718:3600@host.docker.internal:17895 https://api.ipify.org
```

The same username always exits through the same node while the pool file is
unchanged. After the TTL elapses from the first request the credential is
rejected. Only HTTP and SOCKS5 upstreams are forwardable; a pool with none of
those rejects every connection. Leases live in the `hash-gate-data` volume.

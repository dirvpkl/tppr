# Proxy Guide

How to create and manage proxies in this repo. All commands run from the repo
root. `services.toml`, `pool-sources.toml` and `mihomo/providers/mine.yaml`
are gitignored: credentials never leave the machine.

## One node per service, proxies may repeat

A paid proxy is the upstream account (server, port, credentials); a node is
a named entry in `mine.yaml` that uses it. Several nodes may point at the
same paid proxy — same creds under different names (e.g. `tg1`, `cfprg`,
`ggfr` on one account) — that is normal. Each service still gets its OWN
node: `name = X` must be followed by `primary = "X"` (except `select = true`
accounts like `opencode-acc`). When a service needs egress: add a node in
`mine.yaml` for it (creds may repeat an existing paid proxy), then point
only that service at it. A service without its own node gets no primary —
pool-only is not a fallback, it is an outage waiting to happen.

## Steady state (keep it like this)

Every service resolves to its OWN named node — verify with
`services.toml` (`name = X` must be followed by `primary = "X"` for all
managed services, except `select = true` accounts like `opencode-acc`).
Spot-check after every change: regenerate (`generate.py`), validate
(`compose config`), reload, then CONNECT-test 2–3 services. If a new
service arrives: create its node in `mine.yaml` first (creds may repeat an
existing paid proxy),
then the service block, then regenerate → validate → reload → probe.

## Port map

| Port | What | Auth |
| --- | --- | --- |
| 17893 | Dispatcher: one account per login, each routed to its own group | login + password |
| 17894 | Hash gate, all pools: random node per connection, password is the TTL | any name + TTL |

The controller API (`HOST_CONTROLLER_PORT`, localhost only) has no password:
keep it off the LAN.

## Add a paid proxy

Append it to `mihomo/providers/mine.yaml`:

```yaml
proxies:
  - name: "my-proxy-01"
    type: socks5
    server: 203.0.113.10
    port: 1080
    username: "user"
    password: "pass"
```

Anything listed there is automatically added to the `POOL` and `CUSTOM`
groups on the next reload, and becomes available as `primary`/`fallback` for
accounts. No group editing is needed. Then apply:

```sh
./scripts/reload.ps1
curl --proxy http://127.0.0.1:20005 https://www.gstatic.com/generate_204 -o /dev/null -w "%{http_code}\n"
```

## Create an account

```sh
python scripts/new_accounts.py --count 1 --name acc --port 17893 --provider free-pool
```

Names continue past existing ones (`acc`, `acc1`, `acc2`, …). Passwords are
generated into `services.toml` and stored nowhere else. Then reload.

## Account recipes

Free pool only, no pinned proxy:

```toml
[[services]]
name = "acc01"
subscriptions = ["free-pool"]
username = "acc01"
password = "at-least-8-chars"
```

Pinned proxy with free-pool fallback (`tg1` must exist in `mine.yaml`):

```toml
[[services]]
name = "acc02"
primary = "tg1"
subscriptions = ["free-pool"]
username = "acc02"
password = "at-least-8-chars"
```

Extra proxies between the primary and the pool:

```toml
[[services]]
name = "acc03"
primary = "tg1"
fallback = ["tg2", "tg3"]
subscriptions = ["free-pool"]
username = "acc03"
password = "at-least-8-chars"
```

Locked to its proxy, never falls back (refuses the connection instead):

```toml
[[services]]
name = "acc04"
primary = "tg1"
lock_proxy = true
username = "acc04"
password = "at-least-8-chars"
```

Rotating exit, a different node per connection (pool only, no pinning):

```toml
[[services]]
name = "rotate"
port = 20005
balance = "round-robin"
subscriptions = ["free-pool"]
```

Externally steered exit: the group is a plain `select` list and never moves
on its own — a prober container picks the node (see `post-prober/`). The
service needs `select = true` (requires `subscriptions`, forbids everything
else); the probe itself lives in the gitignored `post-prober.toml` +
`post-prober-body.json` (copy the `.example` files to start):

```toml
[[services]]
name = "managed"
subscriptions = ["my-vless"]
select = true
username = "managed"
password = "at-least-8-chars"
```

Then copy `post-prober.example.toml` to `post-prober.toml` and
`post-prober-body.example.json` to `post-prober-body.json`, fill in the URL,
headers and expected status, and run `./scripts/reload.ps1`. The prober refuses
to start if `target.group` / `target.provider` do not belong to that account, so
a typo there is a startup error, not a silently idle group.

Check what it settled on:

```powershell
Invoke-RestMethod http://127.0.0.1:19090/proxies/SVC_managed | Select-Object -ExpandProperty now
docker inspect tppr-post-prober --format '{{.State.Health.Status}}'
```

`unhealthy` means no probe round completed within
`POST_PROBER_HEARTBEAT_MAX_AGE_S` (default 180 s).

Connect to any account through the dispatcher (HTTP and SOCKS5 both work):

```
socks5://username:password@host:17893
http://username:password@host:17893
```

## Expiring random access (port 17894)

No account needed. Every connection exits through a random pool node. The
username is any name, the password is a TTL in seconds (5 to 2592000).
The countdown starts at the first request:

```sh
curl --proxy socks5h://random:60@host:17894 https://api.ipify.org
```

After the TTL the credential is rejected. Only HTTP and SOCKS5 upstreams are
reachable this way.

## Apply and verify

```sh
./scripts/reload.ps1
curl --proxy http://127.0.0.1:20005 https://www.gstatic.com/generate_204 -o /dev/null -w "%{http_code}\n"
```

The generator fails loudly on typos (unknown `primary`, duplicate ports,
passwords under 8 chars) instead of starting a silently wrong config. See
`scripts/README.md` for the full error table.

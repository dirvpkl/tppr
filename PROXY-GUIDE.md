# Proxy Guide

How to create and manage proxies in this repo. All commands run from the repo
root. `services.toml`, `pool-sources.toml` and `mihomo/providers/mine.yaml`
are gitignored: credentials never leave the machine.

## Port map

| Port | What | Auth |
| --- | --- | --- |
| 17890 | Everything: paid proxies plus the whole free pool, rule-routed | none |
| 17891 | Free pool only | none |
| 17892 | Paid proxies only (`mine.yaml`) | none |
| 17893 | Dispatcher: one account per login, each routed to its own group | login + password |
| 17894 | Hash gate, all pools: username picks the node, password is the TTL | hash + TTL |
| 17895 | Hash gate, free pool only | hash + TTL |
| 17896 | Hash gate, paid proxies only | hash + TTL |
| 20001+ | Fixed per-service ports, one group each | none |

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

Connect to any account through the dispatcher (HTTP and SOCKS5 both work):

```
socks5://username:password@host:17893
http://username:password@host:17893
```

## Expiring hash access (ports 17894–17896)

No account needed. The username is a hex hash that deterministically picks the
exit node; the password is a TTL in seconds (15 to 2592000). The countdown
starts at the first request:

```sh
python -c "import secrets;print(secrets.token_hex(8))"
curl --proxy socks5h://<hash>:3600@host:17894 https://api.ipify.org
```

Same hash, same exit node (until the pool file refreshes). After the TTL the
credential is rejected. Only HTTP and SOCKS5 upstreams are reachable this way.

## Apply and verify

```sh
./scripts/reload.ps1
curl --proxy http://127.0.0.1:17890 https://www.gstatic.com/generate_204 -o /dev/null -w "%{http_code}\n"
```

The generator fails loudly on typos (unknown `primary`, duplicate ports,
passwords under 8 chars) instead of starting a silently wrong config. See
`scripts/README.md` for the full error table.

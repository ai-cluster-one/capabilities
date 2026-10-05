# Bootstrapping a Coolify instance from a fresh server

This guide takes a setup session from a fresh server reachable as root over SSH to a Coolify instance this machine is paired with through `coolify connect`. Follow the steps in order; each ends with a check, and a step whose check fails is repaired before the next one starts. Every pitfall sits at the step where it bites.

Proven against Coolify 4.3.23 on Ubuntu 24.04. Several steps use Coolify internals that have no API; before following this on another version, read the version you install (step 1) and recheck each step marked *internal* against it.

## Before you start

Placeholders: `<server-ip>` is the server's public address, `<server-ipv6>` its public IPv6 address if it has one, `<domain>` the name the instance will answer on (by default `coolify.<ip-with-dashes>.sslip.io`, see step 4), `<name>` the connection id this machine will know the instance by (for example `main`), and `<email>` the root user's address. This machine pairs with one instance through a machine-level connection: `--global` writes it under `~/.config/coolify/`, and `--default` makes it the machine's default. Run the session inside a project directory; that project uses the pairing once it grants it (step 2).

`<email>` is never guessed. Read the address of the Claude account signed in on this Mac, show it to the user, and ask whether the root user should have that address or another one; use the address the user confirms or gives:

```sh
python3 -c 'import json, pathlib; print(json.loads((pathlib.Path.home() / ".claude.json").read_text()).get("oauthAccount", {}).get("emailAddress") or "")'
```

It prints nothing when no Claude account is signed in, and fails when `~/.claude.json` does not exist; then ask the user for the address. Either way the address must pass step 1's check that its domain resolves.

No secret is ever printed, put in a prompt, or put on a command line. Each one is generated where it is used, travels through a pipe or a file mode 0600, and is read back by a parser (step 8). Remote steps run as a script on standard input, `ssh root@<server-ip> bash -s <<'EOF' ... EOF`, so a value the script holds in a shell variable is never part of any process's arguments.

Keep the session's working files in one directory only you can read; every command below names it again, so a shell that keeps no variables between commands still finds it:

```sh
mkdir -p -m 700 ~/.cache/coolify-bootstrap
```

A few calls have no `coolify` verb yet. Make them with this helper. It finds the connection's URL and the file its token is in from `coolify connections`, reads the token from that file with a parser, sends it only as a request header, and redacts any key, password or token field in what it prints:

```sh
cat > ~/.cache/coolify-bootstrap/coolify-api.py <<'PY'
"""coolify-api.py <connection> METHOD PATH [BODY_FILE | -]"""
import json, os, subprocess, sys, urllib.error, urllib.request
from pathlib import Path

def parse_env(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        out[key] = value.strip().strip('"').strip("'")
    return out

def redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if v and any(w in k.lower() for w in ("private_key", "password", "token")) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value

name, method, path = sys.argv[1:4]
body = None
if len(sys.argv) > 4:
    body = sys.stdin.buffer.read() if sys.argv[4] == "-" else Path(sys.argv[4]).read_bytes()
rows = json.loads(subprocess.run(["coolify", "connections"], capture_output=True, text=True, check=True).stdout)["connections"][name]["keys"]
url = next(r["value"] for r in rows if r["key"] == "base_url")
secret = next(r for r in rows if r["secret"])
token = parse_env(secret["source"])[secret["key"]] if secret["source"] else os.environ[secret["key"]]
request = urllib.request.Request(url.rstrip("/") + "/api/v1" + path, data=body, method=method, headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        status, text = response.status, response.read().decode()
except urllib.error.HTTPError as error:
    status, text = error.code, error.read().decode()
try:
    text = json.dumps(redact(json.loads(text)))
except ValueError:
    pass
print(status, text)
sys.exit(0 if status < 400 else 1)
PY
```

Once step 2 has paired the instance, `python3 ~/.cache/coolify-bootstrap/coolify-api.py <name> GET /version` answers `200 4.3.23`.

## 1. Install Coolify with its root user

Pick the version and install it with the root user's three values in the environment of the install script; the version is the script's first argument:

```sh
ssh root@<server-ip> bash -s <<'EOF'
set -eu
export ROOT_USERNAME=admin
export ROOT_USER_EMAIL='<email>'
export ROOT_USER_PASSWORD="$(openssl rand -hex 24)-Aa9_"
curl -fsSL https://cdn.coollabs.io/coolify/install.sh -o /root/coolify-install.sh
bash /root/coolify-install.sh 4.3.23
EOF
```

The installer prints "Updated value of ROOT_USER_PASSWORD as the current value was empty" even though `ROOT_USER_PASSWORD` was set above and the root user is created as asked; the message is harmless and does not mean the password was ignored.

Pitfall: the seeder fails silently. If the email's domain does not resolve, or the password fails Coolify's strength and breach check, the install finishes and no user is created. Use an address on a real domain with DNS records (`dig +short MX <domain-of-email>` or `dig +short A <domain-of-email>` answers), and let the script generate the password as above: random, with upper case, lower case, digits and a symbol. The password then stays in plain text in `/data/coolify/source/.env` on the server; it is the only copy, and nothing in this guide reads it.

Check that the user exists and the version is the one you asked for:

```sh
ssh root@<server-ip> 'docker exec coolify php artisan tinker --execute "echo App\Models\User::find(0) ? \"root user present\" : \"NO ROOT USER\";"; docker inspect coolify --format "{{.Config.Image}}"'
```

It answers `root user present` and `docker.io/coollabsio/coolify:4.3.23`. With no root user, fix the email or password and run the install again.

## 2. Mint the API token, enable the API, and pair

*Internal.* Coolify has no supported headless way to mint a token. This one creates a root-ability token for the root user in the root team through Laravel's tinker, and is undocumented, so first confirm it still applies to the installed version: `docker exec coolify php artisan list` lists `tinker`, and the step 1 check found user 0 and team 0 (`App\Models\Team::find(0)->name` is `Root Team`). If either has moved, stop and find the new path rather than guessing.

Check `coolify connections` before running this: pass `--default` only if it shows no default yet, since this first pairing must not move one that already exists; if one is set, drop `--default` from the command below.

The token goes straight from the server into `coolify connect` on standard input. The same remote script turns the API on with `POST /api/v1/enable`, sending the token as a header read from standard input, and prints the token only into the pipe:

```sh
ssh root@<server-ip> bash -s <<'EOF' | coolify connect <name> --url http://<server-ip>:8000 --token-stdin --global --default
set -eu
TOKEN=$(docker exec coolify php artisan tinker --execute 'session(["currentTeam"=>App\Models\Team::find(0)]); echo App\Models\User::find(0)->createToken("agent",["root"])->plainTextToken;')
printf 'Authorization: Bearer %s\n' "$TOKEN" | curl -fsS -o /dev/null -w 'enable: %{http_code}\n' -X POST -H @- http://127.0.0.1:8000/api/v1/enable >&2
printf '%s\n' "$TOKEN"
EOF
```

Pitfall: `/api/v1/enable` and `/api/v1/servers/{uuid}/validate` take POST only; a GET answers 405 with "This endpoint has changed to a POST request" and changes nothing. Pitfall: never echo the token, paste it, or write it into a prompt; if it is ever shown, revoke it and mint another.

`coolify connect` writes the machine's connection entry (URL and the name of the key that holds the token, nothing secret) to `~/.config/coolify/connections.json` and the token into `~/.config/coolify/credentials.env` at mode 0600, then runs the doctor probe.

Reads — `coolify connections`, `coolify --connection <name> doctor`, `coolify servers` — work from any directory on the machine pairing alone, read-only; every write this guide asks for from here on, such as creating a project, a database, or updating an app, runs in a project the user has granted the pairing to, so a setup workspace such as an app's service folder must be such a project.

A project uses this machine pairing once it grants it, by running `capabilities set coolify grant <name> '{"enabled": true}'` in that project; this session does not lift that gate itself, so ask the user to run that command for the project it works in now, or confirm the grant already exists, before any write below, since until then `connect` answers `"usable_here": false` with that command. Check that its answer has `"ok": true` and `"version": "4.3.23"`, and, once the grant is confirmed, that `coolify connections` lists `<name>` with its token masked. If the probe fails, read the `enable:` status the script printed on stderr first: anything but a 2xx means the API is still off.

## 3. Turn auto-update on so that it holds

Turn the instance setting on, set the `.env` line, and recreate the `coolify` container:

```sh
ssh root@<server-ip> bash -s <<'EOF'
set -eu
docker exec coolify php artisan tinker --execute 'App\Models\InstanceSettings::get()->update(["is_auto_update_enabled"=>true]);'
cd /data/coolify/source
if grep -q '^AUTOUPDATE=' .env; then sed -i 's/^AUTOUPDATE=.*/AUTOUPDATE=true/' .env; else echo 'AUTOUPDATE=true' >> .env; fi
docker compose --env-file .env -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate coolify
EOF
```

Pitfall: the `AUTOUPDATE` line in `/data/coolify/source/.env` resets the instance setting every time Coolify starts, so changing the setting alone does not survive the next restart. *Internal*: the setting is changed through tinker; the compose files are the ones the running container names in its `com.docker.compose.project.config_files` label, and `.env` carries `LATEST_IMAGE`, which keeps the recreated container on the installed version.

Check, after the container is back:

```sh
ssh root@<server-ip> 'grep "^AUTOUPDATE=" /data/coolify/source/.env; docker exec coolify php artisan tinker --execute "echo json_encode(App\Models\InstanceSettings::get()->only([\"is_auto_update_enabled\"]));"; docker inspect coolify --format "{{.Config.Image}}"'
```

It answers `AUTOUPDATE=true`, `{"is_auto_update_enabled":true}` and the version from step 1, and `coolify --connection <name> doctor` still answers ok.

## 4. Serve the instance over HTTPS and close its direct ports

Everything in this step is done on the server over root SSH: no cloud firewall, provider API or DNS change is needed. The instance's name is `coolify.<ip-with-dashes>.sslip.io`, where `<ip-with-dashes>` is `<server-ip>` with its dots replaced by dashes; sslip.io answers every such name with the address written in it, so the name points at the server without anyone creating a record. That name is `<domain>` from here on.

Give the instance that name and have Coolify's proxy serve it, which requests its certificate from Let's Encrypt:

```sh
ssh root@<server-ip> bash -s <<'EOF'
set -eu
docker exec coolify php artisan tinker --execute '$s = App\Models\InstanceSettings::get(); $s->fqdn = "https://<domain>"; $s->save(); App\Models\Server::find(0)->setupDynamicProxyConfiguration();'
EOF
```

*Internal*: setting the domain is a UI action in Coolify, and this is the code path it runs. The certificate can take a minute. Check it from this machine:

```sh
curl -fsS https://<domain>/api/health   # OK
openssl s_client -connect <domain>:443 -servername <domain> </dev/null 2>/dev/null | openssl x509 -noout -issuer -dates   # issuer is Let's Encrypt
```

Until both pass, do not close any port. Pitfall: sslip.io names share one Let's Encrypt rate limit across everyone who uses them, so the request can be refused for reasons that have nothing to do with this server. The proxy then serves its own default certificate instead (the issuer names Traefik, not Let's Encrypt), and `ssh root@<server-ip> 'docker logs coolify-proxy 2>&1 | grep -i acme | tail -5'` shows the refusal, such as "too many certificates already issued". The fallback is a domain the owner points at the server: an A record for a name of their choosing with the value `<server-ip>`. It is the one act in this guide that needs anything outside the server. Ask the owner for it, then run the commands above again with that name as `<domain>`.

Re-pair on the HTTPS URL. The token is already in the credentials file, and `--token-env` resolves the key through the same files a verb reads it from. Check `coolify connections` first: keep `--default` only if it already shows none, since this re-pair must not move an existing machine default; if one is set, drop `--default` from the command below:

```sh
coolify connect <name> --url https://<domain> --token-env <KEY> --global --default
```

where `<KEY>` is the key `coolify connections` names for `<name>` (by default `COOLIFY_<NAME>_TOKEN`). Its probe answers ok on the new URL.

Then close 8000 (UI and API over plain HTTP), 6001 and 6002 to the outside. Pitfall: Docker publishes container ports through its own packet-filter rules, which a host firewall such as `ufw` never sees, so the rules go in iptables' `DOCKER-USER` chain, the one chain Docker evaluates before its own and never rewrites. A published port is rewritten to the container's port before that chain sees the packet, so the rules match the port the connection was made to (`--ctorigdstport`), not the one it was rewritten to. They drop only new connections arriving on the public interface: replies and established connections still pass, and so do loopback and the traffic between Coolify's containers on Docker's own bridges, which never arrive on that interface. A server with a public IPv6 address gets the same rules for IPv6, hooked into both ip6tables' `DOCKER-USER` and `INPUT`. Where the Docker network carries IPv6, as Coolify's own does, a published port is rewritten for IPv6 just as for IPv4 and the connection passes `DOCKER-USER`; where it does not, Docker's own proxy, a process on the host, answers the port and the connection ends in `INPUT` without passing `DOCKER-USER`. Matching the port the connection was made to covers both paths, so one chain serves both hooks; closing only IPv4 would leave the three ports open over IPv6.

The rules are kept in a script that a systemd unit runs every time Docker starts. That is how they survive a reboot. `iptables-persistent` is not used: it restores a saved copy of the whole ruleset before Docker starts, Docker's chains from the last boot included, and Docker then rewrites those, so the two fight over the same tables. The unit runs after Docker, rebuilds only its own chain, and is safe to run again; the block below restarts it, so running the block a second time applies an edited script rather than keeping the rules from the first run.

```sh
ssh root@<server-ip> bash -s <<'EOF'
set -eu
cat > /usr/local/sbin/coolify-close-direct-ports <<'SH'
#!/bin/sh
# Drop new connections from the public interface to Coolify's direct ports.
set -eu
IF=$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
[ -n "$IF" ]
iptables -N COOLIFY-DIRECT 2>/dev/null || iptables -F COOLIFY-DIRECT
iptables -A COOLIFY-DIRECT -i "$IF" -p tcp -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 8000 -j DROP
iptables -A COOLIFY-DIRECT -i "$IF" -p tcp -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 6001:6002 -j DROP
iptables -A COOLIFY-DIRECT -j RETURN
iptables -C DOCKER-USER -j COOLIFY-DIRECT 2>/dev/null || iptables -I DOCKER-USER 1 -j COOLIFY-DIRECT
if ip -6 addr show dev "$IF" scope global | grep -q inet6; then
  ip6tables -N COOLIFY-DIRECT 2>/dev/null || ip6tables -F COOLIFY-DIRECT
  ip6tables -A COOLIFY-DIRECT -i "$IF" -p tcp -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 8000 -j DROP
  ip6tables -A COOLIFY-DIRECT -i "$IF" -p tcp -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 6001:6002 -j DROP
  ip6tables -A COOLIFY-DIRECT -j RETURN
  if ip6tables -S DOCKER-USER >/dev/null 2>&1; then
    ip6tables -C DOCKER-USER -j COOLIFY-DIRECT 2>/dev/null || ip6tables -I DOCKER-USER 1 -j COOLIFY-DIRECT
  fi
  ip6tables -C INPUT -j COOLIFY-DIRECT 2>/dev/null || ip6tables -I INPUT 1 -j COOLIFY-DIRECT
fi
SH
chmod 755 /usr/local/sbin/coolify-close-direct-ports
cat > /etc/systemd/system/coolify-close-direct-ports.service <<'UNIT'
[Unit]
Description=Close Coolify's direct ports 8000, 6001 and 6002 to the public interface
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/coolify-close-direct-ports

[Install]
WantedBy=docker.service
UNIT
systemctl daemon-reload
systemctl enable coolify-close-direct-ports.service
systemctl restart coolify-close-direct-ports.service
iptables -S DOCKER-USER
iptables -S COOLIFY-DIRECT
ip6tables -S COOLIFY-DIRECT 2>/dev/null || true
EOF
```

`iptables -S DOCKER-USER` lists `-A DOCKER-USER -j COOLIFY-DIRECT` first, and `iptables -S COOLIFY-DIRECT` the two drops and the return; on a server with public IPv6, `ip6tables -S COOLIFY-DIRECT` lists the same. Public database ports (step 6) are not touched by these rules and stay open.

Check from this machine that the three ports no longer answer, and that the instance still works through its proxy and on the server itself:

```sh
for port in 8000 6001 6002; do curl -s -o /dev/null --connect-timeout 5 http://<server-ip>:$port/; echo "$port: curl exit $?"; done   # 28 (timed out) for each
for port in 8000 6001 6002; do curl -s -o /dev/null --connect-timeout 5 "http://[<server-ipv6>]:$port/"; echo "$port: curl exit $?"; done   # the same over IPv6, where both ends have it
curl -fsS https://<domain>/api/health   # OK
ssh root@<server-ip> 'curl -fsS http://127.0.0.1:8000/api/health'   # OK: loopback is kept
coolify --connection <name> doctor   # ok on https://<domain>
```

Any exit other than 28 (or 7, refused) means something still answers on that port. Then prove the rules come back: `ssh root@<server-ip> reboot`, wait until SSH answers again, and repeat `iptables -S DOCKER-USER` and the checks above; `systemctl status coolify-close-direct-ports.service` shows it ran after Docker started.

## 5. Prepare the deploy source on the server

Deploys come from bare repositories on the server itself, owned by a `git` user whose shell is `git-shell`. Create the user, the repository root, and its `authorized_keys`, allow fetching any reachable commit so that Coolify can deploy an older commit for a rollback, and make a deploy key for Coolify:

```sh
ssh root@<server-ip> bash -s <<'EOF'
set -eu
id git >/dev/null 2>&1 || useradd --create-home --shell /usr/bin/git-shell git
install -d -o git -g git -m 755 /srv/git
install -d -o git -g git -m 700 /home/git/.ssh
touch /home/git/.ssh/authorized_keys && chown git:git /home/git/.ssh/authorized_keys && chmod 600 /home/git/.ssh/authorized_keys
git config --system uploadpack.allowReachableSHA1InWant true
test -f /root/.ssh/coolify-deploy || ssh-keygen -q -t ed25519 -N '' -C coolify-deploy -f /root/.ssh/coolify-deploy
grep -qF "$(cut -d' ' -f2 /root/.ssh/coolify-deploy.pub)" /home/git/.ssh/authorized_keys || echo "no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat /root/.ssh/coolify-deploy.pub)" >> /home/git/.ssh/authorized_keys
EOF
```

Add this machine's public key the same way, so it can push the bodies it deploys. Do not assume a fixed name such as `~/.ssh/id_ed25519.pub`: find the key SSH actually uses for this server with `ssh -G root@<server-ip> | grep -i identityfile`, which lists the private key files SSH tries in order, including defaults that do not exist; the first listed file that exists is the one SSH picks for this host, and its `.pub` half is `<identity-file>.pub` below. Check that `<identity-file>.pub` exists before running the command, since a missing file would append a line without a key:

```sh
ssh root@<server-ip> "echo 'no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat <identity-file>.pub)' >> /home/git/.ssh/authorized_keys"
```

Register the deploy key's private half in Coolify (`POST /security/keys`; `coolify` has no verb for it yet). It streams from the server into the request and is never written on this machine or printed:

```sh
ssh root@<server-ip> "jq -n --rawfile k /root/.ssh/coolify-deploy '{name: \"git-deploy\", description: \"pulls from /srv/git\", private_key: \$k}'" | python3 ~/.cache/coolify-bootstrap/coolify-api.py <name> POST /security/keys -
```

Pitfall: Coolify clones from these repositories without verifying the host key. The repositories are on the same server, so this matters only if the server's own address is spoofed to itself; record it and go on.

Check: `coolify sources` lists `git-deploy` under `private_deploy_keys`, its uuid is what `app create --private-deploy-key` takes, and on the server `git config --system --get uploadpack.allowReachableSHA1InWant` answers `true`, `stat -c '%U %a' /srv/git` answers `git 755`, and `getent passwd git` ends in `/usr/bin/git-shell`.

## 6. Create a Postgres with TLS enforced

A fresh instance has a server (the one Coolify installed onto itself in step 1) but no project yet, and `database create` needs the uuid of each. Find `<server-uuid>` with `coolify servers`, which lists the one server and its uuid. Create the project with `coolify projects create <name>`, whose answer carries the new project's uuid as `<project-uuid>`.

Every database is Postgres 18 with pgvector, public on a port of its own, with a generated password. With nothing project-specific to call it, name `<db-name>` `capabilities-store`, `<user>` and `<db>` both `capabilities`, and `<port>` `5432`. Generate the password into a file only you can read, and give it to `database create` from that file; the trailing newline `openssl rand -hex 32 >` writes into that file is harmless, since both Coolify's `--set-file` and `capabilities store set` strip it when they read the file back:

```sh
(umask 077; openssl rand -hex 32 > ~/.cache/coolify-bootstrap/pg-password)
coolify database create --engine postgresql --project <project-uuid> --server <server-uuid> --environment production --name <db-name> --image pgvector/pgvector:pg18 --set postgres_user=<user> --set postgres_db=<db> --set-file postgres_password=$HOME/.cache/coolify-bootstrap/pg-password --set-json is_public=true --set-json public_port=<port> --instant-deploy
```

Its answer carries the database uuid and connection URLs with the password redacted. Pitfall: Coolify's API refuses `enable_ssl` and `ssl_mode` on create and on update, and Coolify's own SSL setting still accepts plain-text connections, so TLS is enforced in three more acts that have no API in 4.3.23:

1. *Internal*: switch SSL on in the database's model: `ssh root@<server-ip> "docker exec coolify php artisan tinker --execute '\$d = App\Models\StandalonePostgresql::where(\"uuid\", \"<db-uuid>\")->first(); \$d->enable_ssl = true; \$d->ssl_mode = \"require\"; \$d->save();'"`.
2. Restart it through the API, which issues its certificate: `coolify restart <db-uuid> --type database`. Coolify's stored status reads `exited:unhealthy` for about five to six minutes afterward although the container is healthy within seconds, because Sentinel, the agent inside the server that reports container health back to Coolify, reports only on a state change or every 300 seconds; check the container itself with `ssh root@<server-ip> docker ps` for the immediate answer, and only then wait out the five to six minutes for `coolify databases <db-uuid>` to catch up and show `running:healthy`.
3. In the container, whose name is the database uuid, replace the catch-all `host all all all ...` line of `/var/lib/postgresql/18/docker/pg_hba.conf` with the two lines `hostnossl all all all reject` and `hostssl all all all scram-sha-256`, then restart it through the API again as in 2. The rule survives an API restart; whether it survives a Coolify update is not proven, which is why the store's doctor checks it.

Check from this machine, with no password needed for either answer:

```sh
psql -w "host=<server-ip> port=<port> user=<user> dbname=<db> sslmode=disable" -c 'select 1'   # pg_hba.conf rejects connection ... no encryption
psql -w "host=<server-ip> port=<port> user=<user> dbname=<db> sslmode=require" -c 'select 1'   # fe_sendauth: no password supplied
```

The first is refused for having no encryption; the second gets as far as asking for the password over TLS. Both checks go over IPv4, which is all `<server-ip>` is: the public Postgres answers over IPv4 only and refuses a connection over IPv6 outright, unlike the proxy in step 4, so there is no IPv6 form of this check to run.

For the central store, the last act of this step is `capabilities store set`, giving it the host, port, database, user, `sslmode=require` and the password from `~/.cache/coolify-bootstrap/pg-password` through standard input or the file, never on a command line; `capabilities help` gives its exact flags. Remove `~/.cache/coolify-bootstrap` once the store's doctor answers ok.

## 7. Point application health checks at 127.0.0.1

An application's health check calls `localhost` by default, which fails for an application that listens on IPv4 only. Set every application's health-check host to `127.0.0.1` when it is created:

```sh
coolify app update <app-uuid> --health-check-host 127.0.0.1
```

Check: `coolify applications <app-uuid>` shows `"health_check_host": "127.0.0.1"`.

## 8. Read credentials files with a parser, never by sourcing

A Coolify token contains `|`. Sourcing a credentials file in a shell (`source`, `.`, `set -a; . file`, `export $(cat file)`) splits the line at the `|`, tries to run the rest as a command, and prints part of the token in the error. Read such a file only with a parser: `coolify` itself, which reads `.env.local`, `.env` and `~/.config/coolify/credentials.env` with the capability contract's parser, or the helper above, which does the same.

Check, at the end of the session: nothing you ran sourced a credentials file, `coolify connections` shows every token masked, and no token, password or connection URL with a password appears in the session's output.

# Bootstrapping a Coolify instance from a fresh server

This guide takes a setup session from a fresh server reachable as root over SSH to a Coolify instance this machine is paired with through `coolify connect`. Follow the steps in order; each ends with a check, and a step whose check fails is repaired before the next one starts. Every pitfall sits at the step where it bites.

Proven against Coolify 4.3.23 on Ubuntu 24.04. Several steps use Coolify internals that have no API; before following this on another version, read the version you install (step 1) and recheck each step marked *internal* against it.

## Before you start

Placeholders: `<server-ip>` is the server's public address, `<domain>` the name the instance will answer on, `<name>` the connection id this machine will know the instance by (for example `main`), and `<email>` the root user's address. This machine pairs with one instance through a machine-level connection: `--global` writes it under `~/.config/coolify/`, and `--default` makes it the machine's default. Run the session inside a project directory; that project uses the pairing once it grants it (step 2).

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

Pitfall: the seeder fails silently. If the email's domain does not resolve, or the password fails Coolify's strength and breach check, the install finishes and no user is created. Use an address on a real domain with DNS records (`dig +short MX <domain-of-email>` or `dig +short A <domain-of-email>` answers), and let the script generate the password as above: random, with upper case, lower case, digits and a symbol. The password then stays in plain text in `/data/coolify/source/.env` on the server; it is the only copy, and nothing in this guide reads it.

Check that the user exists and the version is the one you asked for:

```sh
ssh root@<server-ip> 'docker exec coolify php artisan tinker --execute "echo App\Models\User::find(0) ? \"root user present\" : \"NO ROOT USER\";"; docker inspect coolify --format "{{.Config.Image}}"'
```

It answers `root user present` and `docker.io/coollabsio/coolify:4.3.23`. With no root user, fix the email or password and run the install again.

## 2. Mint the API token, enable the API, and pair

*Internal.* Coolify has no supported headless way to mint a token. This one creates a root-ability token for the root user in the root team through Laravel's tinker, and is undocumented, so first confirm it still applies to the installed version: `docker exec coolify php artisan list` lists `tinker`, and the step 1 check found user 0 and team 0 (`App\Models\Team::find(0)->name` is `Root Team`). If either has moved, stop and find the new path rather than guessing.

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

`coolify connect` writes the machine's connection entry (URL and the name of the key that holds the token, nothing secret) to `~/.config/coolify/connections.json` and the token into `~/.config/coolify/credentials.env` at mode 0600, then runs the doctor probe. A project uses this machine pairing once it grants it, by running `capabilities set coolify grant <name> '{"enabled": true}'` in the project; do that now in the project this session runs in, since every later `coolify` command here needs it, and until then `connect` answers `"usable_here": false` with that command. Check that its answer has `"ok": true` and `"version": "4.3.23"`, and, after the grant, that `coolify connections` lists `<name>` with its token masked. If the probe fails, read the `enable:` status the script printed on stderr first: anything but a 2xx means the API is still off.

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

Point a DNS A record for `<domain>` at `<server-ip>`, then give the instance that domain and have Coolify's proxy serve it with a certificate:

```sh
ssh root@<server-ip> bash -s <<'EOF'
set -eu
docker exec coolify php artisan tinker --execute '$s = App\Models\InstanceSettings::get(); $s->fqdn = "https://<domain>"; $s->save(); App\Models\Server::find(0)->setupDynamicProxyConfiguration();'
EOF
```

*Internal*, and the least proven step here: setting the domain is a UI action in Coolify, and this is the code path it runs. Check it from this machine: `curl -fsS https://<domain>/api/health` answers `OK` with a certificate curl accepts. Until it does, do not close any port.

Re-pair on the HTTPS URL. The token is already in the credentials file, and `--token-env` resolves the key through the same files a verb reads it from:

```sh
coolify connect <name> --url https://<domain> --token-env <KEY> --global --default
```

where `<KEY>` is the key `coolify connections` names for `<name>` (by default `COOLIFY_<NAME>_TOKEN`). Its probe answers ok on the new URL.

Then close 8000 (UI and API over plain HTTP), 6001 and 6002 at the cloud provider's firewall, keeping 22, 80, 443 and any public database port (step 6) open. Pitfall: Docker publishes container ports through its own packet-filter rules, ahead of a host firewall such as `ufw`, so closing them on the host does not close them; it has to be the provider's firewall in front of the server.

Check from this machine that plain HTTP no longer answers and the paired URL still does:

```sh
curl -m 5 http://<server-ip>:8000/api/health   # fails to connect
nc -z -G 5 <server-ip> 6001; nc -z -G 5 <server-ip> 6002   # both fail
coolify --connection <name> doctor   # ok on https://<domain>
```

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

Add this machine's public key the same way, so it can push the bodies it deploys (here `~/.ssh/id_ed25519.pub`; use the key this machine pushes with):

```sh
ssh root@<server-ip> "echo 'no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat ~/.ssh/id_ed25519.pub)' >> /home/git/.ssh/authorized_keys"
```

Register the deploy key's private half in Coolify (`POST /security/keys`; `coolify` has no verb for it yet). It streams from the server into the request and is never written on this machine or printed:

```sh
ssh root@<server-ip> "jq -n --rawfile k /root/.ssh/coolify-deploy '{name: \"git-deploy\", description: \"pulls from /srv/git\", private_key: \$k}'" | python3 ~/.cache/coolify-bootstrap/coolify-api.py <name> POST /security/keys -
```

Pitfall: Coolify clones from these repositories without verifying the host key. The repositories are on the same server, so this matters only if the server's own address is spoofed to itself; record it and go on.

Check: `coolify sources` lists `git-deploy` under `private_deploy_keys`, its uuid is what `app create --private-deploy-key` takes, and on the server `git config --system --get uploadpack.allowReachableSHA1InWant` answers `true`, `stat -c '%U %a' /srv/git` answers `git 755`, and `getent passwd git` ends in `/usr/bin/git-shell`.

## 6. Create a Postgres with TLS enforced

Every database is Postgres 18 with pgvector, public on a port of its own, with a generated password. Generate the password into a file only you can read, and give it to `database create` from that file:

```sh
(umask 077; openssl rand -hex 32 > ~/.cache/coolify-bootstrap/pg-password)
coolify database create --engine postgresql --project <project-uuid> --server <server-uuid> --environment production --name <db-name> --image pgvector/pgvector:pg18 --set postgres_user=<user> --set postgres_db=<db> --set-file postgres_password=$HOME/.cache/coolify-bootstrap/pg-password --set-json is_public=true --set-json public_port=<port> --instant-deploy
```

Its answer carries the database uuid and connection URLs with the password redacted. Pitfall: Coolify's API refuses `enable_ssl` and `ssl_mode` on create and on update, and Coolify's own SSL setting still accepts plain-text connections, so TLS is enforced in three more acts that have no API in 4.3.23:

1. *Internal*: switch SSL on in the database's model: `ssh root@<server-ip> "docker exec coolify php artisan tinker --execute '\$d = App\Models\StandalonePostgresql::where(\"uuid\", \"<db-uuid>\")->first(); \$d->enable_ssl = true; \$d->ssl_mode = \"require\"; \$d->save();'"`.
2. Restart it through the API, which issues its certificate: `coolify restart <db-uuid> --type database`. It reports `exited` states for up to a minute while starting; wait until `coolify databases <db-uuid>` shows `running:healthy`.
3. In the container, whose name is the database uuid, replace the catch-all `host all all all ...` line of `/var/lib/postgresql/18/docker/pg_hba.conf` with the two lines `hostnossl all all all reject` and `hostssl all all all scram-sha-256`, then restart it through the API again as in 2. The rule survives an API restart; whether it survives a Coolify update is not proven, which is why the store's doctor checks it.

Check from this machine, with no password needed for either answer:

```sh
psql -w "host=<server-ip> port=<port> user=<user> dbname=<db> sslmode=disable" -c 'select 1'   # pg_hba.conf rejects connection ... no encryption
psql -w "host=<server-ip> port=<port> user=<user> dbname=<db> sslmode=require" -c 'select 1'   # fe_sendauth: no password supplied
```

The first is refused for having no encryption; the second gets as far as asking for the password over TLS.

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

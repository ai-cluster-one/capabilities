# Routing Skyscanner requests through an Oxylabs residential proxy

Skyscanner's bot protection blocks an address that searches too often, and the remedy is a fresh address rather than a browser session. This guide shows how a skyscanner connection reaches Skyscanner through an Oxylabs residential proxy user: the fields it takes, how skyscanner composes them into the proxy Oxylabs expects, how it holds one address per attempt, and how to prove where requests leave from.

## The block, and why a proxy is the remedy

From one address, Skyscanner answers normally for roughly 15 to 25 searches in half an hour and then refuses every request from that address for some minutes: with 403, with an answer that points at its captcha, or with a search that never completes. Repeating the call from the same address fails the same way until the block lifts on its own, and a cookie harvested from a fresh browser on a blocked address is refused too. What clears it is a request from another address, which a residential proxy supplies: each attempt leaves from a home connection, so no one address carries the whole search volume.

The bot protection also judges each of Skyscanner's country sites apart, and grows stricter on a site that carries dense traffic. skyscanner therefore rotates its calls across the country sites, and answers any refusal by starting the whole call again on the next site and from a new exit address, up to 12 attempts, naming each refusal on stderr; only then does it report exit 7 `blocked`. A site that refused is passed over for 30 minutes. Each site is tried once per call before any is tried again; once all have been, the remaining attempts go to the site refused longest ago, each on a fresh session, so the cooldown only orders the sites and never ends a call before its 12 attempts are spent. Skyscanner also flags single residential addresses, so a fresh proxy address is now and then refused on its first request; the next attempt carries on from another one. Direct, the rotation across sites still applies, but every attempt leaves from the same address.

## The fields

A connection carries the proxy as separate fields, and skyscanner builds the proxy from them on every call:

- `oxylabs_username` - the proxy user's own name as the Oxylabs dashboard shows it under the residential proxy users, with nothing added. Leaving it empty sends requests direct.
- `oxylabs_password_env` - the env key holding that user's password. The password is a secret: `connection add` takes it only on stdin and keeps it where `skyscanner help` (`Where a secret resolves`) says - the agent's vault by default - and the entry keeps only the key's name.
- `oxylabs_country` - `auto` (the default) for an exit address in the country of the site each attempt uses, or a two-letter country code that pins every attempt to that country.
- `oxylabs_host` and `oxylabs_port` - the residential entry point, default `pr.oxylabs.io` and `7777`.
- `oxylabs_session_minutes` - how long one attempt may hold its address, 1 to 30, default 10.

`skyscanner help` lists every connection field with its type and default.

Write a connection with the proxy user's name on argv and the password on stdin, so the password never reaches the command line:

```sh
printf '%s' '{"oxylabs_password_env": "<password>"}' | \
  skyscanner connection add <id> --default --field oxylabs_username=<user> --stdin
```

`--project` (the default) keeps the connection for one project and the password in the agent's vault, or in the project's `.env.local` with `--file` or where no vault is usable there; `--machine` keeps it for the machine and the password in `~/.config/skyscanner/credentials.env`, where every verb run outside a project, and every agent on the machine, finds it. `skyscanner connections` then shows each field as it resolves, the password masked, and the proxy the connection uses.

## How the proxy is composed

Oxylabs reads its targeting from the proxy username. skyscanner composes it as

```
customer-<oxylabs_username>-cc-<country>-sessid-<session id>-sesstime-<oxylabs_session_minutes>
```

where `<country>` is `oxylabs_country`, or with `auto` the country of the attempt's site.

then connects to `http://<composed username>:<password>@<oxylabs_host>:<oxylabs_port>`, the password percent-encoded. This is why `oxylabs_username` holds the bare name: a value that already carries `customer-`, `-cc-` or `-sessid-` is refused with exit 6 `bad_oxylabs_username` rather than composed twice.

## One address per attempt

skyscanner draws a new random session id for every attempt, and Oxylabs keeps the address of that id for up to `oxylabs_session_minutes`. Every request of an attempt - the autosuggest calls, the search and each of its polls - then reads as one visitor on one address, and the next attempt, or the next call, starts on a new one. 10 minutes covers the longest search. When any request of an attempt is refused, the address and the session are dropped and the whole call starts again under a new session id on the next site; so does an attempt whose exit address fails in transport, since Oxylabs may end a session early when the home behind the address goes offline.

## Choosing the country

The exit country is independent of the market. The market (`market`, or `--market`) chooses which booking agents answer, whichever country site serves the call, the currency (`currency`, or `--currency`) what the prices are in, and the exit country only where the requests appear to come from. With `auto`, each attempt leaves from the country of the site it uses - www.skyscanner.de from Germany, www.skyscanner.fr from France, www.skyscanner.net from Great Britain - so it looks like that site's ordinary visitor, which the bot protection refuses less often than a visitor from elsewhere. A pinned country sends every attempt, on every site, from that one country; a country with few residential addresses in the Oxylabs pool rotates through fewer of them.

## Proving where requests leave from

`skyscanner doctor` proves the whole chain for each connection. With a proxy it first asks `https://ip.oxylabs.io/location` through the proxy and reports the exit address and the country each geolocation provider places it in, failing with exit 8 `exit_country_mismatch` when that country is not the one it asked for - `oxylabs_country`, or with `auto` the country of the site a call would use next, both named in `exit`; then it makes an autosuggest request to Skyscanner on the site a call would use next, moving to the following sites while one refuses, names in `domain` the site that answered and in `refused` those that refused before it, and fails with exit 7 `blocked` when no site did, trying each at most once. A refused password reads as exit 2 `proxy_auth`, and an unreachable entry point as exit 5. Without a proxy, `doctor` checks Skyscanner direct and warns that the address will be blocked under volume.

Run `doctor` after writing a connection and again whenever searches start failing as `blocked`: a direct connection needs a proxy or a pause, and a proxied one that keeps failing points at the exit country's pool or at a pause too.

## Oxylabs' own reference

The residential proxy parameters, the entry points and the account's traffic limits are Oxylabs' to state: [Oxylabs residential proxies documentation](https://developers.oxylabs.io/proxies/residential-proxies).

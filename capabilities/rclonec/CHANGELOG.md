# rclonec — change log

## 2026-11-07 — The remote is named for the connection's label, so `<label>:path` keeps working

A connection the store keys by an id is still addressed as `<label>:path`: the remote rclonec defines for rclone (`RCLONE_CONFIG_<LABEL>_*`) is named for the connection's label, and for its id as well where the two differ, so `<id>:path` answers as `--connection <id>` does. The write gate recognises a destination addressed by either name, the remote-name check applies to the label, `doctor` lists `<label>:` and takes `--connection` by id or label, and `connections` gives each connection's `label` beside its `remote`. In a files project the key is both, as before. Declares `capabilities-contract>=0.44,<1`.

## 2026-11-01 — `rclonec` carries rclone's own logo as its icon

The bundle's `icon.png` is now rclone's official logo symbol, `graphics/logo/logo_symbol/logo_symbol_color_128px.png` from the rclone repository, unmodified: 128 x 128, full colour on a transparent ground, where it was a 32 x 32 image. `capabilities icon rclonec` answers its path and size. Nothing else changes.

rclone's notice, which ships with the logo:

Copyright (C) 2012 by Nick Craig-Wood http://www.craig-wood.com/nick/

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## 2026-10-31 — `rclonec help` names the agent's vault in the credential cascade

Bare `rclonec help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `rclonec` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon rclonec` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-28 — `rclonec connections` names each secret by the env key it resolves from

A secret row in `rclonec connections` now names the env key under `secret_env` as its `key`, as every capability's report does, where it used to read `<option> (<ENV_KEY>)`. A reader that looks a key up by name - `capabilities secrets move` among them - therefore finds rclonec reading it, so rclonec's keys move into the agent's vault like any other, and a key the vault could not be read for carries the vault's note on why. The option a key fills is still the connection's `secret_env`. Nothing else changes.

## 2026-10-27 — `rclonec` fetches rclone itself on first use where none is on PATH, and declares it in its manifest

Where `rclone` is not on PATH, the first command that needs it - a forwarded command, `doctor`, or the write gate asking `rclone help flags` - fetches rclone's own standalone build for this platform (linux or macOS, amd64 or arm64) from the latest release on `downloads.rclone.org`, checks it against that release's `SHA256SUMS`, and keeps it at `$XDG_STATE_HOME/rclonec/programs/rclone`, as the user and without root or a package manager; every later run uses it. An `rclone` on PATH is still used first and nothing is fetched then. A fetch that fails exits 5 `rclone_not_found` as before, naming what failed, and `doctor` reports it under `rclone.error`. `rclonec manifest` now carries `programs`, naming rclone and what stops without it, so `capabilities doctor` can say it is missing.

## 2026-10-27 — `rclonec doctor` lists the path a connection names in `check_path`, so a bucket-scoped key is reported ready

A connection entry may name `check_path`, a path under its remote such as the one bucket an S3 or R2 key is scoped to. `rclonec doctor` then proves the connection with `rclone lsd <id>:<check_path>` instead of `rclone lsd <id>:`, which on S3 is ListBuckets and is refused to a bucket-scoped key, so doctor reported a working connection as `not_ready`. A connection without `check_path` is checked as before, the doctor output is unchanged, and the field never reaches rclone's configuration. `check_path` is declared among the connection's fields, so `rclonec connection add <id> --field check_path=<bucket>` writes it.

## 2026-10-13 — Outside any project, `rclonec connections` and `rclonec doctor` read the machine's own connections

`rclonec` declares `connections` and `doctor` as machine reads. Run outside any project, `rclonec connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `rclonec doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `rclonec` declares what a person is asked for, and writes it through the contract

`rclonec manifest` now declares a connection's fields under `connections.fields` (`type`, `options`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `rclonec connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `rclonec connection remove <id>` takes it out again with the secrets it wrote. Bare `rclonec help` ends with a section rendered from these declarations. Nothing else changes.

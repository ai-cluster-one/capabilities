# rclonec — change log

## 2026-10-27 — `rclonec doctor` lists the path a connection names in `check_path`, so a bucket-scoped key is reported ready

A connection entry may name `check_path`, a path under its remote such as the one bucket an S3 or R2 key is scoped to. `rclonec doctor` then proves the connection with `rclone lsd <id>:<check_path>` instead of `rclone lsd <id>:`, which on S3 is ListBuckets and is refused to a bucket-scoped key, so doctor reported a working connection as `not_ready`. A connection without `check_path` is checked as before, the doctor output is unchanged, and the field never reaches rclone's configuration. `check_path` is declared among the connection's fields, so `rclonec connection add <id> --field check_path=<bucket>` writes it.

## 2026-10-13 — Outside any project, `rclonec connections` and `rclonec doctor` read the machine's own connections

`rclonec` declares `connections` and `doctor` as machine reads. Run outside any project, `rclonec connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `rclonec doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `rclonec` declares what a person is asked for, and writes it through the contract

`rclonec manifest` now declares a connection's fields under `connections.fields` (`type`, `options`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `rclonec connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `rclonec connection remove <id>` takes it out again with the secrets it wrote. Bare `rclonec help` ends with a section rendered from these declarations. Nothing else changes.

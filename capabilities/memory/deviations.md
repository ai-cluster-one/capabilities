# memory deviations

This file's sole purpose is to hold `memory`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults and the [DOCTRINE](../../DOCTRINE.md), kept apart so an audit reads them as choices, not drift (DOCTRINE - *[Deviations are allowed - and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*).

## `adopt` reads the machine services' opt-in lists to find project roots

A capability resolves a sibling's values through that sibling's surface rather than reading its files (rule 7). With no root named, `memory adopt` looks for the projects to adopt in every `$XDG_CONFIG_HOME/<capability>/service/projects.{json,toml}` on the machine and takes the `root` of each entry it finds there, without asking the capability that owns the list.

The machine holds no registry of its projects, and those lists are the only record of which project roots live on it. The read is a default for one migration verb and decides nothing by itself: it only proposes roots, every root is reported with the list it came from, a list that does not read is reported rather than guessed at, and naming roots on the command line reads no list at all. Nothing is written to any list but memory's own.

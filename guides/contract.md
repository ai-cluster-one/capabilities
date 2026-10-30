# The shared capability contract

Use this guide to understand how a capability binds the shared contract code from the `capabilities-contract` library, which version range it declares, and how a change to the shared code ships.

The shared code - the core, store and connections tiers - lives once, in the
`capabilities-contract` library (`capabilities_contract.capability`). A
capability declares the library in its PEP 723 header by the range the manager
supports and binds the tiers it carries after its own declarations:

```python
from capabilities_contract.capability import bind as _contract_bind

_contract_bind(globals(), "core")
_contract_bind(globals(), "store")
_contract_bind(globals(), "connections")  # only with connections
```

`capabilities new` writes both into a new skeleton. Installation and
`capabilities audit` refuse a script that does not bind the core and store
tiers, or whose declared range admits a library version older than the first
to carry the shared code or none the manager supports (`contract-range`).

A change to the shared code is made in the library, tested by the library's own
suite and released as a library version inside the range. Every capability
whose range admits it picks it up with no edit of its own - the manager brings
an installed capability's library to the newest release in its range on
`capabilities install` and `update` - so
the change touches no capability script and is judged by the library's suite
and the suites of the capabilities that need it. A change that would break a
capability is released outside the range, and capabilities move to the new
range one at a time.

Only the executable is brought forward. Another script a bundle runs under its
own PEP 723 header - a service daemon, a helper the CLI runs with
`uv run --script` - keeps the environment uv first made for it, with whatever
library its range let in then. A release therefore refuses such a script whose
range admits a library older than the first to read the store setting the
manager writes (`contract-range`).

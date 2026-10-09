# clickup — change log

## 2026-10-09 — `connections` drops its `store` block

`clickup connections` no longer carries `store` (`mode`, `resolved_by`, `orphan_grants`): records are kept in files only, so there is no mode to report and no grant kept apart from its connection.

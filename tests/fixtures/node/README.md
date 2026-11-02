# Node fixtures

`telegram-connections.json` and `whatsapp-connections.json` are `<capability> connections --json` as each capability prints it, captured from throwaway connections in an isolated lane: one added with `--identity` (`personal`, `main`), one with `--field expected_account_id=…` (`fielded`, whatsapp, which declares no main identifier and so reports no identity for it) and one with neither (`spare`). Machine paths are rewritten under `/home/user`; the account ids are synthetic. Node login reads the binding from each entry's `identity`, else from its `expected_account_id` key.

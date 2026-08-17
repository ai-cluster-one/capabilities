# Authoring a managed capability

Use this guide to decide whether a system needs a capability, shape its smallest useful surface, create its source bundle, and bring it to release readiness.

Create a source with `capabilities source init <id> [--remote <git-url>]` and
ask the manager for its editable path with `capabilities source path <id>`.

Create a package with:

```sh
capabilities new <name> --source <id>
```

Add `--core-only` only when the capability genuinely has no connections. The
manager writes `capabilities/<name>/bin/<name>` and stamps the generated
contract fences.

## Decide before creating

Establish the system to reach, the two or three operations that matter, the
authentication shape, and where the capability will be used.

- Ordered steps consuming existing tools belong in a routine.
- Light reusable instructions belong in a skill or prompt.
- A one-off operation needs no package.
- A system to reach, a small verb surface, and credentials form a capability.

## Shape the contract

- Define the smallest domain verb surface and identify mutating verbs.
- Use `WRITE_DEFAULT = False` when writes leave the system.
- Declare credentials and choose project or user scope.
- Declare state when credentials mint sessions or caches.
- Keep success on stdout and structured failures on stderr.

Implement declarations, doctor, and domain verbs outside the generated
contract fences. Use placeholders in examples and resolve consumer-specific
values through the standard configuration surfaces.

## Verify and release

Stage the intended files and run `capabilities source index <id> --staged`.
Run `capabilities dev check <session>` when local validation feedback is useful,
commit the result, and follow `capabilities guide publishing` for the release
transaction. The candidate CI performs the authoritative immutable verification.

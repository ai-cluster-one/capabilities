# Conforming an existing CLI

Use this guide to bring an existing CLI to the capability contract while preserving the behavior of its domain verbs.

Read `SHEBANG.md` in the capability source first. Map the existing script
against the contract in this order:

- Declaration constants: name, summary, scope, credentials, write verbs,
  write default, docs base, state, post-install steps, and optional service.
- The project/global gate before domain dispatch, including exit 4.
- Contract verbs: help, doctor, connections, stub, manifest, guide, ids, refs.
- Credential resolution through the shared cascade, with secrets absent from
  command-line flags and committed files.
- Named connections and the per-connection write gate.
- State placement matching the credential scope.
- Structured success and error output with the standard exit taxonomy.
- Consumer-neutral code, help, and examples.

Create a managed package with `capabilities new <name> --source <id>`, then move
the domain implementation into the generated skeleton outside the contract
fences. Keep the existing domain arguments and output stable where they are
already intentional.

Verify the result with `capabilities audit <name> --from <path>`, run the
capability's doctor against the real service, and compare a representative
domain operation with the previous behavior.

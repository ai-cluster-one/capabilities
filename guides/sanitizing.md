# Sanitizing a capability project envelope

Use this guide to review one capability's consuming-project envelope for correct placement, unambiguous ownership, and accidental sensitive or consumer-specific content.

Inventory the envelope through the capability's own surface:

```sh
<name> refs
<name> ids list
<name> connections
```

An empty envelope is conformant. Review only content that exists.

## Placement checks

Inspect connections, identifiers, references, service config, and state in the
resolved project envelope.

- Connections hold explicitly chosen endpoints, identities, and behavior.
- Identifiers hold discovered non-secret structural values.
- References hold the project-specific model and interpretation.
- Routines hold executable project procedure.
- State follows the scope of the credentials that minted it.

## Content checks

For every reference, confirm that its description still represents its body.
Split a body that has grown into multiple independently loadable topics.

Walk both directions of the identifiers/reference boundary: structural lookup
values belong in identifiers, while treatment and interpretation belong in
references. A reference holds the model rather than a copy of lookup values.

Check domain ownership and consumer identity. Move sibling-domain facts to the
capability that owns that domain, keep the consuming project's identity in its
own files, and remove resolved or dead notes.

Consolidate duplicated facts into one home. Rewrite prose that defines itself
through history, negation, or a neighbouring system. Keep pointers flowing from
consumers to assets; an asset describes itself rather than naming routines or
workflows that consume it.

Move step-by-step project task recipes into routines while references retain the
model those routines apply. Move open work and resolved fix-it notes to the
project's task system.

Keep secrets out of committed files and state out of durable project content.

## Close

Report each finding with its file, the relevant fragment, the governing rule,
and the proposed move or fix. Apply changes within the consuming project's own
editing policy, rerun `<name> refs`, and refresh generated context through the
project's selected context owner.

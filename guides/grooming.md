# Grooming a capability

Use this guide to review an installed capability for contract drift, dead weight, stale guidance, and gaps in its useful verb surface.

Start with the mechanical checks:

```sh
capabilities audit <name>
capabilities doctor
<name> doctor
```

Then review the capability's deviations, help, verb surface, guides, connection
declarations, identifiers, and references:

- Confirm that help remains the single source of truth for the executable
  surface and that its examples remain consumer-neutral.
- Remove verbs that no longer earn their place and identify common operations
  that deserve a direct verb.
- Confirm that each guide file has an accurate title and preview paragraph and
  that repeatedly rediscovered authoring knowledge has one capability-owned
  guide.
- Keep chosen connection values in the connection registry and discovered
  structural values in identifiers.
- Confirm that secrets remain indirect and each connection's write policy is
  still correct.
- Review references for current descriptions, domain ownership, and placement.

Apply changes in a managed dev session, rerun the audit and manager selfcheck,
and finish through the release workflow in `capabilities guide dev`.

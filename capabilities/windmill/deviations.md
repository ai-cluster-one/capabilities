# windmill — recorded deviations

## A settings-envelope tier in the implicit default connection's cascade

The implicit default connection resolves through five tiers, not four: the
project settings envelope (`.capabilities/windmill/settings.json`, flat keys
`url` / `workspace` / `folder`) sits between project env and user config.

Why: a consuming project pins its instance and workspace as committed,
non-secret project config — visible in review, shared by every collaborator —
without requiring each developer to mirror them into `.env`. The token stays
in env tiers; only non-secret wiring lives in the envelope.

Intent preserved: the order is deterministic and an explicit override still
beats project config beats user config beats process env. The extra tier
applies to the implicit default only; registry connections carry their
non-secret wiring in their own entries.

# callva — recorded deviations

## A settings-envelope tier in the implicit default connection's cascade

The implicit default connection resolves its non-secret keys through five
tiers, not four: the project settings envelope
(`.capabilities/callva/settings.json`, flat key `api_url`) sits between
project env and user config. The secret (`CALLVA_API_KEY`) never resolves
from the envelope — it resolves through the env tiers alone.

Why: a consuming project pins its API base URL as committed, non-secret
project config — visible in review, shared by every collaborator — without
requiring each developer to mirror it into `.env`.

Intent preserved: the order is deterministic and an explicit override still
beats project config beats user config beats process env. The extra tier
applies to the implicit default only; registry connections carry their
non-secret wiring in their own entries.

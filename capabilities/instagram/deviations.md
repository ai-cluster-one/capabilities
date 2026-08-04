# instagram — recorded deviations

## Private browser-session transport

Instagram does not expose an official API for the personal-account profile,
thread-creation, and proactive messaging surface implemented here. The
capability reproduces authenticated requests made by instagram.com and stores a
redacted request template plus live cookies as per-connection user state. A
successful browser capture is the authentication ceremony; `doctor` proves the
session and exact account identity with a live read.

The web protocol is not stable. Profile reads refresh the dynamic CSRF-adjacent
form values that Instagram embeds in its HTML. A session or template failure is
reported rather than hidden behind broad retries. Challenges, checkpoints,
CAPTCHA, 2FA, and password entry remain browser-owned user interactions.

## No automatic retry after a send begins

A network failure after the message POST starts has ambiguous delivery. The
capability reports `delivery_unknown` and never retries automatically, because a
retry could create a duplicate external message. The consuming workflow owns
manual reconciliation.

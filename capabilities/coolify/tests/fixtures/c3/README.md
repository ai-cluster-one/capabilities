# C3 rollback fixtures

Anonymized Coolify 4.3.23 responses for `app rollback` and `app rollback-images`. Values are synthetic.

- `app-rollback-images-compose.json` is the answer recorded for a Docker Compose application that names its own images: Coolify lists only images named after the application's uuid, so it finds none and reports no current tag.
- `app-rollback-images.json` is the same answer for an application whose images Coolify tags per commit, in the shape the 4.3.23 controller returns: the current tag and each image's tag, created_at and is_current.
- `app-rollback.json` is the queued rollback, carrying the deployment UUID that `wait --deployment` takes; `app-rollback-skipped.json` is the answer without one when Coolify skips a duplicate of a queued deployment.
- `app-rollback-422.json` is Coolify's refusal of a ref it does not accept.

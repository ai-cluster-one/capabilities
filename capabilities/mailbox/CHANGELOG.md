# mailbox — change log

## Unreleased

- `send --bcc ADDR` (repeatable): blind recipients go on the SMTP envelope only; no Bcc header is ever written, so no copy of the message, the saved Sent copy included, names them. The result reports them under `bcc`.
- A From display name: the connection registry's optional `display_name`, or `send --from-name NAME` over it, rendered with `formataddr`; the envelope sender stays the bare address. The result reports the `from` header sent.
- The envelope no longer carries an empty recipient when a message has no Cc (`getaddresses` of an empty header read as one blank address).

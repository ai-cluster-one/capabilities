# Wiring a connection to Google Drive

Google Drive has no static API key: every path to a user's files is OAuth. The question is only which kind, and the answer decides how much ongoing maintenance the connection needs.

A **service account** is the low-maintenance path. Its JSON key does not expire, no OAuth consent screen is configured, and an app using one to reach its own data is not submitted for scope verification. A **user OAuth token** is the alternative, and needs the consent screen, a publishing decision, and a token that an app left in testing status stops honouring after a week.

Prefer the service account. Which of its two shapes you use depends on the account.

## Workspace: a service account that acts as a user

This is the full read-write arrangement, and the one to reach for when files must be created as well as read.

The service account is granted domain-wide delegation and then impersonates a real user in the domain. Because the acting identity is that user, files it creates are owned by that user and count against that user's storage. Drive applies that user's permissions and nothing else, so **what the connection can reach is decided entirely by what an administrator shared with that user** — the capability holds no permission model of its own.

Give the agent its own user in the domain rather than borrowing a person's. Its edits then show as its own in Drive's history, so agent work and human work stay tellable apart. That user needs a licence like any other.

1. In the Google Cloud console, pick or create a project and enable the Drive API.
2. Create a service account and download a JSON key for it. No OAuth consent screen is involved.
3. Open the service account's details and copy its **numeric client ID**.
4. In the Admin console, go to Security → Access and data control → API controls → Manage Domain Wide Delegation, add a new entry with that client ID, and authorize the scopes the connection needs (`https://www.googleapis.com/auth/drive` for read-write, `.../auth/drive.readonly` for read-only).
5. Share the folders the agent should reach with the user it will impersonate.

Delegation is domain-wide by construction: the key authorizes impersonating **any** user in the domain within the granted scopes. The connection pins one, but whoever holds the key can change that. Treat it as a domain-level secret, give the service account nothing but the Drive scopes, and use it for nothing else.

```json
{
  "type": "drive",
  "options": {"scope": "drive", "impersonate": "<user@domain>",
              "export_formats": "md,csv"},
  "secret_env": {"service_account_credentials": "<ENV_KEY>"},
  "allow_write": true
}
```

The env key holds the JSON key file's contents, not a path to it. `rclone` accepts the blob inline, so the connection carries no file and moves to another machine as an environment variable.

## Consumer account: a service account with folders shared to it

Without a domain there is no delegation, so the service account acts only as itself. Share a folder with the service account's own address — it appears in the Drive share dialog like any collaborator — and it reaches what was shared and nothing else.

Expect this to be **read-only in practice**. A service account created after April 2025 has no Drive storage of its own and cannot own files, so it reads and downloads from a shared folder but fails to create new ones there. Declare `"allow_write": false` and let the gate say so plainly, rather than discovering it as a quota error mid-transfer.

Writing from a consumer account means a user OAuth token instead: register an OAuth client, run `rclone authorize "drive" <client_id> <client_secret>` on a machine with a browser, and carry the token blob it prints into `secret_env` as `token`.

## Reading documents as text

Native Docs, Sheets and Slides are not files with bytes until something asks for an export format. Set `export_formats` on the connection — `md` for Docs, `csv` for Sheets — and `rclone cat <remote>:path/to/doc` returns the document as markdown.

Leaving it unset gets the rclone default of `docx,xlsx,pptx,svg`, which is rarely what an agent wants.

## Shared drives and subfolders

`team_drive` takes a shared drive's id and scopes the remote to it. `root_folder_id` re-roots the remote on one folder, which is the narrowest way to hand over exactly one tree without relying on sharing alone.

Every other option is an rclone drive backend option, spelled as rclone spells it: `rclone help backend drive` prints the set, and [rclone's Drive documentation](https://rclone.org/drive/) explains them.

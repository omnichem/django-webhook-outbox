# Security notes

Everything in this package is configured by staff in the Django admin. The
trust model is: **whoever can edit channels or tracked tables can make the
server send arbitrary data from your database to an arbitrary URL.** Give
those permissions only to people you would trust with that.

## Secrets in channel config are stored in plaintext (1.0)

Webhook headers such as `Authorization: Bearer ...` are stored as plain JSON
in `NotificationChannel.config`. As a result:

- Anyone with *view* permission on notification channels in the admin can
  read the tokens.
- The tokens end up in database dumps, backups and replicas.

Use a token that is dedicated to this receiver and can be rotated, limit the
`view_notificationchannel` / `change_notificationchannel` permissions, and
treat backups accordingly. Encryption of the config field is not part of
1.0.

## Template placeholders call methods

Each part of a `{dotted.path}` placeholder is read with `getattr`, and
**every callable on the way, except managers, is called with no
arguments**. This is what makes `{get_status_display}` work, but it also
means:

- `{delete}` in a message template **deletes the tracked row** when the
  poller renders it. The same goes for any zero-argument method with side
  effects (`{save}` bumps `updated_at` and so triggers a new UPDATE event on every poll after the dedup window; a custom `{send_invoice}` sends an invoice; …), on the row
  or on any related object reachable through foreign keys.
- Any attribute reachable from the row can be put into the message and sent
  to the channel URL, for example `{user.password}` (the password hash) or
  tokens kept on related models.

Only trusted administrators should have `add_trackedtable` /
`change_trackedtable`. Review templates like code. 1.0 keeps this behaviour
for compatibility; `validate_template()` checks field paths but does not
block methods.

## SSRF: private addresses are allowed on purpose

Webhook URLs only have to be absolute `http(s)` URLs. Private, loopback and
link-local addresses are **deliberately allowed**, because the expected
receiver is often a neighbouring internal service. As a result, a staff
member can point a channel at internal endpoints, including cloud metadata
services, and for non-2xx responses read the first 500 characters of the
response body back from `error_message` in the admin. Redirects are not
followed, which stops redirect-based pivots, but a direct URL works.

If that is not acceptable in your environment:

- restrict outbound traffic of the worker with an egress firewall or proxy
  (httpx honours `HTTPS_PROXY` / `HTTP_PROXY`), and/or
- register your own channel class that validates destinations (see
  [channels.md](channels.md)) and remove the built-in one with
  `"CHANNELS": {"webhook": None, "safe_webhook": "..."}`.

## Other notes

- TLS certificates are verified (httpx default). There is no option to turn
  verification off.
- There is no request signing in envelope v1. Receivers should authenticate
  the caller with the secret header and only accept HTTPS.
- Error messages stored in the outbox can contain fragments of receiver
  responses. They are shown in the admin and are not sent anywhere else.
- Self-tracking (tracking `webhook_outbox`'s own models) would create a
  feedback loop. The admin does not offer these models, and system check
  W013 reports existing rules that track them.

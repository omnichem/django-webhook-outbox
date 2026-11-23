# Settings reference

All settings live in one dict:

```python
WEBHOOK_OUTBOX = {
    "MAX_RETRIES": 3,
    "WEBHOOK_TIMEOUT": 30,
    "SITE_URL": "https://shop.example.com",
}
```

Keys you leave out take their default. Each value is read when it is used,
so `override_settings` works in tests. Unknown keys trigger the system check
warning `webhook_outbox.W001`.

| Key | Default | Legacy fallback | Meaning |
| --- | --- | --- | --- |
| `MAX_RETRIES` | `3` | `NOTIFICATION_MAX_RETRIES` | Failed attempts before an entry becomes *failed*. |
| `WEBHOOK_TIMEOUT` | `30` | `NOTIFICATION_WEBHOOK_TIMEOUT` | HTTP timeout in seconds for webhook channels whose config has no `timeout`. |
| `SITE_URL` | `""` | `SITE_URL` | Base URL put in front of the admin path in `{admin_link}`. Trailing slashes are stripped. |
| `DEDUP_WINDOW_SECONDS` | `300` | – | Window of the duplicate check, see [semantics.md](semantics.md). |
| `DELIVERY_BATCH_SIZE` | `100` | – | Entries per delivery pass. |
| `POLL_INTERVAL` | `30` | – | Seconds between polls in `webhook_outbox_worker`. Pass the same value to `beat_schedule()` for Celery. |
| `SEND_INTERVAL` | `10` | – | Seconds between delivery passes. |
| `CHANNELS` | `{}` | – | Extra channel classes, `{"type": "dotted.path.Class"}` (or the class itself). Merged over the built-in `{"webhook": "webhook_outbox.services.channels.webhook.WebhookChannel"}`; map a type to `None` to remove it. |
| `TRACKABLE_EXCLUDE_APPS` | `[]` | – | App labels or full app names never offered as trackable in the admin. `django.*` apps and `webhook_outbox` itself are always excluded. |
| `ADMIN_RETRY_HOOK` | `None` | – | Dotted path to a `callable(outbox_id)` that the admin *Retry* action calls after resetting an entry, e.g. `"webhook_outbox.contrib.celery.tasks.enqueue_single_notification"` or `"webhook_outbox.runner.deliver_entry"` (this one sends synchronously inside the admin request). If the hook raises, the error is logged and the entry stays pending. |
| `POLL_OVERLAP_SECONDS` | `60` | – | *1.1.* The poll cursor trails the poll time by this many seconds, so rows that commit up to this long after their `created_at` / `updated_at` are still found. The overlap is de-duplicated exactly. `0` restores the 1.0 cursor, which misses late commits. See [semantics.md](semantics.md#detection-rules-polling). |
| `ENVELOPE_VERSION` | `1` | – | *1.1.* Envelope sent by webhook channels: `1` (the 1.0 document) or `2`. A channel overrides it with `"envelope_version"` in its config. See [envelope.md](envelope.md). |
| `ENVELOPE_V1_EVENT_ID` | `False` | – | *1.1.* Add `"event_id"` as a seventh key to envelope v1. |
| `SIGNAL_CAPTURE` | `{}` | – | *1.1.* Models whose `post_save` / `post_delete` queue events: `["shop.order"]`, or `{"shop.order": {"fields": ["status"]}}` to also send the old/new values of those fields (envelope v2). Empty: off. See [semantics.md](semantics.md#signal-capture). |
| `ENQUEUE_HOOK` | `None` | – | *1.1.* Dotted path to a `callable(outbox_id)` called on commit of the transaction in which `enqueue()` or signal capture queued an entry, e.g. `"webhook_outbox.contrib.celery.tasks.enqueue_single_notification"`. If it raises, the error is logged and the entry stays pending. |

## Precedence

For keys that have a legacy fallback, the value is taken from the first
place that has it:

1. `WEBHOOK_OUTBOX[key]`
2. the legacy Django setting (`NOTIFICATION_MAX_RETRIES`,
   `NOTIFICATION_WEBHOOK_TIMEOUT`, `SITE_URL`)
3. the default

The fallbacks let a project that used the original in-house app switch
without renaming settings. New settings have no `NOTIFICATION_*` names.
`NOTIFICATION_POLL_INTERVAL` / `NOTIFICATION_SEND_INTERVAL` were only ever
used to build the Celery beat schedule; they are not read by the package.

## System checks

| Id | When |
| --- | --- |
| `webhook_outbox.E001` | `WEBHOOK_OUTBOX` is not a dict |
| `webhook_outbox.W001` | unknown key in `WEBHOOK_OUTBOX` |
| `webhook_outbox.E002` | `CHANNELS` is not a dict |
| `webhook_outbox.E003` | a `CHANNELS` entry cannot be imported or is not a `BaseChannel` subclass |
| `webhook_outbox.E004` | `TRACKABLE_EXCLUDE_APPS` is not a list of strings |
| `webhook_outbox.E005` | `ADMIN_RETRY_HOOK` or `ENQUEUE_HOOK` is not an importable callable |
| `webhook_outbox.E006` | a numeric setting is not a number of the right type or is out of range: `MAX_RETRIES` and `DELIVERY_BATCH_SIZE` integers >= 1, `DEDUP_WINDOW_SECONDS` and `POLL_OVERLAP_SECONDS` integers >= 0, `WEBHOOK_TIMEOUT`, `POLL_INTERVAL` and `SEND_INTERVAL` numbers > 0 (strings, e.g. read from the environment, are rejected) |
| `webhook_outbox.E007` | `ENVELOPE_VERSION` is not `1` or `2`, or `ENVELOPE_V1_EVENT_ID` is not a bool |
| `webhook_outbox.E008` | `SIGNAL_CAPTURE` names an unknown model or field, a webhook_outbox model, or has the wrong shape |
| `webhook_outbox.W010` | a channel row uses a channel type with no registered class (database check) |
| `webhook_outbox.W011` | a tracked table points to a model that is not installed (database check) |
| `webhook_outbox.W012` | a tracked model lacks `created_at` / `updated_at` for its event type (database check; DELETE rules are not polled and need neither) |
| `webhook_outbox.W013` | a tracked table tracks a webhook_outbox model, i.e. self-tracking (database check) |

Database checks run with `manage.py check --database default` and during
`migrate`. They only warn, so a stale row can never block a deployment.

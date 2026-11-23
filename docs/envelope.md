# Envelopes

`WebhookChannel` POSTs one JSON object per outbox entry. Two formats exist:

- **Envelope v1** is the default. It is the exact document 1.0 (and the code
  the package was extracted from) sends. It is frozen: with default settings
  1.1 sends the same bytes as 1.0.
- **Envelope v2** (1.1+) is opt-in. It adds an event id, a structured
  source, old/new values and producer data.

The version is chosen per channel:

1. `"envelope_version"` in the channel config, e.g.
   `{"url": "https://...", "envelope_version": 2}`;
2. otherwise `WEBHOOK_OUTBOX["ENVELOPE_VERSION"]` (default `1`).

Custom channels get the same `NotificationPayload` and can call
`payload.as_dict(version)` (or `as_v1_dict()` / `as_v2_dict()`), or
`webhook_outbox.services.channels.get_envelope_version(channel)` to honour
the same setting.

## Envelope v1

```http
POST /hooks/orders HTTP/1.1
Host: receiver.example.com
Content-Type: application/json
Authorization: Bearer <token>          (headers from the channel config)

{"event":"insert","source_model":"shop.order","source_pk":"1042","tracked_table":"New orders","message":"New order A-1042 from Acme Corp","occurred_at":"2026-08-03T10:00:00.123456+00:00"}
```

### Fields

The six keys are always present, in this order, and every value is a
string. There is no version key. By default there is no event id either.

| Key | Value |
| --- | --- |
| `event` | The event type of the outbox entry: `"insert"` or `"update"` from the poller, `"delete"` from signal capture, or the `event_type` passed to `enqueue()`. |
| `source_model` | `"<app_label>.<model>"`, the lowercase content type of the tracked model, e.g. `"shop.order"`. |
| `source_pk` | Primary key of the row, as a string (`str(pk)`). |
| `tracked_table` | `name` of the TrackedTable (the tracking rule) that produced the event. |
| `message` | The rendered `message_template` (or the `message` passed to `enqueue()`). |
| `occurred_at` | `created_at` of the outbox entry in ISO 8601 (`datetime.isoformat()`): when the event was queued, not when the row changed. With `USE_TZ = True` it is UTC with a `+00:00` offset and microseconds (dropped when they are zero). With `USE_TZ = False` it is a naive local time without an offset. |
| `event_id` | **Only with `WEBHOOK_OUTBOX["ENVELOPE_V1_EVENT_ID"] = True`**, as the seventh key: the event id, see [below](#event-id). |

JSON Schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "django-webhook-outbox envelope v1",
  "type": "object",
  "required": ["event", "source_model", "source_pk", "tracked_table", "message", "occurred_at"],
  "additionalProperties": false,
  "properties": {
    "event": {"type": "string", "minLength": 1, "maxLength": 10},
    "source_model": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*\\.[a-z0-9_]+$"},
    "source_pk": {"type": "string"},
    "tracked_table": {"type": "string"},
    "message": {"type": "string"},
    "occurred_at": {"type": "string"},
    "event_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    }
  }
}
```

In 1.0 `event` was only `"insert"` or `"update"`. It can only be something
else if you add DELETE rules with signal capture or call `enqueue()` with
another event type, both new in 1.1.

## Envelope v2

```json
{
  "schema_version": 2,
  "event_id": "1b0e6f0a-3c4d-4e5f-8a9b-0c1d2e3f4a5b",
  "event_type": "update",
  "occurred_at": "2026-08-03T10:00:00.123456+00:00",
  "source": {"model": "shop.order", "pk": "1042"},
  "tracked_table": "Order status",
  "message": "Order A-1042 is now paid",
  "changes": {"status": {"old": "new", "new": "paid"}},
  "extra": {"actor": "payment-gateway"}
}
```

All nine keys are always present, in this order.

| Key | Value |
| --- | --- |
| `schema_version` | Always the number `2`. |
| `event_id` | The event id (UUID string), see [below](#event-id). |
| `event_type` | As `event` in v1: `"insert"`, `"update"`, `"delete"` or the type given to `enqueue()` (at most 10 characters). |
| `occurred_at` | When the source row changed, if known: its `created_at` (insert) or `updated_at` (update) as read by the poller, signal capture or `enqueue()`. Otherwise (deletes, custom event types, models without timestamps, entries queued by 1.0) the time the event was queued. Same ISO 8601 format as v1. |
| `source` | `{"model": "<app_label>.<model>", "pk": "<str(pk)>"}`, the v1 `source_model` / `source_pk`. |
| `tracked_table` | `name` of the tracking rule. |
| `message` | The rendered message. |
| `changes` | `{"<field>": {"old": ..., "new": ...}}` or `null`. See [Old and new values](#old-and-new-values). |
| `extra` | The JSON object given to `enqueue(extra=...)`, or `null`. |

Values in `changes` and `extra` are encoded with Django's
`DjangoJSONEncoder` when they are stored: datetimes, dates, times and
durations become ISO 8601 strings, `Decimal` and `UUID` become strings.

JSON Schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "django-webhook-outbox envelope v2",
  "type": "object",
  "required": [
    "schema_version", "event_id", "event_type", "occurred_at", "source",
    "tracked_table", "message", "changes", "extra"
  ],
  "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 2},
    "event_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    },
    "event_type": {"type": "string", "minLength": 1, "maxLength": 10},
    "occurred_at": {"type": "string"},
    "source": {
      "type": "object",
      "required": ["model", "pk"],
      "additionalProperties": false,
      "properties": {
        "model": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*\\.[a-z0-9_]+$"},
        "pk": {"type": "string"}
      }
    },
    "tracked_table": {"type": "string"},
    "message": {"type": "string"},
    "changes": {
      "type": ["object", "null"],
      "additionalProperties": {
        "type": "object",
        "required": ["old", "new"],
        "additionalProperties": false,
        "properties": {"old": {}, "new": {}}
      }
    },
    "extra": {"type": ["object", "null"]}
  }
}
```

### Old and new values

`changes` is only filled when the producer knows the old values:

- **`enqueue(..., changes={...})`**: what the caller passes.
- **Signal capture** with `"fields"` configured: on update, the listed
  fields whose value changed (the old value is read from the database in
  `pre_save`). Creates and deletes have `changes: null`.
- **Polling** always gives `changes: null`. The poller only sees the row as
  it is when it polls.

`changes` and `extra` are refused by `enqueue()` for a channel that uses
envelope v1, because v1 cannot carry them.

## Event id

Every outbox entry written by 1.1 gets a random UUID (`event_id`) when it is
created. It is stored on the entry, so **every delivery attempt of an entry
sends the same id**, including retries from the admin. Entries written by
1.0 have no id yet and get one, stored the same way, on their first delivery
attempt after the upgrade.

Receivers can use it to drop duplicates: delivery is at-least-once (see
below). v2 always has it. v1 has it only with `ENVELOPE_V1_EVENT_ID`.

## Serialisation

The body is produced by httpx (`json=`), so its exact bytes depend on the
httpx version:

- httpx >= 0.28: compact (`","` / `":"` separators), UTF-8, non-ASCII
  characters sent as they are.
- httpx 0.27: `", "` / `": "` separators, non-ASCII escaped as `\uXXXX`.

Both forms are the same JSON document. **Receivers must parse the JSON and
must not compare raw bytes.** `tests/test_envelope_golden.py` pins both
byte forms and the key order of v1 and v2, and validates both against the
schemas on this page.

## Receiver contract

- Any 2xx response means *delivered*. The body is ignored.
- Any other status, a redirect, a timeout (the channel `timeout`, by default
  `WEBHOOK_TIMEOUT` = 30 s) or a connection error is a failed attempt. The
  first 500 characters of a non-2xx response body are stored in the outbox
  entry's `error_message`, visible in the admin.
- The same event can arrive more than once (at-least-once delivery), and
  events of one row can arrive out of order after retries. Make the handler
  idempotent, e.g. by remembering `event_id`.
- There is no request signature. Authenticate with a secret header
  (`"headers": {"Authorization": "Bearer ..."}`) and use HTTPS.

## Versioning

Within 1.x an envelope version never changes. New keys, or a change in
their meaning, come as a new envelope version, opt-in per channel. Older
versions stay available. The only addition to v1 is the opt-in `event_id`
key.

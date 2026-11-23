# Envelope v1

`WebhookChannel` POSTs one JSON object per outbox entry. The format is
**envelope v1**. Release 1.0 sends exactly the document the original
implementation sent, so existing receivers keep working unchanged.

```http
POST /hooks/orders HTTP/1.1
Host: receiver.example.com
Content-Type: application/json
Authorization: Bearer <token>          (headers from the channel config)

{"event":"insert","source_model":"shop.order","source_pk":"1042","tracked_table":"New orders","message":"New order A-1042 from Acme Corp","occurred_at":"2026-08-03T10:00:00.123456+00:00"}
```

## Fields

The six keys are always present, in this order, and every value is a
string. Nothing else is sent: there is no version key and no event id (see
[limitation 6](semantics.md#known-limitations)).

| Key | Value |
| --- | --- |
| `event` | `"insert"` or `"update"`: the event type of the tracking rule. |
| `source_model` | `"<app_label>.<model>"`, the lowercase content type of the tracked model, e.g. `"shop.order"`. |
| `source_pk` | Primary key of the row, as a string (`str(pk)`). |
| `tracked_table` | `name` of the TrackedTable (the tracking rule) that produced the event. |
| `message` | The rendered `message_template`. |
| `occurred_at` | `created_at` of the outbox entry in ISO 8601 (`datetime.isoformat()`): when the poller queued the event, not when the row changed. With `USE_TZ = True` it is UTC with a `+00:00` offset and microseconds (dropped when they are zero). With `USE_TZ = False` it is a naive local time without an offset. |

JSON Schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "django-webhook-outbox envelope v1",
  "type": "object",
  "required": ["event", "source_model", "source_pk", "tracked_table", "message", "occurred_at"],
  "additionalProperties": false,
  "properties": {
    "event": {"enum": ["insert", "update"]},
    "source_model": {"type": "string", "pattern": "^[a-z0-9_]+\\.[a-z0-9_]+$"},
    "source_pk": {"type": "string"},
    "tracked_table": {"type": "string"},
    "message": {"type": "string"},
    "occurred_at": {"type": "string"}
  }
}
```

## Serialisation

The body is produced by httpx (`json=`), so its exact bytes depend on the
httpx version:

- httpx >= 0.28: compact (`","` / `":"` separators), UTF-8, non-ASCII
  characters sent as they are.
- httpx 0.27: `", "` / `": "` separators, non-ASCII escaped as `\uXXXX`.

Both forms are the same JSON document. **Receivers must parse the JSON and
must not compare raw bytes.** `tests/test_envelope_golden.py` pins both
byte forms and the key order.

## Receiver contract

- Any 2xx response means *delivered*. The body is ignored.
- Any other status, a redirect, a timeout (the channel `timeout`, by default
  `WEBHOOK_TIMEOUT` = 30 s) or a connection error is a failed attempt. The
  first 500 characters of a non-2xx response body are stored in the outbox
  entry's `error_message`, visible in the admin.
- The same event can arrive more than once (at-least-once delivery), and
  events of one row can arrive out of order after retries. Make the handler
  idempotent.
- There is no request signature in v1. Authenticate with a secret header
  (`"headers": {"Authorization": "Bearer ..."}`) and use HTTPS.

## Versioning

The envelope will not change within 1.x. Any future change to the keys or
their meaning will get a new envelope version, opt-in per channel, and the
v1 document will stay available.

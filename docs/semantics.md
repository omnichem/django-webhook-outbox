# Semantics and known limitations

Release 1.0.0 reproduces the behaviour of the code it was extracted from
exactly. Most of the limitations below come from timestamp polling. They are
kept on purpose in 1.0 so that existing deployments behave the same after
switching to the package. Each one is pinned by a test named
`test_limitation_*` in `tests/test_semantics.py`, so a change in behaviour
cannot slip in unnoticed.

In short: **delivery is best-effort and at-least-once once an entry is in
the outbox. Detection is best-effort and can miss changes.** Do not use 1.0
for anything that must see every change exactly once (billing, audit logs,
replication).

## Detection rules

For a tracked table with cursor `since = last_polled_at`, a poll that starts
at `now`:

| Rule | Condition |
| --- | --- |
| First poll (`since` is NULL) | Sets `last_polled_at = now`, reports nothing (no backfill). |
| INSERT | `created_at > since` |
| UPDATE | `updated_at > since AND created_at < since` |
| Cursor | Set to `now` (the poll's start time) after the query, before outbox entries are written. |
| Dedup | A change is skipped if an outbox entry with the same `(tracked_table, source_pk, event_type)` has `created_at >= now - DEDUP_WINDOW_SECONDS` (300 s). |

Other rules:

- Rows are read through the model's default `objects` manager. A manager
  that hides rows (soft delete, tenancy) hides their events. A model without
  an `objects` manager makes its tracked table fail on every poll. The error
  is logged, the cursor does not move, and other tables are not affected.
- A tracked model without the required fields (`created_at` for insert,
  `created_at` and `updated_at` for update), or a content type whose model no
  longer exists, is skipped with an error in the log. Its cursor is not
  touched. System checks W011/W012 report both cases.
- Inactive tracked tables, and tracked tables of an inactive channel, are
  not polled, so their cursor stands still. **When you re-activate them, every change
  since deactivation is reported at once.**
- An error while polling one table is logged and does not stop the others.
  The same holds per row: one row whose outbox entry cannot be written does
  not stop the other rows.
- The message is rendered when the poller finds the row, from the row as it
  is *at that moment*, not as it was when it changed.
- `{field.path}` placeholders traverse attributes. Every callable on the way
  (except managers) is called with no arguments. A path that resolves to
  `None` renders as `[field.path]`. If rendering raises, the raw template is
  stored. See [security.md](security.md) for the hazards of the callable rule.

## Delivery rules

- One pass takes up to `DELIVERY_BATCH_SIZE` (100) entries with
  `status = pending AND retry_count < MAX_RETRIES`, ordered by `created_at`.
- 2xx: `status = sent`, `sent_at = now`, `error_message = ""`. `retry_count`
  is kept.
- Anything else: `retry_count += 1`, `error_message` = the error. Once
  `retry_count >= MAX_RETRIES` (3), `status = failed`.
- Redirects are not followed, so a 3xx response is a failure (`HTTP 302: `).
- Error formats: `HTTP <code>: <first 500 characters of the body>`,
  `Request timed out`, `Request error: <detail>`, `Unexpected error: <detail>`,
  `Channel is inactive`, `Unsupported channel type: <type>`, or the
  `ValueError` message of an invalid channel config.
- The result of a pass is `{"sent": n, "failed": n}`. `failed` counts entries
  that became *failed* in this pass (plus entries whose processing raised),
  not every failed attempt.
- **Retry selected failed notifications** (admin) resets failed entries to
  `pending`, `retry_count = 0` and an empty `error_message`. If
  `ADMIN_RETRY_HOOK` is set, it is then called with each entry's pk.
- **Mark selected as sent** sets `status = sent` and `sent_at = now` with a
  bulk `UPDATE`, so `updated_at`, `retry_count` and `error_message` stay as
  they were.
- `deliver_entry(pk)` (and the Celery task `send_single_notification`) sends
  one entry whatever its retry count, unless it is already *sent*, and
  records the result the same way.

## Known limitations

These are deliberately **not** fixed in 1.0.

1. **The cursor moves before the outbox entries are written.** The cursor
   is saved first, then each entry is inserted in its own transaction. If an
   insert fails, or the process dies in between, the change is lost: the
   next poll starts after it.
2. **Late commits are missed.** `created_at` / `updated_at` are set by the
   application when the row is saved, not when its transaction commits. Take
   a row saved at 12:00:05 in a transaction that commits at 12:00:20. A poll
   at 12:00:10 does not see the row yet, but still moves the cursor to
   12:00:10, so the next poll does not see it either. Long transactions and
   clock skew between application servers make this more likely.
3. **The dedup window suppresses legitimate events.** A second real change
   to the same row, of the same event type, within 5 minutes of the previous
   notification is dropped. The window is there to absorb overlapping polls,
   but it cannot tell those apart from real changes.
4. **Changes collapse.**
   - Several updates of a row between two polls give one UPDATE event.
   - A row created and updated between two polls gives only an INSERT
     event.
   - A row whose `created_at` equals the cursor exactly (same microsecond)
     is reported neither as an insert (`>`) nor as an update (`<`).
   - Deletes are not detected at all.
   - `QuerySet.update()` does not touch `auto_now` fields, so bulk updates
     are invisible unless they set `updated_at` themselves. Saving a row
     without changes still bumps `updated_at` and gives an UPDATE event.
5. **No old/new values.** The envelope carries the rendered message, not
   which fields changed or their previous values. Update rules cannot be
   limited to certain fields.
6. **No event id.** The envelope has no unique id, and `occurred_at` is when
   the poller queued the entry (up to one poll interval after the change),
   not when the row changed. Delivery is at-least-once: if the receiver
   answered 2xx but the process died before the entry was marked *sent*, it
   is sent again. Receivers that must deduplicate can use
   `(tracked_table, source_model, source_pk, event, occurred_at)` as a
   best-effort key.
7. **No row locking.** Neither the poller nor the delivery takes locks while
   choosing work. The dedup check is check-then-insert with no unique
   constraint, and the delivery locks an entry only *after* sending it, to
   record the result. Two workers polling or delivering at the same time can
   queue or send the same change twice. **Run exactly one poller and one
   delivery worker per database.**

More behaviours to be aware of:

- **No backoff.** Retries run on every delivery pass, so with the defaults
  an entry gets 3 attempts within about 20–30 seconds. A receiver that is
  down for a minute leaves its entries *failed* until someone retries them
  in the admin.
- If you lower `MAX_RETRIES`, pending entries that already have
  `retry_count >= MAX_RETRIES` are never picked again and never become
  *failed*. Reset them in the admin or by SQL.
- An entry queued for a channel that is deactivated afterwards uses up its
  attempts with `Channel is inactive` and becomes *failed*.
- The outbox is never pruned. Delete old *sent* entries yourself if the
  table grows too large, e.g. with a periodic
  `NotificationOutbox.objects.filter(status="sent", sent_at__lt=cutoff).delete()`.
- Polling filters on `created_at` / `updated_at` of the tracked models. Index
  those columns on large tables.
- `str(tracked_table)` raises `AttributeError` if the tracked model no longer
  exists (the content type is stale). Delete such tracked tables. System
  check W011 lists them.

# Semantics and known limitations

Release 1.0.0 reproduces the behaviour of the code it was extracted from
exactly, known limitations included. Release 1.1 closes the data-loss paths
of the poller and adds two more ways to produce events: an explicit
`enqueue()` API and optional signal capture. Each limitation that is still
there is pinned by a test named `test_limitation_*` (mostly in
`tests/test_semantics.py`), and each one fixed in 1.1 by a `test_fixed_*`
test, so a change in behaviour cannot slip in unnoticed.

In short:

- **Delivery is at-least-once** once an entry is in the outbox. Every
  attempt of an entry sends the same `event_id`.
- **Polling no longer loses a change it has seen.** An error while queueing
  leaves the cursor where it was, and rows that commit up to
  `POLL_OVERLAP_SECONDS` (60 s) late are still found. Polling still cannot
  see deletes, updates that do not touch `updated_at`, or intermediate
  states between two polls.
- **`enqueue()` is exact.** The event is written in your transaction and
  commits or rolls back with your change. Use it for anything that must not
  be missed.

## Capture methods

| | Polling (default) | Signal capture (opt-in) | `enqueue()` |
| --- | --- | --- | --- |
| Set up with | a tracked table in the admin | `SIGNAL_CAPTURE` + tracked tables | a call in your code + a tracked table |
| `Model.save()` create / update | yes, via `created_at` / `updated_at` | yes | where you call it |
| `Model.delete()`, `QuerySet.delete()` | **no** | yes (DELETE rules) | yes, with a `(model, pk)` ref |
| `QuerySet.update()`, `bulk_update()` | only if they set `updated_at` | **no** | if you call it for the rows |
| `bulk_create()` | yes | **no** | if you call it |
| Raw SQL, other services writing the table | yes, if they set the timestamps | **no** | **no** |
| Every intermediate state | **no**: collapsed between polls, and by the dedup window | every `save()` | every call |
| Old/new values (`changes`, envelope v2) | **no** (`null`) | for the listed `"fields"` | what you pass |
| Producer data (`extra`, envelope v2) | no | no | yes |
| Custom event types | no | no | yes (`tracked_table=` needed) |
| Atomic with your change | no (reads committed rows later) | yes | yes |
| Message rendered from | the row at poll time | the instance at save time | the instance (or your `message`) at call time |
| Filter (`predicate`, 1.2) checked on | the row at poll time, in the query | the instance at save time | the instance (a ref is loaded) at call time |
| Cost | one query per tracked table and poll | one query per captured save/delete (+1 with `"fields"`) | one INSERT per call |

**Which one to use:**

- **Polling** needs no code and works for models you do not own and for
  rows written outside the ORM. Use it when "something changed, here is the
  current state" is enough and missing a state in between is fine.
- **Signal capture** suits ORM-only writes where you need deletes, every
  `save()` or the old values of a few fields, without touching the code
  that saves. It is best-effort, see [below](#signal-capture).
- **`enqueue()`** suits business events, state-machine transitions and
  anything that must not be lost or collapsed. Call it in the same
  transaction as the change, after `save()`.

You can mix them on one tracked table. A change queued by signal capture or
`enqueue()` as `insert` / `update` records the row's `created_at` /
`updated_at`, so the poller recognises it and does not queue it again.

### Blind spots

Polling:

- deletes (soft deletes through a manager that hides rows look like nothing
  happened at all);
- `QuerySet.update()` / `bulk_update()` that do not set `updated_at`
  (`auto_now` is only applied by `save()`);
- intermediate states: several updates between two polls give one event,
  and the dedup window drops a second change of the same row within 300 s;
- a row created and updated within one second gives only the INSERT event;
- old values;
- rows that commit more than `POLL_OVERLAP_SECONDS` after their timestamp;
- changes made while a tracked table (or its channel) is inactive are
  reported all at once on re-activation, as the current state.

Signal capture:

- `QuerySet.update()`, `bulk_create()`, `bulk_update()`, raw SQL, writes
  from other processes that do not use your models;
- fixture loading (`raw=True` saves are skipped);
- `save(update_fields=[...])` without `updated_at` is captured, but without
  a source timestamp;
- a failure to queue the event is logged and the save goes on: the event is
  lost.

`enqueue()`:

- only sees what you call it for. A code path that forgets the call loses
  the event.

## Detection rules (polling)

For a tracked table with cursor `since = last_polled_at`, a poll that starts
at `now`. `overlap` is `POLL_OVERLAP_SECONDS` (default 60).

| Rule | Condition |
| --- | --- |
| First poll (`since` is NULL) | Sets `last_polled_at = now` and reports nothing (no backfill). |
| INSERT | `created_at > since` |
| UPDATE | `updated_at > since AND (created_at < since OR updated_at > created_at + 1 s)`. The second part applies only when `overlap > 0`. |
| DELETE rules | Not polled. They get events from signal capture and `enqueue()`. |
| Dedup | A change is skipped if an outbox entry with the same `(tracked_table, source_pk, event_type)` exists and either has the same source row timestamp (`source_changed_at`, exact) or `created_at >= now - window`. The window is the tracked table's `dedup_window_seconds`, else `DEDUP_WINDOW_SECONDS` (300 s). |
| Cursor | Set to `max(since, now - overlap)` once every detected change is queued or skipped as a duplicate. If any change could not be queued, the cursor stays at `since`. |

How a poll runs:

1. `poll_table()` opens a transaction and locks the tracked table's row
   (`SELECT ... FOR NO KEY UPDATE` on PostgreSQL, so it does not delay the
   commits of transactions that queued events; `FOR UPDATE` where that is
   not supported, such as MySQL; SQLite has no row locks and serialises
   writers instead). It re-reads the cursor under the lock, so a second poller of the
   same table waits and then continues from the cursor the first one left.
2. It finds the changes, and writes each entry in its own savepoint. One
   row that fails, in rendering or in the insert, does not stop the others.
3. It moves the cursor and commits. An error before the commit (a failing
   query, a killed process) rolls everything back, so the next poll reads
   the same window again.

**The overlap.** `created_at` / `updated_at` are set by the application
when the row is saved, not when its transaction commits. Take a row saved
at 12:00:05 in a transaction that commits at 12:00:20. A poll at 12:00:10
does not see it. In 1.0 that poll moved the cursor to 12:00:10, so the row
was never seen. In 1.1 the cursor trails the polls by `overlap`, so the
next poll still reads from before 12:00:05 and finds the row. Rows found
again in the overlap are recognised by their timestamp and queued once.

With `overlap > 0` the cursor is not the time of the last poll but up to
`overlap` before it. Because of that, the UPDATE rule also reports a row
created after the cursor when its `updated_at` is more than one second
after its `created_at`. Without this, an update made within a minute of an
insert would never be reported. The one-second margin separates a real
update from the creation itself, which writes both fields microseconds
apart. This assumes `created_at` is set at save time (`auto_now_add`); a
`created_at` filled earlier, for example by `default=timezone.now` on an
instance built more than one second before `save()`, also reports the
creation as an UPDATE.

`POLL_OVERLAP_SECONDS = 0` restores the 1.0 cursor (`since = now`) and its
late-commit race.

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
- **A row that can never be queued holds the cursor of its tracked table.**
  An example is a template that always raises. The poll logs the error
  every time and re-reads the window, which keeps growing. Rows queued in
  the meantime are not queued twice. Fix the cause, or deactivate the
  tracked table.
- The message is rendered when the poller finds the row, from the row as it
  is *at that moment*, not as it was when it changed.
- `{field.path}` placeholders traverse attributes. Every callable on the way
  (except managers) is called with no arguments. A path that resolves to
  `None` renders as `[field.path]`. If rendering raises, the raw template is
  stored. See [security.md](security.md) for the hazards of the callable rule.

## enqueue()

```python
from django.db import transaction
from webhook_outbox import enqueue

with transaction.atomic():
    order.status = "paid"
    order.save()
    enqueue(
        order,
        event_type="update",
        changes={"status": {"old": "new", "new": "paid"}},  # envelope v2 only
        extra={"payment_id": payment.pk},                   # envelope v2 only
    )
```

- The entry is a plain INSERT in the current transaction, on the database
  of `NotificationOutbox`. It is atomic with your change when both are on
  the same database. If the transaction rolls back, there is no entry.
  Delivery runners only see committed entries.
- `ENQUEUE_HOOK`, if set, is called with the entry's pk on commit
  (`transaction.on_commit`), e.g. to deliver it right away. A failing hook
  is logged. The entry stays pending and the next delivery pass sends it.
- The tracked table is the subscription: it gives the channel and the
  message template. Pass `tracked_table=`, or let `enqueue()` pick the one
  active tracked table for the model, `event_type` and (if given)
  `channel`. **No tracked table, or several active ones, raises
  `TrackedTableLookupError`.** If the tracked table or its channel is
  inactive, nothing is queued and `None` is returned. To send an event to
  several channels, call `enqueue()` once per tracked table.
- `event_type` is free text of at most 10 characters. Types other than
  `insert`, `update` and `delete` need `tracked_table=`, because a tracked
  table's own event type is one of those three.
- `message` defaults to the tracked table's template, rendered with the
  instance. With a `(model, pk)` ref the row is loaded for that. A row that
  no longer exists needs an explicit `message`.
- `changes` (`{"field": {"old": ..., "new": ...}}`) and `extra` (a JSON
  object) are only accepted when the channel uses envelope v2. With v1,
  `ValueError` is raised.
- The dedup window does not apply. Every call queues an event.
- For `insert` / `update` the entry records the instance's `created_at` /
  `updated_at`, so polling of the same tracked table skips that change. Call
  `enqueue()` **after** `save()`, otherwise the poller sees a newer
  `updated_at` and reports the change a second time.

## Signal capture

Off by default. Enable it per model:

```python
WEBHOOK_OUTBOX = {
    "SIGNAL_CAPTURE": {
        "shop.order": {"fields": ["status", "total"]},
        "shop.customer": {},
    },
}
```

- `post_save` with `created=True` queues `insert` for every active INSERT
  tracked table of the model. Other saves queue `update` for every active
  UPDATE tracked table. `post_delete` queues `delete` for every active
  DELETE tracked table. One save or delete gives one event per matching
  tracked table, also when the same tracked table is polled (see above).
- The events are written in the transaction of the save or delete.
- `"fields"`: on update, `changes` gets the listed fields whose value
  changed, as `{"field": {"old": ..., "new": ...}}`. Foreign keys give the
  related pks. The old values are read from the database in `pre_save`,
  which costs one query per save. This only applies to channels that use
  envelope v2; v1 channels get the event without `changes`. Creates and
  deletes have `changes: null`. Only list fields whose values may leave
  your system: they are sent to the receiver.
- **Best-effort.** Queueing happens in a savepoint. If it fails, the error
  is logged and the save or delete goes on, without the event. The
  [blind spots](#blind-spots) above apply.
- webhook_outbox's own models cannot be captured (system check E008).

## Filters

*1.2.* A tracked table can have a filter (`predicate`): a boolean
expression over the fields of the tracked model. Only rows that match it
produce events, whichever way the event is produced.

```text
status = "paid" and total >= 100
company.country in ("DE", "AT") and not archived = true
coupon_code is null
```

| | |
| --- | --- |
| Comparisons | `=` (or `==`), `!=`, `<`, `<=`, `>`, `>=`, `in (...)`, `not in (...)`, `is null`, `is not null` |
| Logic | `and`, `or`, `not`, parentheses. `not` binds tightest, then `and`, then `or`. Keywords are case-insensitive. |
| Values | strings in `"..."` or `'...'` (escapes `\\`, `\"`, `\'`), integers and decimals (`-12`, `0.5`, no exponent), `true`, `false` |
| Fields | a field of the model, or `relation.field` along forward foreign keys / one-to-one fields, at most 3 names. A foreign key on its own compares its primary key: `company = 5`, `company_id = 5`. |
| Field types | text (`CharField`, `TextField` and subclasses), integers, `FloatField`, `DecimalField`, `BooleanField`, `UUIDField` (a string), foreign keys. Dates, JSON, files and the like are refused. |
| Type rules | the value must fit the field: `count = "5"`, `name = 5` and `flag = 1` are errors. `<`, `<=`, `>`, `>=` only work on numbers (string ordering depends on the database collation). Compare with null only through `is null` / `is not null`. |

- **When it is checked.** Polling adds the filter to the detection query,
  so it sees the row as it is at poll time. Signal capture checks the
  instance at `save()` / `delete()`. `enqueue()` checks the instance as you
  pass it (a `(model, pk)` ref is loaded; a ref to a row that is gone raises
  `ValueError`, pass the instance instead).
- **State, not transition.** The filter knows the current values, not the
  old ones. An UPDATE rule with `status = "paid"` reports every change of a
  paid row, not only the moment it became paid. To report a transition
  once, track an audit/history model with INSERT (`to_state = "paid"`), use
  `enqueue()` at the transition, or keep the dedup window. Polling also
  misses a row that matched only between two polls
  (`test_limitation_filter_sees_the_row_at_poll_time`).
- **NULL.** Like Django's `exclude()`: a comparison with a missing value
  (a null column, or a null foreign key on the way) is false; its negation
  (`!=`, `not in`, `not (...)`) is true. `company.type.name != "LLC"`
  therefore matches rows without a company type. Polling and the in-memory
  check give the same result for every row; the test suite runs both on the
  same data on SQLite and PostgreSQL.
- **Text comparison** is the database's `=`: case-sensitive on PostgreSQL
  and SQLite, but it follows the column collation on MySQL/MariaDB, where
  the in-memory check (always case-sensitive) may differ.
- **Invalid filters.** The admin validates the filter on save. A filter
  that became invalid later (a field was removed) is reported by the system
  check `webhook_outbox.W014`. The poller then skips the tracked table
  **without moving its cursor**, so the changes made meanwhile are reported
  once the filter is fixed. `enqueue()` raises `PredicateError` (a
  `ValueError`), and signal capture logs the error and queues nothing for
  that rule (the save itself goes through).
- **Several rules per channel.** Since 1.2 a model, event type and channel
  can have several tracked tables, e.g. one per filter. `enqueue()` without
  `tracked_table=` then finds several active rules and raises
  `TrackedTableLookupError`; pass `tracked_table=`. Each rule has its own
  cursor and dedup window, so a row matching two rules gives two events.
  Nothing stops two rules with the same filter (or two without a filter)
  on one channel either; they send every event twice.
- What a filter may reach is restricted, see
  [security.md](security.md#filters).

## Delivery rules

- One pass takes up to `DELIVERY_BATCH_SIZE` (100) entries with
  `status = pending AND retry_count < MAX_RETRIES`, ordered by `created_at`.
- An entry without an `event_id` (written by 1.0) gets one, stored, before
  its first attempt. Later attempts send the same id.
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

The numbering is the one of 1.0. Items marked *fixed* are fixed in 1.1.

1. *Fixed in 1.1:* **the cursor moved before the outbox entries were
   written.** In 1.0, if an insert failed or the process died in between,
   the change was lost. Now the entries and the cursor are written in one
   transaction, and the cursor does not move while a change could not be
   queued.
2. **Late commits.** *Fixed in 1.1 up to `POLL_OVERLAP_SECONDS`* (60 s): a
   row whose transaction commits at most that long after its `created_at` /
   `updated_at` is still reported, once. Longer transactions, or clock skew
   between application servers larger than the overlap, are still missed.
   With `POLL_OVERLAP_SECONDS = 0` this is the 1.0 behaviour.
3. **The dedup window suppresses legitimate events.** A second real change
   to the same row, of the same event type, within 5 minutes of the previous
   notification is dropped by the poller. The default window stays 300 s,
   as in 1.0. Since 1.1 the poller recognises re-reads by the row timestamp
   and pollers no longer run concurrently on one table, so the window is no
   longer needed against duplicates. Set `dedup_window_seconds = 0` on a
   tracked table, or `DEDUP_WINDOW_SECONDS = 0`, to report every change the
   poller sees. `enqueue()` and signal capture never apply the window.
4. **Changes collapse** (polling).
   - Several updates of a row between two polls give one UPDATE event.
   - A row created and updated between two polls gives only an INSERT
     event. With `POLL_OVERLAP_SECONDS > 0` this only holds when the update
     came within one second of the creation. Later updates are reported as
     UPDATE events too.
   - A row whose `created_at` equals the cursor exactly (same microsecond)
     is reported neither as an insert (`>`) nor as an update (`<`).
   - Deletes are not detected by polling. Use signal capture or `enqueue()`
     with a DELETE tracked table.
   - `QuerySet.update()` does not touch `auto_now` fields, so bulk updates
     are invisible unless they set `updated_at` themselves. Saving a row
     without changes still bumps `updated_at` and gives an UPDATE event.
5. **No old/new values in polling.** The poller sends the rendered message
   with `changes: null` (envelope v2). Update rules cannot be limited to
   changes of certain fields: a [filter](#filters) (1.2) narrows them by the
   current values of the row, not by what changed. Old/new values come from `enqueue(changes=...)` or from
   signal capture with `"fields"`, over envelope v2.
6. **Event id.** *Fixed in 1.1, opt-in:* every entry has a stable
   `event_id`, sent in envelope v2 and, with `ENVELOPE_V1_EVENT_ID`, in v1.
   v1 `occurred_at` is still when the entry was queued (up to one poll
   interval after the change). v2 `occurred_at` is the row's timestamp when
   known. Delivery is at-least-once: if the receiver answered 2xx but the
   process died before the entry was marked *sent*, it is sent again.
   Receivers should deduplicate by `event_id`. Without it, they can use
   `(tracked_table, source_model, source_pk, event, occurred_at)` as a
   best-effort key.
7. **No row locking in delivery.** *The poller is fixed in 1.1:* it locks
   the tracked table's row, so two pollers of one table no longer queue the
   same change twice. Delivery still takes no locks while choosing work, and
   it locks an entry only *after* sending it, to record the result. Two
   delivery workers at the same time can send the same entry twice. **Run
   exactly one delivery worker per database.**

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
  those columns on large tables. The duplicate check filters outbox entries
  by `(tracked_table, source_pk, event_type)`. On a large outbox, an index on
  those columns helps; the package does not add one, to keep the 1.1
  migration a plain `ADD COLUMN`.
- `str(tracked_table)` raises `AttributeError` if the tracked model no longer
  exists (the content type is stale). Delete such tracked tables. System
  check W011 lists them.

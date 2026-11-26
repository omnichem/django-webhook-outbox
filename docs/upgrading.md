# Upgrade notes

## Versioning policy

- Versions follow semantic versioning and are taken from git tags (`X.Y.Z`).
- Within 1.x envelope v1 stays the same, and new migrations only add
  things (1.1 adds nullable columns) or relax constraints (1.2 adds a
  column with an empty default and drops a unique constraint). Documented semantics change only as
  described below and in the changelog.
- Fixes for the [known limitations](semantics.md#known-limitations) change
  observable behaviour. They are opt-in or come with a major version, unless
  they fix lost events without changing the envelope. The changelog says
  so.

## Upgrading from 1.1 to 1.2

```console
pip install -U django-webhook-outbox
python manage.py migrate webhook_outbox
```

Migration `0003_1_2_predicate`:

- adds `TrackedTable.predicate` (text, `''` for existing rules: they keep
  their behaviour exactly);
- drops the unique constraint on `(content_type, event_type, channel)`, so
  several rules (with different filters) can send the same model's events
  to one channel.

Nothing else changes for rules without a filter: detection, the envelope,
`enqueue()` and signal capture behave as in 1.1. Two things to know:

- **`enqueue()` lookups can become ambiguous.** Once you add a second active
  rule for the same model, event type and channel, `enqueue()` without
  `tracked_table=` raises `TrackedTableLookupError` (as it already did for
  several channels without `channel=`). Pass `tracked_table=` in that code.
- **Rolling back to 1.1** (`migrate webhook_outbox 0002`) re-creates the
  unique constraint and fails while two rules share (model, event type,
  channel). Delete or merge them first. The filters are dropped with the
  column.

Filters are described in [semantics.md](semantics.md#filters); what they may
reach in [security.md](security.md#filters).

## Upgrading from 1.0 to 1.1

```console
pip install -U django-webhook-outbox
python manage.py migrate webhook_outbox
```

**The migration.** `0002_1_1_event_fields` only adds nullable columns:
`event_id`, `source_changed_at`, `changes` and `extra` on the outbox, and
`dedup_window_seconds` on tracked tables. It also adds the `delete` event
type choice, which needs no SQL. Every operation is a plain
`ALTER TABLE ... ADD COLUMN ... NULL`, with no backfill and no table rewrite
on PostgreSQL. Rows written by 1.0 keep `NULL` and stay valid. Each one
gets an `event_id` on its first delivery attempt.

**PostgreSQL: locks during the migration.** The migration is
metadata-only and fast, but each `ALTER TABLE` takes an `ACCESS EXCLUSIVE`
lock on the outbox table and on the tracked-table table, and Django runs the
whole migration in one transaction, so the locks are held until it commits.
Behind a long-running transaction that touches either table the `ALTER`
waits, and every query that arrives after it queues behind it, so traffic
to those tables can stall. Run the migration with a short `lock_timeout`
and retry if it fails (a failed migration rolls back completely):

```console
PGOPTIONS="-c lock_timeout=5s" python manage.py migrate webhook_outbox
```

`PGOPTIONS` is read by libpq, so it works with psycopg 2 and 3. The same
setting can be passed through the `OPTIONS` of a dedicated migration
database alias: `{"options": "-c lock_timeout=5000"}`. Do not leave it on
the connections of the running application unless you want it there.

**Rolling upgrade: never run 1.0 and 1.1 pollers at once.** Stop every 1.0
poller (`webhook_outbox_worker`, `webhook_outbox_poll`, Celery beat
entries) before you start a 1.1 poller. A 1.0 poller does not take the
tracked-table lock, does not write `source_changed_at`, and advances the
cursor before it writes the entries, which undoes the 1.1 lost-event fixes.
Entries written
by 1.0 have no `source_changed_at`, so when 1.1 re-reads rows inside the
overlap it can only fall back to the dedup window for them. With
`DEDUP_WINDOW_SECONDS` (or a tracked table's `dedup_window_seconds`) lower
than `POLL_OVERLAP_SECONDS`, that gives duplicates.

**The envelope does not change.** With default settings the webhook body
is byte-for-byte the 1.0 document. Envelope v2 and the v1 `event_id` key
are opt-in (`ENVELOPE_VERSION`, `ENVELOPE_V1_EVENT_ID`, or
`"envelope_version"` in a channel config).

**Behaviour changes by default.** These fix lost events and do not change
the envelope:

1. **The poller writes entries and the cursor in one transaction.** If a
   change cannot be queued (for example, the template raises or the insert
   fails), the cursor stays where it was and the next poll retries. The
   other rows of the same poll are still queued, once. A row that can never
   be queued now holds its tracked table's cursor and is logged on every
   poll. In 1.0 it was silently skipped.
2. **`POLL_OVERLAP_SECONDS = 60`.** The cursor (`last_polled_at`) now trails
   the poll time by up to 60 s, and each poll re-reads that minute. Rows
   that commit late are reported (once) instead of being lost. A side
   effect: a row created and then updated more than a second later, between
   two polls, now also gives an UPDATE event (1.0 reported only the INSERT).
   Set `POLL_OVERLAP_SECONDS = 0` for the 1.0 cursor.
3. **Polls lock the tracked table's row** (`SELECT ... FOR NO KEY UPDATE`;
   PostgreSQL only, SQLite takes no row lock) and re-read the cursor under
   the lock. A second poller of the same table
   waits instead of queueing the same change again. Delivery is unchanged:
   still run one delivery worker per database.
4. **Re-reads are recognised by the row timestamp**, independently of
   `DEDUP_WINDOW_SECONDS`. The window itself still applies with its 1.0
   default of 300 s. Tracked tables can override it
   (`dedup_window_seconds`).
5. **DELETE rules are skipped by the poller**, and `poll_all_tables()` only
   polls insert and update rules. Tracked tables in 1.0 had only those two.
6. **`last_polled_at` trails the poll time** by up to
   `POLL_OVERLAP_SECONDS`. Do not use it as "the time of the last poll".
7. **A row that cannot be queued holds back its table's cursor.** The poll
   logs at `ERROR` level `N change(s) of <app_label>.<model> could not be
   queued; the cursor stays at <timestamp> and the next poll retries them`
   (for example `shop.order`, the lowercase content-type name) on every run,
   until the cause is fixed or the tracked table is deactivated.
8. **Create, then update more than a second later, between two polls** now
   gives an INSERT *and* an UPDATE event (see item 2).

**Downgrading** to 1.0 needs no schema change: 1.0 ignores the new
nullable columns. To drop them, run `migrate webhook_outbox 0001_initial`
*before* installing 1.0. Reversing `0002` drops the columns with their data:
`event_id`, `source_changed_at`, `changes`, `extra` and
`dedup_window_seconds`. Queued entries keep working, but lose their event
ids, and per-rule dedup windows are gone. A cursor left by 1.1 can
be up to `POLL_OVERLAP_SECONDS` behind. 1.0 re-reads that window, and its
dedup window drops the repeats as long as `DEDUP_WINDOW_SECONDS` is larger
than the overlap. DELETE rules survive the reversal of `0002`: delete or
deactivate them first. 1.0 logs an `ERROR` for each of them on every poll,
and their admin forms fail to save.

`TablePoller.poll()` still detects changes and moves the cursor at once,
for code that called it directly. `poll_table()` no longer uses it. Call
`poll_table()` to get the 1.1 guarantees. `poll()` now applies the overlap
too: it moves the cursor to `max(cursor, now - POLL_OVERLAP_SECONDS)`, so
consecutive calls return the rows inside the overlap window again. It does
not de-duplicate. Code that calls it and writes its own outbox entries must
de-duplicate them itself (for example by `source_pk` and the row timestamp),
or set `POLL_OVERLAP_SECONDS = 0`. That setting is global: it also brings
back the 1.0 late-commit loss for `poll_table()`.

## Migrating from the original in-house `notifications` app

1.0.0 is a 1:1 port of the outgoing webhook pipeline of an in-house
`notifications` app (models `NotificationChannel`, `TrackedTable`,
`NotificationOutbox`, Celery tasks in `notifications.tasks`). What changes
when you switch to the package:

| Before | After |
| --- | --- |
| app label `notifications`, tables `notifications_*` | app label `webhook_outbox`, tables `webhook_outbox_*` |
| `utils.models.TimeStampedModel` | `webhook_outbox.models.TimeStampedModel` (same fields and verbose names) |
| `NOTIFICATION_MAX_RETRIES`, `NOTIFICATION_WEBHOOK_TIMEOUT`, `SITE_URL` | still read as fallbacks; prefer `WEBHOOK_OUTBOX[...]` |
| `NOTIFICATION_POLL_INTERVAL`, `NOTIFICATION_SEND_INTERVAL` (beat schedule) | `WEBHOOK_OUTBOX["POLL_INTERVAL"/"SEND_INTERVAL"]`, `beat_schedule(...)` |
| task `notifications.tasks.poll_tracked_tables` | `webhook_outbox.poll_tracked_tables` |
| task `notifications.tasks.process_outbox` | `webhook_outbox.process_outbox` |
| task `notifications.tasks.send_single_notification` | `webhook_outbox.send_single_notification` |
| `notifications.services.poller` / `.template` / `.channels` | `webhook_outbox.services.poller` / `.template` / `.channels` |
| `notifications.tasks.send_notification` | `webhook_outbox.services.delivery.send_notification` |
| `CHANNEL_REGISTRY` dict | `WEBHOOK_OUTBOX["CHANNELS"]`, `get_channel_registry()` |

The envelope, `source_model` values, detection rules, retry rules, admin
actions and their messages are the same, so receivers need no changes.

### Behaviour differences

These are deliberate and small:

1. **`send_single_notification` keeps failed attempts.** In the original
   task, a failed attempt that was not the last one raised `self.retry()`
   inside the `transaction.atomic()` block, which rolled back the
   `retry_count` / `error_message` update. The delivery code now lives in a
   plain function that commits the attempt first; the Celery task then asks
   Celery to retry. The entry's `retry_count` therefore moves on every
   attempt, as it already did in `process_outbox`.

   **The retry budget changes with it.** In the original, attempts made by
   the Celery single-send task did not count against `MAX_RETRIES`: an entry
   retried from the admin could get up to 4 task attempts plus 3 delivery
   pass attempts. Now all attempts share one budget of `MAX_RETRIES` (3 by
   default), so such an entry becomes *failed* sooner.
2. **The admin "Retry" action no longer needs Celery.** It resets the
   entries as before. The immediate push (`send_single_notification.delay`)
   happens only when `ADMIN_RETRY_HOOK` is set. Without the hook, the next
   delivery pass sends the entries, within `SEND_INTERVAL`. If the hook
   raises (for example, the broker is down), the action logs the error and
   shows a warning instead of failing with a 500.
3. **Self-tracking guard.** The admin no longer offers the package's own
   models as trackable (the original offered its own `NotificationOutbox`),
   honours `TRACKABLE_EXCLUDE_APPS`, and rejects a posted content type that
   is not in the list. The original only filtered the dropdown.
4. **Channel types in the admin come from the registry**, so custom
   channel classes can be selected.
5. Cosmetic: the app's verbose name is *Webhook outbox*; help texts and
   examples use generic names (`shop.order`, `example.com`).

### Moving the data

The tables have new names, but the columns are the same. Plan the switch so
that the old and the new poller never run at the same time:

1. Install the package and add `"webhook_outbox"` to `INSTALLED_APPS`. Keep
   the old app installed for now.
2. Stop the old pipeline: remove its beat entries and let the running
   tasks finish.
3. Add a data migration in one of your apps that copies the rows
   (PostgreSQL shown):

   ```python
   from django.db import migrations

   COPY = """
   INSERT INTO webhook_outbox_notificationchannel
       (id, created_at, updated_at, name, channel_type, config, is_active)
   SELECT id, created_at, updated_at, name, channel_type, config, is_active
   FROM notifications_notificationchannel;

   INSERT INTO webhook_outbox_trackedtable
       (id, created_at, updated_at, name, content_type_id, event_type,
        message_template, channel_id, last_polled_at, is_active)
   SELECT id, created_at, updated_at, name, content_type_id, event_type,
          message_template, channel_id, last_polled_at, is_active
   FROM notifications_trackedtable;

   INSERT INTO webhook_outbox_notificationoutbox
       (id, created_at, updated_at, tracked_table_id, source_model, source_pk,
        event_type, message, status, error_message, sent_at, retry_count)
   SELECT id, created_at, updated_at, tracked_table_id, source_model, source_pk,
          event_type, message, status, error_message, sent_at, retry_count
   FROM notifications_notificationoutbox;
   """

   RESET_SEQUENCES = "".join(
       f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
       f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table};"
       for table in (
           "webhook_outbox_notificationchannel",
           "webhook_outbox_trackedtable",
           "webhook_outbox_notificationoutbox",
       )
   )


   class Migration(migrations.Migration):
       dependencies = [
           ("webhook_outbox", "0001_initial"),
           ("notifications", "0005_switch_channels_to_webhook"),
       ]
       operations = [
           migrations.RunSQL(COPY + RESET_SEQUENCES, migrations.RunSQL.noop),
       ]
   ```

   Because `last_polled_at` is copied, the new poller continues where the
   old one stopped: changes made during the switch are not lost (within the
   [limitations](semantics.md#known-limitations)). If you would rather start
   fresh, skip the copy and re-create the channels and tracked tables in the
   admin. Their first poll then starts from "now", without backfill.
4. Start the new runner ([runners.md](runners.md)). Purge any queued Celery
   messages that still use the old task names.
5. **Grant the admin permissions again.** Permissions and groups are keyed
   by content type, so `notifications.change_trackedtable` and the like do
   not carry over to `webhook_outbox.*`. Until you grant the new
   `webhook_outbox.*` permissions to the staff users and groups, they lose
   access to the admin pages.
6. **Rename Celery task routes and annotations.** If `CELERY_TASK_ROUTES`,
   `task_annotations` or similar settings mention the old
   `notifications.tasks.*` names, change them to the new
   `webhook_outbox.*` task names (see the table above).
7. Once everything works, remove the old models in a later release.

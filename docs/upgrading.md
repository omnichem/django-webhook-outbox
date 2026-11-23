# Upgrade notes

## Versioning policy

- Versions follow semantic versioning and are taken from git tags (`X.Y.Z`).
- Within 1.x the envelope (v1), the model schema and the documented
  semantics stay the same. New migrations will only add things.
- Fixes for the [known limitations](semantics.md#known-limitations) change
  observable behaviour. They will be opt-in or come with a major version,
  and the changelog will say so.

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

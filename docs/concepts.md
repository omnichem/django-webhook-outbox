# Concepts

The app has three models (in the admin under *Webhook outbox*) and two
periodic jobs.

## Models

**NotificationChannel** is where notifications go: a `channel_type`
(`webhook` built in) and a JSON `config`. For a webhook the config holds the
URL, optional headers and an optional timeout ([channels.md](channels.md)).
You can deactivate a channel with `is_active`.

**TrackedTable** is a tracking rule. It has:

- `content_type`: the model to watch. It must have `created_at`, and also
  `updated_at` for update events.
- `event_type`: `insert` or `update`.
- `message_template`: text with `{field}` / `{relation.field}` placeholders
  and the special `{admin_link}`.
- `channel`: where the notifications go.
- `last_polled_at`: the poll cursor. It is maintained by the poller; don't
  edit it.

Each combination of (model, event type, channel) can exist only once.

**NotificationOutbox** is one notification waiting to be sent, or already
sent. It holds the rendered `message`, the source (`source_model`,
`source_pk`, `event_type`), the `status` (`pending`, `sent` or `failed`),
`retry_count`, the last `error_message` and `sent_at`.

## Jobs

```
  your models                  webhook_outbox                         receiver
 ─────────────            ─────────────────────────               ──────────────
  row created  ──poll──►  TrackedTable cursor
  row updated             │ detect rows by created_at/updated_at
                          │ render message_template
                          ▼
                          NotificationOutbox (pending)
                          │
                          └──deliver──► channel.send(envelope) ──POST JSON──► 2xx?
                                         │ yes: status=sent
                                         │ no:  retry_count+1, failed at MAX_RETRIES
```

**Poll** (`poll_all_tables`, every `POLL_INTERVAL` = 30 s by default). Each
active tracked table whose channel is also active is polled on its own:

1. The first poll only sets `last_polled_at = now` and returns. Rows that
   existed before are not backfilled.
2. Later polls look for rows with `created_at > last_polled_at` (insert
   rules) or with `updated_at > last_polled_at` and
   `created_at < last_polled_at` (update rules).
3. The cursor moves to the time the poll started.
4. For every row found, the template is rendered and a pending outbox entry
   is created. The entry is skipped if the same (tracked table, row, event
   type) was queued within the last `DEDUP_WINDOW_SECONDS` (300 s).

**Deliver** (`process_outbox`, every `SEND_INTERVAL` = 10 s). Takes up to
`DELIVERY_BATCH_SIZE` (100) pending entries with `retry_count < MAX_RETRIES`,
oldest first, and sends each one through its channel. A success marks the
entry *sent*. A failure increments `retry_count` and stores the error; when
`retry_count` reaches `MAX_RETRIES` (3) the entry becomes *failed*.

In the admin you can reset failed entries to pending (**Retry selected
failed notifications**) or close entries without sending them (**Mark
selected as sent**).

The exact rules, and what they cannot guarantee, are in
[semantics.md](semantics.md).

## Where the code lives

| What | Module |
| --- | --- |
| Poller | `webhook_outbox.services.poller` (`TablePoller`, `poll_all_tables`, `poll_table`) |
| Template renderer | `webhook_outbox.services.template` |
| Envelope, channels, dispatcher | `webhook_outbox.services.channels` |
| Delivery (status transitions) | `webhook_outbox.services.delivery` |
| Runners | `webhook_outbox.runner` (`poll_once`, `deliver_once`, `deliver_entry`, `run_forever`) |
| Commands | `webhook_outbox_worker`, `webhook_outbox_poll`, `webhook_outbox_deliver` |
| Celery tasks (optional) | `webhook_outbox.contrib.celery.tasks` |

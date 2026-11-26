# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [semantic versioning](https://semver.org/). Versions come from git tags.

## [Unreleased]

## [1.2.1] - 2026-11-26

Run `migrate webhook_outbox` after upgrading (the new migration only records
the changed help text; the database schema does not change).

### Changed

- The *Filter* field help text of a tracked table (`TrackedTable.predicate`)
  now ends with two example filters: `status in ("paid", "shipped")` and
  `status = "paid" and customer.country is not null`. Migration
  `0004_predicate_help_text_examples` records the new help text. Russian
  translation updated.

## [1.2.0] - 2026-11-26

Run `migrate webhook_outbox` after upgrading; see
[docs/upgrading.md](docs/upgrading.md#upgrading-from-11-to-12).

### Added

- **Tracked table filters** (`TrackedTable.predicate`, *Filter* in the
  admin). A boolean expression over the tracked model's fields, e.g.
  `status = "paid" and company.country in ("DE", "AT")`: only matching rows
  produce events. Comparisons `= != < <= > >=`, `in`, `not in`, `is null`,
  `is not null`; `and`, `or`, `not`, parentheses; string, number, `true` /
  `false` literals; forward foreign keys up to 3 names. Polling applies it
  in the detection query, signal capture and `enqueue()` check the instance
  in memory, with the same result. See
  [docs/semantics.md](docs/semantics.md#filters).
- The filter is parsed by the package and compiled to a Django `Q`: no SQL,
  no `eval`, no lookups from the text (`__` refused), typed literals, size
  limits, no access to `django.*` models, and the new setting
  `PREDICATE_DENY_FIELDS` (default `*password*`, `*token*`, `*secret*`,
  `*api_key*`). See [docs/security.md](docs/security.md#filters).
- `webhook_outbox.services.predicate`: `compile_predicate()`, `Predicate`
  (`as_q()`, `matches()`), `PredicateError`; `TrackedTable.get_predicate()`.
- System checks `webhook_outbox.E009` (`PREDICATE_DENY_FIELDS` is not a
  list of strings) and `webhook_outbox.W014` (a stored filter is invalid).
- Migration `0003_1_2_predicate`: the `predicate` column (`''` for existing
  rules).
- Russian translations of the new admin texts and filter errors.

### Changed

- **Several tracked tables per (model, event type, channel)** are allowed:
  migration `0003_1_2_predicate` drops that unique constraint. `enqueue()`
  without `tracked_table=` raises `TrackedTableLookupError` when several
  active rules match, as it already did across channels.
- A tracked table with an invalid filter is skipped by the poller without
  moving its cursor, and logged on every poll.
- `enqueue()` returns `None` for a row that does not match the filter, and
  raises `PredicateError` (a `ValueError`) for an invalid filter.

## [1.1.0] - 2026-11-24

Run `migrate webhook_outbox` after upgrading; see
[docs/upgrading.md](docs/upgrading.md#upgrading-from-10-to-11).

### Known limitations

- **Late commits are caught only up to `POLL_OVERLAP_SECONDS`** (default
  `60`). A row whose transaction commits more than that many seconds after
  its `created_at` / `updated_at` can still be missed, silently: no entry and
  no log line ([#11](https://github.com/omnichem/django-webhook-outbox/issues/11)).
  Workarounds: write those events with `webhook_outbox.enqueue()` or
  `SIGNAL_CAPTURE` (both queue the entry in the same transaction), or raise
  `POLL_OVERLAP_SECONDS`.
- **Delivery is unchanged**: no row locking and no backoff
  ([#6](https://github.com/omnichem/django-webhook-outbox/issues/6)). Run
  exactly one delivery worker per database.
- Django 6 is not supported yet (`Django>=4.2,<6`).

### Changed (behaviour, on by default: fixes lost events, envelope unchanged)

- **The poller no longer loses events on errors** (#2). Detection, the
  outbox inserts and the cursor update run in one transaction that locks
  the tracked table's row (`SELECT ... FOR NO KEY UPDATE`; PostgreSQL only,
  SQLite takes no row lock) and re-reads the cursor.
  If any detected change cannot be queued, the cursor does not move and the
  next poll retries the window. The other rows are still queued, once. A
  row that can never be queued now holds its tracked table's cursor and is
  logged on every poll.
- **Late commits are caught** (#2). New setting `POLL_OVERLAP_SECONDS`,
  default `60`: the cursor moves to `max(cursor, poll start - 60 s)`, so rows
  whose transaction commits up to a minute after their timestamp are still
  reported. `last_polled_at` therefore trails the last poll by up to the
  overlap. Set it to `0` for the 1.0 cursor.
- With an overlap, an UPDATE rule also reports a row created after the
  cursor once its `updated_at` is more than one second after its
  `created_at`. A row created and then updated between two polls can
  therefore now give an INSERT *and* an UPDATE event (1.0: INSERT only).
- Re-read rows are recognised exactly by the source row timestamp (new
  column `source_changed_at`), independently of the dedup window. Two
  pollers of one tracked table no longer queue the same change twice.
- `poll_all_tables()` / `poll_table()` skip DELETE rules.
- `TablePoller.poll()` (kept for direct callers) also applies
  `POLL_OVERLAP_SECONDS` now and does not de-duplicate, so it can return rows
  again; see [docs/upgrading.md](docs/upgrading.md#upgrading-from-10-to-11).

### Added

- Migration `0002_1_1_event_fields`: nullable columns only (#3).
  `NotificationOutbox.event_id` / `source_changed_at` / `changes` / `extra`,
  and `TrackedTable.dedup_window_seconds`. The `delete` event type choice is
  choices-only. Rows written by 1.0 keep `NULL`.
- **Envelope v2** (#3), opt-in with `WEBHOOK_OUTBOX["ENVELOPE_VERSION"] = 2`
  or `"envelope_version": 2` in a webhook channel config:
  `{schema_version, event_id, event_type, occurred_at, source: {model, pk},
  tracked_table, message, changes, extra}`. Its JSON schema is published in
  [docs/envelope.md](docs/envelope.md). **v1 stays the default, and with
  default settings it is byte-for-byte the 1.0 document.**
- **`event_id`** (#3): a UUID stored on every outbox entry, so retries send
  the same id. Entries from 1.0 get one, stored, on their first delivery
  attempt. Always in v2; in v1 only with `ENVELOPE_V1_EVENT_ID = True`.
- **`webhook_outbox.enqueue()`** (#4): queues an event in the caller's
  transaction, so it commits or rolls back with the business change. It
  routes through a tracked table (`tracked_table=`, or the single active
  one for model, event type and optional `channel`; otherwise
  `TrackedTableLookupError`). It takes `message`, and `changes` / `extra`
  for v2 channels. The dedup window does not apply. Insert/update events
  record the row timestamp, so polling does not queue them again. New
  setting `ENQUEUE_HOOK`: a `callable(outbox_id)` run on commit.
- **Signal capture** (#4), off by default: `SIGNAL_CAPTURE` lists the models
  whose `post_save` / `post_delete` queue `insert` / `update` / `delete`
  events, optionally with old/new values of chosen `"fields"`. It is
  best-effort: failures are logged and never break the save.
- `delete` event type for tracked tables. DELETE rules are fed by signal
  capture and `enqueue()`, never polled.
- Per-tracked-table `dedup_window_seconds` (`0` turns the window off).
- System checks E007 (envelope settings) and E008 (`SIGNAL_CAPTURE`). E005
  also covers `ENQUEUE_HOOK`, E006 covers `POLL_OVERLAP_SECONDS`, and W012
  ignores DELETE rules.
- Docs: a "which capture method to use" table and the blind spots of
  polling, signals and `enqueue()` in [docs/semantics.md](docs/semantics.md).
- Admin shows the new fields; Russian translation updated.

## [1.0.0] - 2026-11-23

First public release. A 1:1 behavioural port of the outgoing webhook
pipeline of an in-house Django `notifications` app.

### Added

- Models `NotificationChannel`, `TrackedTable` and `NotificationOutbox`
  (app label `webhook_outbox`), with field names, verbose names and
  constraints unchanged, and the abstract `TimeStampedModel`. A single
  `0001_initial` migration equals the original final schema.
- Timestamp poller (`TablePoller`, `poll_all_tables`, `poll_table`) and the
  `{dotted.path}` / `{admin_link}` template renderer, ported unchanged.
- Envelope v1 (`event`, `source_model`, `source_pk`, `tracked_table`,
  `message`, `occurred_at`), `BaseChannel`, `WebhookChannel` (httpx) and a
  settings-driven channel registry.
- Delivery as plain functions (`process_outbox`, `send_single_notification`).
  Runners `poll_once`, `deliver_once`, `deliver_entry` and `run_forever`,
  with graceful SIGTERM/SIGINT handling. Management commands
  `webhook_outbox_worker`, `webhook_outbox_poll` and `webhook_outbox_deliver`.
- Optional Celery integration, `webhook_outbox.contrib.celery` (tasks and
  `beat_schedule()`).
- One `WEBHOOK_OUTBOX` setting dict, with fallbacks to
  `NOTIFICATION_MAX_RETRIES`, `NOTIFICATION_WEBHOOK_TIMEOUT` and `SITE_URL`.
- Admin without Celery: `retry_failed` with an optional `ADMIN_RETRY_HOOK`,
  `mark_as_sent`, and a self-tracking guard for the trackable models.
- System checks for settings, unknown channel types, tracked models without
  timestamps and self-tracking.
- Russian translation.

### Behaviour differences from the original code

See [docs/upgrading.md](docs/upgrading.md#behaviour-differences). The known
limitations of the original code are kept on purpose and documented in
[docs/semantics.md](docs/semantics.md#known-limitations).

[Unreleased]: https://github.com/omnichem/django-webhook-outbox/compare/1.2.1...HEAD
[1.2.1]: https://github.com/omnichem/django-webhook-outbox/compare/1.2.0...1.2.1
[1.2.0]: https://github.com/omnichem/django-webhook-outbox/compare/1.1.0...1.2.0
[1.1.0]: https://github.com/omnichem/django-webhook-outbox/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/omnichem/django-webhook-outbox/releases/tag/1.0.0

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [semantic versioning](https://semver.org/). Versions come from git tags.

## [Unreleased]

## [1.0.0] - unreleased (published when the `1.0.0` tag is pushed)

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

[Unreleased]: https://github.com/omnichem/django-webhook-outbox/compare/1.0.0...HEAD
[1.0.0]: https://github.com/omnichem/django-webhook-outbox/releases/tag/1.0.0

# django-webhook-outbox

Outgoing webhooks for Django models, configured in the Django admin.

You pick a model and an event (`insert` or `update`) in the admin and write
a message template. A poller finds new and changed rows by their
`created_at` / `updated_at` timestamps and writes them to an outbox table.
A delivery pass then POSTs each outbox entry as JSON to the channel's URL and
retries failures.

- No database triggers and no signals: any model with `created_at` /
  `updated_at` fields can be tracked, including models of third-party apps.
- Delivery goes through an outbox table: pending, sent and failed entries are
  visible in the admin, and failed ones can be retried from there.
- Celery is optional. The core runs from a management command
  (`webhook_outbox_worker`), cron, or any scheduler; `webhook_outbox.contrib.celery`
  provides ready-made tasks if you already use Celery.
- English and Russian admin.

The package was extracted from a production Django service. Release 1.0.0
keeps that code's behaviour exactly, known limitations included. They are
listed in [docs/semantics.md](docs/semantics.md); read that page before you
rely on the package for anything that must not lose or duplicate events.

## Requirements

- Python 3.10+
- Django 4.2, 5.0, 5.1 or 5.2 (CI runs 4.2, 5.0 and 5.2 on PostgreSQL)
- httpx 0.27+
- Celery 5.3+ (optional, `[celery]` extra)

## Installation

```console
pip install django-webhook-outbox
# or, with the Celery tasks:
pip install "django-webhook-outbox[celery]"
```

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "webhook_outbox",
]

WEBHOOK_OUTBOX = {
    "SITE_URL": "https://shop.example.com",  # used by {admin_link}
}
```

```console
python manage.py migrate webhook_outbox
```

## Quick start

1. Make sure the model you want to track has `created_at` (insert events)
   and `updated_at` (update events) fields. The easiest way is to inherit the
   bundled base class:

   ```python
   from webhook_outbox.models import TimeStampedModel

   class Order(TimeStampedModel):
       number = models.CharField(max_length=20)
       customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
   ```

2. In the admin, add a **Notification Channel**, type *Webhook*, with this
   config:

   ```json
   {"url": "https://receiver.example.com/hooks/orders",
    "headers": {"Authorization": "Bearer <token>"},
    "timeout": 10}
   ```

3. Add a **Tracked Table**: model *orders*, event *Insert*, the channel from
   step 2, and a message template:

   ```text
   New order {number} from {customer.name}
   {admin_link}
   ```

4. Run a worker (see below). The first poll of a tracked table only stores
   its starting point and does not backfill. Orders created after that are
   POSTed to the receiver:

   ```json
   {
     "event": "insert",
     "source_model": "shop.order",
     "source_pk": "1042",
     "tracked_table": "New orders",
     "message": "New order A-1042 from Acme Corp\nhttps://shop.example.com/admin/shop/order/1042/change/",
     "occurred_at": "2026-08-03T10:00:00.123456+00:00"
   }
   ```

   A 2xx response marks the entry *sent*. Any other response, a timeout or
   a connection error counts as a failed attempt. After `MAX_RETRIES` (3)
   failed attempts the entry becomes *failed*.

### Running without Celery

```console
python manage.py webhook_outbox_worker          # poll every 30s, deliver every 10s
python manage.py webhook_outbox_worker --once   # one pass, e.g. from cron
```

Run **one** worker process per database (see
[limitations](docs/semantics.md#known-limitations)). It stops cleanly on
SIGTERM/SIGINT. Systemd, Kubernetes and cron examples are in
[docs/runners.md](docs/runners.md).

### Running with Celery

```python
# settings.py
from webhook_outbox.contrib.celery.schedule import beat_schedule

INSTALLED_APPS += ["webhook_outbox.contrib.celery"]

CELERY_BEAT_SCHEDULE = {
    **beat_schedule(),  # poll every 30s, deliver every 10s
    # ... your other entries
}

WEBHOOK_OUTBOX = {
    # "Retry" in the admin pushes the entry to Celery immediately
    "ADMIN_RETRY_HOOK": "webhook_outbox.contrib.celery.tasks.enqueue_single_notification",
}
```

## Documentation

- [Concepts](docs/concepts.md): channels, tracked tables, the outbox, the data flow
- [Semantics and known limitations](docs/semantics.md)
- [Settings reference](docs/settings.md)
- [Envelope v1](docs/envelope.md): the JSON document receivers get
- [Channels](docs/channels.md): webhook config, writing your own channel
- [Security notes](docs/security.md)
- [Runners](docs/runners.md): worker loop, cron, Celery
- [Adopting the app in a service without Celery](docs/without-celery.md)
- [Upgrade notes](docs/upgrading.md), including migrating from the original in-house app
- [Changelog](CHANGELOG.md), [Contributing](CONTRIBUTING.md)

## License

MIT, see [LICENSE](LICENSE).

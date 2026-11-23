# Adopting the app in a service without Celery

The core of the package has no task queue dependency. A service with no
Celery, Redis or RabbitMQ needs only the database and one extra process.

1. **Install**

   ```console
   pip install django-webhook-outbox
   ```

   ```python
   INSTALLED_APPS += ["webhook_outbox"]
   WEBHOOK_OUTBOX = {
       "SITE_URL": "https://service.example.com",
   }
   ```

   ```console
   python manage.py migrate webhook_outbox
   python manage.py check --database default
   ```

2. **Make your models trackable.** Give them `created_at` / `updated_at`,
   for example by inheriting `webhook_outbox.models.TimeStampedModel`. Add
   database indexes on those columns for large tables.

3. **Configure** a channel and a tracked table in the admin
   ([README](../README.md#quick-start)).

4. **Run the worker** next to your web process, as a single instance:

   ```console
   python manage.py webhook_outbox_worker
   ```

   Procfile:

   ```procfile
   web: gunicorn service.wsgi
   webhook-outbox: python manage.py webhook_outbox_worker
   ```

   If you can't run an extra long-lived process, use cron with
   `webhook_outbox_worker --once` every minute ([runners.md](runners.md#cron)).

5. **Retries from the admin.** Without `ADMIN_RETRY_HOOK`, retried entries
   are picked up by the worker's next delivery pass (within `SEND_INTERVAL`,
   10 s). To send them immediately in the admin request instead, set
   `"ADMIN_RETRY_HOOK": "webhook_outbox.runner.deliver_entry"`. The admin
   request then waits for the receiver, up to the channel timeout per entry.

6. **Monitor** the outbox. Entries with status *failed* need attention.
   For an alert, a query like
   `NotificationOutbox.objects.filter(status="failed", updated_at__gte=...)`
   is enough. The worker logs to the `webhook_outbox.*` loggers.

7. **Tests in your project.** The delivery code is plain Python. In tests,
   call `webhook_outbox.runner.poll_once()` / `deliver_once()` directly and
   replace the HTTP layer, for example by patching
   `webhook_outbox.services.channels.webhook.httpx.Client` with a client
   built on `httpx.MockTransport`. See `tests/conftest.py` in this
   repository for a fixture you can copy.

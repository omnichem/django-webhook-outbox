# Runners

The package does two periodic jobs: **poll** (find changes, write outbox
entries) and **deliver** (send pending entries). Choose one way to run them.

> Run **one** poller and **one** delivery worker per database. Neither job
> locks the rows it works on (see [semantics.md](semantics.md)), so two
> concurrent workers can queue or send duplicates.

## Plain worker loop (no Celery)

```console
python manage.py webhook_outbox_worker [--only poll|deliver] [--poll-interval S] [--send-interval S]
python manage.py webhook_outbox_worker --once [--only poll|deliver]
```

- Polls every `POLL_INTERVAL` (30 s) and delivers every `SEND_INTERVAL`
  (10 s), both starting immediately.
- SIGTERM or SIGINT stops the loop after the step in progress finishes.
  Nothing is cut off in the middle of an HTTP request.
- An exception in a step is logged and the loop keeps going. Database
  connections are checked between steps (`close_old_connections`), so a
  restarted database or `CONN_MAX_AGE` does not kill the worker.
- `--once` runs one poll and/or one delivery pass, prints a summary and
  exits.

From Python:

```python
import threading
from webhook_outbox import runner

runner.poll_once()                 # -> number of entries queued
runner.deliver_once(limit=None)    # -> {"sent": n, "failed": n}
runner.deliver_entry(pk)           # -> {"success": bool, "error": str} / {"status": "already_sent"} / {"error": "Entry not found"}

stop = threading.Event()
runner.run_forever(poll_interval=30, send_interval=10, stop_event=stop)
```

Signal handlers are only installed when `run_forever` runs in the main
thread (pass `install_signal_handlers=False` to skip them). The previous
handlers are restored when it returns.

### systemd

```ini
[Unit]
Description=webhook outbox worker
After=network-online.target

[Service]
WorkingDirectory=/srv/shop
ExecStart=/srv/shop/.venv/bin/python manage.py webhook_outbox_worker
Restart=always
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

Set `TimeoutStopSec` above your largest channel timeout, so that a delivery
in progress can finish.

### Kubernetes / docker compose

Run it as a separate deployment with **`replicas: 1`** and the `Recreate`
strategy (not `RollingUpdate`, which runs two pods for a moment):

```yaml
spec:
  replicas: 1
  strategy: {type: Recreate}
  template:
    spec:
      terminationGracePeriodSeconds: 60
      containers:
        - name: webhook-outbox
          image: shop:latest
          command: ["python", "manage.py", "webhook_outbox_worker"]
```

### Cron

If a resolution of one minute is enough:

```cron
* * * * *  cd /srv/shop && .venv/bin/python manage.py webhook_outbox_worker --once
```

Use `flock` (or a similar lock) so that a slow run does not overlap with the
next one:
`flock -n /tmp/webhook_outbox.lock python manage.py webhook_outbox_worker --once`.

The single-step commands are also available: `webhook_outbox_poll`, and
`webhook_outbox_deliver [--limit N] [--entry PK]`.

## Celery

```console
pip install "django-webhook-outbox[celery]"
```

```python
# settings.py
from webhook_outbox.contrib.celery.schedule import beat_schedule

INSTALLED_APPS += ["webhook_outbox.contrib.celery"]   # lets autodiscover_tasks() find the tasks

CELERY_BEAT_SCHEDULE = {
    **beat_schedule(),            # or beat_schedule(poll_interval=30, send_interval=10)
}

WEBHOOK_OUTBOX = {
    "ADMIN_RETRY_HOOK": "webhook_outbox.contrib.celery.tasks.enqueue_single_notification",
}
```

| Task name | Function | Notes |
| --- | --- | --- |
| `webhook_outbox.poll_tracked_tables` | `runner.poll_once()` | Returns `{"queued": n}`. Retried by Celery on error (3 times, 60 s apart). |
| `webhook_outbox.process_outbox` | `runner.deliver_once()` | Returns `{"sent": n, "failed": n}`. |
| `webhook_outbox.send_single_notification(outbox_id)` | one entry | Retried by Celery (3 times, 10 s apart) while the entry stays pending after a failed attempt. |

`webhook_outbox.contrib.celery` can only be imported when Celery is
installed. The core package never imports Celery.

The rule of one worker per database applies here too: keep a single beat
scheduler, and avoid queue setups where one poll or delivery task can still
be running when the next one starts (for example, route these tasks to a
queue with concurrency 1). Beat itself does not prevent overlap.

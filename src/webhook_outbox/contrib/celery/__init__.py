"""
Optional Celery integration.

Add ``"webhook_outbox.contrib.celery"`` to INSTALLED_APPS (so Celery's
``autodiscover_tasks()`` finds ``webhook_outbox.contrib.celery.tasks``) and
schedule the tasks with Celery beat, e.g. via ``beat_schedule()``.

Requires the ``celery`` extra: ``pip install "django-webhook-outbox[celery]"``.
"""

try:
    import celery  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised without celery only
    raise ImportError(
        "webhook_outbox.contrib.celery requires Celery. "
        'Install it with: pip install "django-webhook-outbox[celery]"'
    ) from exc

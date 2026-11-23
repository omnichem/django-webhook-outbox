from django.apps import AppConfig


class WebhookOutboxCeleryConfig(AppConfig):
    name = "webhook_outbox.contrib.celery"
    label = "webhook_outbox_celery"
    verbose_name = "Webhook outbox (Celery)"

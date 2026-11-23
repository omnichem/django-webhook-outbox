from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class WebhookOutboxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "webhook_outbox"
    label = "webhook_outbox"
    verbose_name = _("Webhook outbox")

    def ready(self):
        from django.core.signals import setting_changed

        from . import capture, checks  # noqa: F401  (registers the system checks)

        capture.connect()
        setting_changed.connect(
            capture.on_setting_changed,
            dispatch_uid="webhook_outbox.capture.on_setting_changed",
        )

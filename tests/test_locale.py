from django.utils import translation

from webhook_outbox.apps import WebhookOutboxConfig
from webhook_outbox.models import NotificationChannel, NotificationOutbox, TrackedTable


def test_russian_translations():
    with translation.override("ru"):
        assert (
            str(NotificationOutbox._meta.verbose_name_plural) == "Исходящие уведомления"
        )
        assert str(TrackedTable._meta.verbose_name) == "Отслеживаемая таблица"
        assert (
            str(NotificationChannel._meta.get_field("name").verbose_name)
            == "Наименование"
        )
        assert (
            str(NotificationOutbox._meta.get_field("created_at").verbose_name)
            == "Создано"
        )
        assert str(NotificationOutbox.Status.FAILED.label) == "Ошибка"
        assert str(WebhookOutboxConfig.verbose_name) == "Исходящие вебхуки"
        assert (
            str(translation.gettext("Queued {} notification(s) for retry.")).format(2)
            == "Поставлено в очередь на повторную отправку: 2."
        )


def test_english_is_the_source_language():
    with translation.override("en"):
        assert (
            str(NotificationOutbox._meta.verbose_name_plural)
            == "Notification Outbox Entries"
        )

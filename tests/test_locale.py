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
        assert str(TrackedTable.EventType.DELETE.label) == "Удаление"
        assert (
            str(NotificationOutbox._meta.get_field("event_id").verbose_name)
            == "ID события"
        )
        assert (
            str(TrackedTable._meta.get_field("dedup_window_seconds").verbose_name)
            == "Окно дедупликации (секунды)"
        )
        assert str(TrackedTable._meta.get_field("predicate").verbose_name) == "Фильтр"
        from tests.testapp.models import RegistrationRequest
        from webhook_outbox.services.predicate import PredicateError, compile_predicate

        try:
            compile_predicate("status = 5", RegistrationRequest)
        except PredicateError as e:
            assert str(e) == "'status': ожидается строка в кавычках. (символ 10)"
        else:
            raise AssertionError("expected a PredicateError")
        # The event type label does not hijack the admin's "Delete" button.
        assert str(translation.gettext("Delete")) == "Удалить"
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

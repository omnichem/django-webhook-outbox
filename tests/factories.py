"""Small factories for the tests."""

from itertools import count

from django.contrib.contenttypes.models import ContentType

from tests.testapp.models import Company, OrganizationType, Person, RegistrationRequest
from webhook_outbox.models import NotificationChannel, NotificationOutbox, TrackedTable

_seq = count(1)

WEBHOOK_CONFIG = {"url": "https://example.com/hook"}


def create_channel(**fields) -> NotificationChannel:
    values = {
        "name": f"Channel {next(_seq)}",
        "channel_type": NotificationChannel.ChannelType.WEBHOOK,
        "config": dict(WEBHOOK_CONFIG),
    }
    values.update(fields)
    return NotificationChannel.objects.create(**values)


def create_tracked_table(model, event_type="insert", **fields) -> TrackedTable:
    values = {
        "name": f"Tracked {next(_seq)}",
        "content_type": ContentType.objects.get_for_model(model),
        "event_type": event_type,
        "message_template": "Test",
    }
    values.update(fields)
    if "channel" not in values:
        values["channel"] = create_channel()
    return TrackedTable.objects.create(**values)


def create_outbox_entry(tracked, **fields) -> NotificationOutbox:
    values = {
        "tracked_table": tracked,
        "source_model": tracked.model_path,
        "source_pk": "1",
        "event_type": tracked.event_type,
        "message": "Test",
    }
    values.update(fields)
    return NotificationOutbox.objects.create(**values)


def create_organization_type(name="LLC") -> OrganizationType:
    return OrganizationType.objects.create(name=name)


def create_company(name="Test Company", inn="1234567890", **fields) -> Company:
    values = {
        "company_name": name,
        "inn": inn,
        "company_type": fields.pop("company_type", None) or create_organization_type(),
        "address": "123 Test St",
    }
    values.update(fields)
    return Company.objects.create(**values)


def create_person(email="test@example.com") -> Person:
    return Person.objects.create(email=email)


def create_request(company=None, user=None, **fields) -> RegistrationRequest:
    return RegistrationRequest.objects.create(
        company=company or create_company(),
        user=user or create_person(),
        **fields,
    )

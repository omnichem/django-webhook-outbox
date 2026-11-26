"""Tracked table filters: TrackedTable.predicate (1.2)."""

import logging
import random
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core import checks
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from tests.factories import (
    create_channel,
    create_company,
    create_organization_type,
    create_person,
    create_request,
    create_tracked_table,
)
from tests.testapp.models import Company, RegistrationRequest
from webhook_outbox import enqueue
from webhook_outbox.models import NotificationOutbox, TrackedTable
from webhook_outbox.services.poller import poll_table
from webhook_outbox.services.predicate import (
    MAX_COMPARISONS,
    MAX_DEPTH,
    MAX_IN_ITEMS,
    MAX_LENGTH,
    BoolOp,
    Comparison,
    Not,
    PredicateError,
    compile_predicate,
    parse,
)


def compile_request(text):
    return compile_predicate(text, RegistrationRequest)


def error_of(text, model=RegistrationRequest) -> PredicateError:
    with pytest.raises(PredicateError) as info:
        compile_predicate(text, model)
    return info.value


# --- syntax ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_filter_is_no_filter(text):
    assert parse(text) is None
    assert compile_request(text) is None


def test_and_binds_tighter_than_or():
    tree = parse('status = "a" or status = "b" and processed = true')
    assert isinstance(tree, BoolOp) and tree.op == "or"
    assert isinstance(tree.items[1], BoolOp) and tree.items[1].op == "and"


def test_parentheses_and_not():
    tree = parse('not (status = "a" or status = "b")')
    assert isinstance(tree, Not)
    assert isinstance(tree.item, BoolOp) and tree.item.op == "or"


def test_keywords_are_case_insensitive():
    tree = parse('status IN ("a") AND NOT processed = TRUE OR company IS NOT NULL')
    assert tree.op == "or"
    assert tree.items[1] == Comparison("company", "is not null", (), 44)


@pytest.mark.parametrize(
    ("text", "op", "values"),
    [
        ('status = "new"', "=", ["new"]),
        ('status == "new"', "=", ["new"]),
        ("status != 'new'", "!=", ["new"]),
        ("id < 5", "<", ["5"]),
        ("id <= -5", "<=", ["-5"]),
        ("id > 1.25", ">", ["1.25"]),
        ("id >= 0", ">=", ["0"]),
        ('status in ("a", "b",)', "in", ["a", "b"]),
        ('status not in ("a")', "not in", ["a"]),
        ("company is null", "is null", []),
        ("company is not null", "is not null", []),
    ],
)
def test_comparisons(text, op, values):
    tree = parse(text)
    assert tree.op == op
    assert [literal.value for literal in tree.values] == values


def test_string_escapes():
    tree = parse(r'status = "a \"b\" \\ c\'"')
    assert tree.values[0].value == 'a "b" \\ c\''


@pytest.mark.parametrize(
    ("text", "message", "position"),
    [
        ('status = "open', "Unterminated string", 10),
        (r'status = "a\n"', "Invalid escape", 12),
        ("status = ", "Expected a value", 10),
        ('status "a"', "Expected an operator", 8),
        ('status = "a" status = "b"', "expected 'and', 'or' or the end", 14),
        ('(status = "a"', "Expected ')'", 14),
        ('status = "a")', "expected 'and', 'or' or the end", 13),
        ("status in ()", "Expected a value", 12),
        ('status in ("a" "b")', "Expected ',' or ')'", 16),
        ('status not = "a"', "Expected 'in' after 'not'", 12),
        ("status is 5", "Expected 'null' after 'is'", 11),
        ("status = null", "Use 'is null'", 10),
        ('and = "a"', "reserved word", 1),
        ('status = "a" and', "Expected a field name", 17),
        ('status ~ "a"', "Unexpected character '~'", 8),
        ('status = "a"; DROP TABLE x', "Unexpected character ';'", 13),
        ("_state = 1", "cannot start with '_'", 1),
        ("company._state = 1", "Invalid field name", 1),
        ("id = 5abc", "Invalid number", 6),
        ("id = 1.", "Invalid number", 6),
        ("id = 1e5", "Invalid number", 6),
    ],
)
def test_syntax_errors(text, message, position):
    error = error_of(text)
    assert message in error.message
    assert error.position == position - 1
    assert f"(at character {position})" in str(error)


def test_errors_are_value_errors():
    assert issubclass(PredicateError, ValueError)


# --- limits ---------------------------------------------------------------------


def test_length_limit():
    text = "status = " + '"' + "x" * MAX_LENGTH + '"'
    assert "too long" in error_of(text).message


def test_depth_limit():
    assert compile_request("(" * (MAX_DEPTH - 1) + "id = 1" + ")" * (MAX_DEPTH - 1))
    assert (
        "nested too deeply"
        in error_of("(" * MAX_DEPTH + "id = 1" + ")" * MAX_DEPTH).message
    )
    assert "nested too deeply" in error_of("not " * MAX_DEPTH + "id = 1").message


def test_comparison_limit():
    assert compile_request(" or ".join(["id = 1"] * MAX_COMPARISONS))
    text = " or ".join(["id = 1"] * (MAX_COMPARISONS + 1))
    assert "Too many comparisons" in error_of(text).message


def test_in_limit():
    assert compile_request(f"id in ({', '.join(['1'] * MAX_IN_ITEMS)})")
    text = f"id in ({', '.join(['1'] * (MAX_IN_ITEMS + 1))})"
    assert "Too many values" in error_of(text).message


def test_number_length_limit():
    assert "too long" in error_of("id = " + "9" * 31).message


# --- fields and types -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        # Django lookups cannot be smuggled in through the field name.
        ('status__regex = "^(a+)+$"', "'__' is not allowed"),
        ('company__company_name = "x"', "'__' is not allowed"),
        ('company.company_name__startswith = "x"', "'__' is not allowed"),
        ('nope = "x"', "has no field 'nope'"),
        ('company.nope = "x"', "has no field 'nope'"),
        ('status.value = "x"', "not a foreign key"),
        ('company.company_type.name.x = "x"', "at most 3 names"),
        # Reverse relations and many-to-many: several values per row.
        ("company.registrationrequest = 1", "only fields of the row and foreign keys"),
        # Ordering only on numbers (string collation differs between databases).
        ('status < "b"', "only works on numbers"),
        ("processed > true", "only works on numbers"),
        # Literals must fit the field.
        ("status = 5", "expects a string in quotes"),
        ('id = "5"', "expects a number"),
        ("id = 1.5", "expects a whole number"),
        ("processed = 1", "expects true or false"),
        ('processed = "true"', "expects true or false"),
        ("company = true", "expects a number"),
        ("created_at is null", "DateTimeField, which cannot be used"),
    ],
)
def test_invalid_fields_and_values(text, message):
    assert message in error_of(text).message


def test_foreign_key_compares_by_primary_key():
    predicate = compile_request("company = 5")
    assert str(predicate.as_q()) == "(AND: ('company', 5))"
    assert compile_request("company_id in (5, 6)")


def test_django_app_models_cannot_be_entered():
    # TrackedTable.content_type points to django.contrib.contenttypes.
    error = error_of('content_type.app_label = "auth"', TrackedTable)
    assert "models of django.* apps cannot be used" in error.message
    # Comparing the foreign key itself is fine.
    assert compile_predicate("content_type = 3", TrackedTable)


@pytest.mark.parametrize(
    "text",
    [
        'password = "x"',  # names are checked before the model lookup
        'user.api_token = "x"',
        'user.client_secret = "x"',
        'user.API_KEY = "x"',
    ],
)
def test_deny_list_by_default(text):
    assert "cannot be used in a filter" in error_of(text).message


@override_settings(WEBHOOK_OUTBOX={"PREDICATE_DENY_FIELDS": ["email", "*_number"]})
def test_deny_list_is_configurable():
    assert "cannot be used" in error_of('user.email = "a@example.com"').message
    assert "cannot be used" in error_of('company.registration_number = "1"').message
    assert compile_request('company.company_name = "x"')


def test_other_types():
    retry = compile_predicate("retry_count >= 2", NotificationOutbox)
    assert retry.matches(NotificationOutbox(retry_count=2))
    assert not retry.matches(NotificationOutbox(retry_count=1))

    event_id = uuid.uuid4()
    by_uuid = compile_predicate(f'event_id = "{event_id}"', NotificationOutbox)
    assert by_uuid.matches(NotificationOutbox(event_id=event_id))
    assert by_uuid.matches(NotificationOutbox(event_id=str(event_id)))
    assert not by_uuid.matches(NotificationOutbox(event_id=uuid.uuid4()))
    assert "expects a UUID" in error_of('event_id = "nope"', NotificationOutbox).message


# --- database and Python evaluation agree ---------------------------------------


@pytest.fixture
def requests_set():
    """Rows with nulls on the way (no company type, blank address)."""
    with_type = create_company("Alpha", "1")
    without_type = create_company("Beta", "2", address="")
    without_type.company_type = None
    without_type.save()
    alice = create_person("alice@example.com")
    bob = create_person("bob@example.com")
    return [
        create_request(with_type, alice, status="new", processed=False),
        create_request(with_type, bob, status="approved", processed=True),
        create_request(without_type, alice, status="approved", processed=False),
        create_request(without_type, bob, status="new", processed=True),
    ]


AGREEMENT_CASES = [
    ('status = "approved"', [1, 2]),
    ('status != "approved"', [0, 3]),
    ('status in ("new", "approved")', [0, 1, 2, 3]),
    ('status not in ("new")', [1, 2]),
    ("processed = true", [1, 3]),
    ("not processed = true", [0, 2]),
    ('status = "approved" and processed = true', [1]),
    ('status = "approved" or processed = true', [1, 2, 3]),
    ('not (status = "approved" and processed = true)', [0, 2, 3]),
    ('company.company_type.name = "LLC"', [0, 1]),
    ('company.company_type.name != "LLC"', [2, 3]),
    ('company.company_type.name not in ("LLC")', [2, 3]),
    ("company.company_type is null", [2, 3]),
    ("company.company_type.name is null", [2, 3]),
    ("company.company_type.name is not null", [0, 1]),
    ('company.address = ""', [2, 3]),
    ('company.company_name = "Beta" and user.email = "bob@example.com"', [3]),
    ('status = "x\' OR 1=1 --"', []),
    # A "not" over a compound condition on a nullable path (rows 2 and 3 have
    # no company type): SQL must not turn NOT (NULL AND NULL) into "no match".
    (
        'not (company.company_type != 999999 and company.company_type.name = "LLC")',
        [2, 3],
    ),
    (
        '(company.company_type.name is null and company.company_type.name = "LLC")'
        ' or not (company.company_type.name = "LLC")',
        [2, 3],
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("text", "expected"), AGREEMENT_CASES)
def test_database_and_python_agree(requests_set, text, expected):
    predicate = compile_request(text)
    expected_pks = {requests_set[i].pk for i in expected}

    in_database = set(
        RegistrationRequest.objects.filter(predicate.as_q()).values_list(
            "pk", flat=True
        )
    )
    in_python = {
        row.pk for row in RegistrationRequest.objects.all() if predicate.matches(row)
    }

    assert in_database == expected_pks
    assert in_python == expected_pks


FUZZ_ATOMS = [
    'status = "approved"',
    'status != "new"',
    'status in ("new", "x")',
    'status not in ("approved")',
    "processed = true",
    "processed != false",
    'company.company_type.name = "LLC"',
    'company.company_type.name != "JSC"',
    'company.company_type.name in ("LLC", "JSC")',
    'company.company_type.name not in ("JSC")',
    "company.company_type.name is null",
    "company.company_type is not null",
    "company.company_type != 999999",
    "company.company_type.id > 0",
    "company.company_type.id <= 999999",
    'company.address = ""',
    'user.email = "bob@example.com"',
]


def _random_filter(rng, depth):
    if depth == 0 or rng.random() < 0.3:
        text = rng.choice(FUZZ_ATOMS)
    else:
        items = [_random_filter(rng, depth - 1) for _ in range(rng.randint(2, 3))]
        text = "(" + f" {rng.choice(['and', 'or'])} ".join(items) + ")"
    return f"not {text}" if rng.random() < 0.4 else text


@pytest.mark.django_db
def test_database_and_python_agree_on_random_filters(requests_set):
    create_request(
        create_company("Gamma", "3", company_type=create_organization_type("JSC")),
        create_person("carol@example.com"),
        status="approved",
    )
    rows = list(
        RegistrationRequest.objects.select_related("company__company_type", "user")
    )
    rng = random.Random(20261126)
    mismatches = []
    for _ in range(2000):
        text = _random_filter(rng, rng.randint(1, 3))
        predicate = compile_request(text)
        in_database = set(
            RegistrationRequest.objects.filter(predicate.as_q()).values_list(
                "pk", flat=True
            )
        )
        in_python = {row.pk for row in rows if predicate.matches(row)}
        if in_database != in_python:
            mismatches.append(text)
    assert mismatches == []


@pytest.mark.django_db
def test_numeric_comparisons_agree(requests_set):
    pks = sorted(row.pk for row in requests_set)
    text = f"id < {pks[1]} or id >= {pks[3]}"
    predicate = compile_request(text)
    in_database = set(
        RegistrationRequest.objects.filter(predicate.as_q()).values_list(
            "pk", flat=True
        )
    )
    in_python = {
        row.pk for row in RegistrationRequest.objects.all() if predicate.matches(row)
    }
    assert in_database == in_python == {pks[0], pks[3]}


@pytest.mark.django_db
def test_value_that_looks_like_sql_is_only_a_value():
    company = create_company()
    tricky = "x' OR 1=1 --"
    row = create_request(company, status=tricky)
    create_request(company, status="new")
    predicate = compile_request(f'status = "{tricky}"')
    assert list(RegistrationRequest.objects.filter(predicate.as_q())) == [row]


# --- the model and the admin ----------------------------------------------------


@pytest.mark.django_db
def test_model_clean_reports_the_filter_error():
    tracked = create_tracked_table(RegistrationRequest, "update")
    tracked.predicate = "status = 5"
    with pytest.raises(ValidationError) as info:
        tracked.full_clean()
    assert "expects a string" in info.value.message_dict["predicate"][0]

    tracked.predicate = 'status = "approved"'
    tracked.full_clean()


@pytest.mark.django_db
def test_get_predicate():
    tracked = create_tracked_table(RegistrationRequest, "update")
    assert tracked.get_predicate() is None
    tracked.predicate = 'status = "approved"'
    assert tracked.get_predicate().matches(RegistrationRequest(status="approved"))


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser("admin", "admin@example.com", "pw")
    client.force_login(user)
    return client


def _rule_form(channel, **fields):
    data = {
        "name": "Approved requests",
        "is_active": "on",
        "content_type": ContentType.objects.get_for_model(RegistrationRequest).pk,
        "event_type": "update",
        "message_template": "{status}",
        "channel": channel.pk,
        "predicate": "",
    }
    data.update(fields)
    return data


@pytest.mark.django_db
def test_admin_rejects_an_invalid_filter(admin_client):
    channel = create_channel()
    response = admin_client.post(
        reverse("admin:webhook_outbox_trackedtable_add"),
        _rule_form(channel, predicate='status__regex = "x"'),
    )
    assert response.status_code == 200
    assert "is not allowed in field names" in response.content.decode()
    assert not TrackedTable.objects.exists()


@pytest.mark.django_db
def test_admin_saves_two_filtered_rules_for_one_channel(admin_client):
    channel = create_channel()
    url = reverse("admin:webhook_outbox_trackedtable_add")
    for name, status in (("Approved", "approved"), ("New", "new")):
        response = admin_client.post(
            url, _rule_form(channel, name=name, predicate=f'status = "{status}"')
        )
        assert response.status_code == 302, response.content.decode()[:2000]
    assert sorted(TrackedTable.objects.values_list("predicate", flat=True)) == [
        'status = "approved"',
        'status = "new"',
    ]

    changelist = admin_client.get(
        reverse("admin:webhook_outbox_trackedtable_changelist")
    )
    assert changelist.status_code == 200


# --- polling --------------------------------------------------------------------


def _poll_ready(tracked):
    """Put the cursor in the past so the next poll sees new changes."""
    tracked.last_polled_at = timezone.now() - timedelta(minutes=5)
    TrackedTable.objects.filter(pk=tracked.pk).update(
        last_polled_at=tracked.last_polled_at
    )


@pytest.mark.django_db
def test_poller_queues_only_matching_inserts():
    tracked = create_tracked_table(
        RegistrationRequest, "insert", predicate='status = "approved"'
    )
    _poll_ready(tracked)
    company = create_company()
    approved = create_request(company, status="approved")
    create_request(company, status="new")

    assert poll_table(tracked) == 1
    assert list(NotificationOutbox.objects.values_list("source_pk", flat=True)) == [
        str(approved.pk)
    ]


@pytest.mark.django_db
def test_poller_queues_only_matching_updates():
    company = create_company()
    first = create_request(company)
    second = create_request(company)
    past = timezone.now() - timedelta(minutes=10)
    RegistrationRequest.objects.update(created_at=past, updated_at=past)
    first.refresh_from_db()
    second.refresh_from_db()
    tracked = create_tracked_table(
        RegistrationRequest, "update", predicate='status = "approved"'
    )
    _poll_ready(tracked)

    first.status = "approved"
    first.save()
    second.processed = True
    second.save()

    assert poll_table(tracked) == 1
    assert NotificationOutbox.objects.get().source_pk == str(first.pk)


@pytest.mark.django_db
def test_poller_skips_an_invalid_filter_and_keeps_the_cursor(caplog):
    tracked = create_tracked_table(RegistrationRequest, "insert")
    _poll_ready(tracked)
    cursor = tracked.last_polled_at
    # Bypass model validation, as a stale filter after a schema change would.
    TrackedTable.objects.filter(pk=tracked.pk).update(predicate='gone = "x"')
    tracked.refresh_from_db()
    row = create_request(status="approved")

    with caplog.at_level(logging.ERROR, logger="webhook_outbox.services.poller"):
        assert poll_table(tracked) == 0
    assert "invalid filter" in caplog.text
    tracked.refresh_from_db()
    assert tracked.last_polled_at == cursor
    assert not NotificationOutbox.objects.exists()

    # Once fixed, the change made meanwhile is reported.
    TrackedTable.objects.filter(pk=tracked.pk).update(predicate='status = "approved"')
    tracked.refresh_from_db()
    assert poll_table(tracked) == 1
    assert NotificationOutbox.objects.get().source_pk == str(row.pk)


@pytest.mark.django_db
def test_limitation_filter_sees_the_row_at_poll_time():
    """The filter reads the row when it is polled, not at each change: a row
    that matched only between two polls gives no event."""
    tracked = create_tracked_table(
        RegistrationRequest, "insert", predicate='status = "approved"'
    )
    _poll_ready(tracked)
    row = create_request(status="approved")
    row.status = "new"
    row.save()

    assert poll_table(tracked) == 0


# --- enqueue() and signal capture -----------------------------------------------


@pytest.mark.django_db
def test_enqueue_skips_a_row_that_does_not_match():
    tracked = create_tracked_table(
        RegistrationRequest, "update", predicate='status = "approved"'
    )
    row = create_request(status="new")
    assert enqueue(row, event_type="update") is None
    assert not NotificationOutbox.objects.exists()

    row.status = "approved"
    entry = enqueue(row, event_type="update")
    assert entry is not None and entry.tracked_table == tracked


@pytest.mark.django_db
def test_enqueue_checks_the_instance_in_memory():
    create_tracked_table(RegistrationRequest, "update", predicate='status = "approved"')
    row = create_request(status="approved")
    row.status = "new"  # not saved
    assert enqueue(row, event_type="update") is None


@pytest.mark.django_db
def test_enqueue_loads_a_ref_for_the_filter():
    create_tracked_table(RegistrationRequest, "update", predicate='status = "approved"')
    approved = create_request(status="approved")
    new = create_request(status="new")
    assert enqueue((RegistrationRequest, approved.pk), event_type="update", message="m")
    assert (
        enqueue((RegistrationRequest, new.pk), event_type="update", message="m") is None
    )


@pytest.mark.django_db
def test_enqueue_with_a_filter_needs_the_row():
    create_tracked_table(RegistrationRequest, "delete", predicate='status = "approved"')
    with pytest.raises(ValueError, match="does not exist"):
        enqueue(("testapp.registrationrequest", 999), event_type="delete", message="m")


@pytest.mark.django_db
def test_enqueue_raises_for_an_invalid_filter():
    tracked = create_tracked_table(RegistrationRequest, "update")
    TrackedTable.objects.filter(pk=tracked.pk).update(predicate="gone = 1")
    with pytest.raises(PredicateError):
        enqueue(create_request(), event_type="update")


@pytest.mark.django_db
def test_enqueue_picks_the_rule_explicitly_when_several_share_a_channel():
    channel = create_channel()
    approved = create_tracked_table(
        RegistrationRequest, "update", channel=channel, predicate='status = "approved"'
    )
    create_tracked_table(
        RegistrationRequest, "update", channel=channel, predicate='status = "new"'
    )
    row = create_request(status="approved")
    with pytest.raises(LookupError, match="2 active tracked tables"):
        enqueue(row, event_type="update")
    assert enqueue(row, event_type="update", tracked_table=approved)


CAPTURE_COMPANY = override_settings(
    WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": ["testapp.company"]}
)


@pytest.mark.django_db
def test_signal_capture_applies_the_filter_per_rule():
    channel = create_channel()
    big = create_tracked_table(
        Company, "insert", channel=channel, predicate='company_name = "Big"'
    )
    create_tracked_table(
        Company, "insert", channel=channel, predicate='company_name = "Small"'
    )
    unfiltered = create_tracked_table(Company, "insert")
    with CAPTURE_COMPANY:
        create_company("Big")

    assert set(NotificationOutbox.objects.values_list("tracked_table", flat=True)) == {
        big.pk,
        unfiltered.pk,
    }


@pytest.mark.django_db
def test_signal_capture_logs_an_invalid_filter_and_keeps_the_save(caplog):
    tracked = create_tracked_table(Company, "insert")
    TrackedTable.objects.filter(pk=tracked.pk).update(predicate="gone = 1")
    other = create_tracked_table(Company, "insert")
    with (
        CAPTURE_COMPANY,
        caplog.at_level(logging.ERROR, logger="webhook_outbox.capture"),
    ):
        company = create_company("Saved")

    assert Company.objects.filter(pk=company.pk).exists()
    assert "cannot queue" in caplog.text
    assert list(NotificationOutbox.objects.values_list("tracked_table", flat=True)) == [
        other.pk
    ]


@pytest.mark.django_db
def test_signal_capture_filters_deletes_on_the_instance():
    create_tracked_table(Company, "delete", predicate='company_name = "Gone"')
    with CAPTURE_COMPANY:
        create_company("Gone").delete()
        create_company("Kept").delete()
    assert NotificationOutbox.objects.count() == 1


# --- system checks --------------------------------------------------------------


def ids(messages):
    return [message.id for message in messages]


@pytest.mark.django_db
def test_invalid_filter_warns_in_database_checks():
    tracked = create_tracked_table(RegistrationRequest, "update")
    TrackedTable.objects.filter(pk=tracked.pk).update(predicate="gone = 1")
    create_tracked_table(Company, "update", predicate='company_name = "x"')
    messages = checks.run_checks(databases=["default"])
    assert ids(messages) == ["webhook_outbox.W014"]
    assert "has no field 'gone'" in messages[0].msg


@pytest.mark.parametrize("value", ["*password*", [1], None])
def test_deny_fields_setting_is_validated(value):
    with override_settings(WEBHOOK_OUTBOX={"PREDICATE_DENY_FIELDS": value}):
        assert "webhook_outbox.E009" in ids(checks.run_checks())

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

MIGRATION_1_1 = ("webhook_outbox", "0002_1_1_event_fields")
MIGRATION_1_2 = ("webhook_outbox", "0003_1_2_predicate")
MIGRATION_1_2_1 = ("webhook_outbox", "0004_predicate_help_text_examples")
LATEST = MIGRATION_1_2_1


@pytest.mark.django_db
def test_no_missing_migrations():
    out = StringIO()
    call_command("makemigrations", "webhook_outbox", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


def test_migrations():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    names = sorted(
        name for app, name in loader.disk_migrations if app == "webhook_outbox"
    )
    assert names == [
        "0001_initial",
        "0002_1_1_event_fields",
        "0003_1_2_predicate",
        "0004_predicate_help_text_examples",
    ]


def _database_operations(operations):
    for operation in operations:
        if isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _database_operations(operation.database_operations)
        else:
            yield operation


def test_1_1_migration_is_additive():
    """0002 only adds nullable columns (plus a choices-only AlterField)."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    migration = loader.disk_migrations[MIGRATION_1_1]
    for operation in _database_operations(migration.operations):
        if isinstance(operation, migrations.AddField):
            assert operation.field.null, operation.describe()
            assert not operation.field.has_default(), operation.describe()
        else:
            assert isinstance(operation, migrations.AlterField), operation.describe()
            assert operation.name == "event_type"


def test_1_2_migration_adds_the_filter_and_drops_the_unique_rule():
    """0003: predicate with '' for existing rules, no unique (model, event, channel)."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    operations = list(
        _database_operations(loader.disk_migrations[MIGRATION_1_2].operations)
    )
    assert [type(op) for op in operations] == [
        migrations.AlterUniqueTogether,
        migrations.AddField,
    ]
    assert operations[0].unique_together == set()
    added = operations[1]
    assert added.name == "predicate"
    assert added.field.default == ""
    assert added.field.blank


@pytest.mark.django_db(transaction=True)
def test_rules_from_1_1_get_an_empty_filter():
    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_1_1])
    try:
        old_apps = executor.loader.project_state([MIGRATION_1_1]).apps
        ContentType = old_apps.get_model("contenttypes", "ContentType")
        Channel = old_apps.get_model("webhook_outbox", "NotificationChannel")
        Tracked = old_apps.get_model("webhook_outbox", "TrackedTable")
        content_type = ContentType.objects.get(app_label="testapp", model="company")
        channel = Channel.objects.create(name="c", config={"url": "https://x.test"})
        Tracked.objects.create(
            name="t",
            content_type=content_type,
            event_type="update",
            message_template="m",
            channel=channel,
        )
    finally:
        executor.loader.build_graph()
        executor.migrate([LATEST])

    from webhook_outbox.models import TrackedTable

    tracked = TrackedTable.objects.get(name="t")
    assert tracked.predicate == ""
    assert tracked.get_predicate() is None


@pytest.mark.django_db(transaction=True)
def test_1_1_migration_sql_only_adds_columns():
    out = StringIO()
    call_command("sqlmigrate", *MIGRATION_1_1, stdout=out)
    statements = [
        line
        for line in out.getvalue().splitlines()
        if line.strip()
        and not line.startswith("--")
        and line not in ("BEGIN;", "COMMIT;")
    ]
    assert statements
    assert all(
        line.startswith("ALTER TABLE") and "ADD COLUMN" in line for line in statements
    )


@pytest.mark.django_db(transaction=True)
def test_rows_written_by_1_0_keep_null_event_ids():
    """
    An AddField with default=uuid4 would give every existing row the *same*
    id. Rows from 1.0 must keep NULL, and new rows get distinct ids.
    """
    executor = MigrationExecutor(connection)
    executor.migrate([("webhook_outbox", "0001_initial")])
    try:
        old_apps = executor.loader.project_state(
            [("webhook_outbox", "0001_initial")]
        ).apps
        ContentType = old_apps.get_model("contenttypes", "ContentType")
        Channel = old_apps.get_model("webhook_outbox", "NotificationChannel")
        Tracked = old_apps.get_model("webhook_outbox", "TrackedTable")
        Outbox = old_apps.get_model("webhook_outbox", "NotificationOutbox")
        content_type = ContentType.objects.get(app_label="testapp", model="company")
        channel = Channel.objects.create(name="c", config={"url": "https://x.test"})
        tracked = Tracked.objects.create(
            name="t",
            content_type=content_type,
            event_type="insert",
            message_template="m",
            channel=channel,
        )
        for pk in ("1", "2"):
            Outbox.objects.create(
                tracked_table=tracked,
                source_model="testapp.company",
                source_pk=pk,
                event_type="insert",
                message="m",
            )
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([LATEST])

    from webhook_outbox.models import NotificationOutbox

    assert list(NotificationOutbox.objects.values_list("event_id", flat=True)) == [
        None,
        None,
    ]
    new = [
        NotificationOutbox.objects.create(
            tracked_table_id=tracked.pk,
            source_model="testapp.company",
            source_pk=pk,
            event_type="insert",
            message="m",
        )
        for pk in ("3", "4")
    ]
    assert None not in {entry.event_id for entry in new}
    assert new[0].event_id != new[1].event_id

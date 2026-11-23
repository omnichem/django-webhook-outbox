from io import StringIO

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_no_missing_migrations():
    out = StringIO()
    call_command("makemigrations", "webhook_outbox", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


def test_single_initial_migration():
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    names = sorted(
        name for app, name in loader.disk_migrations if app == "webhook_outbox"
    )
    assert names == ["0001_initial"]

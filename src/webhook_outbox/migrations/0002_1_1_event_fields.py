# django-webhook-outbox 1.1: additive changes only.
#
# event_id: an AddField with a callable default would compute it once and
# give every existing row the same id. The column is therefore added without
# a default; the uuid4 default only lives in the migration state (Django sets
# it in Python). Existing rows keep NULL and get an id on their first
# delivery attempt.

import uuid

import django.core.serializers.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_outbox", "0001_initial"),
    ]

    operations = [
        # Database: a plain nullable column, no default, so existing rows keep
        # NULL (and no table rebuild on SQLite). State: the uuid4 default the
        # model has, so new rows get an id from Django.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.AddField(
                    model_name="notificationoutbox",
                    name="event_id",
                    field=models.UUIDField(blank=True, editable=False, null=True),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="notificationoutbox",
                    name="event_id",
                    field=models.UUIDField(
                        blank=True,
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique id of the event, stable across retries. Rows queued before 1.1 get one on their first delivery attempt.",
                        null=True,
                        verbose_name="Event ID",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="notificationoutbox",
            name="source_changed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Timestamp of the source row the event was built from (created_at for insert, updated_at for update). Used to recognise a change that is already queued.",
                null=True,
                verbose_name="Source changed at",
            ),
        ),
        migrations.AddField(
            model_name="notificationoutbox",
            name="changes",
            field=models.JSONField(
                blank=True,
                encoder=django.core.serializers.json.DjangoJSONEncoder,
                help_text='Old and new values: {"field": {"old": ..., "new": ...}}',
                null=True,
                verbose_name="Changes",
            ),
        ),
        migrations.AddField(
            model_name="notificationoutbox",
            name="extra",
            field=models.JSONField(
                blank=True,
                encoder=django.core.serializers.json.DjangoJSONEncoder,
                help_text="Data supplied by the producer of the event",
                null=True,
                verbose_name="Extra data",
            ),
        ),
        migrations.AddField(
            model_name="trackedtable",
            name="dedup_window_seconds",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Overrides WEBHOOK_OUTBOX['DEDUP_WINDOW_SECONDS'] for this rule; 0 disables the window. Leave empty to use the setting.",
                null=True,
                verbose_name="Dedup window (seconds)",
            ),
        ),
        # Choices only: no SQL.
        migrations.AlterField(
            model_name="trackedtable",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("insert", "Insert"),
                    ("update", "Update"),
                    ("delete", "Delete"),
                ],
                max_length=10,
                verbose_name="Event Type",
            ),
        ),
    ]

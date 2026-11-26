# django-webhook-outbox 1.2: tracked table filters.
#
# * predicate: a text column, '' (no filter) for existing rules, so their
#   behaviour does not change.
# * The unique (content_type, event_type, channel) constraint is dropped:
#   rules with different filters can now send to the same channel. Nothing
#   relies on it: enqueue() already refuses an ambiguous lookup.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_outbox", "0002_1_1_event_fields"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="trackedtable",
            unique_together=set(),
        ),
        migrations.AddField(
            model_name="trackedtable",
            name="predicate",
            field=models.TextField(
                blank=True,
                default="",
                help_text='Optional. Only rows that match are notified, e.g. status = "paid" and total >= 100. Operators: = != < <= > >= in (...), not in (...), is null, is not null, and, or, not, parentheses. Strings in quotes, numbers, true, false. Foreign keys: company.country = "DE". Empty: every row.',
                verbose_name="Filter",
            ),
        ),
    ]

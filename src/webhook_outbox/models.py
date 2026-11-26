import uuid

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


class TimeStampedModel(models.Model):
    """
    Abstract base with ``created_at`` / ``updated_at``.

    The poller needs exactly these two fields on a tracked model: inherit
    from this class (or declare the same two fields) in your own models.
    """

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        abstract = True


class NotificationChannel(TimeStampedModel):
    """
    Configuration for notification delivery channels (Webhook, future: Email, Slack, etc.)
    """

    class ChannelType(models.TextChoices):
        WEBHOOK = "webhook", _("Webhook")

    name = models.CharField(
        max_length=100,
        verbose_name=_("Name"),
        help_text=_("Human-readable name for this channel"),
    )

    channel_type = models.CharField(
        max_length=20,
        choices=ChannelType.choices,
        default=ChannelType.WEBHOOK,
        verbose_name=_("Channel Type"),
    )

    config = models.JSONField(
        verbose_name=_("Configuration"),
        help_text=_(
            'Channel-specific config. Webhook: {"url": "https://...", '
            '"headers": {"Authorization": "Bearer ..."}, "timeout": 30}'
        ),
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
    )

    class Meta:
        verbose_name = _("Notification Channel")
        verbose_name_plural = _("Notification Channels")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_channel_type_display()})"


class TrackedTable(TimeStampedModel):
    """
    Configuration for which database tables to monitor for changes.
    """

    class EventType(models.TextChoices):
        INSERT = "insert", _("Insert")
        UPDATE = "update", _("Update")
        # Not polled: produced by signal capture or enqueue() only.
        # Context: the bare "Delete" is the admin's delete button label.
        DELETE = "delete", pgettext_lazy("event type", "Delete")

    name = models.CharField(
        max_length=100,
        verbose_name=_("Name"),
        help_text=_("Human-readable name for this tracking rule"),
    )

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        verbose_name=_("Model"),
        help_text=_("Django model to track"),
    )

    event_type = models.CharField(
        max_length=10,
        choices=EventType.choices,
        verbose_name=_("Event Type"),
    )

    # Without a predicate, UPDATE events notify on any update to the record.
    predicate = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Filter"),
        help_text=_(
            "Optional. Only rows that match are notified, e.g. "
            'status = "paid" and total >= 100. Operators: = != < <= > >= '
            "in (...), not in (...), is null, is not null, and, or, not, "
            "parentheses. Strings in quotes, numbers, true, false. "
            'Foreign keys: company.country = "DE". Empty: every row.'
        ),
    )

    message_template = models.TextField(
        verbose_name=_("Message Template"),
        help_text=_(
            "Template with {field} placeholders. "
            "Supports FK traversal: {company.company_name}, {status.value}"
        ),
    )

    channel = models.ForeignKey(
        NotificationChannel,
        on_delete=models.CASCADE,
        related_name="tracked_tables",
        verbose_name=_("Notification Channel"),
    )

    last_polled_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Last Polled At"),
        help_text=_("Timestamp of the last successful poll"),
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
    )

    dedup_window_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Dedup window (seconds)"),
        help_text=_(
            "Overrides WEBHOOK_OUTBOX['DEDUP_WINDOW_SECONDS'] for this rule; "
            "0 disables the window. Leave empty to use the setting."
        ),
    )

    class Meta:
        verbose_name = _("Tracked Table")
        verbose_name_plural = _("Tracked Tables")
        ordering = ["content_type__app_label", "content_type__model"]
        # 1.2: no unique (model, event, channel) any more, so that rules with
        # different filters can send to the same channel.

    def __str__(self):
        model_name = (
            self.content_type.model_class()._meta.verbose_name
            if self.content_type
            else "Unknown"
        )
        return f"{self.name} ({model_name} - {self.get_event_type_display()})"

    def clean(self):
        super().clean()
        if not self.predicate or not self.content_type_id:
            return
        model = self.get_model_class()
        if model is None:
            return
        from webhook_outbox.services.predicate import PredicateError, compile_predicate

        try:
            compile_predicate(self.predicate, model)
        except PredicateError as e:
            raise ValidationError({"predicate": str(e)}) from None

    def get_predicate(self):
        """
        The compiled filter, or None when there is none. Raises
        ``PredicateError`` when it is invalid (or the model is gone).
        """
        from webhook_outbox.services.predicate import PredicateError, compile_predicate

        if not self.predicate or not self.predicate.strip():
            return None
        model = self.get_model_class()
        if model is None:
            raise PredicateError(f"Model {self.model_path} is not installed.")
        return compile_predicate(self.predicate, model)

    def get_model_class(self):
        """
        Returns the Django model class for this tracked table.
        """
        if self.content_type:
            return self.content_type.model_class()
        return None

    @property
    def app_label(self):
        """Backwards compatibility property."""
        return self.content_type.app_label if self.content_type else None

    @property
    def model_name(self):
        """Backwards compatibility property."""
        return self.content_type.model if self.content_type else None

    @property
    def model_path(self):
        return f"{self.app_label}.{self.model_name}" if self.content_type else None


class NotificationOutbox(TimeStampedModel):
    """
    Queue of notifications to be sent. Provides reliability through the outbox pattern.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")

    tracked_table = models.ForeignKey(
        TrackedTable,
        on_delete=models.CASCADE,
        related_name="outbox_entries",
        verbose_name=_("Tracked Table"),
    )

    source_model = models.CharField(
        max_length=200,
        verbose_name=_("Source Model"),
        help_text=_("Full model path, e.g., 'shop.order'"),
    )

    source_pk = models.CharField(
        max_length=100,
        verbose_name=_("Source Primary Key"),
    )

    event_type = models.CharField(
        max_length=10,
        verbose_name=_("Event Type"),
    )

    message = models.TextField(
        verbose_name=_("Message"),
        help_text=_("Rendered notification message"),
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name=_("Status"),
        db_index=True,
    )

    error_message = models.TextField(
        blank=True,
        verbose_name=_("Error Message"),
    )

    sent_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Sent At"),
    )

    retry_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Retry Count"),
    )

    # --- added in 1.1 (all nullable: rows written by 1.0 keep NULL) ---------

    event_id = models.UUIDField(
        null=True,
        blank=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name=_("Event ID"),
        help_text=_(
            "Unique id of the event, stable across retries. Rows queued before "
            "1.1 get one on their first delivery attempt."
        ),
    )

    source_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Source changed at"),
        help_text=_(
            "Timestamp of the source row the event was built from (created_at "
            "for insert, updated_at for update). Used to recognise a change "
            "that is already queued."
        ),
    )

    changes = models.JSONField(
        null=True,
        blank=True,
        encoder=DjangoJSONEncoder,
        verbose_name=_("Changes"),
        help_text=_('Old and new values: {"field": {"old": ..., "new": ...}}'),
    )

    extra = models.JSONField(
        null=True,
        blank=True,
        encoder=DjangoJSONEncoder,
        verbose_name=_("Extra data"),
        help_text=_("Data supplied by the producer of the event"),
    )

    class Meta:
        verbose_name = _("Notification Outbox")
        verbose_name_plural = _("Notification Outbox Entries")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"Notification #{self.pk} - {self.source_model}:{self.source_pk} ({self.status})"

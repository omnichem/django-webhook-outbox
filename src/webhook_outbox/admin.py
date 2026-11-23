import logging

from django import forms
from django.apps import apps
from django.contrib import admin, messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from django.utils.html import format_html
from django.utils.module_loading import import_string
from django.utils.translation import gettext_lazy as _

from .conf import get_setting
from .models import NotificationChannel, NotificationOutbox, TrackedTable
from .services.channels import get_channel_registry

logger = logging.getLogger(__name__)

SELF_APP_NAME = "webhook_outbox"


def _is_excluded_app(app_config) -> bool:
    name = app_config.name
    if name.startswith("django."):
        return True
    # Self-tracking guard: the package's own models (and its contrib apps)
    # would feed the outbox with its own rows.
    if name == SELF_APP_NAME or name.startswith(f"{SELF_APP_NAME}."):
        return True
    excluded = set(get_setting("TRACKABLE_EXCLUDE_APPS") or ())
    return app_config.label in excluded or name in excluded


def get_trackable_content_types():
    """
    Get ContentType objects for models that have timestamp fields.
    Returns list of (content_type_id, verbose_name) tuples.

    Skips django.* apps, webhook_outbox itself and TRACKABLE_EXCLUDE_APPS.
    """
    choices = []
    for app_config in apps.get_app_configs():
        if _is_excluded_app(app_config):
            continue
        for model in app_config.get_models():
            meta = model._meta
            field_names = {f.name for f in meta.get_fields()}
            if "created_at" in field_names:
                has_updated = "updated_at" in field_names
                suffix = "" if has_updated else " (INSERT only)"
                ct = ContentType.objects.get_for_model(model)
                label = f"{meta.verbose_name_plural}{suffix}"
                choices.append((ct.pk, label))
    return sorted(choices, key=lambda x: x[1])


class TrackedTableForm(forms.ModelForm):
    """Custom form for TrackedTable with filtered content_type choices."""

    class Meta:
        model = TrackedTable
        fields = "__all__"  # noqa: DJ007 - the ModelAdmin fieldsets pick the fields

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "content_type" in self.fields:
            choices = get_trackable_content_types()
            field = self.fields["content_type"]
            # Restrict the queryset too: choices alone only change the widget,
            # a posted id outside them would still validate.
            field.queryset = ContentType.objects.filter(
                pk__in=[pk for pk, _label in choices]
            )
            field.choices = [("", "---------")] + choices
            field.widget = forms.Select(choices=[("", "---------")] + choices)


def get_channel_type_choices():
    labels = dict(NotificationChannel.ChannelType.choices)
    choices = []
    for channel_type, channel_class in get_channel_registry().items():
        label = labels.get(channel_type) or getattr(
            channel_class, "verbose_name", channel_type
        )
        choices.append((channel_type, label))
    return choices


class NotificationChannelForm(forms.ModelForm):
    """Channel types offered from the channel registry (WEBHOOK_OUTBOX['CHANNELS'])."""

    class Meta:
        model = NotificationChannel
        fields = "__all__"  # noqa: DJ007 - the ModelAdmin fieldsets pick the fields

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "channel_type" in self.fields:
            field = self.fields["channel_type"]
            self.fields["channel_type"] = forms.ChoiceField(
                choices=get_channel_type_choices(),
                initial=field.initial,
                label=field.label,
                help_text=field.help_text,
                required=True,
            )

    def _get_validation_exclusions(self):
        # The model field's choices only list the built-in "webhook" type (so
        # migrations do not depend on settings); the form field above already
        # validated the value against the registry.
        exclude = super()._get_validation_exclusions()
        exclude.add("channel_type")
        return exclude


@admin.register(NotificationChannel)
class NotificationChannelAdmin(admin.ModelAdmin):
    form = NotificationChannelForm
    list_display = [
        "name",
        "channel_type",
        "is_active_colored",
        "tracked_tables_count",
        "created_at",
    ]
    list_filter = ["channel_type", "is_active"]
    search_fields = ["name"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["name"]

    fieldsets = (
        (
            None,
            {
                "fields": ("name", "channel_type", "is_active"),
            },
        ),
        (
            _("Configuration"),
            {
                "fields": ("config",),
                "description": _(
                    'Webhook: {"url": "https://example.com/hooks/outbox", '
                    '"headers": {"Authorization": "Bearer ..."}, "timeout": 30}. '
                    "Only url is required; headers authenticate the outgoing call."
                ),
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Active"), boolean=True)
    def is_active_colored(self, obj: NotificationChannel) -> bool:
        return obj.is_active

    @admin.display(description=_("Tracked Tables"))
    def tracked_tables_count(self, obj: NotificationChannel) -> int:
        return obj.tracked_tables.count()

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).prefetch_related("tracked_tables")


@admin.register(TrackedTable)
class TrackedTableAdmin(admin.ModelAdmin):
    form = TrackedTableForm
    list_display = [
        "name",
        "model_display",
        "event_type",
        "channel",
        "is_active_colored",
        "last_polled_at",
    ]
    list_filter = ["event_type", "is_active", "channel"]
    search_fields = ["name"]
    readonly_fields = [
        "created_at",
        "updated_at",
        "last_polled_at",
    ]
    ordering = ["name"]
    autocomplete_fields = ["channel"]

    fieldsets = (
        (
            None,
            {
                "fields": ("name", "is_active"),
            },
        ),
        (
            _("Model Configuration"),
            {
                "fields": ("content_type", "event_type"),
                "description": _(
                    "Select the model to track and the event type. "
                    "INSERT: notifies on new records. "
                    "UPDATE: notifies on any change to existing records. "
                    "DELETE: not polled; fed by signal capture or enqueue()."
                ),
            },
        ),
        (
            _("Notification"),
            {
                "fields": ("message_template", "channel"),
                "description": _(
                    "Use {field_name} for direct fields, "
                    "{relation.field} for FK traversal, "
                    "{admin_link} for admin panel URL. "
                    "Example: 'New request for {company.company_name}\\n{admin_link}'"
                ),
            },
        ),
        (
            _("Status"),
            {
                "fields": ("last_polled_at", "dedup_window_seconds"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Active"), boolean=True)
    def is_active_colored(self, obj: TrackedTable) -> bool:
        return obj.is_active

    @admin.display(description=_("Model"))
    def model_display(self, obj: TrackedTable) -> str:
        if obj.content_type:
            model_class = obj.content_type.model_class()
            if model_class:
                return model_class._meta.verbose_name_plural
        return "-"


def _get_retry_hook():
    hook = get_setting("ADMIN_RETRY_HOOK")
    if hook is None:
        return None
    return import_string(hook) if isinstance(hook, str) else hook


@admin.register(NotificationOutbox)
class NotificationOutboxAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "source_model",
        "source_pk",
        "event_type",
        "status_colored",
        "retry_count",
        "created_at",
        "sent_at",
    ]
    list_filter = ["status", "event_type", "tracked_table", "tracked_table__channel"]
    search_fields = ["source_model", "source_pk", "message"]
    readonly_fields = [
        "created_at",
        "updated_at",
        "tracked_table",
        "source_model",
        "source_pk",
        "event_type",
        "message",
        "sent_at",
        "retry_count",
        "event_id",
        "source_changed_at",
        "changes",
        "extra",
    ]
    ordering = ["-created_at"]
    actions = ["retry_failed", "mark_as_sent"]
    date_hierarchy = "created_at"

    fieldsets = (
        (
            _("Source"),
            {
                "fields": ("tracked_table", "source_model", "source_pk", "event_type"),
            },
        ),
        (
            _("Message"),
            {
                "fields": ("message", "changes", "extra"),
            },
        ),
        (
            _("Event"),
            {
                "fields": ("event_id", "source_changed_at"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Status"),
            {
                "fields": ("status", "error_message", "retry_count", "sent_at"),
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Status"))
    def status_colored(self, obj: NotificationOutbox) -> str:
        colors = {
            NotificationOutbox.Status.PENDING: "#f0ad4e",
            NotificationOutbox.Status.SENT: "#5cb85c",
            NotificationOutbox.Status.FAILED: "#d9534f",
        }
        color = colors.get(obj.status, "#777")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.action(description=_("Retry selected failed notifications"))
    def retry_failed(self, request: HttpRequest, queryset: QuerySet) -> None:
        """
        Reset FAILED entries to PENDING (retry_count 0, error cleared). The
        next delivery pass sends them; ADMIN_RETRY_HOOK, if set, is called
        with each entry's pk to push it right away.
        """
        failed = queryset.filter(status=NotificationOutbox.Status.FAILED)
        hook = _get_retry_hook()
        count = 0
        hook_errors = 0

        for entry in failed:
            entry.status = NotificationOutbox.Status.PENDING
            entry.retry_count = 0
            entry.error_message = ""
            entry.save()
            if hook is not None:
                try:
                    hook(entry.pk)
                except Exception:
                    # The entry is already PENDING: the worker picks it up.
                    logger.exception(f"ADMIN_RETRY_HOOK failed for entry #{entry.pk}")
                    hook_errors += 1
            count += 1

        self.message_user(
            request,
            _("Queued {} notification(s) for retry.").format(count),
            messages.SUCCESS,
        )
        if hook_errors:
            self.message_user(
                request,
                _(
                    "Immediate delivery could not be started for {} notification(s); "
                    "they will be sent by the next delivery run."
                ).format(hook_errors),
                messages.WARNING,
            )

    @admin.action(description=_("Mark selected as sent (skip sending)"))
    def mark_as_sent(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.exclude(status=NotificationOutbox.Status.SENT).update(
            status=NotificationOutbox.Status.SENT,
            sent_at=timezone.now(),
        )

        self.message_user(
            request,
            _("Marked {} notification(s) as sent.").format(updated),
            messages.SUCCESS,
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        if obj and obj.status == NotificationOutbox.Status.SENT:
            return False
        return super().has_change_permission(request, obj)

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from webhook_outbox.conf import get_setting

# Version of the default JSON document built by NotificationPayload.as_dict().
# v1 has no version key (1.0 sends the keys exactly as the original
# implementation did); v2 carries "schema_version": 2. See docs/envelope.md.
ENVELOPE_VERSION = 1
ENVELOPE_VERSIONS = (1, 2)


def validate_envelope_version(value: Any) -> int:
    if value not in ENVELOPE_VERSIONS or isinstance(value, bool):
        raise ValueError(
            f"Unsupported envelope version {value!r}; expected one of "
            f"{', '.join(map(str, ENVELOPE_VERSIONS))}"
        )
    return value


def get_envelope_version(channel=None) -> int:
    """
    Envelope version used for a NotificationChannel: its config's
    "envelope_version", else WEBHOOK_OUTBOX["ENVELOPE_VERSION"] (1).
    """
    config = getattr(channel, "config", None)
    if isinstance(config, dict) and config.get("envelope_version") is not None:
        return config["envelope_version"]
    return get_setting("ENVELOPE_VERSION")


@dataclass(frozen=True)
class NotificationPayload:
    """
    Channel-agnostic representation of a notification to be delivered.

    Built from a NotificationOutbox entry, but deliberately decoupled from the
    ORM so channels can be tested without touching the database.
    """

    message: str
    event_type: str
    source_model: str
    source_pk: str
    tracked_table: str
    occurred_at: datetime
    # Added in 1.1, all optional so 1.0 call sites keep working.
    event_id: UUID | str | None = None
    changes: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None
    # Timestamp of the source row change, when known (v2 "occurred_at").
    source_changed_at: datetime | None = None

    @classmethod
    def from_outbox(cls, entry) -> "NotificationPayload":
        """
        Build a payload from a NotificationOutbox entry.

        Args:
            entry: NotificationOutbox instance

        Returns:
            NotificationPayload
        """
        return cls(
            message=entry.message,
            event_type=entry.event_type,
            source_model=entry.source_model,
            source_pk=entry.source_pk,
            tracked_table=entry.tracked_table.name,
            occurred_at=entry.created_at,
            event_id=getattr(entry, "event_id", None),
            changes=getattr(entry, "changes", None),
            extra=getattr(entry, "extra", None),
            source_changed_at=getattr(entry, "source_changed_at", None),
        )

    def as_dict(
        self, version: int | None = None, *, include_event_id: bool | None = None
    ) -> dict[str, Any]:
        """
        Serialize to a JSON-ready dict.

        Args:
            version: 1 or 2; defaults to WEBHOOK_OUTBOX["ENVELOPE_VERSION"].
            include_event_id: v1 only, add "event_id" as the last key;
                defaults to WEBHOOK_OUTBOX["ENVELOPE_V1_EVENT_ID"].
        """
        if version is None:
            version = get_setting("ENVELOPE_VERSION")
        validate_envelope_version(version)
        if version == 2:
            return self.as_v2_dict()
        if include_event_id is None:
            include_event_id = get_setting("ENVELOPE_V1_EVENT_ID")
        return self.as_v1_dict(include_event_id=include_event_id)

    def as_v1_dict(self, *, include_event_id: bool = False) -> dict[str, Any]:
        """Envelope v1: the 1.0 document, optionally plus "event_id"."""
        data = {
            "event": self.event_type,
            "source_model": self.source_model,
            "source_pk": self.source_pk,
            "tracked_table": self.tracked_table,
            "message": self.message,
            "occurred_at": self.occurred_at.isoformat(),
        }
        if include_event_id:
            data["event_id"] = self._event_id_str()
        return data

    def as_v2_dict(self) -> dict[str, Any]:
        """Envelope v2, see docs/envelope.md."""
        occurred_at = self.source_changed_at or self.occurred_at
        return {
            "schema_version": 2,
            "event_id": self._event_id_str(),
            "event_type": self.event_type,
            "occurred_at": occurred_at.isoformat(),
            "source": {"model": self.source_model, "pk": self.source_pk},
            "tracked_table": self.tracked_table,
            "message": self.message,
            "changes": self.changes,
            "extra": self.extra,
        }

    def _event_id_str(self) -> str | None:
        return None if self.event_id is None else str(self.event_id)


class BaseChannel(ABC):
    """
    Abstract base class for notification channels.
    All channel implementations must inherit from this class.
    """

    def __init__(self, config: dict[str, Any]):
        """
        Initialize the channel with its configuration.

        Args:
            config: Channel-specific configuration dictionary
        """
        self.config = config
        self.validate_config()

    @abstractmethod
    def validate_config(self) -> None:
        """
        Validate that the configuration contains all required fields.
        Raises ValueError if configuration is invalid.
        """

    @abstractmethod
    def send(self, payload: NotificationPayload) -> tuple[bool, str]:
        """
        Send a notification through this channel.

        Args:
            payload: The notification to send. Text-only channels can use
                payload.message; structured channels get the full event.

        Returns:
            Tuple of (success: bool, error_message: str)
            error_message is empty string on success
        """

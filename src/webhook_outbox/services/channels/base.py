from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Version of the JSON document built by NotificationPayload.as_dict(). It is
# not part of the document itself (1.0 sends the keys exactly as the original
# implementation did); see docs/envelope.md.
ENVELOPE_VERSION = 1


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
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-ready dict (envelope v1)."""
        return {
            "event": self.event_type,
            "source_model": self.source_model,
            "source_pk": self.source_pk,
            "tracked_table": self.tracked_table,
            "message": self.message,
            "occurred_at": self.occurred_at.isoformat(),
        }


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

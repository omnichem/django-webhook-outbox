import logging

from django.utils.module_loading import import_string

from webhook_outbox.conf import get_channel_paths
from webhook_outbox.models import NotificationOutbox

from .base import ENVELOPE_VERSION, BaseChannel, NotificationPayload
from .webhook import WebhookChannel

logger = logging.getLogger(__name__)


def load_channel_class(path_or_class) -> type[BaseChannel]:
    """Resolve a CHANNELS value (dotted path or class) to a BaseChannel subclass."""
    channel_class = (
        import_string(path_or_class)
        if isinstance(path_or_class, str)
        else path_or_class
    )
    if not (isinstance(channel_class, type) and issubclass(channel_class, BaseChannel)):
        raise TypeError(f"{path_or_class!r} is not a BaseChannel subclass")
    return channel_class


def get_channel_registry() -> dict[str, type[BaseChannel]]:
    """
    channel_type -> channel class, built from WEBHOOK_OUTBOX["CHANNELS"]
    merged over the built-in {"webhook": WebhookChannel}.

    Entries that cannot be loaded are skipped here (and reported by the
    system check webhook_outbox.E003).
    """
    registry: dict[str, type[BaseChannel]] = {}
    for channel_type, path_or_class in get_channel_paths().items():
        try:
            registry[channel_type] = load_channel_class(path_or_class)
        except Exception as e:
            logger.error(f"Cannot load channel class for '{channel_type}': {e}")
    return registry


def get_channel_class(channel_type: str) -> type[BaseChannel] | None:
    return get_channel_registry().get(channel_type)


def dispatch(entry: NotificationOutbox) -> tuple[bool, str]:
    """
    Deliver an outbox entry through the channel configured on its tracked table.

    Args:
        entry: NotificationOutbox entry to send

    Returns:
        Tuple of (success, error_message)
    """
    channel = entry.tracked_table.channel

    if not channel.is_active:
        return False, "Channel is inactive"

    channel_class = get_channel_class(channel.channel_type)
    if channel_class is None:
        return False, f"Unsupported channel type: {channel.channel_type}"

    try:
        return channel_class(channel.config).send(
            NotificationPayload.from_outbox(entry)
        )
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        logger.exception(f"Error sending via channel '{channel.name}': {e}")
        return False, str(e)


__all__ = [
    "ENVELOPE_VERSION",
    "BaseChannel",
    "NotificationPayload",
    "WebhookChannel",
    "dispatch",
    "get_channel_class",
    "get_channel_registry",
    "load_channel_class",
]

"""django-webhook-outbox: outgoing webhooks for Django models."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("django-webhook-outbox")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.0.0"

__all__ = ["__version__"]

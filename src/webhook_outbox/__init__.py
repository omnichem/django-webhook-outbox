"""django-webhook-outbox: outgoing webhooks for Django models."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("django-webhook-outbox")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.0.0"

__all__ = ["__version__", "enqueue", "TrackedTableLookupError"]


def __getattr__(name):
    # Lazy: importing models at package import time would break app loading.
    if name in ("enqueue", "TrackedTableLookupError"):
        from .services import enqueue as _enqueue_module

        return getattr(_enqueue_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

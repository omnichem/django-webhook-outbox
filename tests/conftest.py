from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest

_RealClient = httpx.Client


class TransportRecorder:
    """Installs an httpx.MockTransport under WebhookChannel and records requests."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict] = []
        self._handler = handler

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def client_factory(self, **kwargs):
        self.client_kwargs.append(kwargs)
        return _RealClient(transport=httpx.MockTransport(self._handle), **kwargs)


@pytest.fixture
def mock_transport():
    """
    ``mock_transport(handler)`` routes every WebhookChannel request to
    ``handler(request) -> httpx.Response`` (no network) and returns the
    recorder with ``.requests``.
    """
    patchers = []

    def install(handler=None):
        recorder = TransportRecorder(handler or (lambda request: httpx.Response(200)))
        patcher = patch(
            "webhook_outbox.services.channels.webhook.httpx.Client",
            side_effect=recorder.client_factory,
        )
        patcher.start()
        patchers.append(patcher)
        return recorder

    yield install

    for patcher in patchers:
        patcher.stop()

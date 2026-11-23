"""Channel classes used by the tests (CHANNELS setting targets)."""

from webhook_outbox.services.channels import BaseChannel

SENT = []


class RecordingChannel(BaseChannel):
    verbose_name = "Recording"

    def validate_config(self):
        pass

    def send(self, payload):
        SENT.append(payload)
        return True, ""


class NotAChannel:
    pass


def retry_hook(outbox_id):
    SENT.append(("hook", outbox_id))


def broken_hook(outbox_id):
    raise ConnectionError("broker down")


ENQUEUED = []


def enqueue_hook(outbox_id):
    ENQUEUED.append(outbox_id)

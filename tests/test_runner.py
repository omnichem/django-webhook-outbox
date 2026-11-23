import signal
import threading
from io import StringIO
from unittest.mock import patch

import httpx
import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from tests.factories import create_company, create_outbox_entry, create_tracked_table
from tests.testapp.models import Company
from webhook_outbox import runner
from webhook_outbox.models import NotificationOutbox


@pytest.fixture
def no_connection_handling():
    # close_old_connections() inside a TestCase transaction would mark it for
    # rollback; the loop tests that use the ORM run as transactional tests.
    with patch("webhook_outbox.runner.close_old_connections"):
        yield


class StepRecorder:
    def __init__(self, stop: threading.Event, stop_after: int, fail_first=False):
        self.calls: list[str] = []
        self.stop = stop
        self.stop_after = stop_after
        self.fail_first = fail_first

    def step(self, name):
        def run(*args, **kwargs):
            self.calls.append(name)
            if len(self.calls) >= self.stop_after:
                self.stop.set()
            if self.fail_first and len(self.calls) == 1:
                raise RuntimeError("first step fails")
            return 0 if name == "poll" else {"sent": 0, "failed": 0}

        return run


def run_loop(recorder, **kwargs):
    with (
        patch("webhook_outbox.runner.poll_once", recorder.step("poll")),
        patch("webhook_outbox.runner.deliver_once", recorder.step("deliver")),
    ):
        runner.run_forever(
            stop_event=recorder.stop, install_signal_handlers=False, **kwargs
        )


def test_run_forever_runs_both_steps(no_connection_handling):
    recorder = StepRecorder(threading.Event(), stop_after=6)
    run_loop(recorder, poll_interval=0.05, send_interval=0.01)
    assert recorder.calls[:2] == ["poll", "deliver"]
    assert recorder.calls.count("deliver") > recorder.calls.count("poll")


@pytest.mark.parametrize(
    ("flags", "expected"), [({"poll": False}, "deliver"), ({"deliver": False}, "poll")]
)
def test_run_forever_only_one_step(no_connection_handling, flags, expected):
    recorder = StepRecorder(threading.Event(), stop_after=3)
    run_loop(recorder, poll_interval=0.01, send_interval=0.01, **flags)
    assert set(recorder.calls) == {expected}


def test_run_forever_survives_step_errors(no_connection_handling):
    recorder = StepRecorder(threading.Event(), stop_after=4, fail_first=True)
    run_loop(recorder, poll_interval=0.01, send_interval=0.01)
    assert len(recorder.calls) == 4


def test_run_forever_validates_arguments():
    with pytest.raises(ValueError):
        runner.run_forever(poll_interval=0, send_interval=1)
    with pytest.raises(ValueError):
        runner.run_forever(poll_interval=1, send_interval=1, poll=False, deliver=False)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forever_stops_on_signal_and_restores_handler(
    no_connection_handling, signum
):
    previous = signal.getsignal(signum)
    calls = []

    def poll_and_signal():
        calls.append("poll")
        signal.raise_signal(signum)
        return 0

    def deliver():
        calls.append("deliver")
        return {"sent": 0, "failed": 0}

    with (
        patch("webhook_outbox.runner.poll_once", poll_and_signal),
        patch("webhook_outbox.runner.deliver_once", deliver),
    ):
        runner.run_forever(poll_interval=10, send_interval=10)

    # the step in progress finished, nothing ran after the signal
    assert calls == ["poll"]
    assert signal.getsignal(signum) == previous


@pytest.mark.django_db(transaction=True)
def test_run_forever_end_to_end(mock_transport):
    recorder = mock_transport(lambda request: httpx.Response(200))
    tracked = create_tracked_table(Company, message_template="{company_name}")
    runner.poll_once()  # initialise the cursor
    create_company("Loop Co")

    stop = threading.Event()
    real_deliver = runner.deliver_once

    def deliver_then_stop(*args, **kwargs):
        result = real_deliver(*args, **kwargs)
        stop.set()
        return result

    with patch("webhook_outbox.runner.deliver_once", deliver_then_stop):
        runner.run_forever(
            poll_interval=1,
            send_interval=1,
            stop_event=stop,
            install_signal_handlers=False,
        )

    entry = NotificationOutbox.objects.get(tracked_table=tracked)
    assert entry.status == NotificationOutbox.Status.SENT
    assert len(recorder.requests) == 1


# --- management commands --------------------------------------------------------


def call(*args):
    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_worker_once(mock_transport):
    mock_transport()
    tracked = create_tracked_table(Company)
    create_outbox_entry(tracked)

    assert (
        call("webhook_outbox_worker", "--once")
        == "Queued 0 notification(s).\nSent: 1, Failed: 0\n"
    )


@pytest.mark.django_db
def test_worker_once_only(mock_transport):
    mock_transport()
    assert (
        call("webhook_outbox_worker", "--once", "--only", "poll")
        == "Queued 0 notification(s).\n"
    )
    assert (
        call("webhook_outbox_worker", "--once", "--only", "deliver")
        == "Sent: 0, Failed: 0\n"
    )


def test_worker_loop_arguments():
    with patch("webhook_outbox.runner.run_forever") as run_forever:
        call("webhook_outbox_worker", "--only", "deliver", "--send-interval", "2.5")
    run_forever.assert_called_once_with(
        poll_interval=None, send_interval=2.5, poll=False, deliver=True
    )


def test_worker_rejects_bad_interval():
    with pytest.raises(CommandError):
        call("webhook_outbox_worker", "--poll-interval", "0")


@pytest.mark.django_db
def test_poll_command():
    assert call("webhook_outbox_poll") == "Queued 0 notification(s).\n"


@pytest.mark.django_db
@override_settings(WEBHOOK_OUTBOX={"DELIVERY_BATCH_SIZE": 1})
def test_deliver_command(mock_transport):
    mock_transport()
    tracked = create_tracked_table(Company)
    for pk in ("1", "2", "3"):
        create_outbox_entry(tracked, source_pk=pk)

    assert call("webhook_outbox_deliver") == "Sent: 1, Failed: 0\n"
    assert call("webhook_outbox_deliver", "--limit", "5") == "Sent: 2, Failed: 0\n"
    with pytest.raises(CommandError):
        call("webhook_outbox_deliver", "--limit", "0")


@pytest.mark.django_db
def test_deliver_command_single_entry(mock_transport):
    mock_transport(lambda request: httpx.Response(500, text="no"))
    entry = create_outbox_entry(create_tracked_table(Company))

    assert call("webhook_outbox_deliver", "--entry", str(entry.pk)) == (
        f"Entry #{entry.pk} failed: HTTP 500: no\n"
    )
    with pytest.raises(CommandError):
        call("webhook_outbox_deliver", "--entry", "999999")
    entry.status = NotificationOutbox.Status.SENT
    entry.save()
    assert "already sent" in call("webhook_outbox_deliver", "--entry", str(entry.pk))

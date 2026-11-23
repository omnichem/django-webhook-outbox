from django.core.management.base import BaseCommand, CommandError

from webhook_outbox import runner


class Command(BaseCommand):
    help = (
        "Run the webhook outbox worker loop: poll tracked tables and deliver "
        "pending outbox entries until SIGTERM/SIGINT."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one poll and/or one delivery pass, then exit.",
        )
        parser.add_argument(
            "--only",
            choices=["poll", "deliver"],
            help="Run only the poller or only the delivery.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=None,
            help="Seconds between polls (default: WEBHOOK_OUTBOX['POLL_INTERVAL']).",
        )
        parser.add_argument(
            "--send-interval",
            type=float,
            default=None,
            help="Seconds between deliveries (default: WEBHOOK_OUTBOX['SEND_INTERVAL']).",
        )

    def handle(self, *args, **options):
        only = options["only"]
        do_poll = only in (None, "poll")
        do_deliver = only in (None, "deliver")

        if options["once"]:
            if do_poll:
                queued = runner.poll_once()
                self.stdout.write(f"Queued {queued} notification(s).")
            if do_deliver:
                result = runner.deliver_once()
                self.stdout.write(f"Sent: {result['sent']}, Failed: {result['failed']}")
            return

        try:
            runner.run_forever(
                poll_interval=options["poll_interval"],
                send_interval=options["send_interval"],
                poll=do_poll,
                deliver=do_deliver,
            )
        except ValueError as e:
            raise CommandError(str(e)) from e

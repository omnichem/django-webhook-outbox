from django.core.management.base import BaseCommand, CommandError

from webhook_outbox import runner


class Command(BaseCommand):
    help = "Deliver pending outbox entries once (or one entry with --entry)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Entries to handle (default: WEBHOOK_OUTBOX['DELIVERY_BATCH_SIZE']).",
        )
        parser.add_argument(
            "--entry",
            type=int,
            default=None,
            metavar="PK",
            help="Send this outbox entry now, whatever its retry count.",
        )

    def handle(self, *args, **options):
        if options["entry"] is not None:
            result = runner.deliver_entry(options["entry"])
            if result.get("error") == "Entry not found":
                raise CommandError(f"Outbox entry #{options['entry']} not found")
            if result.get("status") == "already_sent":
                self.stdout.write(f"Entry #{options['entry']} was already sent.")
            elif result["success"]:
                self.stdout.write(f"Entry #{options['entry']} sent.")
            else:
                self.stdout.write(
                    f"Entry #{options['entry']} failed: {result['error']}"
                )
            return

        if options["limit"] is not None and options["limit"] <= 0:
            raise CommandError("--limit must be positive")
        result = runner.deliver_once(limit=options["limit"])
        self.stdout.write(f"Sent: {result['sent']}, Failed: {result['failed']}")

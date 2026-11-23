from django.core.management.base import BaseCommand

from webhook_outbox import runner


class Command(BaseCommand):
    help = "Poll all active tracked tables once and queue outbox entries."

    def handle(self, *args, **options):
        queued = runner.poll_once()
        self.stdout.write(f"Queued {queued} notification(s).")

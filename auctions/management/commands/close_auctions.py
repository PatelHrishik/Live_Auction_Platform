import time

from django.core.management.base import BaseCommand

from auctions import services


class Command(BaseCommand):
    help = (
        "Record results for auctions whose time is up. Correctness never depends on "
        "this running on time (bids check the end time themselves); it only makes "
        "results appear without anyone having to look at the auction first."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running.")
        parser.add_argument("--interval", type=float, default=1.0, help="Seconds between runs.")

    def handle(self, *args, loop: bool, interval: float, **options):
        while True:
            closed = services.close_due_auctions()
            if closed:
                self.stdout.write(f"closed {closed} auction(s)")
            if not loop:
                return
            time.sleep(interval)

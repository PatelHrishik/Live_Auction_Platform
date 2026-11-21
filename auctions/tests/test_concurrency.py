"""Many bidders hitting the same auction at the same instant, on real threads
with separate database connections. These tests check the invariants that
matter, not just "no crash":

  * no lost updates: every accepted bid is in the history, in order
  * the price only ever goes up, by at least the minimum increment
  * the final state equals the last accepted bid
  * closing races with bids and still produces exactly one winner
"""

import random
import threading
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase

from auctions import services
from auctions.models import Auction, Bid
from auctions.services import Reject


def run_concurrently(fns):
    """Start all callables at the same moment; return their results in order."""
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)
    errors = []

    def worker(i, fn):
        try:
            barrier.wait()
            results[i] = fn()
        except Exception as exc:  # surfaced below so the test fails loudly
            errors.append(exc)
        finally:
            connection.close()  # each thread has its own DB connection

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return results


class ConcurrentBiddingTests(TransactionTestCase):
    def assert_history_is_consistent(self, auction_id):
        auction = Auction.objects.get(pk=auction_id)
        bids = list(Bid.objects.filter(auction_id=auction_id).order_by("sequence"))

        self.assertEqual([b.sequence for b in bids], list(range(1, len(bids) + 1)))
        self.assertEqual(auction.bid_count, len(bids))
        if bids:
            self.assertGreaterEqual(bids[0].amount, auction.starting_price)
        for prev, cur in zip(bids, bids[1:]):
            self.assertGreaterEqual(cur.amount, prev.amount + auction.min_increment)
            self.assertNotEqual(cur.bidder_id, prev.bidder_id)
        if bids:
            self.assertEqual(auction.current_price, bids[-1].amount)
            self.assertEqual(auction.leader, bids[-1].bidder_id)
        return auction, bids

    def test_bid_storm_has_no_lost_updates(self):
        auction = services.create_auction(
            title="Storm", starting_price=100, min_increment=5, duration_seconds=3600
        )
        threads, bids_per_thread = 16, 25
        rng = random.Random(42)
        plans = [
            [(f"bidder-{rng.randrange(8)}", rng.randrange(50, 5000)) for _ in range(bids_per_thread)]
            for _ in range(threads)
        ]

        def make_worker(plan):
            return lambda: [services.place_bid(auction.pk, who, amt) for who, amt in plan]

        results = [r for batch in run_concurrently([make_worker(p) for p in plans]) for r in batch]

        auction, bids = self.assert_history_is_consistent(auction.pk)
        accepted = [r for r in results if r.accepted]
        self.assertEqual(len(accepted), len(bids), "an accepted bid is missing from history")
        self.assertGreater(len(bids), 1)
        self.assertEqual(auction.current_price, max(r.bid.amount for r in accepted))
        for r in results:
            if r.reason == Reject.BID_TOO_LOW:
                self.assertIsNotNone(r.min_required)

    def test_same_amount_at_same_instant_only_one_wins(self):
        auction = services.create_auction(
            title="Tie", starting_price=1000, min_increment=1, duration_seconds=3600
        )
        results = run_concurrently(
            [lambda i=i: services.place_bid(auction.pk, f"b{i}", 1000) for i in range(20)]
        )
        self.assertEqual(sum(r.accepted for r in results), 1)
        self.assertEqual(Bid.objects.filter(auction=auction).count(), 1)

    def test_last_second_burst_extends_once_per_bid_and_one_winner(self):
        auction = services.create_auction(
            title="Snipe", starting_price=100, min_increment=10, duration_seconds=3600
        )
        frozen_now = auction.ends_at - timedelta(seconds=1)
        with mock.patch("auctions.clock.now", lambda: frozen_now):
            results = run_concurrently(
                [lambda i=i: services.place_bid(auction.pk, f"b{i}", 100 + i * 10) for i in range(30)]
            )
        auction, bids = self.assert_history_is_consistent(auction.pk)
        self.assertTrue(all(r.accepted or r.reason == Reject.BID_TOO_LOW for r in results))
        self.assertEqual(auction.ends_at, frozen_now + timedelta(seconds=30))

        with mock.patch("auctions.clock.now", lambda: auction.ends_at):
            final = services.get_auction(auction.pk)
        self.assertEqual(final.status, Auction.Status.CLOSED)
        self.assertEqual(final.winner, bids[-1].bidder_id)
        self.assertEqual(final.final_price, bids[-1].amount)

    def test_bids_racing_the_close_cannot_change_the_winner(self):
        auction = services.create_auction(
            title="Race", starting_price=100, min_increment=10, duration_seconds=3600
        )
        services.place_bid(auction.pk, "alice", 100)
        version_before = Auction.objects.get(pk=auction.pk).version

        at_end = auction.ends_at
        with mock.patch("auctions.clock.now", lambda: at_end):
            fns = [lambda i=i: services.place_bid(auction.pk, f"late{i}", 10_000 + i) for i in range(20)]
            fns += [lambda: services.close_if_due(auction.pk)[1] for _ in range(5)]
            fns += [services.close_due_auctions for _ in range(5)]
            results = run_concurrently(fns)

        bid_results, close_results = results[:20], results[20:]
        self.assertTrue(all(r.reason == Reject.AUCTION_CLOSED for r in bid_results))
        # Whoever gets the lock first records the close: a close worker *or* a
        # late bid (bids finalize an expired auction themselves). Either way it
        # must happen exactly once, which the version check below proves.
        self.assertLessEqual(sum(int(c) for c in close_results), 1, "closed more than once")

        final = Auction.objects.get(pk=auction.pk)
        self.assertEqual(final.status, Auction.Status.CLOSED)
        self.assertEqual(final.winner, "alice")
        self.assertEqual(final.version, version_before + 1, "close was recorded more than once")
        self.assertEqual(final.bid_count, 1)

    def test_concurrent_retries_with_same_key_create_one_bid(self):
        auction = services.create_auction(
            title="Retry", starting_price=100, min_increment=10, duration_seconds=3600
        )
        results = run_concurrently(
            [lambda: services.place_bid(auction.pk, "alice", 100, "same-key") for _ in range(15)]
        )
        self.assertTrue(all(r.accepted for r in results))
        self.assertEqual(sum(not r.duplicate for r in results), 1)
        self.assertEqual(Bid.objects.filter(auction=auction).count(), 1)

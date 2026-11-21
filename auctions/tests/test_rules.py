"""Single-threaded tests of every bidding rule, using a frozen clock."""

from datetime import timedelta

from django.test import TestCase

from auctions import services
from auctions.models import Auction, Bid
from auctions.services import Reject

from .helpers import install_fake_clock


class BidRulesTests(TestCase):
    def setUp(self):
        self.clock = install_fake_clock(self)
        self.auction = services.create_auction(
            title="Lamp", starting_price=1000, min_increment=100, duration_seconds=300
        )

    def bid(self, bidder, amount, key=None):
        return services.place_bid(self.auction.pk, bidder, amount, key)

    def reload(self):
        return Auction.objects.get(pk=self.auction.pk)

    def test_first_bid_must_meet_starting_price(self):
        r = self.bid("alice", 999)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, Reject.BID_TOO_LOW)
        self.assertEqual(r.min_required, 1000)

        r = self.bid("alice", 1000)
        self.assertTrue(r.accepted)
        self.assertEqual(self.reload().current_price, 1000)
        self.assertEqual(self.reload().leader, "alice")

    def test_next_bid_must_beat_price_by_min_increment(self):
        self.bid("alice", 1000)
        r = self.bid("bob", 1099)
        self.assertEqual(r.reason, Reject.BID_TOO_LOW)
        self.assertEqual(r.min_required, 1100)
        self.assertTrue(self.bid("bob", 1100).accepted)

    def test_equal_bid_loses_to_the_earlier_one(self):
        self.bid("alice", 1000)
        r = self.bid("bob", 1000)
        self.assertEqual(r.reason, Reject.BID_TOO_LOW)
        self.assertEqual(self.reload().leader, "alice")

    def test_leader_cannot_outbid_themselves(self):
        self.bid("alice", 1000)
        r = self.bid("alice", 5000)
        self.assertEqual(r.reason, Reject.ALREADY_LEADING)
        self.assertEqual(self.reload().current_price, 1000)

    def test_rejected_bids_change_nothing(self):
        self.bid("alice", 1000)
        before = self.reload()
        self.bid("bob", 1)
        self.bid("alice", 9999)
        after = self.reload()
        self.assertEqual(
            (before.current_price, before.leader, before.version, before.bid_count),
            (after.current_price, after.leader, after.version, after.bid_count),
        )
        self.assertEqual(Bid.objects.count(), 1)

    def test_bid_exactly_at_end_time_is_rejected_and_auction_is_closed(self):
        self.bid("alice", 1000)
        self.clock.set(self.auction.ends_at)
        r = self.bid("bob", 5000)
        self.assertEqual(r.reason, Reject.AUCTION_CLOSED)
        a = self.reload()
        self.assertEqual(a.status, Auction.Status.CLOSED)
        self.assertEqual(a.winner, "alice")
        self.assertEqual(a.final_price, 1000)

    def test_invalid_input_raises(self):
        for bidder, amount in [("", 1000), ("alice", 0), ("alice", -5), ("alice", 10.5), ("alice", True)]:
            with self.assertRaises(ValueError):
                self.bid(bidder, amount)

    def test_unknown_auction(self):
        with self.assertRaises(services.AuctionNotFound):
            services.place_bid(999_999, "alice", 1000)


class ExtensionTests(TestCase):
    def setUp(self):
        self.clock = install_fake_clock(self)
        self.auction = services.create_auction(
            title="Watch", starting_price=100, min_increment=10, duration_seconds=120
        )
        self.end = self.auction.ends_at

    def reload(self):
        return Auction.objects.get(pk=self.auction.pk)

    def test_bid_with_more_than_30s_left_does_not_extend(self):
        self.clock.set(self.end - timedelta(seconds=31))
        r = services.place_bid(self.auction.pk, "alice", 100)
        self.assertFalse(r.extended)
        self.assertEqual(self.reload().ends_at, self.end)

    def test_bid_in_last_30s_extends_to_now_plus_30s(self):
        self.clock.set(self.end - timedelta(seconds=10))
        r = services.place_bid(self.auction.pk, "alice", 100)
        self.assertTrue(r.extended)
        self.assertEqual(self.reload().ends_at, self.clock() + timedelta(seconds=30))
        self.assertEqual(r.bid.extended_ends_at, self.clock() + timedelta(seconds=30))

    def test_bid_with_one_millisecond_left_still_counts_and_extends(self):
        self.clock.set(self.end - timedelta(milliseconds=1))
        r = services.place_bid(self.auction.pk, "alice", 100)
        self.assertTrue(r.accepted)
        self.assertTrue(r.extended)

    def test_bidding_war_keeps_extending_and_ends_30s_after_last_bid(self):
        bidders = ["alice", "bob"]
        amount = 100
        for i in range(10):
            a = self.reload()
            self.clock.set(a.ends_at - timedelta(seconds=5))  # always bid with 5s left
            r = services.place_bid(self.auction.pk, bidders[i % 2], amount)
            self.assertTrue(r.accepted, r.reason)
            amount += 10
        last_bid_time = self.clock()
        a = self.reload()
        self.assertEqual(a.ends_at, last_bid_time + timedelta(seconds=30))

        # Still open 1s before the extended end; closed exactly at it.
        self.clock.set(a.ends_at - timedelta(seconds=1))
        self.assertEqual(services.get_auction(a.pk).status, Auction.Status.OPEN)
        self.clock.set(a.ends_at)
        a = services.get_auction(a.pk)
        self.assertEqual(a.status, Auction.Status.CLOSED)
        self.assertEqual(a.winner, "bob")
        self.assertEqual(a.final_price, 190)


class ClosingTests(TestCase):
    def setUp(self):
        self.clock = install_fake_clock(self)

    def make(self, **kw):
        defaults = dict(title="Vase", starting_price=500, min_increment=50, duration_seconds=60)
        return services.create_auction(**{**defaults, **kw})

    def test_close_with_no_bids_has_no_winner(self):
        a = self.make()
        self.clock.advance(seconds=61)
        a = services.get_auction(a.pk)
        self.assertEqual(a.status, Auction.Status.CLOSED)
        self.assertIsNone(a.winner)
        self.assertIsNone(a.final_price)

    def test_close_happens_once_and_result_never_changes(self):
        a = self.make()
        services.place_bid(a.pk, "alice", 500)
        services.place_bid(a.pk, "bob", 600)
        self.clock.advance(seconds=61)

        self.assertEqual(services.close_due_auctions(), 1)
        closed = Auction.objects.get(pk=a.pk)
        self.assertEqual(services.close_due_auctions(), 0)
        _, closed_again = services.close_if_due(a.pk)
        self.assertFalse(closed_again)

        # Late bids bounce and nothing about the result moves.
        self.assertEqual(services.place_bid(a.pk, "carol", 10_000).reason, Reject.AUCTION_CLOSED)
        final = Auction.objects.get(pk=a.pk)
        self.assertEqual(final.winner, "bob")
        self.assertEqual(final.final_price, 600)
        self.assertEqual(final.version, closed.version)

    def test_close_job_ignores_auctions_still_running(self):
        self.make(duration_seconds=60)
        self.clock.advance(seconds=59)
        self.assertEqual(services.close_due_auctions(), 0)


class IdempotencyTests(TestCase):
    def setUp(self):
        install_fake_clock(self)
        self.a = services.create_auction(
            title="Chair", starting_price=100, min_increment=10, duration_seconds=60
        )

    def test_retry_with_same_key_is_not_a_second_bid(self):
        r1 = services.place_bid(self.a.pk, "alice", 100, "k-1")
        r2 = services.place_bid(self.a.pk, "alice", 100, "k-1")
        self.assertTrue(r1.accepted and r2.accepted)
        self.assertFalse(r1.duplicate)
        self.assertTrue(r2.duplicate)
        self.assertEqual(r1.bid.pk, r2.bid.pk)
        self.assertEqual(Bid.objects.filter(auction=self.a).count(), 1)

    def test_retry_still_succeeds_after_being_outbid(self):
        services.place_bid(self.a.pk, "alice", 100, "k-1")
        services.place_bid(self.a.pk, "bob", 200, "k-2")
        r = services.place_bid(self.a.pk, "alice", 100, "k-1")
        self.assertTrue(r.duplicate)
        self.assertEqual(Auction.objects.get(pk=self.a.pk).leader, "bob")

    def test_same_key_different_bid_is_rejected(self):
        services.place_bid(self.a.pk, "alice", 100, "k-1")
        r = services.place_bid(self.a.pk, "alice", 500, "k-1")
        self.assertEqual(r.reason, Reject.IDEMPOTENCY_CONFLICT)

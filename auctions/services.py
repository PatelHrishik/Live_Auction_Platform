"""Auction rules. This module is the only place auction state changes.

Correctness rests on three rules:

1. One writer at a time per auction. Every change (a bid or the close) runs in
   a transaction that first locks the auction row (`select_for_update`). Two
   bids on the same auction can never interleave, so there are no lost updates
   and no "two people both won at $500".

2. Time decides, not timers. A bid is rejected if `now >= ends_at`, checked
   inside the lock. The close job only *records* a result that is already
   fixed: once `ends_at` passes, no bid can change the leader. A late or
   missed close timer can therefore never let a bid sneak in.

3. Closing takes the same lock as bidding, and only closes once. The winner is
   whoever leads at `ends_at`. That is exactly one bidder, or nobody if there
   were no bids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from . import clock
from .models import Auction, Bid

MAX_AMOUNT = 10**15
MAX_DURATION_SECONDS = 7 * 24 * 3600


class AuctionNotFound(Exception):
    pass


class Reject:
    """Reasons a bid can be rejected (returned to clients as-is)."""

    AUCTION_CLOSED = "AUCTION_CLOSED"
    BID_TOO_LOW = "BID_TOO_LOW"
    ALREADY_LEADING = "ALREADY_LEADING"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


@dataclass(frozen=True)
class BidResult:
    accepted: bool
    auction: Auction
    reason: str | None = None
    bid: Bid | None = None
    extended: bool = False
    duplicate: bool = False  # a retry of an already-accepted bid
    min_required: int | None = None


def extension_window() -> timedelta:
    return timedelta(seconds=settings.AUCTION_EXTENSION_SECONDS)


def minimum_next_bid(auction: Auction) -> int:
    if auction.current_price is None:
        return auction.starting_price
    return auction.current_price + auction.min_increment


def create_auction(
    *, title: str, starting_price: int, min_increment: int, duration_seconds: int, seller=None
) -> Auction:
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise ValueError("title must be 1-200 characters")
    if not 1 <= starting_price <= MAX_AMOUNT:
        raise ValueError("starting_price must be a positive integer")
    if not 1 <= min_increment <= MAX_AMOUNT:
        raise ValueError("min_increment must be a positive integer")
    if not 1 <= duration_seconds <= MAX_DURATION_SECONDS:
        raise ValueError(f"duration_seconds must be between 1 and {MAX_DURATION_SECONDS}")

    ends_at = clock.now() + timedelta(seconds=duration_seconds)
    return Auction.objects.create(
        title=title,
        seller=seller,
        starting_price=starting_price,
        min_increment=min_increment,
        original_ends_at=ends_at,
        ends_at=ends_at,
    )


def place_bid(
    auction_id: int, bidder_id: str, amount: int, idempotency_key: str | None = None
) -> BidResult:
    bidder_id = (bidder_id or "").strip()
    if not bidder_id or len(bidder_id) > 64:
        raise ValueError("bidder_id must be 1-64 characters")
    if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= MAX_AMOUNT:
        raise ValueError("amount must be a positive integer (minor units, e.g. cents)")
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 100:
        raise ValueError("idempotency_key must be 1-100 characters")

    with transaction.atomic():
        # Rule 1: take the per-auction lock before reading anything.
        try:
            auction = Auction.objects.select_for_update().get(pk=auction_id)
        except Auction.DoesNotExist:
            raise AuctionNotFound(auction_id) from None

        # A retry of a bid we already accepted returns the original result
        # instead of placing it twice.
        if idempotency_key:
            prior = auction.bids.filter(idempotency_key=idempotency_key).first()
            if prior is not None:
                if prior.bidder_id != bidder_id or prior.amount != amount:
                    return BidResult(
                        accepted=False, auction=auction, reason=Reject.IDEMPOTENCY_CONFLICT
                    )
                return BidResult(
                    accepted=True,
                    auction=auction,
                    bid=prior,
                    duplicate=True,
                    extended=prior.extended_ends_at is not None,
                )

        # Clock is read *after* acquiring the lock, so the time we judge the
        # bid by is the time it actually gets its turn.
        now = clock.now()

        # Rule 2: time decides.
        if auction.status == Auction.Status.CLOSED or now >= auction.ends_at:
            _finalize(auction, now)
            return BidResult(accepted=False, auction=auction, reason=Reject.AUCTION_CLOSED)

        if bidder_id == auction.leader:
            return BidResult(accepted=False, auction=auction, reason=Reject.ALREADY_LEADING)

        min_required = minimum_next_bid(auction)
        if amount < min_required:
            return BidResult(
                accepted=False,
                auction=auction,
                reason=Reject.BID_TOO_LOW,
                min_required=min_required,
            )

        # Soft close: a bid in the last 30s guarantees 30s more for others to answer.
        extended = False
        if auction.ends_at - now <= extension_window():
            new_end = now + extension_window()
            if new_end > auction.ends_at:
                auction.ends_at = new_end
                extended = True

        auction.current_price = amount
        auction.leader = bidder_id
        auction.bid_count += 1
        auction.version += 1
        auction.save(
            update_fields=["current_price", "leader", "ends_at", "bid_count", "version"]
        )
        bid = Bid.objects.create(
            auction=auction,
            sequence=auction.bid_count,
            bidder_id=bidder_id,
            amount=amount,
            placed_at=now,
            idempotency_key=idempotency_key or None,
            extended_ends_at=auction.ends_at if extended else None,
        )

    return BidResult(accepted=True, auction=auction, bid=bid, extended=extended)


def _finalize(auction: Auction, now) -> bool:
    """Record the result of an auction whose time is up. Caller holds the lock.

    Idempotent: returns True only for the call that actually closed it.
    """
    if auction.status == Auction.Status.CLOSED or now < auction.ends_at:
        return False
    auction.status = Auction.Status.CLOSED
    auction.winner = auction.leader or None
    auction.final_price = auction.current_price
    auction.closed_at = now
    auction.version += 1
    auction.save(update_fields=["status", "winner", "final_price", "closed_at", "version"])
    return True


def close_if_due(auction_id: int) -> tuple[Auction, bool]:
    """Close the auction if its time is up. Returns (auction, closed_by_this_call)."""
    with transaction.atomic():
        try:
            auction = Auction.objects.select_for_update().get(pk=auction_id)
        except Auction.DoesNotExist:
            raise AuctionNotFound(auction_id) from None
        closed = _finalize(auction, clock.now())
        return auction, closed


def close_due_auctions() -> int:
    """Close every auction whose time is up. Safe to run from many workers at once:
    each close re-checks under the lock, so an auction is only closed once."""
    due = list(
        Auction.objects.filter(
            status=Auction.Status.OPEN, ends_at__lte=clock.now()
        ).values_list("pk", flat=True)
    )
    return sum(1 for auction_id in due if close_if_due(auction_id)[1])


def get_auction(auction_id: int) -> Auction:
    """Read an auction, recording its result first if its time is already up."""
    try:
        auction = Auction.objects.get(pk=auction_id)
    except Auction.DoesNotExist:
        raise AuctionNotFound(auction_id) from None
    if auction.status == Auction.Status.OPEN and clock.now() >= auction.ends_at:
        auction, _ = close_if_due(auction_id)
    return auction

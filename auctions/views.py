"""HTTP + Server-Sent Events API. Thin: parse input, call services, shape JSON.

Status codes:
  201 bid accepted / auction created     200 duplicate retry of an accepted bid
  400 malformed input                    404 unknown auction
  401 not logged in                      403 logged in, but wrong role
  409 bid rejected by an auction rule (body has `reason`)

Identity comes from the session. Only sellers may create auctions and only
bidders may bid; the bidder is always the logged-in user, never a value from
the request body. Writes are CSRF-protected (send the `X-CSRFToken` header).
"""

import json
import time
from functools import wraps

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import close_old_connections
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from . import clock, services
from .forms import SignupForm
from .models import Auction, Bid, Profile, role_of

STREAM_POLL_SECONDS = 0.25
STREAM_HEARTBEAT_SECONDS = 15


class BadRequest(Exception):
    pass


# --- serialization ---------------------------------------------------------------


def serialize_auction(a: Auction) -> dict:
    now = clock.now()
    is_open = a.status == Auction.Status.OPEN
    return {
        "id": a.pk,
        "title": a.title,
        "seller": a.seller.username if a.seller_id else None,
        "status": a.status,
        "starting_price": a.starting_price,
        "min_increment": a.min_increment,
        "current_price": a.current_price,
        "leader": a.leader or None,
        "min_next_bid": services.minimum_next_bid(a) if is_open else None,
        "bid_count": a.bid_count,
        "original_ends_at": a.original_ends_at.isoformat(),
        "ends_at": a.ends_at.isoformat(),
        "remaining_ms": max(0, int((a.ends_at - now).total_seconds() * 1000)) if is_open else 0,
        "winner": a.winner,
        "final_price": a.final_price,
        "closed_at": a.closed_at.isoformat() if a.closed_at else None,
        "version": a.version,
        # Lets clients correct for their own clock being off when counting down.
        "server_time": now.isoformat(),
    }


def serialize_bid(b: Bid) -> dict:
    return {
        "sequence": b.sequence,
        "bidder_id": b.bidder_id,
        "amount": b.amount,
        "placed_at": b.placed_at.isoformat(),
        "extended_ends_at": b.extended_ends_at.isoformat() if b.extended_ends_at else None,
    }


# --- helpers -------------------------------------------------------------------


def _error(status: int, code: str, message: str, **extra) -> JsonResponse:
    return JsonResponse({"error": code, "message": message, **extra}, status=status)


def _json_body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BadRequest("body must be valid JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("body must be a JSON object")
    return data


def _int(data: dict, name: str, default=None) -> int:
    value = data.get(name, default)
    # Floats are rejected on purpose: money is integer minor units (cents).
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"'{name}' must be an integer")
    return value


def _str(data: dict, name: str, required: bool = True) -> str | None:
    value = data.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"'{name}' must be a non-empty string")
    return value.strip()


def _require_role(role: str):
    """Allow GET to anyone; POST only to a logged-in user with `role`.

    Returns JSON 401/403 instead of redirecting to the login page, since
    callers of the API are scripts and fetch(), not browsers following links.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method == "POST":
                user_role = role_of(request.user)
                if user_role is None:
                    return _error(401, "NOT_AUTHENTICATED", "log in first")
                if user_role != role:
                    return _error(
                        403, "FORBIDDEN", f"only {role.lower()}s can do this"
                    )
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


# --- pages -----------------------------------------------------------------------


@require_GET
@login_required
@ensure_csrf_cookie
def index(request):
    return render(request, "auctions/index.html", {"role": role_of(request.user)})


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("index")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.save())
        return redirect("index")
    return render(request, "registration/signup.html", {"form": form})


# --- API -------------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
@_require_role(Profile.Role.SELLER)
def auctions_collection(request):
    if request.method == "GET":
        services.close_due_auctions()
        auctions = Auction.objects.select_related("seller").order_by("-created_at")[:50]
        return JsonResponse({"auctions": [serialize_auction(a) for a in auctions]})

    try:
        data = _json_body(request)
        auction = services.create_auction(
            title=_str(data, "title"),
            starting_price=_int(data, "starting_price"),
            min_increment=_int(data, "min_increment", default=1),
            duration_seconds=_int(data, "duration_seconds"),
            seller=request.user,
        )
    except (BadRequest, ValueError) as exc:
        return _error(400, "INVALID_REQUEST", str(exc))
    return JsonResponse(serialize_auction(auction), status=201)


@require_GET
def auction_detail(request, auction_id: int):
    try:
        auction = services.get_auction(auction_id)
    except services.AuctionNotFound:
        return _error(404, "NOT_FOUND", "auction not found")
    return JsonResponse(serialize_auction(auction))


@require_http_methods(["GET", "POST"])
@_require_role(Profile.Role.BIDDER)
def auction_bids(request, auction_id: int):
    if request.method == "GET":
        if not Auction.objects.filter(pk=auction_id).exists():
            return _error(404, "NOT_FOUND", "auction not found")
        bids = Bid.objects.filter(auction_id=auction_id).order_by("-sequence")[:100]
        return JsonResponse({"bids": [serialize_bid(b) for b in bids]})

    try:
        data = _json_body(request)
        key = request.headers.get("Idempotency-Key") or _str(
            data, "idempotency_key", required=False
        )
        result = services.place_bid(
            auction_id,
            # Always the logged-in user; any bidder_id in the body is ignored.
            bidder_id=request.user.username,
            amount=_int(data, "amount"),
            idempotency_key=key,
        )
    except (BadRequest, ValueError) as exc:
        return _error(400, "INVALID_REQUEST", str(exc))
    except services.AuctionNotFound:
        return _error(404, "NOT_FOUND", "auction not found")

    auction_json = serialize_auction(result.auction)
    if not result.accepted:
        extra = {"auction": auction_json}
        if result.min_required is not None:
            extra["min_required"] = result.min_required
        return _error(409, result.reason, _REJECT_MESSAGES[result.reason], **extra)

    return JsonResponse(
        {
            "accepted": True,
            "duplicate": result.duplicate,
            "extended": result.extended,
            "bid": serialize_bid(result.bid),
            "auction": auction_json,
        },
        status=200 if result.duplicate else 201,
    )


_REJECT_MESSAGES = {
    services.Reject.AUCTION_CLOSED: "the auction has ended",
    services.Reject.BID_TOO_LOW: "bid is below the minimum next bid",
    services.Reject.ALREADY_LEADING: "you are already the highest bidder",
    services.Reject.IDEMPOTENCY_CONFLICT: "idempotency key was already used for a different bid",
}


@require_GET
def auction_stream(request, auction_id: int):
    """Server-Sent Events: pushes the auction state every time `version` changes.

    This demo polls the database every 250 ms per connection. At scale this is
    replaced by pub/sub fan-out (see README design note); the client contract
    (versioned snapshots) stays the same.
    """
    if not Auction.objects.filter(pk=auction_id).exists():
        return _error(404, "NOT_FOUND", "auction not found")

    def events():
        last_version = None
        last_sent = time.monotonic()
        try:
            yield "retry: 1000\n\n"
            while True:
                auction = services.get_auction(auction_id)
                if auction.version != last_version:
                    last_version = auction.version
                    last_sent = time.monotonic()
                    payload = json.dumps(serialize_auction(auction))
                    yield f"id: {auction.version}\nevent: auction\ndata: {payload}\n\n"
                    if auction.status == Auction.Status.CLOSED:
                        return
                elif time.monotonic() - last_sent > STREAM_HEARTBEAT_SECONDS:
                    last_sent = time.monotonic()
                    yield ": heartbeat\n\n"
                time.sleep(STREAM_POLL_SECONDS)
        finally:
            close_old_connections()

    response = StreamingHttpResponse(events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response

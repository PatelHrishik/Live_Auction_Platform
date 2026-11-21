from django.conf import settings
from django.db import models
from django.db.models import Q


class Profile(models.Model):
    """Which kind of account a user is. Sellers create auctions; bidders bid."""

    class Role(models.TextChoices):
        BIDDER = "BIDDER", "Bidder"
        SELLER = "SELLER", "Seller"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    role = models.CharField(max_length=6, choices=Role.choices, default=Role.BIDDER)

    def __str__(self) -> str:
        return f"{self.user.username} ({self.role})"


def role_of(user) -> str | None:
    """The user's role, or None if anonymous. Users without a profile (e.g. made
    with createsuperuser) count as bidders."""
    if not user.is_authenticated:
        return None
    profile = getattr(user, "profile", None)
    return profile.role if profile else Profile.Role.BIDDER


class Auction(models.Model):
    """One auction. All money values are integers in minor units (e.g. cents)."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    title = models.CharField(max_length=200)
    # Who created the auction. Nullable so auctions made outside the web app
    # (tests, scripts, older rows) stay valid.
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auctions_selling",
    )
    starting_price = models.PositiveBigIntegerField(help_text="Lowest allowed first bid.")
    min_increment = models.PositiveBigIntegerField(
        default=1, help_text="Each bid must beat the current price by at least this much."
    )

    # Live state, only ever changed inside services.place_bid / services._finalize
    # while holding the auction's lock.
    current_price = models.PositiveBigIntegerField(null=True, blank=True)
    leader = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    original_ends_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    bid_count = models.PositiveIntegerField(default=0)
    # Bumped on every change a watcher must see. Clients ignore anything older
    # than the newest version they already have.
    version = models.PositiveBigIntegerField(default=0)

    # Result, written exactly once when the auction closes.
    winner = models.CharField(max_length=64, null=True, blank=True)
    final_price = models.PositiveBigIntegerField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "ends_at"], name="auction_status_ends_at")]
        constraints = [
            models.CheckConstraint(
                condition=Q(min_increment__gte=1), name="auction_min_increment_positive"
            ),
            models.CheckConstraint(
                condition=Q(status="OPEN") | Q(closed_at__isnull=False),
                name="auction_closed_has_closed_at",
            ),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.title} ({self.status})"


class Bid(models.Model):
    """An accepted bid. Append-only: the full history can rebuild the auction."""

    auction = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="bids")
    sequence = models.PositiveIntegerField(help_text="1, 2, 3... in acceptance order.")
    bidder_id = models.CharField(max_length=64)
    amount = models.PositiveBigIntegerField()
    placed_at = models.DateTimeField(help_text="Server time the bid was accepted.")
    idempotency_key = models.CharField(max_length=100, null=True, blank=True)
    extended_ends_at = models.DateTimeField(
        null=True, blank=True, help_text="New end time if this bid extended the auction."
    )

    class Meta:
        ordering = ["auction_id", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["auction", "sequence"], name="bid_unique_sequence"),
            models.UniqueConstraint(
                fields=["auction", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="bid_unique_idempotency_key",
            ),
        ]

    def __str__(self) -> str:
        return f"Auction {self.auction_id} bid #{self.sequence}: {self.bidder_id} {self.amount}"

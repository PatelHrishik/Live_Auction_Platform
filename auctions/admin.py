from django.contrib import admin

from .models import Auction, Bid, Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role")
    list_filter = ("role",)


class BidInline(admin.TabularInline):
    model = Bid
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in Bid._meta.fields]


@admin.register(Auction)
class AuctionAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "seller", "status", "current_price", "leader", "ends_at", "winner")
    list_filter = ("status",)
    inlines = [BidInline]
    # State changes must go through services.py (locking + rules), never the admin.
    readonly_fields = (
        "current_price", "leader", "status", "ends_at", "original_ends_at", "bid_count",
        "version", "winner", "final_price", "closed_at",
    )


@admin.register(Bid)
class BidAdmin(admin.ModelAdmin):
    list_display = ("auction", "sequence", "bidder_id", "amount", "placed_at")
    readonly_fields = [f.name for f in Bid._meta.fields]

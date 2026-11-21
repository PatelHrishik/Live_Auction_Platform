from django.urls import path

from . import views

urlpatterns = [
    path("auctions", views.auctions_collection, name="auctions"),
    path("auctions/<int:auction_id>", views.auction_detail, name="auction-detail"),
    path("auctions/<int:auction_id>/bids", views.auction_bids, name="auction-bids"),
    path("auctions/<int:auction_id>/stream", views.auction_stream, name="auction-stream"),
]

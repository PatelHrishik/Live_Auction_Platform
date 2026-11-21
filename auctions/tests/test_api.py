"""HTTP contract: status codes, error shapes, idempotency header, live stream."""

import json

from django.contrib.auth.models import User
from django.test import TestCase

from auctions.models import Profile

from .helpers import install_fake_clock


def make_user(username, role=Profile.Role.BIDDER):
    user = User.objects.create_user(username, password="pw-for-tests-123")
    Profile.objects.create(user=user, role=role)
    return user


class ApiTests(TestCase):
    def setUp(self):
        self.clock = install_fake_clock(self)
        self.users = {
            "seller": make_user("seller", Profile.Role.SELLER),
            "alice": make_user("alice"),
            "bob": make_user("bob"),
        }

    def as_user(self, name):
        self.client.force_login(self.users[name])

    def post(self, url, body, **headers):
        return self.client.post(url, json.dumps(body), content_type="application/json", **headers)

    def bid(self, url, who, amount, **headers):
        """Bid as `who`. The bidder comes from the session, not the body."""
        self.as_user(who)
        return self.post(url, {"amount": amount}, **headers)

    def create(self, **overrides):
        self.as_user("seller")
        body = {"title": "Guitar", "starting_price": 5000, "min_increment": 500, "duration_seconds": 120}
        r = self.post("/api/auctions", {**body, **overrides})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def test_create_and_get(self):
        a = self.create()
        self.assertEqual(a["status"], "OPEN")
        self.assertEqual(a["min_next_bid"], 5000)
        self.assertEqual(a["seller"], "seller")
        self.assertEqual(a["remaining_ms"], 120_000)
        r = self.client.get(f"/api/auctions/{a['id']}")
        self.assertEqual(r.json()["title"], "Guitar")
        self.assertEqual(len(self.client.get("/api/auctions").json()["auctions"]), 1)

    def test_create_validation(self):
        self.as_user("seller")
        for bad in [
            {"title": "", "starting_price": 1, "duration_seconds": 10},
            {"title": "x", "starting_price": 1.5, "duration_seconds": 10},
            {"title": "x", "starting_price": 1, "duration_seconds": 0},
            {"title": "x", "starting_price": 1, "min_increment": 0, "duration_seconds": 10},
        ]:
            r = self.post("/api/auctions", bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.json()["error"], "INVALID_REQUEST")
        r = self.client.post("/api/auctions", "not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_bid_accept_and_reject(self):
        a = self.create()
        url = f"/api/auctions/{a['id']}/bids"

        r = self.bid(url, "alice", 5000)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["auction"]["leader"], "alice")
        self.assertEqual(r.json()["auction"]["min_next_bid"], 5500)

        r = self.bid(url, "bob", 5200)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "BID_TOO_LOW")
        self.assertEqual(r.json()["min_required"], 5500)

        r = self.bid(url, "alice", 9000)
        self.assertEqual(r.json()["error"], "ALREADY_LEADING")

        r = self.bid(url, "bob", 55.5)
        self.assertEqual(r.status_code, 400)

        bids = self.client.get(url).json()["bids"]
        self.assertEqual(len(bids), 1)

    def test_idempotency_key_header(self):
        a = self.create()
        url = f"/api/auctions/{a['id']}/bids"
        r1 = self.bid(url, "alice", 5000, HTTP_IDEMPOTENCY_KEY="abc")
        r2 = self.bid(url, "alice", 5000, HTTP_IDEMPOTENCY_KEY="abc")
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertTrue(r2.json()["duplicate"])
        self.assertEqual(r1.json()["bid"]["sequence"], r2.json()["bid"]["sequence"])

    def test_late_bid_extends_then_close(self):
        a = self.create(duration_seconds=60)
        url = f"/api/auctions/{a['id']}/bids"
        self.clock.advance(seconds=50)
        r = self.bid(url, "alice", 5000)
        self.assertTrue(r.json()["extended"])
        self.assertEqual(r.json()["auction"]["remaining_ms"], 30_000)

        self.clock.advance(seconds=30)
        r = self.bid(url, "bob", 9000)
        self.assertEqual(r.json()["error"], "AUCTION_CLOSED")
        detail = self.client.get(f"/api/auctions/{a['id']}").json()
        self.assertEqual((detail["status"], detail["winner"], detail["final_price"]), ("CLOSED", "alice", 5000))

    def test_not_found(self):
        self.assertEqual(self.client.get("/api/auctions/404").status_code, 404)
        self.assertEqual(self.bid("/api/auctions/404/bids", "alice", 1).status_code, 404)
        self.assertEqual(self.client.get("/api/auctions/404/stream").status_code, 404)

    def test_stream_sends_final_state_and_ends_when_closed(self):
        a = self.create(duration_seconds=10)
        r = self.bid(f"/api/auctions/{a['id']}/bids", "alice", 5000)
        # The bid landed with 10s left, so it pushed the end to now + 30s.
        self.assertTrue(r.json()["extended"])
        self.clock.advance(seconds=30)
        r = self.client.get(f"/api/auctions/{a['id']}/stream")
        self.assertEqual(r["Content-Type"], "text/event-stream")
        body = b"".join(r.streaming_content).decode()
        data = json.loads(body.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(data["status"], "CLOSED")
        self.assertEqual(data["winner"], "alice")


class AuthTests(TestCase):
    """Who may do what. Identity always comes from the session."""

    def setUp(self):
        install_fake_clock(self)
        self.seller = make_user("seller", Profile.Role.SELLER)
        self.alice = make_user("alice")
        self.client.force_login(self.seller)
        r = self.client.post(
            "/api/auctions",
            json.dumps({"title": "Lamp", "starting_price": 100, "duration_seconds": 60}),
            content_type="application/json",
        )
        self.auction = r.json()
        self.bids_url = f"/api/auctions/{self.auction['id']}/bids"
        self.client.logout()

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_anonymous_cannot_create_or_bid(self):
        r = self.post("/api/auctions", {"title": "x", "starting_price": 1, "duration_seconds": 10})
        self.assertEqual((r.status_code, r.json()["error"]), (401, "NOT_AUTHENTICATED"))
        r = self.post(self.bids_url, {"amount": 100})
        self.assertEqual((r.status_code, r.json()["error"]), (401, "NOT_AUTHENTICATED"))

    def test_bidder_cannot_create(self):
        self.client.force_login(self.alice)
        r = self.post("/api/auctions", {"title": "x", "starting_price": 1, "duration_seconds": 10})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "FORBIDDEN"))

    def test_seller_cannot_bid(self):
        self.client.force_login(self.seller)
        r = self.post(self.bids_url, {"amount": 100})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "FORBIDDEN"))

    def test_bidder_is_the_session_user_not_the_body(self):
        self.client.force_login(self.alice)
        r = self.post(self.bids_url, {"bidder_id": "mallory", "amount": 100})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["auction"]["leader"], "alice")

    def test_reads_are_public(self):
        self.assertEqual(self.client.get("/api/auctions").status_code, 200)
        self.assertEqual(self.client.get(self.bids_url).status_code, 200)

    def test_writes_require_csrf_token(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.alice)
        r = client.post(self.bids_url, json.dumps({"amount": 100}), content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_index_requires_login(self):
        r = self.client.get("/")
        self.assertRedirects(r, "/login/?next=/", fetch_redirect_response=False)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_signup_creates_profile_and_logs_in(self):
        r = self.client.post("/signup/", {
            "username": "newseller", "password1": "a-good-passphrase", "password2": "a-good-passphrase",
            "role": "SELLER",
        })
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        user = User.objects.get(username="newseller")
        self.assertEqual(user.profile.role, Profile.Role.SELLER)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_login_and_logout(self):
        r = self.client.post("/login/", {"username": "alice", "password": "pw-for-tests-123"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        r = self.client.post("/logout/")
        self.assertRedirects(r, "/login/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 302)

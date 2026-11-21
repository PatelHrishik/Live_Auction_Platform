"""Fire many simultaneous bids at a running server, then verify the result.

    python scripts/bid_storm.py                       # new auction, 200 bids, 40 workers
    python scripts/bid_storm.py --bids 1000 --workers 100 --url http://127.0.0.1:8000

Standard library only. Checks the same invariants as the concurrency tests,
but end-to-end over HTTP against the real server.

The API needs a logged-in session, so the script signs up one seller and
`--bidders` bidder accounts (random names) through the normal /signup/ page.
"""

import argparse
import http.cookiejar
import json
import random
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


class Session:
    """One logged-in user: its own cookie jar and CSRF token."""

    def __init__(self, root, username, role):
        self.root = root
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.open(root + "/signup/", timeout=60).read()  # sets csrftoken cookie
        password = secrets.token_urlsafe(16)
        form = urllib.parse.urlencode({
            "username": username, "password1": password, "password2": password,
            "role": role, "csrfmiddlewaretoken": self.csrf(),
        }).encode()
        req = urllib.request.Request(root + "/signup/", data=form, headers={"Referer": root + "/signup/"})
        self.opener.open(req, timeout=60).read()
        if not any(c.name == "sessionid" for c in self.jar):
            sys.exit(f"could not sign up {username!r} at {root}/signup/")

    def csrf(self):
        return next((c.value for c in self.jar if c.name == "csrftoken"), "")

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            self.root + "/api" + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-CSRFToken": self.csrf(),
                     "Referer": self.root + "/"},
        )
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--bids", type=int, default=200)
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--bidders", type=int, default=25)
    args = p.parse_args()
    root = args.url.rstrip("/")
    run = secrets.token_hex(3)

    seller = Session(root, f"storm-seller-{run}", "SELLER")
    bidders = [Session(root, f"storm-{run}-{i}", "BIDDER") for i in range(args.bidders)]
    print(f"signed up 1 seller and {len(bidders)} bidders")

    status, auction = seller.call("POST", "/auctions", {
        "title": "Bid storm", "starting_price": 100, "min_increment": 10, "duration_seconds": 25,
    })
    assert status == 201, auction
    aid = auction["id"]
    print(f"auction {aid} created, ends in 25s (every bid will extend it)")

    rng = random.Random()
    plan = [(rng.choice(bidders), rng.randrange(100, 100_000)) for _ in range(args.bids)]

    started = time.perf_counter()
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(
            lambda wa: wa[0].call("POST", f"/auctions/{aid}/bids", {"amount": wa[1]}),
            plan,
        ))
    elapsed = time.perf_counter() - started

    outcomes = Counter("ACCEPTED" if s == 201 else b.get("error", s) for s, b in results)
    print(f"{len(results)} bids in {elapsed:.2f}s ({len(results) / elapsed:.0f}/s): {dict(outcomes)}")

    _, final = seller.call("GET", f"/auctions/{aid}")
    _, history = seller.call("GET", f"/auctions/{aid}/bids")
    bids = sorted(history["bids"], key=lambda b: b["sequence"])
    accepted_amounts = [b["bid"]["amount"] for s, b in results if s == 201]

    problems = []
    if [b["sequence"] for b in bids] != list(range(1, len(bids) + 1)):
        problems.append("bid sequence has gaps")
    if len(bids) != outcomes["ACCEPTED"] or final["bid_count"] != outcomes["ACCEPTED"]:
        problems.append("accepted bids missing from history (lost update)")
    for a, b in zip(bids, bids[1:]):
        if b["amount"] < a["amount"] + final["min_increment"]:
            problems.append(f"price did not rise enough at seq {b['sequence']}")
    if accepted_amounts and final["current_price"] != max(accepted_amounts):
        problems.append("final price is not the highest accepted bid")
    if bids and final["leader"] != bids[-1]["bidder_id"]:
        problems.append("leader is not the last accepted bidder")

    print(f"final: price={final['current_price']} leader={final['leader']} "
          f"bids={final['bid_count']} ends_at={final['ends_at']}")
    if problems:
        print("FAILED:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("OK: no lost updates, prices strictly increasing, final state matches history")


if __name__ == "__main__":
    main()

"""The single source of time for auction rules.

All rule code calls `clock.now()` instead of `timezone.now()` directly, so:
  * the server's clock is the only clock that decides anything (clients never
    send timestamps), and
  * tests can freeze or move time with `mock.patch("auctions.clock.now", ...)`
    instead of sleeping.
"""

from datetime import datetime

from django.utils import timezone


def now() -> datetime:
    return timezone.now()

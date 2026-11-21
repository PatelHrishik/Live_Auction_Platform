from datetime import datetime, timedelta, timezone
from unittest import mock

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Frozen, manually-advanced clock. Patched over auctions.clock.now."""

    def __init__(self, start: datetime = T0):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)

    def set(self, t: datetime) -> None:
        self.t = t


def install_fake_clock(testcase, start: datetime = T0) -> FakeClock:
    clock = FakeClock(start)
    patcher = mock.patch("auctions.clock.now", clock)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return clock

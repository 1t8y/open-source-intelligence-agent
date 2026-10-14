"""Client-side pacing for a walk over a rate-limited public API.

A daily collection asks a source one question, so pacing it is pointless. A backfill asks
the same source twenty questions in a row, which is what turns a polite client into a
throttled one - so the walk is what carries the limiter.

The clock and the sleeper are injected rather than imported for one reason: a rate limiter
that can only be proven by waiting makes every future test run slower, and a test that
sleeps to prove pacing is a test nobody keeps.
"""

import time
from collections.abc import Callable
from typing import Final

MIN_REQUESTS_PER_MINUTE: Final[int] = 1
SECONDS_PER_MINUTE: Final[float] = 60.0


class RateLimiter:
    """Enforce a minimum interval between calls to one request budget.

    The limiter is deliberately *not* a token bucket: it does not allow a burst and then
    refill. A backfill's whole risk is sustained load, so the simplest correct policy is
    one that never exceeds the configured rate at any instant - the first call is free and
    every later call is at least `60 / per_minute` seconds after the previous one.
    """

    def __init__(
        self,
        *,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < MIN_REQUESTS_PER_MINUTE:
            raise ValueError(f"per_minute must be at least {MIN_REQUESTS_PER_MINUTE}")
        self._interval = SECONDS_PER_MINUTE / per_minute
        self._clock = clock
        self._sleep = sleep
        self._last_release: float | None = None
        self._total_waited = 0.0

    @property
    def per_minute(self) -> int:
        """Return the configured request budget."""
        return round(SECONDS_PER_MINUTE / self._interval)

    @property
    def interval_seconds(self) -> float:
        """Return the enforced minimum spacing between two requests."""
        return self._interval

    @property
    def total_waited_seconds(self) -> float:
        """Return how long this limiter has held callers back in total."""
        return self._total_waited

    def wait(self) -> float:
        """Block until the next request is allowed and return the seconds spent.

        The release time is advanced by the interval rather than read back from the clock.
        Reading it back would let a caller that overslept earn credit towards its next
        request, which is exactly the burst the limiter exists to prevent; taking the
        larger of the two keeps that property while still not double-charging a slow
        sleeper for time it truly spent.
        """
        now = self._clock()
        if self._last_release is None:
            self._last_release = now
            return 0.0
        target = self._last_release + self._interval
        if now >= target:
            self._last_release = now
            return 0.0
        delay = target - now
        self._sleep(delay)
        self._last_release = max(target, self._clock())
        self._total_waited += delay
        return delay

"""Tests for the request pacer a paged walk paces with.

Every test here drives a fake clock. The limiter's contract is about *spacing*, so asserting
it by really sleeping would make the suite slower while proving less: a real sleep can only
show that at least some time passed, whereas a fake clock can show that exactly one interval
passed and no more.
"""

import pytest

from open_source_intelligence.collectors.limiter import RateLimiter


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def jump(self, seconds: float) -> None:
        """Advance without sleeping, as a slow request would."""
        self.now += seconds


def build_limiter(per_minute: int) -> tuple[FakeClock, RateLimiter]:
    """Build a limiter that paces on a fake clock."""
    clock = FakeClock()
    return clock, RateLimiter(per_minute=per_minute, clock=clock.time, sleep=clock.sleep)


def test_the_first_request_is_free() -> None:
    """A walk must not sleep before it has asked anything."""
    clock, limiter = build_limiter(30)

    assert limiter.wait() == 0.0
    assert clock.slept == []


def test_the_interval_is_sixty_seconds_over_the_budget() -> None:
    """Thirty requests a minute is one request every two seconds."""
    _, thirty = build_limiter(30)
    _, ten = build_limiter(10)

    assert thirty.interval_seconds == pytest.approx(2.0)
    assert ten.interval_seconds == pytest.approx(6.0)
    assert thirty.per_minute == 30


@pytest.mark.parametrize("per_minute", [10, 30, 60])
def test_every_later_request_waits_exactly_one_interval(per_minute: int) -> None:
    """N requests cost N-1 intervals, each wait being the interval itself."""
    clock, limiter = build_limiter(per_minute)

    for _ in range(5):
        limiter.wait()

    assert clock.slept == [limiter.interval_seconds] * 4
    assert limiter.total_waited_seconds == pytest.approx(4 * limiter.interval_seconds)


def test_a_source_slower_than_the_interval_makes_the_pacer_wait_nothing() -> None:
    """Pacing guarantees a minimum spacing, not a fixed sleep.

    When each request already takes longer than the interval the budget is satisfied by the
    source's own latency, and the limiter adds nothing. This is why a walk's reported pacing
    is not `(pages - 1) * interval`: on a real, slow API it is usually near zero, and a test
    that asserted the product would fail against the real thing while passing in here.
    """
    clock, limiter = build_limiter(30)

    limiter.wait()
    for _ in range(4):
        clock.jump(3.0)
        assert limiter.wait() == 0.0

    assert clock.slept == []
    assert limiter.total_waited_seconds == 0.0


def test_a_partial_pause_only_waits_the_remainder() -> None:
    """A request that took 1.5s of a 2s interval still has 0.5s to make up."""
    clock, limiter = build_limiter(30)

    limiter.wait()
    clock.jump(1.5)
    waited = limiter.wait()

    assert waited == pytest.approx(0.5)
    assert clock.slept == [pytest.approx(0.5)]


def test_a_natural_gap_does_not_accumulate_burst_credit() -> None:
    """Ten idle seconds do not buy five free requests.

    The release time advances by the interval rather than by the clock, so a source that was
    slow once cannot license a burst afterwards. Crediting the difference would let a walk
    that had already been idle immediately fire as many requests as the idle time allowed -
    exactly the burst this limiter exists to prevent.
    """
    clock, limiter = build_limiter(30)

    limiter.wait()
    clock.jump(10.0)
    assert limiter.wait() == 0.0
    assert limiter.wait() == pytest.approx(limiter.interval_seconds)
    assert limiter.wait() == pytest.approx(limiter.interval_seconds)


def test_an_oversleeping_sleeper_is_charged_the_interval_not_the_oversleep() -> None:
    """Only the delay the limiter asked for is counted, and a longer nap earns no credit.

    A sleeper can overshoot - the process is suspended, the machine is busy. The extra time
    is real but it was not the limiter's cost, so it must not inflate the reported pacing.
    It must not license a burst either: crediting the overshoot would put the next request
    in the same instant as this one, which is the pair the interval exists to separate.
    """
    clock = FakeClock()

    def oversleeping(seconds: float) -> None:
        clock.slept.append(seconds)
        clock.now += seconds + 4.0

    limiter = RateLimiter(per_minute=30, clock=clock.time, sleep=oversleeping)
    limiter.wait()
    waited = limiter.wait()

    assert waited == pytest.approx(2.0)
    assert limiter.total_waited_seconds == pytest.approx(2.0)
    assert clock.now == pytest.approx(6.0)
    assert limiter.wait() == pytest.approx(2.0), "the overshoot was credited as free time"


def test_the_oversleep_rule_keeps_an_idle_gap_to_one_free_request() -> None:
    """A genuinely long gap is a different case from an overslept sleep.

    When nothing was pending there is no sleep to have overshot, so the next request is
    free - but the one after it is not, so the free request can never become a burst.
    """
    clock, limiter = build_limiter(30)

    limiter.wait()
    clock.jump(30.0)

    assert limiter.wait() == 0.0
    assert limiter.wait() == pytest.approx(limiter.interval_seconds)


def test_a_budget_below_one_request_per_minute_is_refused() -> None:
    """A zero or negative budget has no meaningful interval."""
    with pytest.raises(ValueError, match="at least"):
        RateLimiter(per_minute=0)
    with pytest.raises(ValueError, match="at least"):
        RateLimiter(per_minute=-1)


def test_the_total_counts_only_the_delay_the_limiter_added() -> None:
    """The reported pacing is what the limiter cost, not how long the walk took."""
    clock, limiter = build_limiter(60)

    for _ in range(3):
        limiter.wait()

    assert clock.slept == [1.0, 1.0]
    assert limiter.total_waited_seconds == pytest.approx(2.0)

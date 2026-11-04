"""守护分页遍历用来控制节奏的请求节流器。

这里的每个用例都驱动一个假时钟。节流器的契约讲的是*间隔*，若改用真实休眠来断言，
只会让测试更慢却证明更少：真实休眠只能说明「过了一段时间」，假时钟才能说明「恰好
过去了一个间隔，不多不少」。
"""

import pytest

from open_source_intelligence.collectors.limiter import RateLimiter


class FakeClock:
    """只在有人于其上休眠时才前进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def jump(self, seconds: float) -> None:
        """像一次慢请求那样，不经过休眠就把时间推后。"""
        self.now += seconds


def build_limiter(per_minute: int) -> tuple[FakeClock, RateLimiter]:
    """构造一个跑在假时钟上的节流器。"""
    clock = FakeClock()
    return clock, RateLimiter(per_minute=per_minute, clock=clock.time, sleep=clock.sleep)


def test_the_first_request_is_free() -> None:
    """一次请求都还没发出去之前，遍历不该先睡一觉。"""
    clock, limiter = build_limiter(30)

    assert limiter.wait() == 0.0
    assert clock.slept == []


def test_the_interval_is_sixty_seconds_over_the_budget() -> None:
    """每分钟三十次，折算下来就是每两秒一次。"""
    _, thirty = build_limiter(30)
    _, ten = build_limiter(10)

    assert thirty.interval_seconds == pytest.approx(2.0)
    assert ten.interval_seconds == pytest.approx(6.0)
    assert thirty.per_minute == 30


@pytest.mark.parametrize("per_minute", [10, 30, 60])
def test_every_later_request_waits_exactly_one_interval(per_minute: int) -> None:
    """N 次请求只付出 N-1 个间隔，且每次等待恰好是一个间隔。"""
    clock, limiter = build_limiter(per_minute)

    for _ in range(5):
        limiter.wait()

    assert clock.slept == [limiter.interval_seconds] * 4
    assert limiter.total_waited_seconds == pytest.approx(4 * limiter.interval_seconds)


def test_a_source_slower_than_the_interval_makes_the_pacer_wait_nothing() -> None:
    """节流保证的是最小间隔，而不是固定休眠一段时间。

    当每次请求本身就久于间隔时，预算由来源自己的延迟满足，节流器不再额外等待。
    这也是遍历上报的等待时长不等于 `(pages - 1) * interval` 的原因：在真实而缓慢
    的 API 上它通常接近零，若用例断言这个乘积，就会在本地通过、对上真实服务却
    失败。
    """
    clock, limiter = build_limiter(30)

    limiter.wait()
    for _ in range(4):
        clock.jump(3.0)
        assert limiter.wait() == 0.0

    assert clock.slept == []
    assert limiter.total_waited_seconds == 0.0


def test_a_partial_pause_only_waits_the_remainder() -> None:
    """2 秒间隔里已经花掉 1.5 秒的请求，还差 0.5 秒要补。"""
    clock, limiter = build_limiter(30)

    limiter.wait()
    clock.jump(1.5)
    waited = limiter.wait()

    assert waited == pytest.approx(0.5)
    assert clock.slept == [pytest.approx(0.5)]


def test_a_natural_gap_does_not_accumulate_burst_credit() -> None:
    """空闲十秒换不来五次免费请求。

    放行时刻按间隔推进，而不是按时钟推进，因此来源某一次慢了，并不能为之后背书
    一次突发。若把差值记作额度，一段此前已经空闲的遍历就会立刻连发与空闲时长相当
    的请求 —— 那恰恰是这个节流器要挡住的突发。
    """
    clock, limiter = build_limiter(30)

    limiter.wait()
    clock.jump(10.0)
    assert limiter.wait() == 0.0
    assert limiter.wait() == pytest.approx(limiter.interval_seconds)
    assert limiter.wait() == pytest.approx(limiter.interval_seconds)


def test_an_oversleeping_sleeper_is_charged_the_interval_not_the_oversleep() -> None:
    """只统计节流器要求的延迟，睡过头也换不来额度。

    休眠可能睡过头 —— 进程被挂起、机器正忙。多出来的时间是真实的，却不是节流器
    的成本，所以不能抬高上报的等待时长；它同样不能换来突发：把超睡记成额度，就会
    让下一次请求与这一次落在同一瞬间，而这一对正是间隔要分开的。
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
    """真正长时间的空隙，与一次睡过头的休眠不是同一回事。

    当时并没有待放的请求，也就不存在「睡过头」的休眠，于是下一次请求免费 —— 但它
    之后的那次不免费，所以这次免费永远不会演变成突发。
    """
    clock, limiter = build_limiter(30)

    limiter.wait()
    clock.jump(30.0)

    assert limiter.wait() == 0.0
    assert limiter.wait() == pytest.approx(limiter.interval_seconds)


def test_a_budget_below_one_request_per_minute_is_refused() -> None:
    """零或负的预算算不出有意义的间隔。"""
    with pytest.raises(ValueError, match="at least"):
        RateLimiter(per_minute=0)
    with pytest.raises(ValueError, match="at least"):
        RateLimiter(per_minute=-1)


def test_the_total_counts_only_the_delay_the_limiter_added() -> None:
    """上报的等待时长是节流器的成本，不是整趟遍历的耗时。"""
    clock, limiter = build_limiter(60)

    for _ in range(3):
        limiter.wait()

    assert clock.slept == [1.0, 1.0]
    assert limiter.total_waited_seconds == pytest.approx(2.0)

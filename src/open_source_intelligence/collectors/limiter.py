"""在限流的公共 API 上走查时提供客户端节流。

每日采集只向源问一个问题，给它限速没有意义。回填会连着向同一个源问二十个
问题，正是这一点把一个有礼貌的客户端变成被限流的客户端 —— 所以是把限速器
挂在走查上。

时钟与 sleep 都靠注入而不是直接导入，只为一个理由：一个只能靠等待来证明的
限速器，会让此后每次测试都变慢；而靠 sleep 证明节流的测试，没人会保留。
"""

import time
from collections.abc import Callable
from typing import Final

MIN_REQUESTS_PER_MINUTE: Final[int] = 1
SECONDS_PER_MINUTE: Final[float] = 60.0


class RateLimiter:
    """约束同一请求预算下相邻两次调用之间的最小间隔。

    限速器刻意*不是*令牌桶：它不允许先突发再补充。回填的全部风险就是持续
    负载，所以最简单的正确策略是在任何时刻都不超过配置的速率 —— 第一次
    调用免费，此后每次调用都至少在上一次之后 `60 / per_minute` 秒。
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
        """返回配置的请求预算。"""
        return round(SECONDS_PER_MINUTE / self._interval)

    @property
    def interval_seconds(self) -> float:
        """返回两次请求之间强制的最小间隔。"""
        return self._interval

    @property
    def total_waited_seconds(self) -> float:
        """返回该限速器累计挡住了调用方多长时间。"""
        return self._total_waited

    def wait(self) -> float:
        """阻塞到下一次请求被允许，并返回本次等待的秒数。

        释放时间按间隔向前推进，而不是从时钟重新读回。读回会让睡过头的
        调用方为下一次请求攒下信用，而那正是限速器要防的突发；取两者中
        较大的一个，既保住了这条性质，又不会让慢的睡眠者为自己确实花掉
        的时间被重复计费。
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

"""时间源与防抖计时器。

两条不变式直接对应技术设计文档：

* **I2**：同一 Round 至多一个活跃 debounce 计时器 —— 计时器对象按 Round 复用，
  `reset` 内部先取消旧任务再创建新的，因此不会累积。
* **I19**：任何 `cancel()` 都**不作用于当前正在执行的任务** —— `_on_fire` 本身就是由
  本计时器的 `_run` 触发的，此时若取消"当前任务"，会产生自取消，导致状态不确定，
  也让"旧任务全部 cancelled"这类断言口径随实现细节漂移。
"""

from __future__ import annotations

import asyncio
from typing import Callable, Protocol


class Clock(Protocol):
    """可注入的时间源（测试用 FakeClock 替换，避免真等 12 秒）。"""

    def now(self) -> float:
        """当前单调时间（秒）。

        Returns:
            秒数。
        """

    async def sleep(self, seconds: float) -> None:
        """等待若干秒。

        Args:
            seconds: 秒数。
        """


class RealClock:
    """真实时间源：单调时钟 + asyncio.sleep。"""

    def now(self) -> float:
        """当前单调时间。

        Returns:
            秒数。
        """
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        """等待。

        Args:
            seconds: 秒数。
        """
        await asyncio.sleep(seconds)


class DebounceTimer:
    """每 Round 单实例的防抖计时器。

    对象复用是硬要求：每次新建会让旧 `_task` 存活到它自己的到期时刻，
    而退役时的身份校验（检查 `active[key] is rnd`）**拦不住**它——因为 Round 身份是对的，
    错的只是时间，结果就是"提前结算、吞掉后续延长"。
    """

    __slots__ = ("_clock", "_on_fire", "_task", "_deadline", "_paused_remaining")

    def __init__(self, clock: Clock, on_fire: Callable[[], None]) -> None:
        """初始化。

        Args:
            clock: 时间源。
            on_fire: 到期回调（调用方负责绑定到具体 Round）。
        """
        self._clock = clock
        self._on_fire = on_fire
        self._task: asyncio.Task | None = None
        self._deadline: float | None = None
        self._paused_remaining: float | None = None

    # ------------------------------------------------------------------ 公开操作
    def reset(self, seconds: float) -> None:
        """重新计时（取消旧任务后创建新任务）。

        Args:
            seconds: 等待秒数；<= 0 时立即触发。
        """
        self._cancel()
        self._paused_remaining = None
        if seconds <= 0:
            self._fire_now()
            return
        self._deadline = self._clock.now() + seconds
        self._task = asyncio.create_task(self._run(seconds))

    def pause(self) -> None:
        """暂停并记录剩余时间（输入状态感知用）。"""
        if self._deadline is None:
            return
        self._paused_remaining = max(0.0, self._deadline - self._clock.now())
        self._cancel()
        self._deadline = None

    def resume(self) -> None:
        """按剩余时间恢复；剩余为 0 时立即触发。"""
        if self._paused_remaining is None:
            return
        remaining = self._paused_remaining
        self._paused_remaining = None
        if remaining <= 0:
            self._fire_now()
            return
        self._deadline = self._clock.now() + remaining
        self._task = asyncio.create_task(self._run(remaining))

    def cancel(self) -> None:
        """彻底取消（清空暂停剩余）。"""
        self._cancel()
        self._deadline = None
        self._paused_remaining = None

    def remaining(self) -> float:
        """剩余秒数。

        Returns:
            剩余秒数；未武装时为 0。
        """
        if self._paused_remaining is not None:
            return self._paused_remaining
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - self._clock.now())

    def is_armed(self) -> bool:
        """是否处于武装状态（含暂停）。

        Returns:
            True 表示有活跃计时或暂停剩余。
        """
        return self._deadline is not None or self._paused_remaining is not None

    # ------------------------------------------------------------------ 内部
    def _fire_now(self) -> None:
        """在当前调用栈内同步触发回调（不新建任务）。"""
        self._deadline = None
        self._on_fire()

    def _cancel(self) -> None:
        """取消旧任务，但**绝不取消当前正在执行的任务**（I19）。"""
        current = asyncio.current_task()
        if self._task is not None and self._task is not current and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self, seconds: float) -> None:
        """等待到期后触发。

        Args:
            seconds: 等待秒数。
        """
        try:
            await self._clock.sleep(seconds)
        except asyncio.CancelledError:
            # 被 reset/pause/cancel 取消是正常路径，不算异常
            raise
        self._deadline = None
        self._on_fire()

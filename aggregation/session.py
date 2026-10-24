"""聚合的状态模型与存储：桶、轮次、快照、四张表。

四张表各有明确职责，别合并：

* `active`：每个桶当前**收集中的**轮次（I1 靠"单键单值"保证）；
* `finalizing`：**按 `round_id`** 索引的结算中轮次 —— `finalize_lock` 只保证串行、
  不保证唯一，同一桶可以"Round 1 结算中 + Round 2 收集中"；
* `index`：`message_id -> key`，撤回 O(1) 定位；
* `cooldown`：同桶两次结算之间的最小间隔。
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

from astrbot.api import logger

from .config import PLUGIN_NAME

BucketKey = tuple[str, str]


class RoundState(enum.Enum):
    """轮次状态。"""

    COLLECTING = "collecting"
    TYPING_PAUSED = "typing_paused"
    FLUSHING = "flushing"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class Part:
    """保留图文交错顺序的最小单元。"""

    kind: Literal["text", "image"]
    value: str


@dataclass(frozen=True, slots=True)
class Item:
    """一条用户消息的不可变表示。

    不可变是刻意的：撤回通过 `dataclasses.replace` 生成新实例，
    这样"已进入快照的 Item"永远不会被后续操作改掉。
    """

    message_id: str | None
    parts: tuple[Part, ...]
    arrived_at: float
    recalled: bool = False
    image_bytes: int = 0

    def text(self) -> str:
        """本条的文本部分。

        Returns:
            拼接后的文本。
        """
        return "".join(p.value for p in self.parts if p.kind == "text")

    def images(self) -> tuple[str, ...]:
        """本条的所有图片引用。

        Returns:
            引用元组。
        """
        return tuple(p.value for p in self.parts if p.kind == "image")


@dataclass(frozen=True, slots=True)
class Snapshot:
    """退役瞬间冻结的不可变视图（I6：结算在快照上，不阻塞入队）。"""

    items: tuple[Item, ...]

    def text(self, sep: str) -> str:
        """合并文本。

        Args:
            sep: 分隔符。

        Returns:
            合并后的文本。
        """
        return sep.join(t for t in (item.text() for item in self.items) if t)

    def images(self) -> list[str]:
        """合并图片引用（按出现顺序）。

        Returns:
            引用列表。
        """
        out: list[str] = []
        for item in self.items:
            out.extend(item.images())
        return out

    def tail_char(self) -> str:
        """最后一条非空文本的末字符（自适应等待用）。

        Returns:
            单个字符；无文本时为空串。
        """
        for item in reversed(self.items):
            text = item.text().strip()
            if text:
                return text[-1]
        return ""


@dataclass(slots=True)
class Round:
    """一次聚合轮次。"""

    round_id: int
    key: BucketKey
    owner_event: Any
    items: list[Item] = field(default_factory=list)
    frozen: Snapshot | None = None
    flush: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    timer: Any = None
    typing_guard: asyncio.Task | None = None
    started_at: float = 0.0
    typing: bool = False
    short_streak: int = 0
    committed_bytes: int = 0
    shutting_down: bool = False
    channel: str = "private"
    stats_recorded: bool = False
    elapsed_ms: float = 0.0
    state: RoundState = RoundState.COLLECTING
    merged_text: str = ""
    """结算成功并重构事件后写入的合并文本（供话题判定使用）。

    只在真正调用 `reconstruct` / `reconstruct_ordered` 之后赋值；空结果、
    被放弃、重构异常时保持空串——调用方据此判断"这一轮有没有可判定的内容"，
    不要再从事件上反推（事件可能已经被静默或仍是最初那条消息）。
    """

    def snapshot(self) -> Snapshot:
        """生成本轮快照（过滤已撤回的条目）。

        Returns:
            Snapshot。
        """
        return Snapshot(items=tuple(item for item in self.items if not item.recalled))

    def text_len(self) -> int:
        """当前合并文本长度。

        Returns:
            字符数。
        """
        return len(self.snapshot().text(""))

    def tail_char(self) -> str:
        """最后一条非空文本的末字符。

        Returns:
            单个字符或空串。
        """
        return self.snapshot().tail_char()


class SessionStore:
    """四张表 + 状态转移（**所有转移都在同步块内完成，中间无 `await`**）。"""

    def __init__(
        self,
        *,
        clock: Any,
        cfg: Any,
        on_round_end: Callable[[Round], None] | None = None,
    ) -> None:
        """初始化。

        Args:
            clock: 时间源。
            cfg: 配置（读取各字节配额）。
            on_round_end: 轮次结束回调（统计用；必须幂等，见 `stats.add_end`）。
        """
        self.clock = clock
        self.cfg = cfg
        self._on_round_end = on_round_end
        self.active: dict[BucketKey, Round] = {}
        self.finalizing: dict[int, Round] = {}
        self.index: dict[str, BucketKey] = {}
        self.cooldown: dict[BucketKey, float] = {}
        self._locks: dict[BucketKey, asyncio.Lock] = {}
        self._next_round_id = 0

    # ------------------------------------------------------------------ 轮次创建
    def next_round_id(self) -> int:
        """分配一个 `round_id`（只在创建分支消费）。

        Returns:
            新 ID。
        """
        self._next_round_id += 1
        return self._next_round_id

    def get_or_create(self, key: BucketKey, owner_event: Any,
                      channel: str = "private") -> Round:
        """取现有轮次，没有则创建。

        Args:
            key: 桶键。
            owner_event: 若需创建，用该事件作 Owner。
            channel: `private` / `group`。

        Returns:
            桶对应的轮次。
        """
        rnd = self.active.get(key)
        if rnd is not None:
            return rnd
        rnd = Round(
            round_id=self.next_round_id(),
            key=key,
            owner_event=owner_event,
            started_at=self.clock.now(),
            channel=channel,
        )
        self.active[key] = rnd
        return rnd

    def append(self, rnd: Round, item: Item) -> bool:
        """把一条消息追加进轮次（I12 + I9 + 轮次配额）。

        Args:
            rnd: 轮次。
            item: 已解析并完成附件保活的条目。

        Returns:
            True 表示该条**文本**被接受（图片可能因配额被裁剪）；False 表示整条被丢弃。
        """
        if rnd.state not in (RoundState.COLLECTING, RoundState.TYPING_PAUSED):
            logger.warning(
                f"[{PLUGIN_NAME}] 轮次状态 {rnd.state} 不接受追加，丢弃一条消息"
            )
            return False

        if rnd.committed_bytes + item.image_bytes > self.cfg.keep_round_max_bytes:
            # 只丢图片，文本仍然入队（文档 §13.4）
            text_parts = tuple(p for p in item.parts if p.kind == "text")
            if not text_parts:
                logger.warning(
                    f"[{PLUGIN_NAME}] 单轮图片配额已满且本条无文本，丢弃整条"
                )
                return False
            logger.warning(
                f"[{PLUGIN_NAME}] 单轮图片配额已满"
                f"（{rnd.committed_bytes}/{self.cfg.keep_round_max_bytes}），本条图片被跳过"
            )
            item = replace(item, parts=text_parts, image_bytes=0)

        rnd.items.append(item)
        rnd.committed_bytes += item.image_bytes
        if item.message_id:
            self.index[item.message_id] = rnd.key
        if len(item.text()) <= int(self.cfg.short_message_threshold):
            rnd.short_streak += 1
        else:
            rnd.short_streak = 0
        return True

    # ------------------------------------------------------------------ 状态转移
    def begin_finalize(self, rnd: Round) -> bool:
        """同步退役：身份校验 → 冻结 → 移出 active → 登记 finalizing → 清索引。

        Args:
            rnd: 待退役轮次。

        Returns:
            True 表示真正完成了退役；False 表示该轮已被 abort / 已被其他路径退役。
        """
        if self.active.get(rnd.key) is not rnd:
            return False
        del self.active[rnd.key]
        rnd.frozen = rnd.snapshot()
        rnd.state = RoundState.FLUSHING
        rnd.typing = False
        if rnd.timer is not None:
            rnd.timer.cancel()
        if rnd.typing_guard is not None and not rnd.typing_guard.done():
            current = asyncio.current_task()
            if rnd.typing_guard is not current:
                rnd.typing_guard.cancel()
            rnd.typing_guard = None
        self.finalizing[rnd.round_id] = rnd
        for item in rnd.frozen.items:
            if item.message_id:
                self.index.pop(item.message_id, None)
        return True

    def end_finalize(self, rnd: Round, cooldown: float) -> None:
        """幂等收尾。

        Args:
            rnd: 轮次。
            cooldown: 结束后要写入的 cooldown 秒数（<= 0 不写）。
        """
        if rnd.state is RoundState.DONE and rnd.round_id not in self.finalizing:
            return
        self.finalizing.pop(rnd.round_id, None)
        rnd.state = RoundState.DONE
        if cooldown > 0:
            self.cooldown[rnd.key] = self.clock.now() + cooldown
        rnd.done.set()
        self._notify_end(rnd)

    def abort(self, rnd: Round, cause: str) -> None:
        """幂等放弃：active / finalizing / 已 DONE 三种情形都安全。

        本方法**不静默 Owner**，也不取消正在 `_finalize` 的协程：
        Owner 必须在 `_after_flush` 或检查点看到 `shutting_down` 后自己静默。

        Args:
            rnd: 轮次。
            cause: 原因（写日志用）。
        """
        if rnd.state is RoundState.DONE:
            return
        rnd.shutting_down = True
        if rnd.timer is not None:
            rnd.timer.cancel()
        if rnd.typing_guard is not None and not rnd.typing_guard.done():
            current = asyncio.current_task()
            if rnd.typing_guard is not current:
                rnd.typing_guard.cancel()
            rnd.typing_guard = None
        if self.active.get(rnd.key) is rnd:
            del self.active[rnd.key]
            for item in rnd.items:
                if item.message_id:
                    self.index.pop(item.message_id, None)
        self.finalizing.pop(rnd.round_id, None)
        rnd.state = RoundState.DONE
        rnd.flush.set()
        rnd.done.set()
        self._notify_end(rnd)
        logger.warning(
            f"[{PLUGIN_NAME}] aggregate.abort key={rnd.key} round_id={rnd.round_id}"
            f" cause={cause} items={len(rnd.items)}"
        )

    def _notify_end(self, rnd: Round) -> None:
        """把"轮次已结束"通知给统计（幂等由 `stats.add_end` 保证）。

        Args:
            rnd: 轮次。
        """
        if self._on_round_end is None:
            return
        try:
            self._on_round_end(rnd)
        except Exception as exc:  # noqa: BLE001 - 统计绝不能影响主流程
            logger.debug(f"[{PLUGIN_NAME}] 统计回调失败（已忽略）：{exc}")

    # ------------------------------------------------------------------ 查询
    def cooldown_remaining(self, key: BucketKey) -> float:
        """同桶 cooldown 剩余秒数。

        Args:
            key: 桶键。

        Returns:
            剩余秒数；无 cooldown 或已过期返回 0。
        """
        until = self.cooldown.get(key)
        if until is None:
            return 0.0
        remaining = until - self.clock.now()
        if remaining <= 0:
            self.cooldown.pop(key, None)
            return 0.0
        return remaining

    def finalizing_rounds(self, key: BucketKey) -> list[Round]:
        """同键所有结算中的轮次。

        Args:
            key: 桶键。

        Returns:
            轮次列表（0~2 个）。
        """
        return [r for r in self.finalizing.values() if r.key == key]

    def finalize_lock(self, key: BucketKey) -> asyncio.Lock:
        """取该桶的结算锁（懒建，I7）。

        Args:
            key: 桶键。

        Returns:
            `asyncio.Lock`。
        """
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def wait_idle(self, key: BucketKey) -> None:
        """等待该桶所有结算中的轮次结束。

        Args:
            key: 桶键。
        """
        while True:
            pending = self.finalizing_rounds(key)
            if not pending:
                return
            await asyncio.gather(*(r.done.wait() for r in pending))

    def gc_cooldown(self) -> None:
        """清理已过期的 cooldown 记录（避免无界增长）。"""
        now = self.clock.now()
        for key in [k for k, until in self.cooldown.items() if until <= now]:
            self.cooldown.pop(key, None)

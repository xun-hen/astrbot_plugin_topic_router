"""本地统计与运行历史（v0.8 新增功能）。

三条硬约束（违反任一条，统计自己就会变成新的故障源）：

1. **累加同步、无 IO**：所有 `add_*` 都是 O(1) 纯内存操作，不 `await`、不碰磁盘。
   否则它会挤进事件路径的延迟预算（设计文档 §18：非 Owner 事件 < 1 ms）；
2. **维度有界**：群聊下 `(umo, sender)` 组合无上界。这里用"键满即拒绝新键 + 溢出计数"，
   不做 LRU（LRU 会让 TOP-N 抖动，同一会话在榜上忽隐忽现）；
3. **不碰正文**：`FlushRecord` 只存元数据。**代码评审必须逐字段检查**，
   并有测试断言落盘文件里不含任何消息文本。

为什么 `flush_total` / `abort_total` 合并成一个 `add_end`：被 abort 的轮次，其
`_finalize` 的 `finally` **仍会**调 `end_finalize`。如果 flush 记 `end_finalize`、
abort 记 `abort()`，同一轮会被计两次，`merge_ratio` 的分母当场被污染。
所以改成"一次性归类"：`add_end` 用 `Round.stats_recorded` 保证只生效一次，
再用 `Round.shutting_down` 决定计入哪一边。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import logger

from .config import PLUGIN_NAME

STATE_VERSION = 1

# 内部常量：没有需要用户调的理由，所以不做成配置（文档 §15.2 的取舍）
UMO_MAX_LEN = 64
SENDER_MAX_LEN = 32

# 固定桶（毫秒）；最后一个元素是 +inf 桶的下界
BUCKETS_MS: tuple[int, ...] = (10, 25, 50, 100, 250, 500, 1000, 3000, 12000)

DROP_CAUSES = ("empty", "denied", "failed", "shutdown", "abandoned")


@dataclass(slots=True)
class Histogram:
    """固定桶直方图（可序列化、可跨重启累加、内存恒定）。"""

    counts: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))

    def observe(self, ms: float) -> None:
        """记录一次观测。

        Args:
            ms: 耗时毫秒。
        """
        for idx, upper in enumerate(BUCKETS_MS):
            if ms < upper:
                self.counts[idx] += 1
                return
        self.counts[-1] += 1

    def total(self) -> int:
        """观测总数。

        Returns:
            次数。
        """
        return sum(self.counts)

    def quantile_upper(self, q: float) -> int:
        """近似分位：返回**所在桶的上界**（不是精确分位）。

        Args:
            q: 0~1 的分位。

        Returns:
            毫秒值；无样本时为 0。
        """
        total = self.total()
        if total <= 0:
            return 0
        target = max(1, int(total * max(0.0, min(1.0, q)) + 0.999))
        seen = 0
        for idx, count in enumerate(self.counts):
            seen += count
            if seen >= target:
                return BUCKETS_MS[idx] if idx < len(BUCKETS_MS) else BUCKETS_MS[-1] * 2
        return BUCKETS_MS[-1] * 2

    def to_list(self) -> list[int]:
        """序列化。

        Returns:
            计数列表。
        """
        return list(self.counts)

    @classmethod
    def from_list(cls, raw: Any) -> "Histogram":
        """反序列化（长度不符则重置）。

        Args:
            raw: 列表。

        Returns:
            Histogram。
        """
        expect = len(BUCKETS_MS) + 1
        if not isinstance(raw, list) or len(raw) != expect:
            return cls()
        try:
            return cls(counts=[int(x) for x in raw])
        except (TypeError, ValueError):
            return cls()


@dataclass(slots=True)
class ChannelStats:
    """按渠道的累计计数。"""

    flush: int = 0
    abort: int = 0
    items: int = 0
    images: int = 0
    elapsed_ms_sum: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化。

        Returns:
            字典。
        """
        return {
            "flush": self.flush,
            "abort": self.abort,
            "items": self.items,
            "images": self.images,
            "elapsed_ms_sum": round(self.elapsed_ms_sum, 3),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ChannelStats":
        """反序列化。

        Args:
            raw: 字典。

        Returns:
            ChannelStats。
        """
        if not isinstance(raw, dict):
            return cls()
        def num(key: str, cast=float) -> Any:
            try:
                return cast(raw.get(key, 0) or 0)
            except (TypeError, ValueError):
                return cast(0)
        return cls(
            flush=int(num("flush", int)),
            abort=int(num("abort", int)),
            items=int(num("items", int)),
            images=int(num("images", int)),
            elapsed_ms_sum=float(num("elapsed_ms_sum", float)),
        )


@dataclass(frozen=True, slots=True)
class FlushRecord:
    """一轮结算的元数据。**不含任何消息正文**（隐私红线）。"""

    ts: float
    round_id: int
    umo: str
    sender: str
    channel: str
    items: int
    images: int
    elapsed_ms: int
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        """序列化。

        Returns:
            字典。
        """
        return {
            "ts": round(self.ts, 3),
            "round_id": self.round_id,
            "umo": self.umo,
            "sender": self.sender,
            "channel": self.channel,
            "items": self.items,
            "images": self.images,
            "elapsed_ms": self.elapsed_ms,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "FlushRecord | None":
        """反序列化。

        Args:
            raw: 字典。

        Returns:
            FlushRecord 或 None（数据不合法时）。
        """
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                ts=float(raw.get("ts", 0.0) or 0.0),
                round_id=int(raw.get("round_id", 0) or 0),
                umo=str(raw.get("umo", ""))[:UMO_MAX_LEN],
                sender=str(raw.get("sender", ""))[:SENDER_MAX_LEN],
                channel=str(raw.get("channel", "private")),
                items=int(raw.get("items", 0) or 0),
                images=int(raw.get("images", 0) or 0),
                elapsed_ms=int(raw.get("elapsed_ms", 0) or 0),
                outcome=str(raw.get("outcome", "done")),
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """延迟汇总（桶近似，明确不是精确分位）。"""

    p50_ms: int
    p95_ms: int
    buckets: tuple[int, ...]
    total: int


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """对外只读投影：页面与日志都只能拿到它。"""

    counters: Mapping[str, int]
    merge_ratio: float
    by_channel: Mapping[str, ChannelStats]
    by_umo_top: tuple[tuple[str, int], ...]
    by_sender_top: tuple[tuple[str, int], ...]
    drop_by_cause: Mapping[str, int]
    latency: LatencySummary
    history: tuple[FlushRecord, ...]
    revision: int
    saved_at: float


class Stats:
    """累计统计 + 环形历史 + 原子落盘。"""

    def __init__(self, *, data_dir: Path, cfg: Any, clock: Any) -> None:
        """初始化。

        Args:
            data_dir: 插件数据目录。
            cfg: 配置。
            clock: 时间源。
        """
        self.cfg = cfg
        self.clock = clock
        self.path = data_dir / "stats.json"

        self.flush_total = 0
        self.abort_total = 0
        self.drop_total = 0
        self.items_total = 0
        self.images_total = 0
        self.bytes_cached_total = 0
        self.interrupt_total = 0
        self.interrupt_timeout_total = 0
        self.typing_unknown_total = 0
        self.preempted_total = 0
        self.dim_overflow_total = 0
        self.by_channel: dict[str, ChannelStats] = {}
        self.by_umo: dict[str, int] = {}
        self.by_sender: dict[str, int] = {}
        self.drop_by_cause: dict[str, int] = {}
        self.hist = Histogram()
        self.history: deque[FlushRecord] = deque(maxlen=max(0, int(cfg.stats_history_size)))
        self.revision = 0
        self.first_seen_at = 0.0
        self.first_config_hash = ""
        self.default_kept = 0
        self.saved_at = 0.0

        self._pending_flush = 0
        self._last_flush_revision = -1

    # ------------------------------------------------------------------ 累加
    def add_end(self, rnd: Any) -> None:
        """轮次结束的唯一归类入口（幂等）。

        Args:
            rnd: 已结束的 Round。
        """
        if getattr(rnd, "stats_recorded", True):
            return
        rnd.stats_recorded = True

        if getattr(rnd, "shutting_down", False):
            self.abort_total += 1
            cs = self._channel(rnd)
            cs.abort += 1
            self._remember(rnd, outcome="abort")
            self.revision += 1
            self._pending_flush += 1
            return

        items = list(getattr(rnd, "items", []) or [])
        images = sum(len(item.images()) for item in items)
        self.flush_total += 1
        self.items_total += len(items)
        self.images_total += images
        self.bytes_cached_total += int(getattr(rnd, "committed_bytes", 0) or 0)
        elapsed_ms = float(getattr(rnd, "elapsed_ms", 0.0) or 0.0)

        cs = self._channel(rnd)
        cs.flush += 1
        cs.items += len(items)
        cs.images += images
        cs.elapsed_ms_sum += elapsed_ms

        self.hist.observe(elapsed_ms)
        self._remember(rnd, outcome="done", items=len(items), images=images,
                       elapsed_ms=int(elapsed_ms))
        self.revision += 1
        self._pending_flush += 1

    def _channel(self, rnd: Any) -> ChannelStats:
        """取（或建）渠道统计。

        Args:
            rnd: 轮次。

        Returns:
            ChannelStats。
        """
        name = str(getattr(rnd, "channel", "private") or "private")
        cs = self.by_channel.get(name)
        if cs is None:
            cs = ChannelStats()
            self.by_channel[name] = cs
        return cs

    def _remember(self, rnd: Any, *, outcome: str, items: int = 0,
                  images: int = 0, elapsed_ms: int = 0) -> None:
        """写入环形历史，并累加有界的维度表。

        Args:
            rnd: 轮次。
            outcome: `done` / `abort`。
            items: 条数。
            images: 图片数。
            elapsed_ms: 耗时。
        """
        key = getattr(rnd, "key", ("", ""))
        umo = _truncate(str(key[0] if len(key) > 0 else ""), UMO_MAX_LEN)
        sender = _truncate(str(key[1] if len(key) > 1 else ""), SENDER_MAX_LEN)

        self._bump_dim(self.by_umo, umo)
        if getattr(rnd, "channel", "private") == "group" and sender:
            self._bump_dim(self.by_sender, sender)

        if self.history.maxlen:
            self.history.append(
                FlushRecord(
                    ts=self.clock.now(),
                    round_id=int(getattr(rnd, "round_id", 0) or 0),
                    umo=umo,
                    sender=sender,
                    channel=str(getattr(rnd, "channel", "private") or "private"),
                    items=items,
                    images=images,
                    elapsed_ms=elapsed_ms,
                    outcome=outcome,
                )
            )

    def _bump_dim(self, table: dict[str, int], key: str) -> None:
        """维度表累加（有界）。

        Args:
            table: 维度表。
            key: 键。
        """
        if not key:
            return
        if key in table:
            table[key] += 1
            return
        if len(table) >= int(self.cfg.stats_max_dimensions):
            self.dim_overflow_total += 1
            return
        table[key] = 1

    def add_drop(self, cause: str) -> None:
        """记录一次"未产出输出"的静默。

        Args:
            cause: 原因（`empty`/`denied`/`failed`/`shutdown`/`abandoned`）。
        """
        self.drop_total += 1
        self.drop_by_cause[cause] = self.drop_by_cause.get(cause, 0) + 1
        self.revision += 1

    def add_interrupt(self, *, timeout: bool = False) -> None:
        """记录一次指令中断。

        Args:
            timeout: 该次中断是否超时。
        """
        self.interrupt_total += 1
        if timeout:
            self.interrupt_timeout_total += 1
        self.revision += 1

    def add_typing_unknown(self) -> None:
        """记录一次 typing 判定失败（只计数，日志由调用方打 debug 防刷屏）。"""
        self.typing_unknown_total += 1
        self.revision += 1

    def add_preempted(self) -> None:
        """记录一次"本该处理却被更高优先级者挤掉"（抢占检测命中）。

        这个计数存在的意义：优先级是否够，靠"我觉得够"是答不了的。
        有它就等于有了证据——一旦非零，说明真有执行者排在前面。
        """
        self.preempted_total += 1
        self.revision += 1

    # ------------------------------------------------------------------ 派生
    def merge_ratio(self) -> float:
        """合并率 = 每条下游调用承载了几条用户消息（G1 的量化口径）。

        Returns:
            比值；无数据时为 0。
        """
        return self.items_total / max(1, self.flush_total)

    def need_flush(self) -> bool:
        """按"累计次数"判断是否需要落盘（间隔由后台任务另行判断）。

        Returns:
            True 表示该落了。
        """
        every = max(1, int(self.cfg.stats_flush_every))
        return self._pending_flush >= every

    # ------------------------------------------------------------------ 投影
    def snapshot(self, history_limit: int = 20) -> StatsSnapshot:
        """生成只读投影。

        Args:
            history_limit: 附带多少条最近历史（0 表示不带）。

        Returns:
            StatsSnapshot。
        """
        limit = max(0, min(len(self.history), int(history_limit)))
        recent = tuple(list(self.history)[-limit:]) if limit else ()
        return StatsSnapshot(
            counters={
                "flush_total": self.flush_total,
                "abort_total": self.abort_total,
                "drop_total": self.drop_total,
                "items_total": self.items_total,
                "images_total": self.images_total,
                "bytes_cached_total": self.bytes_cached_total,
                "interrupt_total": self.interrupt_total,
                "interrupt_timeout_total": self.interrupt_timeout_total,
                "typing_unknown_total": self.typing_unknown_total,
                "preempted_total": self.preempted_total,
                "dim_overflow_total": self.dim_overflow_total,
                "default_kept": self.default_kept,
            },
            merge_ratio=round(self.merge_ratio(), 3),
            by_channel=dict(self.by_channel),
            by_umo_top=_top(self.by_umo),
            by_sender_top=_top(self.by_sender),
            drop_by_cause=dict(self.drop_by_cause),
            latency=LatencySummary(
                p50_ms=self.hist.quantile_upper(0.5),
                p95_ms=self.hist.quantile_upper(0.95),
                buckets=tuple(self.hist.to_list()),
                total=self.hist.total(),
            ),
            history=recent,
            revision=self.revision,
            saved_at=self.saved_at,
        )

    def history_records(self, limit: int) -> list[dict[str, Any]]:
        """最近 N 条历史（页面 API 用）。

        Args:
            limit: 条数。

        Returns:
            字典列表。
        """
        limit = max(0, min(len(self.history), int(limit)))
        if not limit:
            return []
        return [record.to_dict() for record in list(self.history)[-limit:]]

    # ------------------------------------------------------------------ A3
    def track_config(self, config_hash: str, *, min_days: float = 7.0) -> None:
        """记录"首次生效配置"用于 A3（默认配置留存）的本地近似。

        Args:
            config_hash: 生效配置的短哈希。
            min_days: 判定为"留存"所需的天数。
        """
        now = time.time()
        if not self.first_seen_at:
            self.first_seen_at = now
            self.first_config_hash = config_hash
            logger.info(f"[{PLUGIN_NAME}] 首次启动记录生效配置哈希 {config_hash}")
            return
        if config_hash != self.first_config_hash:
            return
        age_days = (now - self.first_seen_at) / 86400.0
        if age_days >= min_days:
            self.default_kept = max(1, self.default_kept)

    # ------------------------------------------------------------------ 持久化
    def to_dict(self) -> dict[str, Any]:
        """序列化。

        Returns:
            字典。
        """
        return {
            "version": STATE_VERSION,
            "saved_at": round(time.time(), 3),
            "counters": {
                "flush_total": self.flush_total,
                "abort_total": self.abort_total,
                "drop_total": self.drop_total,
                "items_total": self.items_total,
                "images_total": self.images_total,
                "bytes_cached_total": self.bytes_cached_total,
                "interrupt_total": self.interrupt_total,
                "interrupt_timeout_total": self.interrupt_timeout_total,
                "typing_unknown_total": self.typing_unknown_total,
                "preempted_total": self.preempted_total,
                "dim_overflow_total": self.dim_overflow_total,
                "default_kept": self.default_kept,
            },
            "by_channel": {k: v.to_dict() for k, v in self.by_channel.items()},
            "by_umo": dict(self.by_umo),
            "by_sender": dict(self.by_sender),
            "drop_by_cause": dict(self.drop_by_cause),
            "histogram": self.hist.to_list(),
            "history": [record.to_dict() for record in self.history],
            "first_seen_at": round(self.first_seen_at, 3),
            "first_config_hash": self.first_config_hash,
        }

    def save(self) -> bool:
        """原子落盘（失败只 warning，绝不影响聚合）。

        Returns:
            True 表示写成功。
        """
        if not self._write_once():
            return False
        self._pending_flush = 0
        self._last_flush_revision = self.revision
        self._truncate_if_needed()
        return True

    def _write_once(self) -> bool:
        """写一次文件（不做体积裁剪，避免与 `save` 互相递归）。

        Returns:
            True 表示写成功。
        """
        payload = self.to_dict()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] stats.persist_failed path={self.path} reason={exc}")
            return False
        self.saved_at = time.time()
        return True

    def _truncate_if_needed(self, limit_bytes: int = 64 * 1024) -> None:
        """超过目标体积时裁剪（先砍 by_sender，再砍 history）。

        只调用 `_write_once()`，**不调用 `save()`** —— 后者会再进来一次，
        形成无限递归（这个 bug 最初就是这么写出来的）。

        Args:
            limit_bytes: 目标上限。
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= limit_bytes:
            return
        dropped_senders = len(self.by_sender)
        dropped_history = 0
        if dropped_senders:
            self.by_sender.clear()
            self._write_once()
        while self.history:
            try:
                size = self.path.stat().st_size
            except OSError:
                break
            if size <= limit_bytes:
                break
            self.history.popleft()
            dropped_history += 1
            self._write_once()
        logger.info(
            f"[{PLUGIN_NAME}] stats.truncated dropped_senders={dropped_senders}"
            f" dropped_history={dropped_history}"
        )

    def load(self) -> None:
        """从磁盘恢复；版本不符或数据损坏则丢弃重建。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            logger.info(f"[{PLUGIN_NAME}] 统计文件版本不符或缺失，已重建")
            return

        counters = raw.get("counters") or {}
        def num(key: str, cast=int) -> Any:
            try:
                return cast(counters.get(key, 0) or 0)
            except (TypeError, ValueError):
                return cast(0)

        self.flush_total = int(num("flush_total"))
        self.abort_total = int(num("abort_total"))
        self.drop_total = int(num("drop_total"))
        self.items_total = int(num("items_total"))
        self.images_total = int(num("images_total"))
        self.bytes_cached_total = int(num("bytes_cached_total"))
        self.interrupt_total = int(num("interrupt_total"))
        self.interrupt_timeout_total = int(num("interrupt_timeout_total"))
        self.typing_unknown_total = int(num("typing_unknown_total"))
        self.preempted_total = int(num("preempted_total"))
        self.dim_overflow_total = int(num("dim_overflow_total"))
        self.default_kept = int(num("default_kept"))

        self.by_channel = {
            str(k): ChannelStats.from_dict(v)
            for k, v in (raw.get("by_channel") or {}).items()
        }
        self.by_umo = _bounded_map(raw.get("by_umo"), self.cfg.stats_max_dimensions)
        self.by_sender = _bounded_map(raw.get("by_sender"), self.cfg.stats_max_dimensions)
        self.drop_by_cause = {
            str(k): int(v) for k, v in (raw.get("drop_by_cause") or {}).items()
            if isinstance(v, (int, float))
        }
        self.hist = Histogram.from_list(raw.get("histogram"))
        restored: list[FlushRecord] = []
        for entry in raw.get("history") or []:
            record = FlushRecord.from_dict(entry)
            if record is not None:
                restored.append(record)
        self.history.clear()
        for record in restored[-self.history.maxlen:] if self.history.maxlen else []:
            self.history.append(record)
        self.first_seen_at = float(raw.get("first_seen_at", 0.0) or 0.0)
        self.first_config_hash = str(raw.get("first_config_hash", ""))
        self.saved_at = float(raw.get("saved_at", 0.0) or 0.0)

    def reset(self) -> None:
        """清零统计（页面"清空统计"用）；保留 A3 的首次记录。"""
        first_seen_at = self.first_seen_at
        first_hash = self.first_config_hash
        maxlen = self.history.maxlen
        self.__init__(data_dir=self.path.parent, cfg=self.cfg, clock=self.clock)
        self.history = deque(maxlen=maxlen)
        self.first_seen_at = first_seen_at
        self.first_config_hash = first_hash
        self.revision += 1
        self.save()
        logger.info(f"[{PLUGIN_NAME}] stats.reset by=web")


def _truncate(text: str, limit: int) -> str:
    """截断字符串。

    Args:
        text: 原串。
        limit: 上限。

    Returns:
        截断后的串。
    """
    return text if len(text) <= limit else text[:limit]


def _top(table: dict[str, int], limit: int = 10) -> tuple[tuple[str, int], ...]:
    """取 TOP-N（按次数降序）。

    Args:
        table: 维度表。
        limit: 条数。

    Returns:
        元组序列。
    """
    ordered = sorted(table.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(ordered[:limit])


def _bounded_map(raw: Any, limit: int) -> dict[str, int]:
    """反序列化维度表并裁剪到上限。

    Args:
        raw: 原始字典。
        limit: 上限。

    Returns:
        裁剪后的字典。
    """
    if not isinstance(raw, dict):
        return {}
    ordered = sorted(
        ((str(k), int(v)) for k, v in raw.items() if isinstance(v, (int, float))),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return dict(ordered[: max(0, int(limit))])


def config_hash(cfg: Any) -> str:
    """对生效配置算短哈希（A3 用）。

    Args:
        cfg: 配置对象。

    Returns:
        8 位十六进制字符串。
    """
    import hashlib

    payload = {
        name: getattr(cfg, name)
        for name in getattr(cfg, "__slots__", ()) or ()
        if not name.startswith("_")
    }
    if not payload:
        payload = {k: v for k, v in vars(cfg).items() if not k.startswith("_")}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]

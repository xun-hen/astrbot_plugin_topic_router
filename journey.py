"""阶梯放权：mark → hint → auto 按天自动升级（v0.10.0）。

用户的需求是"先观察几天，再逐步放权"：跑 mark 几天、没出问题就自动切 hint，
再过几天自动切 auto。这里把这个节奏做成**可配置 + 可回退**：

* 所有判断都是纯函数（`target_stage`），可以用假时钟在毫秒内跑完几个月；
* 真正的状态只有 `promotion.json` 一份（起始时间、上次自动写入的 mode、事件流水）；
* **用户手动改过 mode 就暂停自动放权**——插件不能把用户的选择覆盖掉；
* 只升不降：当前 mode 已经高于目标档位时什么都不做。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STAGE_ORDER: dict[str, int] = {"off": 0, "mark": 1, "hint": 2, "auto": 3}
"""档位高低。`off` 最低：它表示"先别判"，不该被自动升级绕过。"""

DEFAULT_MARK_DAYS = 3.0
DEFAULT_HINT_DAYS = 3.0


def rank(mode: str) -> int:
    """把 mode 折算成可比较的档位。

    Args:
        mode: 模式名。

    Returns:
        档位；未知模式按 off 处理（保守：宁可不动）。
    """
    return STAGE_ORDER.get(str(mode or "").strip().lower(), 0)


@dataclass(frozen=True)
class Plan:
    """放权计划（来自插件配置）。"""

    enabled: bool = False
    mark_days: float = DEFAULT_MARK_DAYS
    hint_days: float = DEFAULT_HINT_DAYS
    to_auto: bool = True

    @classmethod
    def from_config(cls, raw: Any) -> Plan:
        """从配置字典构造（容错：任何非法值都退回默认）。

        Args:
            raw: `config.promotion` 子项。

        Returns:
            计划。
        """
        if not isinstance(raw, dict):
            return cls()

        def flag(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return default

        def days(value: Any, default: float) -> float:
            try:
                got = float(value)
            except (TypeError, ValueError):
                return default
            if got < 0:
                return default
            return got

        return cls(
            enabled=flag(raw.get("enabled"), False),
            mark_days=days(raw.get("mark_days"), DEFAULT_MARK_DAYS),
            hint_days=days(raw.get("hint_days"), DEFAULT_HINT_DAYS),
            to_auto=flag(raw.get("to_auto"), True),
        )


def target_stage(elapsed_days: float, plan: Plan) -> str:
    """按已运行天数算出"应该处于"的档位（纯函数）。

    Args:
        elapsed_days: 自启用放权起经过的天数（可为小数）。
        plan: 放权计划。

    Returns:
        `mark` / `hint` / `auto`。
    """
    if elapsed_days < plan.mark_days:
        return "mark"
    if elapsed_days < plan.mark_days + plan.hint_days:
        return "hint"
    return "auto" if plan.to_auto else "hint"


class Journey:
    """放权状态机（同步、无 IO 竞争，落盘由调用方触发）。"""

    def __init__(self, path: Path) -> None:
        """初始化。

        Args:
            path: `promotion.json` 的路径。
        """
        self.path = Path(path)
        self.started_at: float = 0.0
        self.last_applied: str = "mark"
        self.paused: bool = False
        self.pause_reason: str = ""
        self.events: list[dict[str, Any]] = []
        self.load()

    # ------------------------------------------------------------------ 持久化
    def load(self) -> None:
        """读盘（文件损坏时回到初始状态，绝不抛）。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self.started_at = float(raw.get("started_at") or 0.0)
        self.last_applied = str(raw.get("last_applied") or "mark")
        self.paused = bool(raw.get("paused"))
        self.pause_reason = str(raw.get("pause_reason") or "")
        events = raw.get("events")
        self.events = [e for e in events if isinstance(e, dict)][-40:] if isinstance(events, list) else []

    def save(self) -> None:
        """写盘（原子替换）。"""
        payload = {
            "version": 1,
            "started_at": self.started_at,
            "last_applied": self.last_applied,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "events": self.events[-40:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ 状态
    def start(self, now: float, mode: str) -> None:
        """开始计时（首次启用或手动重置）。

        Args:
            now: 当前时间戳。
            mode: 当前实际模式，作为"上次自动写入"的基准。
        """
        self.started_at = now
        self.last_applied = mode
        self.paused = False
        self.pause_reason = ""
        self.events.append({"ts": now, "event": "start", "from": mode, "to": mode})
        self.save()

    def elapsed_days(self, now: float) -> float:
        """已运行天数。

        Args:
            now: 当前时间戳。

        Returns:
            天数；还没开始时为 0。
        """
        if self.started_at <= 0:
            return 0.0
        return max(0.0, (now - self.started_at) / 86400.0)

    def stage_view(self, now: float, plan: Plan, current_mode: str) -> dict[str, Any]:
        """给页面用的进度视图。

        Args:
            now: 当前时间戳。
            plan: 计划。
            current_mode: 当前实际模式。

        Returns:
            进度字典。
        """
        days = self.elapsed_days(now)
        target = target_stage(days, plan)
        mark_end = plan.mark_days
        hint_end = plan.mark_days + plan.hint_days
        if days < mark_end:
            next_stage, left = "hint", mark_end - days
        elif days < hint_end:
            next_stage, left = ("auto" if plan.to_auto else "-"), hint_end - days
        else:
            next_stage, left = "-", 0.0
        return {
            "enabled": plan.enabled,
            "started_at": self.started_at,
            "elapsed_days": round(days, 2),
            "current_mode": current_mode,
            "target_stage": target,
            "next_stage": next_stage,
            "days_to_next": round(max(0.0, left), 2),
            "mark_days": plan.mark_days,
            "hint_days": plan.hint_days,
            "to_auto": plan.to_auto,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "events": self.events[-10:],
        }

    def step(self, now: float, plan: Plan, current_mode: str) -> str | None:
        """推进一步：返回需要写入的新 mode，或 None 表示不动。

        Args:
            now: 当前时间戳。
            plan: 计划。
            current_mode: 当前实际模式。

        Returns:
            新 mode（`hint` / `auto`）或 None。
        """
        if not plan.enabled:
            return None
        if self.paused:
            return None
        if self.started_at <= 0:
            self.start(now, current_mode)
            return None

        days = self.elapsed_days(now)
        target = target_stage(days, plan)
        if rank(target) <= rank(current_mode):
            return None

        # 用户手动改过 mode：尊重用户，暂停自动放权（不写回、不覆盖）。
        if self.last_applied and current_mode != self.last_applied:
            self.paused = True
            self.pause_reason = (
                f"检测到手动切换（{self.last_applied} -> {current_mode}），已暂停自动放权"
            )
            self.events.append(
                {"ts": now, "event": "pause", "reason": self.pause_reason},
            )
            self.save()
            return None

        self.last_applied = target
        self.events.append(
            {
                "ts": now,
                "event": "upgrade",
                "from": current_mode,
                "to": target,
                "day": round(days, 2),
            },
        )
        self.save()
        return target


def now_ts() -> float:
    """当前时间戳（单独抽出来便于测试替换）。

    Returns:
        秒级时间戳。
    """
    return time.time()

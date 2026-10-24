"""结构化日志与调试快照。

分工（文档 §19）：日志说"发生了什么事"，统计说"累计到什么程度"，
快照说"现在正在发生什么"。三者职责不重叠，混用会得出"合并率在跳动"这类错觉。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from .config import PLUGIN_NAME

# 日志事件名 → 级别；集中在此便于"新加事件必须显式声明级别"
_LEVELS: dict[str, int] = {
    "aggregate.start": 20,        # info
    "aggregate.append": 10,       # debug
    "aggregate.pause": 20,
    "aggregate.typing_unknown": 10,
    "aggregate.typing_detected": 20,
    "aggregate.recall": 20,
    "aggregate.flush": 20,
    "aggregate.drop": 20,
    "aggregate.abort": 30,        # warning
    "aggregate.interrupt_timeout": 30,
    "aggregate.stale_timer": 10,
    "attach.persist_failed": 30,
    "attach.quota_exceeded": 30,
    "capability.missing": 30,
    "capability.detected": 20,
    "stats.persist_failed": 30,
    "stats.truncated": 20,
    "stats.reset": 20,
    "page.api_error": 30,
}


def log_event(name: str, **fields: Any) -> None:
    """输出一条结构化日志（`事件名 key=value ...`）。

    为什么不用 `logger.info(..., extra={...})`：AstrBot 的日志格式化器只渲染
    `%(message)s`，`extra` 里的字段不会出现在日志文件里；而把字段拼进 message
    才能被 grep 到——**可检索性比"结构化"更重要**。

    Args:
        name: 事件名（见 `_LEVELS`）。
        **fields: 附加字段。
    """
    level = _LEVELS.get(name, 20)
    parts = [f"[{PLUGIN_NAME}] {name}"]
    for key, value in fields.items():
        if isinstance(value, float):
            value = round(value, 3)
        parts.append(f"{key}={value!r}" if isinstance(value, str) and " " in value else f"{key}={value}")
    message = " ".join(parts)
    if level >= 30:
        logger.warning(message)
    elif level >= 20:
        logger.info(message)
    else:
        logger.debug(message)


def snapshot_state(store: Any, typing_guard_alive: Any = None) -> list[dict[str, Any]]:
    """调试快照：当前活跃 / 结算中的轮次（页面数据源之一）。

    ⚠️ **本函数必须全同步、内部禁止新增 `await`。**

    它遍历 `store.active` / `store.finalizing`；一旦遍历中途被其它协程修改，
    Python 会抛 `RuntimeError: dictionary changed size during iteration`。
    现在之所以安全，正是因为它通篇没有挂起点——遍历期间不会让出控制权。
    谁为了"顺便算点东西"在这里加 `await`，就会把它变成偶发崩溃。

    Args:
        store: SessionStore。
        typing_guard_alive: 可选，判断看门狗是否存活的可调用对象。

    Returns:
        行字典列表。
    """
    rows: list[dict[str, Any]] = []

    # 先对字典项做同步拷贝，再遍历拷贝
    for key, rnd in list(store.active.items()):
        timer = getattr(rnd, "timer", None)
        guard = getattr(rnd, "typing_guard", None)
        rows.append(
            {
                "umo": key[0],
                "sender": key[1],
                "round_id": rnd.round_id,
                "state": rnd.state.value,
                "phase": "active",
                "items": len(rnd.items),
                "remaining": round(timer.remaining(), 2) if timer is not None else 0.0,
                "typing": bool(rnd.typing),
                "guard_alive": bool(guard is not None and not guard.done()),
                "cooldown": round(store.cooldown_remaining(key), 2),
            }
        )

    for rnd in list(store.finalizing.values()):
        rows.append(
            {
                "umo": rnd.key[0],
                "sender": rnd.key[1],
                "round_id": rnd.round_id,
                "state": rnd.state.value,
                "phase": "finalizing",
                "items": len(rnd.items),
                "remaining": 0.0,
                "typing": bool(rnd.typing),
                "guard_alive": False,
                "cooldown": 0.0,
            }
        )

    return rows


def now_ts() -> float:
    """当前墙钟时间（仅用于展示，不用于计时）。

    Returns:
        时间戳。
    """
    return time.time()

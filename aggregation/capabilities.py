"""平台能力探测：把"这个平台支不支持"集中到一处，失败即降级。

为什么不做多层 `try/except ImportError`：那样只能回答"模块在不在"，回答不了
"这个适配器会不会投递这个事件"。这里改成**基于实际观测的能力探测**，并把结果
一次性打进启动日志（文档 §16「可诊断性是 A4 的落地」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---- typing 判定映射（P1-2）----
# 依据 NapCat 的 input_status 扩展事件，**不是** OneBot v11 标准字段。
# 换适配器可能失效，所以映射集中在这里，且判定失败一律 no-op（不影响合并）。
TYPING_EVENT_TYPES: dict[int, bool] = {1: True, 2: False}
TYPING_TEXT_MARKERS = ("正在输入", "typing", "composing")

RECALL_NOTICE_TYPES = ("group_recall", "friend_recall", "recall")
INPUT_STATUS_NOTICE_TYPES = ("input_status",)


def raw_of(event: Any) -> Any:
    """取事件的原始消息对象（dict 优先，setattr 兜底由调用方处理）。

    Args:
        event: 消息事件。

    Returns:
        raw_message 或 None。
    """
    obj = getattr(event, "message_obj", None)
    return getattr(obj, "raw_message", None) if obj is not None else None


def raw_get(event: Any, *keys: str, default: Any = None) -> Any:
    """从 raw_message 里按多个候选键取值。

    Args:
        event: 消息事件。
        *keys: 候选键，按顺序尝试。
        default: 都没命中时的返回值。

    Returns:
        取到的值或 default。
    """
    raw = raw_of(event)
    if raw is None:
        return default
    for key in keys:
        if isinstance(raw, dict):
            if key in raw and raw[key] is not None:
                return raw[key]
        else:
            value = getattr(raw, key, None)
            if value is not None:
                return value
    return default


def probe_typing(raw: Any) -> bool | None:
    """把一条 input_status 通知解析成"正在输入 / 停止输入 / 无法判定"。

    Args:
        raw: 原始通知对象（dict 或对象）。

    Returns:
        True=正在输入；False=停止输入；None=无法判定（调用方应 no-op）。
    """
    def _get(key: str) -> Any:
        if isinstance(raw, dict):
            return raw.get(key)
        return getattr(raw, key, None)

    event_type = _get("event_type")
    if event_type is not None:
        try:
            mapped = TYPING_EVENT_TYPES.get(int(event_type))
        except (TypeError, ValueError):
            mapped = None
        if mapped is not None:
            return mapped

    text = _get("status_text")
    if isinstance(text, str) and text.strip():
        lowered = text.lower()
        if any(marker in lowered or marker in text for marker in TYPING_TEXT_MARKERS):
            return True
        return False
    return None


def is_typing_notice(event: Any) -> bool:
    """该事件是否是一条"输入状态"通知。

    Args:
        event: 消息事件。

    Returns:
        True 表示是输入状态通知。
    """
    if raw_of(event) is None:
        return False
    if raw_get(event, "post_type") == "notice":
        notice = raw_get(event, "notice_type")
        if notice in INPUT_STATUS_NOTICE_TYPES:
            return True
        # 部分适配器把 input_status 放在 sub_type
        if raw_get(event, "sub_type") in INPUT_STATUS_NOTICE_TYPES:
            return True
    return raw_get(event, "event_type") is not None and (
        raw_get(event, "status_text") is not None
        or raw_get(event, "notice_type") in INPUT_STATUS_NOTICE_TYPES
    )


def is_recall_notice(event: Any) -> bool:
    """该事件是否是一条撤回通知。

    Args:
        event: 消息事件。

    Returns:
        True 表示是撤回通知。
    """
    if raw_of(event) is None:
        return False
    notice = raw_get(event, "notice_type")
    if notice in RECALL_NOTICE_TYPES:
        return True
    return raw_get(event, "post_type") == "notice" and bool(
        raw_get(event, "message_id") or raw_get(event, "target_id")
    ) and notice is not None


def recalled_message_id(event: Any) -> str | None:
    """从撤回通知里取出被撤回消息的 ID。

    Args:
        event: 撤回通知事件。

    Returns:
        消息 ID；取不到返回 None。
    """
    value = raw_get(event, "message_id", "msg_id", "target_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(slots=True)
class Capabilities:
    """平台能力。

    分两类，刻意不混：

    * **静态**（`probe()` 从事件对象上直接看得出）：临时文件跟踪、typing 反馈接口；
    * **动态**（只能等它真的发生一次才知道）：`input_status` 通知、撤回通知。
      适配器会不会投递这两类 notice，文档无法保证，所以由运行时观测记录
      （`mark_typing_seen` / `mark_recall_seen`），第一次看到时记一条 info。
    """

    temp_file_tracking: bool = False
    """事件是否提供 track_temporary_local_file（用于附件保活）。"""

    temp_file_untrack: bool = False
    """事件是否提供 untrack_temporary_local_file（缺失则复制失败时退化为 base64）。"""

    typing_feedback: bool = False
    """是否可能通过事件发送 typing 状态（各适配器差异大，保守探测）。"""

    typing_seen: bool = False
    """运行时是否真的收到过 input_status 通知。"""

    recall_seen: bool = False
    """运行时是否真的收到过撤回通知。"""

    def mark_typing_seen(self) -> bool:
        """记录"收到过输入状态通知"。

        Returns:
            True 表示这是第一次（调用方据此只记一次日志）。
        """
        if self.typing_seen:
            return False
        self.typing_seen = True
        return True

    def mark_recall_seen(self) -> bool:
        """记录"收到过撤回通知"。

        Returns:
            True 表示这是第一次。
        """
        if self.recall_seen:
            return False
        self.recall_seen = True
        return True

    def as_log_line(self) -> str:
        """一行能力摘要（写进启动日志）。

        Returns:
            形如 `input_status=? recall=? temp_file=Yes untrack=No` 的字符串。
        """
        def mark(flag: bool) -> str:
            return "Yes" if flag else "No"

        def seen(flag: bool) -> str:
            return "Yes" if flag else "未观测到"

        return (
            f"input_status={seen(self.typing_seen)}"
            f" recall={seen(self.recall_seen)}"
            f" temp_file={mark(self.temp_file_tracking)}"
            f" untrack={mark(self.temp_file_untrack)}"
            f" typing_feedback={mark(self.typing_feedback)}"
        )


def probe(event: Any) -> Capabilities:
    """对当前事件做一次**静态**能力探测。

    只探测"从这个事件对象上能直接观察到什么"，不猜平台名——平台名到能力的映射表
    永远会过时，而事件本身带着答案。动态能力（是否投递 notice）见 `Capabilities` 说明。

    Args:
        event: 消息事件。

    Returns:
        Capabilities（动态字段初始为 False）。
    """
    return Capabilities(
        temp_file_tracking=hasattr(event, "track_temporary_local_file"),
        temp_file_untrack=hasattr(event, "untrack_temporary_local_file"),
        typing_feedback=hasattr(event, "send_typing") or hasattr(event, "set_typing"),
    )

"""入参解析、出参重构、静默——与 AstrBot 事件对象打交道的**唯一**一层。

为什么要单独一层：事件对象的形态在真实环境里并不统一（`message_str` 在多个位置、
`raw_message` 可能是 dict 也可能是对象），把这些差异散落在业务逻辑里，
就会变成"某个平台能合并、某个平台不能"的玄学问题。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import At, AtAll, Image, Plain

from .config import PLUGIN_NAME
from .session import Item, Part, Round

# 图片引用里允许直接交给 Image.fromURL 的 scheme
_URL_SCHEMES = ("http://", "https://", "base64://", "data:")


@dataclass(slots=True)
class Parsed:
    """一条入站消息的解析结果。"""

    parts: list[Part] = field(default_factory=list)
    message_id: str | None = None
    image_bytes: int = 0
    """"缓存后实际落盘字节数"（退化路径取原文件大小 / base64 长度）。"""

    degraded: list[str] = field(default_factory=list)
    """因配额或失败被跳过的图片引用（仅用于日志）。"""

    def text(self) -> str:
        """文本部分。

        Returns:
            拼接后的文本。
        """
        return "".join(p.value for p in self.parts if p.kind == "text")

    def images(self) -> tuple[str, ...]:
        """图片引用。

        Returns:
            引用元组。
        """
        return tuple(p.value for p in self.parts if p.kind == "image")

    def has_content(self) -> bool:
        """是否有值得合并的内容。

        Returns:
            True 表示有文本或图片。
        """
        return bool(self.text().strip()) or bool(self.images())

    def to_item(self, arrived_at: float) -> Item:
        """转成不可变 Item。

        Args:
            arrived_at: 到达时间。

        Returns:
            Item。
        """
        return Item(
            message_id=self.message_id,
            parts=tuple(self.parts),
            arrived_at=arrived_at,
            image_bytes=self.image_bytes,
        )


def _component_source(comp: Any) -> str:
    """从 Image 组件里取出可用的引用（path 优先，其次 file，最后 url）。

    Args:
        comp: Image 组件。

    Returns:
        引用字符串；取不到返回空串。
    """
    for attr in ("path", "file", "url"):
        value = getattr(comp, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _format_at(comp: Any) -> str:
    """把 At 渲染成文本占位。

    Args:
        comp: At 组件。

    Returns:
        文本。
    """
    name = getattr(comp, "name", "") or getattr(comp, "qq", "")
    return f"@{name}" if name else "@"


def parse_incoming(event: Any, cfg: Any) -> Parsed:
    """解析一条入站消息（纯函数，不修改事件）。

    Args:
        event: 消息事件。
        cfg: 配置（用到短消息阈值以外的解析开关时从这里取）。

    Returns:
        Parsed。
    """
    parsed = Parsed()
    message_id = _event_message_id(event)
    parsed.message_id = message_id

    obj = getattr(event, "message_obj", None)
    components = list(getattr(obj, "message", None) or [])
    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")

    if components:
        for comp in components:
            if isinstance(comp, Plain):
                text = getattr(comp, "text", "") or ""
                if text:
                    parsed.parts.append(Part(kind="text", value=text))
            elif isinstance(comp, Image):
                source = _component_source(comp)
                if source:
                    parsed.parts.append(Part(kind="image", value=source))
            elif isinstance(comp, AtAll):
                parsed.parts.append(Part(kind="text", value="@全体成员"))
            elif isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq and qq == self_id:
                    # 剥除指向机器人自己的 @（文档 §12.3）；剥除数只记 debug
                    logger.debug(f"[{PLUGIN_NAME}] 剥除指向自身的 @ 提及")
                    continue
                parsed.parts.append(Part(kind="text", value=_format_at(comp)))
    else:
        # 没有组件信息时退回 message_str（部分适配器只给这个）
        text = str(getattr(event, "message_str", "") or "")
        if text:
            parsed.parts.append(Part(kind="text", value=text))

    return parsed


def _event_message_id(event: Any) -> str | None:
    """取消息 ID（撤回索引的键）。

    Args:
        event: 消息事件。

    Returns:
        消息 ID；取不到返回 None（**缺失不进索引**，否则所有缺失消息会共享 None 键）。
    """
    obj = getattr(event, "message_obj", None)
    value = getattr(obj, "message_id", None) if obj is not None else None
    if value is None:
        value = getattr(event, "message_id", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_command(text: str, prefixes: list[str]) -> bool:
    """判断是否是指令消息。

    Args:
        text: 消息文本。
        prefixes: 指令前缀列表。

    Returns:
        True 表示应当打断聚合。
    """
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    return any(prefix and stripped.startswith(prefix) for prefix in prefixes)


def silence(event: Any) -> None:
    """让事件停止传播且不产生输出（公开 API）。

    Args:
        event: 消息事件。
    """
    event.stop_event()


def _build_image(ref: str) -> Any:
    """按引用类型构造 Image 组件。

    Args:
        ref: 图片引用。

    Returns:
        Image 组件；失败抛异常由调用方处理。
    """
    if ref.startswith(_URL_SCHEMES):
        return Image.fromURL(ref)
    if os.path.isabs(ref) and os.path.exists(ref):
        return Image.fromFileSystem(ref)
    return Image(file=ref)


def build_chain(text: str, images: list[str], cfg: Any) -> list[Any]:
    """构造组件链（文本段 + 图片段）。

    Args:
        text: 合并后的文本。
        images: 图片引用列表。
        cfg: 配置。

    Returns:
        组件列表。单图失败只丢该图（文档 §11.3）。
    """
    chain: list[Any] = []
    if text:
        chain.append(Plain(text=text))
    for ref in images:
        try:
            chain.append(_build_image(ref))
        except Exception as exc:  # noqa: BLE001 - 单图失败不影响整轮
            logger.warning(f"[{PLUGIN_NAME}] 构造图片组件失败，已跳过：{ref[:60]} ({exc})")
    return chain


def build_chain_ordered(snapshot: Any, images: list[str], cfg: Any) -> list[Any]:
    """按 Part 顺序构造组件链（`preserve_part_order=true`）。

    消费规则（文档 §11.3）：同引用只消费一次；快照里出现两次而引用只有一次时，
    首次保留、后续视为丢弃；剩余引用按原顺序追加链尾。

    Args:
        snapshot: 冻结快照。
        images: 可用于构造的图片引用列表。
        cfg: 配置。

    Returns:
        组件列表。
    """
    pool = list(images)
    consumed: set[int] = set()
    chain: list[Any] = []

    for item in snapshot.items:
        for part in item.parts:
            if part.kind == "text":
                if part.value:
                    chain.append(Plain(text=part.value))
                continue
            found = None
            for idx, ref in enumerate(pool):
                if idx in consumed:
                    continue
                if ref == part.value:
                    found = idx
                    break
            if found is None:
                continue
            consumed.add(found)
            try:
                chain.append(_build_image(pool[found]))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 构造图片组件失败，已跳过：{pool[found][:60]} ({exc})")

    # 增强包新增的图（若有）追加到链尾
    for idx, ref in enumerate(pool):
        if idx in consumed:
            continue
        try:
            chain.append(_build_image(ref))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 构造图片组件失败，已跳过：{ref[:60]} ({exc})")
    return chain


def sync_raw_message(event: Any, text: str, images: list[str]) -> None:
    """尽量同步 `raw_message`，照顾读它的 aiocqhttp 系插件。

    Args:
        event: 消息事件。
        text: 合并后文本。
        images: 图片引用。
    """
    obj = getattr(event, "message_obj", None)
    raw = getattr(obj, "raw_message", None) if obj is not None else None
    if not isinstance(raw, dict):
        return
    segments: list[dict[str, Any]] = []
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    for ref in images:
        segments.append({"type": "image", "data": {"file": ref}})
    try:
        raw["message"] = segments
        raw["raw_message"] = text
    except Exception as exc:  # noqa: BLE001 - 只影响读 raw 的少数插件
        logger.debug(f"[{PLUGIN_NAME}] 同步 raw_message 失败（已忽略）：{exc}")


def reconstruct(event: Any, text: str, images: list[str], cfg: Any) -> None:
    """把合并结果写回事件（三处字段保持一致，G2）。

    Args:
        event: Owner 事件。
        text: 合并后文本。
        images: 图片引用列表。
        cfg: 配置。
    """
    event.message_str = text
    obj = getattr(event, "message_obj", None)
    if obj is not None:
        obj.message_str = text
        obj.message = build_chain(text, images, cfg)
    sync_raw_message(event, text, images)


def reconstruct_ordered(event: Any, snapshot: Any, images: list[str], cfg: Any) -> None:
    """保序版重构。

    Args:
        event: Owner 事件。
        snapshot: 冻结快照。
        images: 图片引用列表。
        cfg: 配置。
    """
    text = snapshot.text(cfg.separator)
    event.message_str = text
    obj = getattr(event, "message_obj", None)
    if obj is not None:
        obj.message_str = text
        obj.message = build_chain_ordered(snapshot, images, cfg)
    # 保序时 message_str 仍是纯文本拼接：下游不能假设它与组件一一对应
    sync_raw_message(event, text, images)


def key_of(event: Any) -> tuple[str, str]:
    """算桶键 `(umo, sender_id)`（I8：群聊桶键必须含 sender_id）。

    Args:
        event: 消息事件。

    Returns:
        桶键。
    """
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    sender = ""
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            sender = str(getter() or "")
        except Exception:  # noqa: BLE001
            sender = ""
    return (umo, sender)


def umo_of(event: Any) -> str:
    """取统一消息来源。

    Args:
        event: 消息事件。

    Returns:
        UMO 字符串。
    """
    return str(getattr(event, "unified_msg_origin", "") or "")


def channel_of(event: Any) -> str:
    """判断渠道（private / group）。

    Args:
        event: 消息事件。

    Returns:
        `private` 或 `group`。
    """
    checker = getattr(event, "is_private_chat", None)
    if callable(checker):
        try:
            return "private" if checker() else "group"
        except Exception:  # noqa: BLE001
            pass
    return "group"


def round_tail_char(rnd: Round) -> str:
    """取轮次末字符（转发到 Round，便于调用方少 import）。

    Args:
        rnd: 轮次。

    Returns:
        单个字符或空串。
    """
    return rnd.tail_char()

"""附件生命周期：把临时图片在事件结束前"抢救"到插件缓存目录。

问题（文档 §13.1）：`event_context` 退出时会 `cleanup_temporary_local_files()`，
而第 2+ 条消息的事件在防抖窗口内就结束了——等结算时，它的临时图片**已经被删掉**。
所以必须在 ingest 时复制出来，而不是在 finalize 时再去找原文件。

`untrack` 语义（§13.3，这里最容易写错）：

* 复制**成功** → **不** untrack：原文件由 core 在事件退出时清理，副本由 TTL 管理；
* 复制**失败** → 尝试 untrack 保活原文件（退化为"依赖原文件"）；平台不支持 untrack
  （AstrBot 4.26.5 就没有这个 API）→ 退化为 base64 内联。
* **"复制后立即 untrack" 是错的**：原文件既不会被 core 清理、也不在 TTL 内，形成泄漏。
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import PLUGIN_NAME
from .event_io import Parsed

_URL_SCHEMES = ("http://", "https://", "base64://", "data:")


def _is_url(ref: str) -> bool:
    """是否远程/内联引用。

    Args:
        ref: 引用。

    Returns:
        True 表示不是本地文件路径。
    """
    return ref.startswith(_URL_SCHEMES)


class Attachments:
    """图片缓存与配额。"""

    def __init__(self, *, data_dir: Path, cfg: Any, clock: Any) -> None:
        """初始化。

        Args:
            data_dir: 插件数据目录。
            cfg: 配置。
            clock: 时间源。
        """
        self.cfg = cfg
        self.clock = clock
        self.cache_dir = data_dir / "attachments"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ ingest
    async def persist(self, parsed: Parsed, event: Any = None) -> Parsed:
        """把本地的临时图片复制到缓存目录，替换引用。

        Args:
            parsed: 解析结果（就地修改并返回）。
            event: 消息事件（用于 track/untrack 探测）。

        Returns:
            修改后的 Parsed。
        """
        total = 0
        kept: list[str] = []
        new_parts = []

        for part in parsed.parts:
            if part.kind != "image":
                new_parts.append(part)
                continue
            ref = part.value
            if _is_url(ref):
                kept.append(ref)
                new_parts.append(part)
                continue

            size = _safe_size(ref)
            if size is not None and size > self.cfg.keep_max_bytes:
                logger.warning(
                    f"[{PLUGIN_NAME}] 单图超过上限 {self.cfg.keep_max_bytes}，跳过：{ref[:60]}"
                )
                parsed.degraded.append(ref)
                continue
            if size is not None and total + size > self.cfg.keep_total_max_bytes:
                logger.warning(
                    f"[{PLUGIN_NAME}] 单条消息图片总量超限，跳过剩余图片：{ref[:60]}"
                )
                parsed.degraded.append(ref)
                continue

            try:
                cached, written = self._copy_to_cache(ref)
            except Exception as exc:  # noqa: BLE001 - 复制失败要走降级链
                degraded = self._degrade(ref, event, exc)
                if degraded is None:
                    parsed.degraded.append(ref)
                    continue
                total += len(degraded)
                parsed.image_bytes += len(degraded)
                kept.append(degraded)
                new_parts.append(type(part)(kind="image", value=degraded))
                parsed.degraded.append(ref)
                continue

            total += written
            parsed.image_bytes += written
            kept.append(cached)
            new_parts.append(type(part)(kind="image", value=cached))

        parsed.parts = new_parts
        return parsed

    def _copy_to_cache(self, ref: str) -> tuple[str, int]:
        """内容哈希命名复制到缓存目录（流式 sha256，避免整图进内存）。

        Args:
            ref: 本地文件路径。

        Returns:
            (缓存路径, 字节数)。

        Raises:
            OSError: 源文件不可读。
        """
        src = Path(ref)
        if not src.is_file():
            raise FileNotFoundError(f"临时文件不存在: {ref}")

        digest = hashlib.sha256()
        with src.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        name = digest.hexdigest()[:32] + (src.suffix or ".bin")
        target = self.cache_dir / name
        if target.is_file():
            return str(target), target.stat().st_size

        written = 0
        tmp = target.with_suffix(target.suffix + ".part")
        with src.open("rb") as handle, tmp.open("wb") as out:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                out.write(chunk)
                written += len(chunk)
        os.replace(tmp, target)
        return str(target), written

    def _degrade(self, ref: str, event: Any, exc: Exception) -> str | None:
        """复制失败时的降级链：untrack 保活 → base64 → 放弃。

        Args:
            ref: 原引用。
            event: 消息事件。
            exc: 触发降级的异常。

        Returns:
            可用的新引用（原路径或 base64）；无法降级时返回 None。
        """
        logger.warning(
            f"[{PLUGIN_NAME}] attach.persist_failed ref={ref[:60]} reason={exc}"
        )
        untrack = getattr(event, "untrack_temporary_local_file", None) if event else None
        if callable(untrack):
            try:
                untrack(ref)  # 保活原文件，退化为"依赖原文件"
                logger.info(f"[{PLUGIN_NAME}] 已 untrack 原文件以保活：{ref[:60]}")
                return ref
            except Exception as untrack_exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] untrack 失败：{untrack_exc}")

        try:
            payload = Path(ref).read_bytes()
        except Exception as read_exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 读取失败，放弃该图：{read_exc}")
            return None
        if len(payload) > self.cfg.keep_max_bytes:
            logger.warning(f"[{PLUGIN_NAME}] base64 降级后仍超限，放弃该图：{ref[:60]}")
            return None
        logger.info(f"[{PLUGIN_NAME}] 退化为 base64 内联（{len(payload)} 字节）")
        return "base64://" + base64.b64encode(payload).decode("ascii")

    # ------------------------------------------------------------------ finalize
    def normalize(self, images: list[str]) -> list[str]:
        """结算前校验引用：本地缓存必须还在。

        Args:
            images: 引用列表。

        Returns:
            可用引用列表（缺失的已丢弃并记 warning）。
        """
        out: list[str] = []
        for ref in images:
            if _is_url(ref):
                out.append(ref)
                continue
            try:
                if Path(ref).is_file():
                    out.append(ref)
                    continue
            except OSError:
                pass
            logger.warning(
                f"[{PLUGIN_NAME}] 缓存图片已不存在，丢弃：{ref[:60]}"
                f"（可能是 TTL 清理与结算并发，见文档 §13.6）"
            )
        return out

    # ------------------------------------------------------------------ TTL
    def cleanup(self) -> int:
        """按 mtime 清理过期缓存。

        Returns:
            删除的文件数。
        """
        ttl = float(self.cfg.cache_dir_ttl_hours) * 3600.0
        if ttl <= 0:
            return 0
        now = time.time()
        removed = 0
        try:
            for entry in self.cache_dir.iterdir():
                if not entry.is_file():
                    continue
                try:
                    if now - entry.stat().st_mtime > ttl:
                        entry.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        except OSError as exc:
            logger.debug(f"[{PLUGIN_NAME}] 清理缓存失败：{exc}")
            return removed
        if removed:
            logger.info(f"[{PLUGIN_NAME}] 清理过期图片缓存 {removed} 个")
        return removed


def _safe_size(ref: str) -> int | None:
    """取本地文件大小（取不到返回 None，视为"未知大小"放行）。

    Args:
        ref: 引用。

    Returns:
        字节数或 None。
    """
    try:
        path = Path(ref)
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None

"""每日复盘：把当天的话题判定流水整理成一份可读报告（v0.10.0）。

用户要的是"每天自动用模型整理数据"：判定本身在 mark 阶段只记日志，人很难从
info.log 里看出"这几天判得准不准"。所以这里做三件事：

1. **流水落盘**：每条判定追加一行 JSONL（`review/judgments-YYYY-MM-DD.jsonl`），
   只留最近 N 天，避免无限增长；
2. **统计摘要**：条数、新话题占比、置信度分布、低置信度样例（纯本地计算，不花钱）；
3. **模型复盘**：把摘要交给模型写一段"这几天判得怎么样 / 哪里可能误判 / 要不要进下一档"。
   模型不可用时**退回纯文本摘要**，绝不能因为复盘失败影响正常聊天。

报告存成 `review/YYYY-MM-DD.md`，同时可以通过页面接口读最新一份。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REVIEW_SYSTEM_PROMPT = """你是「话题路由器」插件的复盘助手。

你会收到某一天的话题判定统计与若干样例。请输出一段简洁的中文复盘，必须包含：

1. **一句话结论**：这一天判得准不准（新话题占比是否合理、低置信度是否偏多）；
2. **可疑判定**：从样例里挑 1~3 条最可能是误判的，说明理由；
3. **放权建议**：明确说"建议继续观察 / 可以进入 hint / 可以进入 auto"，并给出理由。

要求：只输出这 3 段，不要再加标题以外的话；不要编造样例里没有的内容；
不确定就直说"样本太少，判断依据不足"。"""

MAX_TEXT = 120


def _day_str(ts: float) -> str:
    """把时间戳格式化成本地日期字符串。

    Args:
        ts: 时间戳。

    Returns:
        `YYYY-MM-DD`。
    """
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


@dataclass
class JudgmentLog:
    """判定流水（按天分文件，JSONL）。"""

    directory: Path
    keep_days: int = 14

    def path_of(self, day: str) -> Path:
        """某天的流水文件路径。

        Args:
            day: `YYYY-MM-DD`。

        Returns:
            路径。
        """
        return Path(self.directory) / f"judgments-{day}.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        """追加一条判定记录（写失败只吞掉，绝不影响判定）。

        Args:
            record: 记录；缺 `ts` 时用当前时间。
        """
        try:
            ts = float(record.get("ts") or time.time())
            record = {**record, "ts": ts}
            path = self.path_of(_day_str(ts))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - 复盘是旁路，不能影响主流程
            return

    def read(self, day: str) -> list[dict[str, Any]]:
        """读某天的记录（坏行跳过）。

        Args:
            day: `YYYY-MM-DD`。

        Returns:
            记录列表。
        """
        path = self.path_of(day)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def read_day(self, day: str | None = None) -> list[dict[str, Any]]:
        """读指定日期（默认今天）。

        Args:
            day: `YYYY-MM-DD`；None 表示今天。

        Returns:
            记录列表。
        """
        return self.read(day or _day_str(time.time()))

    def prune(self, now: float | None = None) -> int:
        """清理超过保留天数的流水文件。

        Args:
            now: 当前时间戳。

        Returns:
            删掉的文件数。
        """
        now = now if now is not None else time.time()
        cutoff = (datetime.fromtimestamp(now) - timedelta(days=max(1, self.keep_days))).date()
        removed = 0
        try:
            files = sorted(Path(self.directory).glob("judgments-*.jsonl"))
        except OSError:
            return 0
        for path in files:
            day = path.stem.removeprefix("judgments-")
            try:
                when = date.fromisoformat(day)
            except ValueError:
                continue
            if when < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed


@dataclass
class ReviewStore:
    """复盘报告存储（每天一份 markdown）。"""

    directory: Path

    def save(self, day: str, text: str) -> Path:
        """写一份报告。

        Args:
            day: `YYYY-MM-DD`。
            text: markdown 正文。

        Returns:
            文件路径。
        """
        path = Path(self.directory) / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def latest(self) -> dict[str, Any] | None:
        """读最新一份报告。

        Returns:
            `{"day", "text", "path"}`；没有则为 None。
        """
        try:
            files = sorted(Path(self.directory).glob("*.md"))
        except OSError:
            return None
        if not files:
            return None
        path = files[-1]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return {"day": path.stem, "text": text, "path": str(path)}


def summarize(records: list[dict[str, Any]], day: str) -> dict[str, Any]:
    """把流水整理成统计摘要（纯函数）。

    Args:
        records: 判定记录。
        day: 日期字符串（只用于回填）。

    Returns:
        摘要字典。
    """
    total = len(records)
    new_count = sum(1 for r in records if str(r.get("verdict")) == "new")
    buckets = {"high": 0, "mid": 0, "low": 0, "very_low": 0}
    sources: dict[str, int] = {}
    modes: dict[str, int] = {}
    sessions: set[str] = set()
    samples: list[dict[str, Any]] = []
    for rec in records:
        try:
            conf = float(rec.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= 0.8:
            buckets["high"] += 1
        elif conf >= 0.6:
            buckets["mid"] += 1
        elif conf >= 0.45:
            buckets["low"] += 1
        else:
            buckets["very_low"] += 1
        src = str(rec.get("source") or "?")
        sources[src] = sources.get(src, 0) + 1
        mode = str(rec.get("mode") or "?")
        modes[mode] = modes.get(mode, 0) + 1
        umo = str(rec.get("umo") or "")
        if umo:
            sessions.add(umo)
        if str(rec.get("verdict")) == "new" and conf < 0.6 and len(samples) < 6:
            samples.append(
                {
                    "text": str(rec.get("text") or "")[:MAX_TEXT],
                    "confidence": round(conf, 2),
                    "source": src,
                    "umo": umo,
                },
            )
    return {
        "day": day,
        "total": total,
        "new_topic": new_count,
        "continue": total - new_count,
        "new_ratio": round(new_count / total, 3) if total else 0.0,
        "sessions": len(sessions),
        "buckets": buckets,
        "sources": sources,
        "modes": modes,
        "samples": samples,
    }


def fallback_text(stats: dict[str, Any]) -> str:
    """模型不可用时的人类可读摘要（纯本地）。

    Args:
        stats: `summarize()` 的结果。

    Returns:
        markdown 正文。
    """
    if not stats.get("total"):
        return (
            f"# {stats.get('day', '')} 话题判定复盘\n\n"
            "今天没有可用的判定记录（插件未判定、或当天没有消息）。\n"
        )
    lines = [
        f"# {stats['day']} 话题判定复盘（本地摘要）",
        "",
        f"- 判定 {stats['total']} 次：新话题 {stats['new_topic']} 次、延续 {stats['continue']} 次"
        f"（新话题占比 {stats['new_ratio']:.0%}）",
        f"- 覆盖会话 {stats['sessions']} 个；置信度分布："
        f"≥0.8 {stats['buckets']['high']}、0.6~0.8 {stats['buckets']['mid']}、"
        f"0.45~0.6 {stats['buckets']['low']}、<0.45 {stats['buckets']['very_low']}",
        f"- 判定来源：{stats['sources']}",
    ]
    if stats["samples"]:
        lines += ["", "## 低置信度的新话题（值得人工看一眼）"]
        for item in stats["samples"]:
            lines.append(
                f"- {item['text']!r}（置信度 {item['confidence']}，来源 {item['source']}）",
            )
    lines += ["", "> 模型复盘不可用（没有可用 provider 或调用失败），以上为本地统计。"]
    return "\n".join(lines) + "\n"


def build_prompt(stats: dict[str, Any]) -> str:
    """把摘要拼成给模型的复盘提示词。

    Args:
        stats: `summarize()` 的结果。

    Returns:
        提示词。
    """
    lines = [
        f"日期：{stats['day']}",
        f"判定次数：{stats['total']}（新话题 {stats['new_topic']} / 延续 {stats['continue']}，"
        f"新话题占比 {stats['new_ratio']:.0%}）",
        f"覆盖会话数：{stats['sessions']}",
        "置信度分布："
        f"≥0.8 {stats['buckets']['high']}；0.6~0.8 {stats['buckets']['mid']}；"
        f"0.45~0.6 {stats['buckets']['low']}；<0.45 {stats['buckets']['very_low']}",
        f"判定来源：{stats['sources']}",
    ]
    if stats["samples"]:
        lines.append("低置信度新话题样例：")
        for item in stats["samples"]:
            lines.append(f"- {item['text']!r}（{item['confidence']}）")
    else:
        lines.append("低置信度新话题样例：无")
    return "\n".join(lines)


def compose_review(stats: dict[str, Any], model_text: str) -> str:
    """把模型复盘与本地统计合成一份报告。

    Args:
        stats: 统计摘要。
        model_text: 模型输出（可能为空）。

    Returns:
        markdown 正文。
    """
    body = (model_text or "").strip()
    head = [
        f"# {stats['day']} 话题判定复盘",
        "",
        f"- 判定 {stats['total']} 次：新话题 {stats['new_topic']}、延续 {stats['continue']}"
        f"（新话题占比 {stats['new_ratio']:.0%}）",
        f"- 会话 {stats['sessions']} 个；来源 {stats['sources']}",
    ]
    if not body:
        return fallback_text(stats)
    return "\n".join(head) + "\n\n## 模型复盘\n\n" + body + "\n"

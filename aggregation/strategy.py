"""等待策略：**纯函数**，只依赖传入的数值。

为什么必须是纯函数：它是整条链路里唯一"决定等多久"的地方，也是变体最多的地方
（固定 / 自适应 / 总上限 / cooldown）。把它做成纯函数，就能用 FakeClock 在毫秒内
跑完 12 秒、60 秒的场景，而不像参考实现那样只能真等。
"""

from __future__ import annotations

from typing import Any

# 结尾标点：句子已说完，可以稍微早点结算
_TERMINAL_PUNCT = tuple("。！？.!?…")
# 表示"话没说完"的结尾：绝不提前结算
_OPEN_PUNCT = tuple("，,、；;：:")

# 自适应加成的档位（秒）——数值刻意取小，避免把总时长推到用户可感知的"卡"
_LONG_BONUS = 1.5
_MID_BONUS = 0.8
_SHORT_BONUS = 0.5
_STREAK_BONUS = 0.5
_TERMINAL_DISCOUNT = 0.4
_UNFINISHED_BONUS = 1.0
"""句末不是终止标点时的加成（v0.9.3 引入，v0.9.4 修正生效条件）。

中文聊天里绝大多数消息**不以标点结尾**（"帮我看看 NAS 硬盘"），而这恰恰是
"话可能还没说完、下一条马上就来"的最强信号。v0.9.2 之前只有"句末是逗号"才加时间，
于是连发的消息总是差 0.3~1 秒落在窗口外，聚合看起来像没生效。

实测（2026-10-25，NAS）：用户连发的相邻消息间隔 2.4~3.9 秒，而窗口只有
2.0~2.5 秒。加这 1.0 秒后，短句的有效窗口变成 2.0 + 0.5 + 1.0 = 3.5 秒，
刚好覆盖，同时**不拖慢**已经用句号/问号/感叹号收尾的句子。

> v0.9.3 首次实现时把条件写成"`tail_char` 为空才加"，而空串只出现在整轮没有
> 文本的时候——等于这个加成从未生效。v0.9.4 改成"句末字符不属于终止标点"。
"""


def adaptive_wait(
    *,
    text_len: int,
    tail_char: str,
    short_streak: int,
    cfg: Any,
) -> float:
    """计算自适应等待（不含总上限与 cooldown）。

    Args:
        text_len: 当前合并后的文本长度。
        tail_char: 最后一条消息的最后一个字符（空串表示无文本）。
        short_streak: 连续短消息计数。
        cfg: 配置对象（需要 adaptive_* 与 debounce_time 字段）。

    Returns:
        等待秒数，已夹到 [adaptive_min_wait, adaptive_max_wait]。
    """
    wait = float(cfg.debounce_time)

    if text_len >= 60:
        wait += _LONG_BONUS
    elif text_len >= 30:
        wait += _MID_BONUS
    elif text_len <= int(cfg.short_message_threshold):
        # 短句：用户很可能还在边想边打，多留一点时间
        wait += _SHORT_BONUS

    if tail_char in _OPEN_PUNCT:
        wait += _SHORT_BONUS
    elif tail_char in _TERMINAL_PUNCT:
        wait -= _TERMINAL_DISCOUNT
    else:
        # 句末不是终止标点（中文聊天里最常见："帮我看看 NAS 硬盘"），按未完句多等一点。
        # 注意：v0.9.3 这里误写成"tail_char 为空（整轮没有文本）才加"，等于永远不生效；
        # 现场扫窗口得到 2.5s（旧值）才发现的。见 `_UNFINISHED_BONUS` 的说明。
        wait += _UNFINISHED_BONUS

    if short_streak >= 2:
        wait += _STREAK_BONUS

    low = float(cfg.adaptive_min_wait)
    high = float(cfg.adaptive_max_wait)
    return max(low, min(high, wait))


def compute(
    *,
    now: float,
    started_at: float,
    text_len: int,
    tail_char: str,
    short_streak: int,
    cooldown_remaining: float,
    cfg: Any,
) -> float:
    """计算本轮下一次等待秒数（文档 §8.4 的计算顺序）。

    调用约束：typing 暂停期间调用方**不得**调用本函数（暂停有自己的恢复语义）。

    计算顺序（后者覆盖前者）：
    1. 非自适应 → `debounce_time`；自适应 → `adaptive_wait()`；
    2. 总上限 → `min(w, started_at + adaptive_max_total_wait - now)`；
    3. cooldown → `max(w, cooldown_remaining)`，仅当 `debounce < min_interval`（文档 §10.2）。

    Args:
        now: 当前时间。
        started_at: 本轮开始时间。
        text_len: 合并后文本长度。
        tail_char: 最后一条消息末字符。
        short_streak: 连续短消息计数。
        cooldown_remaining: 同桶 cooldown 剩余秒数（<= 0 表示无）。
        cfg: 配置对象。

    Returns:
        等待秒数；**<= 0 表示应立即结算**，调用方直接 `_on_fire(rnd)`。
    """
    if cfg.adaptive:
        wait = adaptive_wait(
            text_len=text_len, tail_char=tail_char,
            short_streak=short_streak, cfg=cfg,
        )
    else:
        wait = float(cfg.debounce_time)

    total_left = (started_at + float(cfg.adaptive_max_total_wait)) - now
    wait = min(wait, total_left)

    # cooldown 只在"防抖比最小间隔还短"时才作为保护网生效；
    # 默认 debounce(2.0) > min_interval_group(0.5)，因此不会引入额外延迟。
    if cooldown_remaining > 0:
        wait = max(wait, cooldown_remaining)

    return wait

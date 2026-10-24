"""配置模型：typed 默认值 + 三档预设 + 校验。

设计约束（见技术设计文档 §15）：

* **默认值只在这里与 `_conf_schema.json` 各写一次**，由测试断言两者逐项一致。
  参考实现曾出现 `enable_adaptive_debounce` 代码里是 `True`、schema 里是 `false`
  的漂移，用户看到的值和实际生效的值不同，排障时极其误导。
* 校验失败**回退默认并记 error**，不是静默禁用整个插件。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_topic_router_pro"

PRESET_FAST = "fast"
PRESET_BALANCED = "balanced"
PRESET_PATIENT = "patient"
PRESET_NAMES = (PRESET_FAST, PRESET_BALANCED, PRESET_PATIENT)

# 预设只覆盖这四项，其余保持默认（文档 §15.1）
PRESETS: dict[str, dict[str, Any]] = {
    PRESET_FAST: {
        "debounce_time": 1.0,
        "adaptive": False,
        "adaptive_max_wait": 6.0,
        "adaptive_max_total_wait": 12.0,
    },
    PRESET_BALANCED: {
        "debounce_time": 2.0,
        "adaptive": True,
        "adaptive_max_wait": 6.0,
        "adaptive_max_total_wait": 12.0,
    },
    PRESET_PATIENT: {
        "debounce_time": 3.5,
        "adaptive": True,
        "adaptive_max_wait": 8.0,
        "adaptive_max_total_wait": 20.0,
    },
}

# 扁平旧键 → 新键；只做显式映射，不做"所有分组拍平"
LEGACY_ALIASES: dict[str, str] = {
    "enable": "enabled",
    "wait_time": "debounce_time",
    "window": "debounce_time",
    "max_wait": "adaptive_max_total_wait",
}


@dataclass(slots=True)
class Config:
    """插件的全部配置。字段顺序无关，但默认值必须与 schema 一致。"""

    enabled: bool = True
    command_prefixes: list[str] = field(default_factory=lambda: ["/"])
    separator: str = "\n"
    preset: str = PRESET_BALANCED
    debounce_time: float = 2.0
    adaptive: bool = True
    adaptive_min_wait: float = 1.0
    adaptive_max_wait: float = 6.0
    adaptive_max_total_wait: float = 12.0
    short_message_threshold: int = 10
    recall_filter: bool = True
    typing_detection: bool = True
    max_typing_wait: float = 60.0
    typing_feedback: bool = True
    group_enabled: bool = True
    min_interval_group: float = 0.5
    min_interval_private: float = 0.0
    interrupt_wait: float = 2.0
    preserve_part_order: bool = False
    keep_max_bytes: int = 20 * 1024 * 1024
    keep_total_max_bytes: int = 50 * 1024 * 1024
    keep_round_max_bytes: int = 80 * 1024 * 1024
    cache_dir_ttl_hours: float = 24.0
    enrich_deadline: float = 12.0
    # ---- 统计与页面 ----
    stats_flush_interval: float = 60.0
    stats_flush_every: int = 50
    stats_max_dimensions: int = 128
    stats_history_size: int = 200
    page_enabled: bool = True
    page_push_interval: float = 1.0


def _clamp(value: float, low: float, high: float) -> float:
    """把数值夹到闭区间内。

    Args:
        value: 原值。
        low: 下界。
        high: 上界。

    Returns:
        夹紧后的值。
    """
    return max(low, min(high, value))


def _as_bool(raw: Any, default: bool) -> bool:
    """宽松解析布尔值（配置来自 WebUI，可能是字符串）。

    Args:
        raw: 原始值。
        default: 缺省值。

    Returns:
        解析结果。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if raw is None:
        return default
    return bool(raw)


def _as_float(raw: Any, default: float) -> float:
    """宽松解析浮点。

    Args:
        raw: 原始值。
        default: 缺省值。

    Returns:
        解析结果。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _as_int(raw: Any, default: int) -> int:
    """宽松解析整数。

    Args:
        raw: 原始值。
        default: 缺省值。

    Returns:
        解析结果。
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _as_str_list(raw: Any, default: list[str]) -> list[str]:
    """解析字符串列表（可能是逗号分隔的字符串）。

    Args:
        raw: 原始值。
        default: 缺省值。

    Returns:
        解析结果。
    """
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    if isinstance(raw, str):
        parts = [item.strip() for item in raw.replace("，", ",").split(",")]
        return [item for item in parts if item]
    return list(default)


def _apply_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    """把旧扁平键映射到新键。

    Args:
        raw: 原始配置。

    Returns:
        映射后的新字典（不修改入参）。
    """
    out = dict(raw)
    for old, new in LEGACY_ALIASES.items():
        if old in out and new not in out:
            out[new] = out.pop(old)
    return out


def _coerce(raw: dict[str, Any]) -> Config:
    """按 dataclass 字段名逐个宽松解析。

    Args:
        raw: 已应用别名的配置字典。

    Returns:
        Config 实例。
    """
    defaults = Config()
    kwargs: dict[str, Any] = {}
    for item in fields(Config):
        name = item.name
        default = getattr(defaults, name)
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(default, bool):
            kwargs[name] = _as_bool(value, default)
        elif isinstance(default, float):
            kwargs[name] = _as_float(value, default)
        elif isinstance(default, int):
            kwargs[name] = _as_int(value, default)
        elif isinstance(default, list):
            kwargs[name] = _as_str_list(value, default)
        else:
            kwargs[name] = str(value)
    return Config(**kwargs)


def apply_preset(cfg: Config, name: str | None = None) -> Config:
    """按预设**完整覆盖**那几项（页面切换预设时用）。

    注意与 `load_config` 的区别：这里预设优先，因为用户是在页面上主动点"切换预设"，
    期望的就是预设的整套值。

    Args:
        cfg: 已解析的配置。
        name: 目标预设名；缺省用 `cfg.preset`。

    Returns:
        覆盖后的新配置。
    """
    target = name or cfg.preset
    resolved = target if target in PRESET_NAMES else PRESET_BALANCED
    if target != resolved:
        logger.warning(f"[{PLUGIN_NAME}] 未知预设 {target!r}，回退 {PRESET_BALANCED}")
    return replace(cfg, preset=resolved, **PRESETS[resolved])


def validate(cfg: Config) -> Config:
    """校验并夹紧各项，非法值回退默认（记 error，不放行）。

    除 `preset` 的应用外，本函数是**幂等**的：对已合法的配置再跑一次结果不变。

    Args:
        cfg: 配置。

    Returns:
        校验后的配置。
    """
    problems: list[str] = []

    if cfg.debounce_time <= 0:
        problems.append(f"debounce_time={cfg.debounce_time} 必须 > 0，回退 2.0")
        cfg = replace(cfg, debounce_time=2.0)

    if not (cfg.adaptive_min_wait <= cfg.adaptive_max_wait <= cfg.adaptive_max_total_wait):
        problems.append(
            "自适应等待阈值顺序不合法（要求 min <= max <= total），已回退默认"
        )
        cfg = replace(cfg, adaptive_min_wait=1.0, adaptive_max_wait=6.0,
                      adaptive_max_total_wait=12.0)

    if cfg.adaptive_max_total_wait < cfg.debounce_time:
        # 总上限比基础等待还短会让"总上限"恒生效，直接抬到基础等待
        cfg = replace(cfg, adaptive_max_total_wait=cfg.debounce_time)

    if cfg.min_interval_group < 0:
        problems.append("min_interval_group < 0，回退 0.5")
        cfg = replace(cfg, min_interval_group=0.5)
    if cfg.min_interval_private < 0:
        problems.append("min_interval_private < 0，回退 0")
        cfg = replace(cfg, min_interval_private=0.0)

    for name in ("keep_max_bytes", "keep_total_max_bytes", "keep_round_max_bytes"):
        if getattr(cfg, name) < 0:
            problems.append(f"{name} < 0，回退默认")
            cfg = replace(cfg, **{name: getattr(Config(), name)})

    # interrupt_wait 必须 <= enrich_deadline，且 > 0（文档 §10.3 / §15.3）
    interrupt_wait = cfg.interrupt_wait
    if interrupt_wait <= 0:
        problems.append(f"interrupt_wait={interrupt_wait} 必须 > 0，回退 2.0")
        interrupt_wait = 2.0
    clamped = min(interrupt_wait, cfg.enrich_deadline)
    if clamped != cfg.interrupt_wait:
        logger.debug(
            f"[{PLUGIN_NAME}] interrupt_wait {cfg.interrupt_wait} -> {clamped}"
            f"（受 enrich_deadline={cfg.enrich_deadline} 约束）"
        )
    cfg = replace(cfg, interrupt_wait=clamped)

    # ---- 统计与页面：clamp 范围（文档 §15.3 v0.8）----
    cfg = replace(
        cfg,
        stats_flush_interval=_clamp(cfg.stats_flush_interval, 5.0, 3600.0),
        stats_flush_every=int(_clamp(cfg.stats_flush_every, 1, 10000)),
        stats_max_dimensions=int(_clamp(cfg.stats_max_dimensions, 16, 1024)),
        stats_history_size=int(_clamp(cfg.stats_history_size, 0, 1000)),
        page_push_interval=_clamp(cfg.page_push_interval, 0.5, 10.0),
    )

    if problems:
        for problem in problems:
            logger.error(f"[{PLUGIN_NAME}] 配置问题：{problem}")
    return cfg


def load_config(raw: Any) -> Config:
    """从 AstrBot 传入的配置字典构造 Config。

    预设的语义在这里刻意与"页面切换预设"不同：**用户在配置里显式写过的项优先**，
    预设只补那些没写的项。否则用户把 `debounce_time` 改成 3.0，却因为 `preset`
    仍是 balanced 而被悄悄改回 2.0——排障时最误导人的就是这种"我改的值不生效"。

    Args:
        raw: AstrBot 传入的配置（通常是 dict，可能为 None）。

    Returns:
        已应用预设并校验的配置。
    """
    data = raw if isinstance(raw, dict) else {}
    aliased = _apply_aliases(data)
    cfg = _coerce(aliased)

    target = cfg.preset if cfg.preset in PRESET_NAMES else PRESET_BALANCED
    if cfg.preset != target:
        logger.warning(f"[{PLUGIN_NAME}] 未知预设 {cfg.preset!r}，回退 {target}")
    overrides = {
        key: value for key, value in PRESETS[target].items() if key not in aliased
    }
    cfg = replace(cfg, preset=target, **overrides)
    return validate(cfg)


def describe_effective(cfg: Config) -> dict[str, Any]:
    """给排障用的"真实生效值"摘要（启动日志与页面都会用）。

    Args:
        cfg: 配置。

    Returns:
        关键字段的字典。
    """
    return {
        "preset": cfg.preset,
        "debounce_time": cfg.debounce_time,
        "adaptive": cfg.adaptive,
        "adaptive_max_wait": cfg.adaptive_max_wait,
        "adaptive_max_total_wait": cfg.adaptive_max_total_wait,
        "group_enabled": cfg.group_enabled,
        "interrupt_wait": cfg.interrupt_wait,
    }

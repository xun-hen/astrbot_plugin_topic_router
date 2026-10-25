"""astrbot_plugin_topic_router_pro —— 让机器人自己判断"这是新话题还是上一个话题"。

## 这个文件的写法是被实机逼出来的，改之前请先读完这段

正常情况下 AstrBot 的插件 handler 是**绑定方法**，方法体里用 `self` 拿插件状态。
但在实机（AstrBot 4.28.1 + webchat）上观测到：派发插件 handler 时实例没有被正确
绑定，`self` 拿到的是**事件对象**。证据（来自真实日志）：

    hook_llm_request_v016 被调用 | self=WebChatMessageEvent | has_event=False
    AttributeError: 'WebChatMessageEvent' object has no attribute '_request_from_event'

后果很阴险：只要方法体里写 `self.store` / `self.config`，就会抛 `AttributeError`；
而 AstrBot 的 `star_request.py` 会 `event.stop_event()`，**同一条消息排在后面的
handler 全部不再执行**——表现就是"插件明明加载了，判定日志一条都没有"。

所以本模块的约定是：

1. **状态一律放模块级单例 `RUNTIME`**，方法体只读模块级变量，绝不 `self.xxx`；
2. 共享逻辑写成**模块级函数**，上下文作为第一个参数显式传入（`ctx`）；
3. 三个 handler（`handle_message_v090` / `hook_llm_request_v090` / `command_topic_v090`）
   只做"从参数里认出对象 → 转调模块函数"，参数识别只用 `isinstance`；
4. handler 方法名**带版本号**：AstrBot 的 `get_handler_or_create` 用
   `f"{模块}_{函数名}"` 当注册表键（4.28.1 `core/star/register/star_handler.py:38`），
   键里不含版本，命中旧条目就直接复用——改名是唯一能让新代码真正生效的办法；
5. handler 内部**不允许把异常抛出去**：一旦抛出就会 `stop_event()`，连累同一条
   消息上的其它插件（这个坑我们踩过）。所有可能出错的地方自己兜住并记日志。

## v0.8.4 的架构定案：判定主战场是请求钩子，不是消息事件

上面第 9 行那句 `has_event=False` 是 **v0.1.x 的旧观测，已经不成立**。
v0.8.4 在 AstrBot 4.28.1 实机重新取证（`trace.log` 落盘探针 + 双重标记）：

    ENTRY 行 = 0        # handle_message_v017 一次都没进入函数体
    HOOK  行 = 7        # 钩子路正常
    VERDICT 行 = 7      # 判定正常产出，且全部 via=hook，无一条 via=event

即：**消息事件主入口在进入函数体之前就被挡掉了**，而钩子路一直正常。
`priority=60` 这个 v0.8.0 的修法对它无效——只要挡住我们的执行者优先级更高
（框架内置 `handle_session_control_agent` 挂在 `sys.maxsize`，
见 `builtin_stars/astrbot/main.py:44`），60 永远排不上号。

因此约定改为：

6. **判定主战场是 `on_llm_request`**：它不在 `star_request.py` 那个
   `if event.is_stopped(): break` 循环里，不受任何优先级竞争影响；
7. 钩子**能拿到事件对象**：`call_event_hook` 是 `await handler.handler(event, *args)`，
   实机为 `args=['WeixinOCMessageEvent','ProviderRequest']`。所以钩子路要用事件
   算 `umo` 与去重键，**不要再依赖从 prompt 里抠文本**（那只是老框架的兜底）；
8. 消息事件入口保留为"可选快通道"，但**不得作为判定的唯一依赖**——
   实测它可能一条都不执行。

## v0.9.0：把「消息聚合」内建为判定流水线的第一阶段

用户把一句话拆成三条发时，逐条判定会把 `turns` 推快三倍、把同一件事判成三次。
v0.9.0 把独立插件 `astrbot_plugin_msg_aggregator` 的核心迁入 `aggregation/`：

* 消息入口优先级取插件层顶格 **1000**（不再是 60）：聚合必须尽最大可能**先于**
  普通插件看到消息，否则别人 `stop_event()` 之后连缓冲的机会都没有；
* 缓冲/自适应等待/重构/附件保活完成后，**在合并文本上做一次**话题判定
  （`AggregationService.on_settled` 回调），下游拿到的是合并后的一轮消息；
* 请求钩子保留为**兜底**：入口没被判派发（`_topic_router_seen_v090` 缺失）时
  仍然判定，并把"被抢占"落成 `aggregate.preempted` 告警；
* `aggregation.enabled=false` 时退化成 v0.8.6 的行为（每条消息即时判定）。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:  # 4.24+ 才有 TextPart.mark_as_temp
    from astrbot.core.agent.message import TextPart
except ImportError:  # pragma: no cover - 老版本降级
    TextPart = None  # type: ignore[assignment]

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    def _data_root() -> Path:
        """取 AstrBot 数据目录。

        `ASTROBOT_DATA_PATH` 优先，方便离线测试和把状态文件放到别处。

        Returns:
            数据目录路径。
        """
        override = os.environ.get("ASTROBOT_DATA_PATH")
        if override:
            return Path(override)
        return Path(get_astrbot_data_path())
except ImportError:  # pragma: no cover - 兜底

    def _data_root() -> Path:
        """取 AstrBot 数据目录（老版本兜底）。

        Returns:
            数据目录路径。
        """
        override = os.environ.get("ASTROBOT_DATA_PATH")
        if override:
            return Path(override)
        return Path.home() / ".astrbot" / "data"


from .detection import (  # noqa: E402
    CLASSIFIER_SYSTEM_PROMPT,
    VERDICT_CONTINUE,
    VERDICT_NEW_TOPIC,
    Verdict,
    build_user_prompt,
    combine,
    derive_label,
    heuristic,
    make_hint,
    parse_verdict,
)
from .daily import (  # noqa: E402
    REVIEW_SYSTEM_PROMPT,
    JudgmentLog,
    ReviewStore,
    build_prompt,
    compose_review,
    summarize,
)
from .journey import Journey, Plan  # noqa: E402
from .state import TopicStore  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_topic_router_pro"

MODE_OFF = "off"
MODE_MARK = "mark"
MODE_HINT = "hint"
MODE_AUTO = "auto"

# 送给判定模型的近期用户消息条数上限
RECENT_KEEP = 6

# 同一段文本在这个时间窗内只判定一次（秒）。
#
# **只用于"钩子路跳过事件路已判过的文本"**，不能拿来当"事件路的去重键"——否则用户
# 把同一句话重新发一遍（新的消息 ID、不同的一轮）会被误判成重复。事件路用消息 ID 去重。
DETECT_DEDUP_WINDOW = 90.0

# 钩子路的去重窗口：事件路刚判过的文本，在这么短的时间内被钩子再看到，才认定是同一条消息。
# 取值要覆盖"事件路 → 主模型调用 → 请求钩子"这段延迟。给太大会把用户重发的话误吞。
HOOK_DEDUP_WINDOW = 10.0

# 事件路回头识别"钩子刚判过"的时间窗（秒）。
#
# 背景（v0.8.2 修的不对称去重）：钩子路拿不到事件对象，`_batch_key()` 必然返回 None，
# 所以它无法写 `last_detected`。修复前钩子路判完**只**写内存里的 `RUNTIME.last_detect`，
# 进程一重启就没了，"钩子先判、事件路后到"时事件路认不出来，同一条消息被判两次
# （`turns` +2）。修复后钩子路额外把指纹落到 `TopicState.last_detected_hook`（持久化）。
#
# 这个常量用于解读该指纹的新鲜度（排障时判断"钩子是不是刚判过这句话"）。
# **它不参与事件路的拦截决策**：事件路带平台消息 ID，是权威的消息身份，用户把同一句
# 话重发一遍必须算新一轮。用文本来拦事件路会吞掉真实轮次（`tests/run_tests.py` 的
# `[7]` 用例就是为此存在的）。
CROSS_PATH_DEDUP_WINDOW = 60.0

# 消息事件处理器的优先级。
#
# v0.9.0 起取 **1000**（插件层顶格）：消息聚合必须尽最大可能先于普通插件看到
# 消息——一旦别人先 `stop_event()`，派发循环 break，我们就连缓冲的机会都没有。
#
# 为什么不是 60（v0.8.x 的值）：60 只保证"比常见防抖插件（50）高"，实机仍观测到
# ENTRY=0。为什么不是 `sys.maxsize`：框架内置的 `handle_session_control_agent`
# 正是该值，并列时的先后取决于注册顺序，我们控制不了，也不该跟会话控制抢位。
#
# 1000 只降低被抢占概率，**不提供保证**。保证来自请求钩子里的抢占检测：
# 入口没跑（`SEEN_ATTR` 缺失）时，钩子仍然能判定，并把这次抢占落成
# `aggregate.preempted` 告警。先跑是优化，被抢占也能被发现。
MESSAGE_PRIORITY = 1000

SEEN_ATTR = "_topic_router_seen_v090"
"""事件对象上的"消息入口执行过"标记（与 `aggregation.service` 共用）。

入口第一件事就是打这个标记；请求钩子据此区分"入口没被派发（被抢占）"与
"入口跑了但这一轮不需要判定"。事件对象在整条管线里是同一个实例，属性随事件
回收，不会泄漏。
"""

DETECTED_ATTR = "_topic_router_detected_v090"
"""事件对象上的"话题判定已尝试过"标记。

在调用 `_detect` **之前**置位：判定失败也按延续处理，不需要钩子再试一次；
否则一次模型超时会变成两次（事件路 + 钩子路），把延迟翻倍。
"""

MESSAGE_HANDLER_NAME = "handle_message_v090"
"""消息入口的注册名。

抢占检测要靠它在 `activated_handlers` 里核对"这次到底有没有派发到我们"。
合成事件（主动回复、其它插件直接 `request_llm`）不在派发计划里，不能算抢占。
"""

_DISPATCH_WARN_INTERVAL = 600.0
"""“入口不在派发计划里”告警的最小间隔（秒）。"""

_dispatch_warn_at = 0.0
"""上次告警的时间戳（`time.monotonic()`），用于限流。"""



def _hook_user_message(event: Any, req: Any) -> str:
    """取钩子这一轮要判定的"用户最新消息"。

    优先**直接从事件读**（`event.message_str`）——v0.8.4 实测确认钩子能拿到事件对象
    （`call_event_hook` 是 `await handler.handler(event, *args)`，实机为
    `args=['WeixinOCMessageEvent','ProviderRequest']`），所以有事件就不该再去猜文本。

    只有拿不到事件（老版本框架）时才退回从 `req.prompt` 里抠——判定器自己拼的提示词
    格式固定（见 `detection.build_user_prompt` 的 `用户最新消息：` 行），能按标记切；
    切不到就把"整段提示词足够短就当作消息"。这条兜底路**依赖提示词格式**，
    任何改写 prompt 的插件都会让它失效，所以只作最后手段。

    Args:
        event: 消息事件（可能为 None）。
        req: `ProviderRequest`。

    Returns:
        用户消息正文；取不到时返回空串。
    """
    text = ""
    if event is not None:
        text = (getattr(event, "message_str", "") or "").strip()
    if text:
        return text

    prompt = getattr(req, "prompt", "") or ""
    if not isinstance(prompt, str):
        return ""
    prompt = prompt.strip()
    if not prompt:
        return ""
    marker = "用户最新消息："
    tail = prompt.rsplit(marker, 1)[-1] if marker in prompt else ""
    text = (tail or prompt).strip()
    # 去掉判定器要求模型输出的那句尾巴
    for noise in ("请输出判定 JSON。", "请输出判定JSON。"):
        if text.endswith(noise):
            text = text[: -len(noise)].strip()
    if len(text) > 4000:  # 不是"一条消息"的样子，别拿它去判定
        return ""
    return text


def _umo_from_request(req: Any) -> str:
    """从 `ProviderRequest` 推出会话标识（**兜底路径**）。

    只在钩子拿不到事件对象时才用得到（老版本框架）。有事件时一律用 `_umo(event)`，
    那才是权威标识。取不到就返回空串，由 `_detect` 退回按对话 ID（`cid`）隔离。

    Args:
        req: `ProviderRequest`。

    Returns:
        会话标识或空串。
    """
    conv = getattr(req, "conversation", None)
    for attr in ("unified_msg_origin", "umo", "session_id"):
        value = getattr(conv, attr, None)
        if isinstance(value, str) and value:
            return value
    cid = getattr(conv, "cid", None)
    if isinstance(cid, str) and cid:
        return f"conversation:{cid}"
    return ""


def normalize_text(text: str) -> str:
    """把文本压成便于比较的形式：去空白、去标点、小写、数字折叠。

    为了让"两条路算出的去重键"一致，这里必须和 `detection.normalize` 用同一套规则。
    刻意重新实现一份而不是 import：`detection` 里那份是模块级私有函数（下划线开头），
    跨模块直接用容易在重构时踩空；这段只有一行正则，重复的代价更低。

    Args:
        text: 原始文本。

    Returns:
        归一化后的文本。
    """
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", re.sub(r"\s+", "", (text or "").lower()))
    return re.sub(r"\d+", "0", cleaned)


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号。

    作用不只是好看：日常排错时"跑的是哪一版"必须能一眼确认。

    Returns:
        版本号字符串，读不到时返回 `unknown`。
    """
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "unknown"
    for line in text.splitlines():
        if line.strip().startswith("version:"):
            return line.split(":", 1)[1].strip().strip("'\"") or "unknown"
    return "unknown"


PLUGIN_VERSION = _read_plugin_version()


def _write_import_marker(payload: str) -> None:
    """在插件数据目录里落一个"我在这里"的标记文件。

    为什么除了日志还要文件：排查"包是新的、跑的却是旧模块"这类问题时，日志可能被
    级别、轮转或复制粘贴截断；文件是死的证据，看时间戳就行。

    Args:
        payload: 写进文件的内容。
    """
    try:
        marker_dir = _data_root() / "plugin_data" / PLUGIN_NAME
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "imported.txt").write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} {payload}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


class _Runtime:
    """插件的全部运行时状态。

    刻意放在模块级而不是 `self` 上：见文件头"这个文件的写法是被实机逼出来的"。
    """

    config: dict[str, Any] = {}
    store: TopicStore | None = None
    recent: dict[str, list[str]] = {}
    pending_actions: dict[str, tuple[str, Verdict]] = {}
    pending_replies: dict[str, str] = {}
    """上一轮机器人回复的临时中转：{umo: 文本}。

    只放内存、不落盘：它只在"回复产生 → 下一条消息判定"之间用一次，
    持久化在 state 里的是按会话保留的最近一条。
    """
    last_detect: dict[str, float] = {}
    """去重表：{"<umo>|text:<归一化文本前120字>": 时间戳}。

    判定有两条路：消息事件（`handle_message_v090`）与请求钩子
    （`hook_llm_request_v090`）。两条路都可能先到，靠它保证同一段文本只判一次。
    只放内存：重启后重复判一次无害，落盘反而要处理淘汰策略。
    """
    aggregation: Any = None
    """内建消息聚合服务（`aggregation.service.AggregationService`）。

    为 None 表示聚合关闭或初始化失败——此时消息入口退化成"每条消息即时判定"。
    实例挂在模块级而不是 `self` 上，理由同文件头：handler 的 `self` 可能不是插件实例。
    """
    aggregation_error: str = ""
    """聚合初始化失败的原因（空串表示没有失败，或聚合被显式关闭）。"""

    journey: Any = None
    """阶梯放权状态机（`journey.Journey`），懒初始化。"""

    review_log: Any = None
    """判定流水（`daily.JudgmentLog`），每日复盘的数据源。"""

    review_store: Any = None
    """复盘报告存储（`daily.ReviewStore`）。"""

    extras_tasks: list[Any] = []
    """放权/复盘的后台任务（terminate 时取消）。"""


RUNTIME = _Runtime()

# 插件上下文（AstrBot 构造插件时注入）。存在模块级而不是 `self` 上：实机上 handler
# 的 `self` 可能不是插件实例，见文件头说明。
_CTX: Context | None = None


def _ctx() -> Context | None:
    """取当前插件上下文。

    Returns:
        插件上下文或 None。
    """
    return _CTX



# 模块导入时无条件打一行"身份证"日志 + 落一个标记文件。排错时"到底加载的是哪个
# 文件、哪一版"必须能一眼确认。
logger.info(
    f"[{PLUGIN_NAME}] 模块已导入 {PLUGIN_VERSION} | file={Path(__file__).resolve()}"
)
_write_import_marker(f"version={PLUGIN_VERSION} file={Path(__file__).resolve()}")


# --------------------------------------------------------------------------- 工具


def _extract_response_text(candidate: Any) -> str:
    """从 `LLMResponse` 里取出可读文本。

    Args:
        candidate: 可能是 `LLMResponse`，也可能是别的对象。

    Returns:
        纯文本（取不到就返回空串）。
    """
    for attr in ("completion_text", "reasoning_content"):
        value = getattr(candidate, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_like_event(candidate: Any) -> bool:
    """判断一个参数是不是消息事件。

    模块级函数，不依赖 `self`：实机上 `self` 可能就是事件对象本身。

    Args:
        candidate: 待判断的参数。

    Returns:
        True 表示看起来像消息事件。
    """
    return hasattr(candidate, "unified_msg_origin") and hasattr(
        candidate, "message_str"
    )


def _extract(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
    """从一堆参数里认出（事件, ProviderRequest）。

    只按类型/特征判断，不假设谁在第几个位置——实机上出现过 `self` 是事件、
    请求在 `args` 里、甚至零参数调用（`on_astrbot_loaded` 就是 `handler()`）的情况。
    所以这里允许零参数，认不出就返回 (None, None)。

    Args:
        *args: 任意位置参数。
        **kwargs: 关键字参数。

    Returns:
        `(event, request)`，认不出的位置是 None。
    """
    event: Any = None
    req: Any = None
    for candidate in args:
        if isinstance(candidate, ProviderRequest):
            req = req if req is not None else candidate
        elif event is None and _looks_like_event(candidate):
            event = candidate
    for key in ("event", "req", "request", "provider_request"):
        candidate = kwargs.get(key)
        if isinstance(candidate, ProviderRequest):
            req = req if req is not None else candidate
        elif event is None and _looks_like_event(candidate):
            event = candidate
    return event, req


def _mode() -> str:
    """当前生效的模式。

    Returns:
        `off` / `mark` / `hint` / `auto` 之一。
    """
    raw = str(RUNTIME.config.get("mode", MODE_MARK) or MODE_MARK).strip().lower()
    return raw if raw in {MODE_OFF, MODE_MARK, MODE_HINT, MODE_AUTO} else MODE_MARK


def _debug() -> bool:
    """是否处于调试模式（空跑 + 详细日志）。

    调试模式的语义只有两条，别再往里加东西：

    1. **不看做什么**：一切会改变上下文的动作（`auto` 切换对话、`hint` 注入提示）
       一律不执行，只在日志里写「本想做什么」；
    2. **看得更细**：把启发式得分、去重键、模型原始输出等内部细节落盘。

    判定本身照常执行——不然就没得观察了。

    Returns:
        是否开启调试模式。
    """
    return _flag("debug_mode", False)


def _flag(key: str, default: bool) -> bool:
    """读取布尔配置。

    Args:
        key: 配置键。
        default: 缺省值。

    Returns:
        布尔值。
    """
    value = RUNTIME.config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default if value is None else value)


def _num(key: str, default: float, cast: type = float) -> float:
    """读取数值配置，坏值退回默认。

    Args:
        key: 配置键。
        default: 缺省值。
        cast: 转换类型。

    Returns:
        数值。
    """
    try:
        return cast(RUNTIME.config.get(key, default))
    except (TypeError, ValueError):
        return cast(default)


def _umo(event: Any) -> str:
    """取统一消息来源。

    Args:
        event: 消息事件。

    Returns:
        UMO 字符串。
    """
    return str(getattr(event, "unified_msg_origin", "") or "unknown")


def _store() -> TopicStore:
    """取话题状态存储，必要时懒建。

    Returns:
        TopicStore 实例。
    """
    if RUNTIME.store is None:
        RUNTIME.store = TopicStore(_plugin_data_dir() / "topic_state.json")
    return RUNTIME.store


def _plugin_data_dir() -> Path:
    """插件的持久化数据目录。

    Returns:
        数据目录路径。
    """
    return _data_root() / "plugin_data" / PLUGIN_NAME


def _mark_event(event: Any, attr: str) -> None:
    """给事件对象打一个临时标记（打不上也不致命）。

    Args:
        event: 消息事件。
        attr: 属性名。
    """
    try:
        setattr(event, attr, True)
    except Exception:  # noqa: BLE001 - 少数事件对象可能有 __slots__
        pass


def _event_marked(event: Any, attr: str) -> bool:
    """判断事件对象上有没有某个标记。

    Args:
        event: 消息事件。
        attr: 属性名。

    Returns:
        True 表示有标记。
    """
    try:
        return bool(getattr(event, attr, False))
    except Exception:  # noqa: BLE001
        return False


def _activated_handler_names(event: Any) -> list[str]:
    """读出本次消息的派发计划里的 handler 名字。

    `waking_check` 把最终名单写进事件的 `activated_handlers` extra（`star_request`
    随后就按它逐个执行）。拿到它就等于拿到"框架这次到底有没有排我们"。

    Args:
        event: 消息事件（`on_llm_request` 钩子拿到的也是同一个事件实例）。

    Returns:
        handler 名字列表；框架没写这个 extra 时返回空列表。
    """
    if event is None:
        return []
    try:
        handlers = event.get_extra("activated_handlers")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(handlers, (list, tuple)):
        return []
    names: list[str] = []
    for handler in handlers:
        name = getattr(handler, "handler_name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _warn_if_entry_absent_from_plan(event: Any) -> None:
    """派发计划里没有消息入口时告警（这是 v0.8.x 起的真实故障形态）。

    v0.9.1 的抢占检测要求"`activated_handlers` 存在且**包含**本插件"，所以当
    handler 因为元数据 `name=None` 被 `SessionPluginManager` 静默丢弃时，它反而
    **不会**报警——报警条件本身被这个故障吃掉了。这里补上另一半：计划里明明有
    别的 handler，却没有我们，那就是注册/meta 层面的问题，必须可见。

    同一个进程里最多每 `_DISPATCH_WARN_INTERVAL` 秒报一次，避免每条消息刷屏。

    Args:
        event: 消息事件。
    """
    global _dispatch_warn_at
    names = _activated_handler_names(event)
    if not names or MESSAGE_HANDLER_NAME in names:
        return
    now = time.monotonic()
    if now - _dispatch_warn_at < _DISPATCH_WARN_INTERVAL:
        return
    _dispatch_warn_at = now

    report = _registration_report()
    problems = "；".join(report.get("problems") or []) or "未知（注册表读不到）"
    logger.warning(
        f"[{PLUGIN_NAME}] 消息入口 {MESSAGE_HANDLER_NAME} 不在本次派发计划里"
        f"（计划里有 {len(names)} 个 handler：{', '.join(names[:8])}）"
        f" | 注册表诊断：{problems}"
        f" | 聚合与 /topicweave 会静默失效，请把这一行连同 AstrBot 版本一起反馈"
    )
    _trace(
        "ENTRY_MISSING",
        str(getattr(event, "unified_msg_origin", "-")),
        f"| v={PLUGIN_VERSION} | names={names[:8]} | problems={problems}",
    )


def _registration_report() -> dict[str, Any]:
    """把本插件 handler 在框架注册表里的真实落点整理成一份报告。

    这个函数存在的理由：v0.8.x 到 v0.9.1 的"消息入口从不执行"是**完全静默**的
    ——没有异常、没有 warning。原因是 handler 注册在 `router` 模块路径下，而框架
    的插件元数据登记在 `main` 模块路径下，两者对不上时 `star_map` 里那条匿名元数据
    `name=None`，`SessionPluginManager.filter_handlers_by_session` 会静默跳过它
    （见 `main.py` 文件头）。启动时把这份报告打进日志，同类故障就不会再无声无息。

    Returns:
        报告字典；`ok=False` 时 `problems` 里是给人看的结论。
    """
    try:
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
    except Exception as exc:  # noqa: BLE001 - 离线环境没有框架
        return {"ok": False, "available": False, "problems": [f"读不到框架注册表：{exc}"]}

    entry = None
    for handler in star_handlers_registry:
        if (
            getattr(handler, "handler_name", "") == MESSAGE_HANDLER_NAME
            and "topic_router" in getattr(handler, "handler_full_name", "")
        ):
            entry = handler
            break

    report: dict[str, Any] = {
        "ok": False,
        "available": True,
        "entry_found": entry is not None,
        "handler_module_path": getattr(entry, "handler_module_path", None),
        "enabled": bool(getattr(entry, "enabled", False)),
        "filters": len(getattr(entry, "event_filters", []) or []),
        "problems": [],
    }

    if entry is None:
        report["problems"].append(
            f"注册表里找不到 {MESSAGE_HANDLER_NAME}（插件可能没加载完，或被旧版本顶掉）"
        )
        return report

    module_path = report["handler_module_path"] or ""
    meta = star_map.get(module_path)
    report["metadata_name"] = getattr(meta, "name", None)
    report["metadata_module_path"] = getattr(meta, "module_path", None)
    report["metadata_count"] = len(
        [key for key in star_map if "topic_router" in key]
    )

    if meta is None:
        report["problems"].append(
            f"star_map 里没有 {module_path!r}：框架查不到 handler 所属插件"
        )
    else:
        if not getattr(meta, "name", None):
            report["problems"].append(
                "handler 所属元数据的 name 为空：AstrBot 4.27+ 的会话级插件过滤会"
                "静默丢弃该 handler（消息入口与 /topicweave 都不会被派发）"
            )
        if report["metadata_module_path"] and module_path != report["metadata_module_path"]:
            report["problems"].append(
                f"handler 模块路径({module_path}) 与插件元数据模块路径"
                f"({report['metadata_module_path']}) 不一致"
            )
    if not report["enabled"]:
        report["problems"].append("handler.enabled=False（可能被面板的指令开关关掉了）")
    if report["filters"] == 0:
        report["problems"].append("event_filters 为空：waking_check 会直接跳过")

    try:
        in_plan = entry in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
    except Exception:  # noqa: BLE001
        in_plan = False
    report["in_adapter_message_plan"] = in_plan
    if not in_plan:
        report["problems"].append("不在 AdapterMessageEvent 的候选名单里")

    report["ok"] = not report["problems"]
    return report


def _aggregation_config() -> dict[str, Any]:
    """取 `aggregation` 子配置（容错成空字典）。

    Returns:
        配置字典。
    """
    raw = RUNTIME.config.get("aggregation", {})
    return raw if isinstance(raw, dict) else {}


def _persist_aggregation_preset(values: dict[str, Any]) -> None:
    """把页面切预设后的显式值写回插件配置并落盘（v0.9.3）。

    只改内存里的 `Config` 时，重载插件 / 重启 AstrBot 会悄悄回到旧预设，页面也会
    显示旧值。这里把生效值写进 `RUNTIME.config["aggregation"]`，再调用
    AstrBotConfig 的 `save_config()`（离线测试里 `RUNTIME.config` 是普通 dict，
    没有该方法，自然退化为只改内存）。

    Args:
        values: 预设生效的字段（含 `preset` 本身）。
    """
    config = RUNTIME.config
    aggregation = config.get("aggregation") if isinstance(config, dict) else None
    if not isinstance(aggregation, dict):
        return
    aggregation.update(values)
    save = getattr(config, "save_config", None)
    if callable(save):
        save()


# ------------------------------------------------------- 阶梯放权 / 每日复盘
def _promotion_config() -> dict[str, Any]:
    """取 `promotion` 子配置。

    Returns:
        配置字典（缺失或类型不对时为空字典）。
    """
    raw = RUNTIME.config.get("promotion")
    return raw if isinstance(raw, dict) else {}


def _review_config() -> dict[str, Any]:
    """取 `review` 子配置。

    Returns:
        配置字典（缺失或类型不对时为空字典）。
    """
    raw = RUNTIME.config.get("review")
    return raw if isinstance(raw, dict) else {}


def _init_extras() -> None:
    """初始化放权状态机与复盘存储（幂等）。

    两者都只依赖 `plugin_data` 目录，不依赖聚合是否开启——所以即使
    `aggregation.enabled=false`，阶梯放权与每日复盘也照常工作。
    """
    data_dir = _plugin_data_dir()
    review_dir = data_dir / "review"
    try:
        if RUNTIME.journey is None:
            RUNTIME.journey = Journey(data_dir / "promotion.json")
        if RUNTIME.review_log is None:
            RUNTIME.review_log = JudgmentLog(review_dir / "logs")
        if RUNTIME.review_store is None:
            RUNTIME.review_store = ReviewStore(review_dir)
    except Exception as exc:  # noqa: BLE001 - 旁路功能，失败不能拖垮插件
        logger.warning(f"[{PLUGIN_NAME}] 放权/复盘初始化失败（已忽略）：{exc}")


def _set_mode(mode: str, reason: str = "") -> None:
    """改模式并写回插件配置（自动放权与页面切档共用）。

    Args:
        mode: 目标模式。
        reason: 日志里的原因。
    """
    before = _mode()
    if before == mode:
        return
    RUNTIME.config["mode"] = mode
    save = getattr(RUNTIME.config, "save_config", None)
    if callable(save):
        try:
            save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 写回 mode 失败（本次仍生效）：{exc}")
    logger.info(
        f"[{PLUGIN_NAME}] 模式已切换 {before} → {mode}"
        + (f"（{reason}）" if reason else "")
    )


def _review_view() -> dict[str, Any] | None:
    """页面/接口用的最新一份复盘。

    Returns:
        `{"day","text","path"}` 或 None。
    """
    if RUNTIME.review_store is None:
        _init_extras()
    store = RUNTIME.review_store
    return store.latest() if store is not None else None


def _promotion_view() -> dict[str, Any]:
    """页面/接口用的放权进度。

    Returns:
        进度字典（状态机不可用时带 error 字段）。
    """
    if RUNTIME.journey is None:
        _init_extras()
    journey = RUNTIME.journey
    if journey is None:
        return {"enabled": False, "error": "放权状态机不可用"}
    plan = Plan.from_config(_promotion_config())
    return journey.stage_view(time.time(), plan, _mode())


async def _promotion_reset() -> dict[str, Any]:
    """重置放权计时（从当前模式重新开始数天数）。

    Returns:
        重置后的进度视图。
    """
    _init_extras()
    if RUNTIME.journey is not None:
        RUNTIME.journey.start(time.time(), _mode())
        logger.info(f"[{PLUGIN_NAME}] 阶梯放权已重置，从 {_mode()} 重新计时")
    return _promotion_view()


def _apply_promotion(now: float | None = None) -> str | None:
    """按放权计划推进一步；返回新写入的 mode（None 表示不动）。

    Args:
        now: 时间戳（测试可注入）。

    Returns:
        新 mode 或 None。
    """
    if RUNTIME.journey is None:
        return None
    now = now if now is not None else time.time()
    plan = Plan.from_config(_promotion_config())
    try:
        target = RUNTIME.journey.step(now, plan, _mode())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{PLUGIN_NAME}] 放权推进失败（已忽略）：{exc}")
        return None
    if target is None:
        return None
    _set_mode(target, reason=f"阶梯放权：第 {RUNTIME.journey.elapsed_days(now):.1f} 天")
    return target


def _record_judgment(
    umo: str,
    message: str,
    verdict: Any,
    *,
    via_hook: bool,
) -> None:
    """把一条判定写进每日流水（供复盘使用）。

    Args:
        umo: 会话标识。
        message: 用户消息。
        verdict: 判定结果。
        via_hook: 是否走请求钩子那条路。
    """
    log = RUNTIME.review_log
    if log is None:
        return
    try:
        log.append(
            {
                "ts": time.time(),
                "umo": umo,
                "text": (message or "")[:120],
                "verdict": "new" if getattr(verdict, "is_new_topic", False) else "continue",
                "confidence": round(float(getattr(verdict, "confidence", 0.0) or 0.0), 3),
                "source": str(getattr(verdict, "source", "") or ""),
                "mode": _mode(),
                "via": "hook" if via_hook else "event",
                "topic": str(getattr(verdict, "topic_label", "") or "")[:60],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 复盘是旁路
        logger.debug(f"[{PLUGIN_NAME}] 写判定流水失败（已忽略）：{exc}")


async def _review_provider(ctx: Context | None, umo_hint: str) -> str:
    """给每日复盘挑一个可用模型。

    优先级：`review.provider_id` → 顶层 `provider_id` → 当天最活跃会话的当前模型
    → provider 列表里的第一个。

    Args:
        ctx: 插件上下文。
        umo_hint: 当天最活跃的会话（用来解析"跟随会话"的模型）。

    Returns:
        provider id；空串表示没有可用模型。
    """
    for key in ("provider_id",):
        want = str(_review_config().get(key) or "").strip()
        if want:
            exists = await _provider_exists(ctx, want)
            if exists is not False:
                return want
    want = str(RUNTIME.config.get("provider_id", "") or "").strip()
    if want:
        exists = await _provider_exists(ctx, want)
        if exists is not False:
            return want
    getter = getattr(ctx, "get_current_chat_provider_id", None)
    if getter is not None and umo_hint:
        try:
            pid = await getter(umo=umo_hint)
            if pid:
                return str(pid)
        except Exception:  # noqa: BLE001
            pass
    ids = _list_provider_ids(ctx)
    return ids[0] if ids else ""


async def _run_review(now: float | None = None, *, force: bool = False) -> dict[str, Any] | None:
    """生成当天复盘报告（模型不可用时退回本地摘要）。

    Args:
        now: 时间戳（测试可注入）。
        force: 忽略"auto 模式跳过"的限制（页面手动触发时用）。

    Returns:
        报告字典 `{"day", "text", "path"}`；跳过时返回 None。
    """
    cfg = _review_config()
    enabled = cfg.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not enabled and not force:
        return None
    if not force and _mode() == MODE_AUTO and bool(cfg.get("only_before_auto", True)):
        logger.info(f"[{PLUGIN_NAME}] 已进入 auto，跳过每日复盘（only_before_auto）")
        return None
    _init_extras()
    store, log = RUNTIME.review_store, RUNTIME.review_log
    if store is None or log is None:
        return None

    now = now if now is not None else time.time()
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    records = log.read_day(day)
    stats = summarize(records, day)

    model_text = ""
    umo_hint = ""
    if records:
        counts: dict[str, int] = {}
        for rec in records:
            umo = str(rec.get("umo") or "")
            if umo:
                counts[umo] = counts.get(umo, 0) + 1
        umo_hint = max(counts, key=counts.get) if counts else ""
    if records and _ctx() is not None:
        try:
            provider_id = await _review_provider(_ctx(), umo_hint)
            generate = getattr(_ctx(), "llm_generate", None)
            if provider_id and generate is not None:
                resp = await asyncio.wait_for(
                    generate(
                        chat_provider_id=provider_id,
                        prompt=build_prompt(stats),
                        system_prompt=REVIEW_SYSTEM_PROMPT,
                    ),
                    timeout=60,
                )
                model_text = str(getattr(resp, "completion_text", "") or "")
        except Exception as exc:  # noqa: BLE001 - 复盘失败退回本地摘要
            logger.warning(f"[{PLUGIN_NAME}] 每日复盘模型调用失败，退回本地摘要：{exc}")

    report = compose_review(stats, model_text)
    path = store.save(day, report)
    try:
        log.prune(now)
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        f"[{PLUGIN_NAME}] 每日复盘已生成 {day}：判定 {stats['total']} 次、"
        f"新话题 {stats['new_topic']} 次、模型={'是' if model_text else '否'} → {path}"
    )
    return {"day": day, "text": report, "path": str(path), "stats": stats}


async def _promotion_loop() -> None:
    """后台推进阶梯放权（每 30 分钟一次，足够按天生效）。"""
    while True:
        try:
            await asyncio.sleep(1800)
            _apply_promotion()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 放权循环异常（已忽略）：{exc}")


async def _review_loop() -> None:
    """后台检查"今天的复盘该不该生成"（每 10 分钟一次，幂等）。"""
    while True:
        try:
            await asyncio.sleep(600)
            cfg = _review_config()
            enabled = cfg.get("enabled", False)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
            if not enabled:
                continue
            try:
                hour = int(cfg.get("hour", 23) or 23)
            except (TypeError, ValueError):
                hour = 23
            hour = max(0, min(23, hour))
            now = time.time()
            if datetime.fromtimestamp(now).hour < hour:
                continue
            day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            target = Path(RUNTIME.review_store.directory) / f"{day}.md" if RUNTIME.review_store else None
            if target is not None and target.exists():
                continue
            await _run_review(now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 复盘循环异常（已忽略）：{exc}")


def _start_extras_tasks() -> None:
    """启动放权/复盘的后台任务（幂等）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(f"[{PLUGIN_NAME}] 没有运行中的事件循环，放权/复盘任务延后启动")
        return
    if not RUNTIME.extras_tasks:
        RUNTIME.extras_tasks.append(loop.create_task(_promotion_loop()))
        RUNTIME.extras_tasks.append(loop.create_task(_review_loop()))


async def _stop_extras_tasks() -> None:
    """停止后台任务。"""
    tasks = list(RUNTIME.extras_tasks)
    RUNTIME.extras_tasks.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def _init_aggregation(ctx: Context | None) -> None:
    """按配置创建内建聚合服务（关闭时什么都不做）。

    失败只记 `aggregation_error` 并退化成"每条消息即时判定"，绝不让插件加载失败。

    Args:
        ctx: 插件上下文。
    """
    if RUNTIME.aggregation is not None:
        return
    # 总开关关闭时连聚合都不该启动：否则它会缓冲并静默消息，却又不做判定，
    # 用户看到的是"消息被吞了但机器人不回"。判定层自己有 enabled 检查，
    # 但聚合层必须在更前面就停下。
    if not _flag("enabled", True):
        logger.info(f"[{PLUGIN_NAME}] 插件总开关关闭，消息聚合不启动")
        return
    raw = _aggregation_config()
    # 缺省开启：整合之后的默认形态就是"先聚合、再判定"。
    # 想恢复 v0.8.6 的逐条判定，把 aggregation.enabled 设为 false。
    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not bool(enabled):
        logger.info(f"[{PLUGIN_NAME}] 消息聚合已关闭，按每条消息即时判定")
        return
    try:
        from .aggregation import AggregationService

        service = AggregationService(
            ctx,
            raw,
            data_dir=_plugin_data_dir(),
            seen_attr=SEEN_ATTR,
            message_handler_name=MESSAGE_HANDLER_NAME,
            persist_preset=_persist_aggregation_preset,
        )
        service.on_settled = _on_aggregated
        # 页面后端在聚合服务里；放权/复盘的只读视图与手动触发挂在这里，
        # 避免 web_api 反向 import router（会循环）。
        service.extras = {
            "promotion": _promotion_view,
            "promotion_reset": _promotion_reset,
            "review": _review_view,
            "run_review": lambda: _run_review(force=True),
        }
        RUNTIME.aggregation = service
        RUNTIME.aggregation_error = ""
    except Exception as exc:  # noqa: BLE001 - 聚合不可用不能拖垮判定
        RUNTIME.aggregation_error = f"{type(exc).__name__}: {exc}"
        RUNTIME.aggregation = None
        logger.error(
            f"[{PLUGIN_NAME}] 消息聚合初始化失败，已退化为逐条判定："
            f"{RUNTIME.aggregation_error}"
        )


async def _on_aggregated(event: Any, text: str) -> None:
    """聚合完成后的判定回调（在合并文本上判一次）。

    Args:
        event: Owner 事件（已重构为合并后的消息）。
        text: 合并后的文本。
    """
    if not text.strip():
        return
    _mark_event(event, DETECTED_ATTR)
    try:
        await _detect(_ctx(), event, text, umo=_umo(event))
    except Exception as exc:  # noqa: BLE001 - 判定失败绝不影响正常回复
        logger.warning(
            f"[{PLUGIN_NAME}] 聚合后判定异常（已兜住）：{type(exc).__name__}: {exc}"
        )


def _looks_like_command(text: str) -> bool:
    """粗略判断是否是指令文本（用于避免把指令当成用户消息判定）。

    Args:
        text: 消息文本。

    Returns:
        True 表示看起来像指令。
    """
    stripped = (text or "").lstrip()
    return stripped.startswith(("/", "／"))


def _trace(tag: str, umo: str = "-", extra: str = "") -> None:
    """把排障证据**落盘**，不经过日志系统。

    为什么需要它：判断"某个 handler 到底有没有被调用"时，日志有三个不可靠之处——

    1. 级别：入口日志是 INFO，若运行环境把日志级别调到 WARNING 以上就什么都看不到；
    2. 格式：AstrBot 会给插件日志自动加注 `[文件名:行号]`，不同版本格式不同，
       用正则去 grep 很容易"看起来是 0 次"，实际是被模式写错筛掉了；
    3. 污染：像 AstrBot WebUI 的"日志/终端"面板会把**你敲的命令本身**也记进
       `info.log`，于是 `grep -c 某字符串` 会统计到自己，得到假阳性。

    落盘文件不受以上任何一条影响，是"是否被调用"唯一可信的证据源。

    Args:
        tag: 事件标记，如 `ENTRY` / `HOOK`。
        umo: 会话标识。
        extra: 附加信息（单行）。
    """
    if not _flag("trace_enabled", False):
        return
    try:
        path = _plugin_data_dir() / "trace.log"
        # 单文件封顶，避免长期开启把磁盘写满
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.replace(path.with_suffix(".log.1"))
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {tag} umo={umo} {extra}\n")
    except Exception:  # noqa: BLE001 - 取证绝不影响主流程
        pass


def _batch_key(event: Any, message: str) -> str | None:
    """算一条消息的"批次键"，用于两个入口之间的去重。

    不能只用消息正文：用户完全可能连着发两遍一样的话，那是两轮真实对话。
    优先用平台给的消息 id / 时间戳；拿不到时返回 None（跳过去重）。

    Args:
        event: 消息事件。
        message: 消息正文。

    Returns:
        批次键或 None。
    """
    obj = getattr(event, "message_obj", None)
    if obj is not None:
        msg_id = getattr(obj, "message_id", "") or ""
        stamp = getattr(obj, "timestamp", 0) or 0
        if msg_id or stamp:
            return f"{msg_id}|{stamp}|{message[:60]}"
    return None


def _record_hook_detection(state: Any, text_key: str, now: float) -> None:
    """把"钩子路刚判过这句话"记进会话状态。

    这是一个**诊断用记录**，不是事件路的闸门：事件路带平台消息 ID，是权威的消息
    身份，用户重发同一句话必须算新一轮，所以不能拿文本来拦它（见 `_detect` 里的
    说明，以及 `tests/run_tests.py` 的 `[7]` 用例）。留这份记录是为了在排障时
    一眼看出"钩子路最近判过什么"，配合 log 判断两路是否都在工作。

    Args:
        state: 会话状态。
        text_key: 归一化文本键（`umo|text:...`）。
        now: 当前时间戳。
    """
    fingerprint = text_key.rsplit("text:", 1)[-1]
    state.last_detected_hook = f"{now:.3f}:{fingerprint}"


def _state(ctx: Context | None, umo: str, conv_id: str) -> Any:
    """取话题状态，必要时作废过期状态。

    刻意不碰 `ctx`（只是保留签名对称），所以 `ctx` 为 None 也安全——请求钩子那条路
    拿不到上下文时照样能用。

    Args:
        ctx: 插件上下文，可能为 None。
        umo: 统一消息来源。
        conv_id: 当前对话 ID。

    Returns:
        TopicState。
    """
    store = _store()
    state = store.get(umo)
    if conv_id and state.is_stale_for(conv_id):
        logger.info(
            f"[{PLUGIN_NAME}] 会话 {umo} 的对话已变更（{str(state.conv_id)[:8]} -> "
            f"{str(conv_id)[:8]}），旧话题作废"
        )
        # 只清「当前在聊什么」：累计判定次数和用户设的开关都要留着
        store.clear_topic(umo)
        RUNTIME.recent[umo] = []
        state = store.get(umo)
    if conv_id:
        state.conv_id = conv_id
    return state


async def _conversation_id(ctx: Context | None, event: Any, umo: str = "") -> str:
    """取当前对话 ID，取不到时返回空串。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件（请求钩子那条路没有事件，传 None）。
        umo: 会话标识；事件缺失时用它代替。

    Returns:
        对话 ID。
    """
    manager = getattr(ctx, "conversation_manager", None)
    if manager is None:
        return ""
    key = umo or (_umo(event) if event is not None else "")
    if not key:
        return ""
    try:
        cid = await manager.get_curr_conversation_id(key)
    except Exception as exc:  # noqa: BLE001 - 会话服务异常不能影响主流程
        logger.debug(f"[{PLUGIN_NAME}] 读取当前对话失败: {exc}")
        return ""
    return str(cid or "")


def _recent_for(umo: str) -> list[str]:
    """取某会话的近期用户消息。

    Args:
        umo: 统一消息来源。

    Returns:
        消息列表（旧到新）。
    """
    return RUNTIME.recent.setdefault(umo, [])


def _list_provider_ids(ctx: Context | None) -> list[str]:
    """列出当前可用的 provider ID。

    Args:
        ctx: 插件上下文。

    Returns:
        ID 列表（拿不到就返回空列表）。
    """
    ids: list[str] = []
    try:
        manager = getattr(ctx, "provider_manager", None)
        for item in list(getattr(manager, "provider_insts", []) or []):
            conf = getattr(item, "provider_config", {}) or {}
            pid = str(conf.get("id", "") or "")
            if pid:
                ids.append(pid)
    except Exception as exc:  # noqa: BLE001 - 只是诊断信息
        logger.debug(f"[{PLUGIN_NAME}] 枚举 provider 失败: {exc}")
    return ids


async def _provider_exists(ctx: Context | None, provider_id: str) -> bool | None:
    """判断一个 provider ID 是否真的存在。

    实机教训：配置里写了不存在的 ID（比如 `deepseek/deepseek-v4-flash`，而实际是
    `deepseek/deepseek-flash`）时，`llm_generate` 会抛 `ProviderNotFoundError`
    （见 AstrBot `core/star/context.py`），异常被兜底逻辑吞掉 → 判定模型那层
    **静默失效**，只剩启发式在跑。所以这里先查一遍：配错就明确告警并回退到当前会话模型。

    Args:
        ctx: 插件上下文。
        provider_id: 待检查的 ID。

    Returns:
        True / False；无法判断时返回 None（按"存在"处理，交给调用去试）。
    """
    manager = getattr(ctx, "provider_manager", None)
    getter = getattr(manager, "get_provider_by_id", None)
    if getter is None:
        return None
    try:
        found = await getter(provider_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[{PLUGIN_NAME}] 校验 provider 失败: {exc}")
        return None
    return bool(found)


async def _resolve_provider(ctx: Context | None, event: Any) -> tuple[str, str]:
    """决定判定用哪个模型。

    优先级：配置里指定的 ID（**确认存在才用**）→ 当前会话的模型。
    配置写错时不再静默失败，而是告警 + 回退。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。

    Returns:
        `(provider_id, 说明)`；provider_id 为空表示不可用。
    """
    want = str(RUNTIME.config.get("provider_id", "") or "").strip()
    if want:
        exists = await _provider_exists(ctx, want)
        if exists is None or exists:
            return want, "配置指定"
        available = _list_provider_ids(ctx)
        logger.warning(
            f"[{PLUGIN_NAME}] 配置的判定模型 '{want}' 不存在，已回退到当前会话模型。"
            f"可用的 ID：" + ("、".join(available[:10]) if available else "（枚举不到）")
        )
    getter = getattr(ctx, "get_current_chat_provider_id", None)
    if getter is None:
        return "", "上下文不可用"
    try:
        pid = await getter(umo=_umo(event))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[{PLUGIN_NAME}] 读取当前 provider 失败: {exc}")
        return "", "当前会话模型不可用"
    if not pid:
        return "", "当前会话没有可用模型"
    return str(pid), "跟随当前会话"


async def _ask_llm(
    ctx: Context | None,
    event: Any,
    message: str,
    state: Any,
    heuristic_result: Any,
) -> Verdict | None:
    """问一次判定模型。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        message: 用户最新消息。
        state: 当前话题状态。
        heuristic_result: 启发式结果（用于兜底相似度）。

    Returns:
        Verdict 或 None（任何失败都返回 None，调用方按延续处理）。
    """
    provider_id, note = await _resolve_provider(ctx, event)
    if not provider_id:
        logger.warning(f"[{PLUGIN_NAME}] 无法确定判定模型：{note}，本轮跳过模型判定")
        return None
    generate = getattr(ctx, "llm_generate", None)
    if generate is None:
        logger.warning(f"[{PLUGIN_NAME}] 上下文不支持 llm_generate，跳过模型判定")
        return None
    timeout = max(1.0, _num("timeout_seconds", 8.0))
    include_reply = _flag("include_assistant_reply", True)
    last_reply = (getattr(state, "last_assistant_text", "") or "") if include_reply else ""
    # 把"距上一条消息过了多久"告诉模型。长时间未回复时启发式会判 needs_llm，
    # 但模型只有拿到这个线索才判得动"下一句是否延续"（见 build_user_prompt 的说明）。
    idle_minutes = 0.0
    last_ts = float(getattr(state, "last_ts", 0.0) or 0.0)
    if last_ts:
        idle_minutes = max(0.0, (time.time() - last_ts) / 60)
    prompt = build_user_prompt(
        message,
        state,
        _recent_for(_umo(event)),
        int(_num("history_turns", 3, int)),
        last_assistant=last_reply,
        idle_minutes=idle_minutes,
    )
    try:
        resp = await asyncio.wait_for(
            generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=CLASSIFIER_SYSTEM_PROMPT,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning(
            f"[{PLUGIN_NAME}] 判定模型 {provider_id} 超时（{timeout}s），本轮按延续处理"
        )
        return None
    except Exception as exc:  # noqa: BLE001 - 判定失败绝不影响正常回复
        logger.warning(f"[{PLUGIN_NAME}] 判定模型调用失败（{provider_id}）: {exc}")
        return None
    text = getattr(resp, "completion_text", "") or ""
    parsed = parse_verdict(text, heuristic_result)
    if parsed is None:
        logger.warning(
            f"[{PLUGIN_NAME}] 判定模型输出无法解析，按延续处理：{text[:120]!r}"
        )
    return parsed


def _apply_threshold(verdict: Verdict) -> Verdict:
    """把置信度门槛应用到判定结果上。

    低于 `min_new_topic_confidence` 的新话题判定会被压回「延续」：宁可不切。

    Args:
        verdict: 原始判定。

    Returns:
        处理后的判定。
    """
    floor = _num("min_new_topic_confidence", 0.6)
    if verdict.is_new_topic and verdict.confidence < floor:
        return Verdict(
            verdict=VERDICT_CONTINUE,
            confidence=1.0 - verdict.confidence,
            source=verdict.source,
            similarity=verdict.similarity,
            reason=(
                f"判为新话题但置信度 {verdict.confidence:.2f} < {floor:.2f}，"
                f"保守按延续处理（原判：{verdict.reason}）"
            ),
            topic_label=verdict.topic_label,
            topic_summary=verdict.topic_summary,
            details=verdict.details,
        )
    return verdict


def _under_hourly_cap(state: Any) -> bool:
    """检查本会话是否还有模型判定额度。

    Args:
        state: 话题状态。

    Returns:
        True 表示可以调用模型。
    """
    cap = int(_num("max_checks_per_hour", 30, int))
    if cap <= 0:
        return True
    used = state.checks_last_hour()
    if used >= cap:
        logger.info(
            f"[{PLUGIN_NAME}] 本会话本小时已判定 {used} 次，达到上限 {cap}，本轮只用启发式"
        )
        return False
    return True


def _log_verdict(umo: str, message: str, verdict: Verdict, state: Any) -> None:
    """把判定明细写进日志。

    日志里刻意不用 emoji：Windows 上部分控制台是 GBK，写 emoji 会让日志处理器报
    UnicodeEncodeError（在线程里静默刷屏），纯文本在任何编码下都安全。

    Args:
        umo: 统一消息来源。
        message: 用户消息。
        verdict: 判定结果。
        state: 话题状态。
    """
    if not _flag("debug_log", True):
        return
    tag = "NEW-TOPIC" if verdict.is_new_topic else "CONTINUE"
    preview = message.replace("\n", " ")[:60]
    logger.info(
        f"[{PLUGIN_NAME}] {tag} | {umo} | confidence={verdict.confidence:.2f} "
        f"| source={verdict.source} | topic={state.label or '(none)'} "
        f"| msg={preview} | {verdict.reason} | {verdict.details}"
    )


async def _detect(
    ctx: Context | None,
    event: Any,
    message: str,
    *,
    umo: str = "",
    via_hook: bool = False,
) -> Any:
    """对一个用户消息做话题判定并推进状态。

    全程不碰 `self`：见文件头说明。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件（请求钩子那条路拿不到，传 None）。
        message: 用户消息正文。
        umo: 会话标识；钩子路径下事件缺失，需要调用方显式给。
        via_hook: 是否由请求钩子路调用。为 True 时会把这条消息的指纹落到
            `TopicState.last_detected_hook`（持久化），用于排障与状态核对，
            见 `_record_hook_detection`。

    Returns:
        最终 Verdict；未执行判定时返回 None。
    """
    if not _flag("enabled", True) or _mode() == MODE_OFF:
        return None

    umo = umo or _umo(event)
    if not umo:
        return None
    conv_id = await _conversation_id(ctx, event, umo=umo)
    state = _state(ctx, umo, conv_id)
    if state.disabled:
        logger.info(f"[{PLUGIN_NAME}] 本会话已被 /topicweave off 关闭，跳过判定")
        return None

    # 去重（两条路都可能到，见 DETECT_DEDUP_WINDOW / HOOK_DEDUP_WINDOW /
    # CROSS_PATH_DEDUP_WINDOW 的说明）：
    #
    # v0.8.4 起两路**共用同一把锁**。原因：实测（2026-10-14，AstrBot 4.28.1）表明
    # 请求钩子**也能拿到事件对象**——`call_event_hook` 是
    # `await handler.handler(event, *args)`，实机日志为
    # `args=['WeixinOCMessageEvent', 'ProviderRequest']`、`has_event=True`。
    # 既然钩子也能算出 `消息ID|时间戳` 这个权威键，就没必要再退化成"按文本猜"：
    #   · 有事件对象（任一入口）→ 按消息 ID + 时间戳去重，用户重发同一句话算新一轮；
    #   · 没有事件对象（老版本框架，钩子拿不到事件）→ 退回按文本去重。
    #
    # **不要给任何一条路加"按文本拦"的规则**：带上平台消息 ID 时它是权威的消息身份，
    # 用户完全可能把同一句话重发一遍，那是真实的新一轮。按文本拦会把这一轮吞掉
    # （tests/run_tests.py 的 `[7]` 用例就是为此存在的）。
    now = time.time()
    for stale_key in [
        k for k, ts in RUNTIME.last_detect.items() if now - ts > DETECT_DEDUP_WINDOW
    ]:
        RUNTIME.last_detect.pop(stale_key, None)

    event_key = _batch_key(event, message)
    text_key = f"{umo}|text:{normalize_text(message)[:120]}"
    if event_key is None:
        # 兜底路：拿不到事件（老框架的钩子，或 _extract 没认出来）→ 只能按文本去重
        seen = RUNTIME.last_detect.get(text_key)
        if seen is not None and now - seen < HOOK_DEDUP_WINDOW:
            logger.debug(
                f"[{PLUGIN_NAME}] 这段消息刚由另一个入口判过，跳过：{message[:30]!r}"
            )
            return None
    else:
        if state.last_detected == event_key:
            logger.debug(f"[{PLUGIN_NAME}] 这条消息已判定过（另一个入口先到了），跳过")
            return None
        state.last_detected = event_key
    RUNTIME.last_detect[text_key] = now

    # 取上一轮机器人回复（回复钩子先存进 pending_replies），并落到状态里
    reply = RUNTIME.pending_replies.pop(umo, "")
    if reply:
        state.last_assistant_text = reply

    started = time.perf_counter()
    result = heuristic(
        message,
        state,
        sim_continue=_num("sim_continue", 0.5),
        sim_new_topic=_num("sim_new_topic", 0.12),
        time_gap_minutes=int(_num("time_gap_minutes", 45, int)),
        min_message_length=int(_num("min_message_length", 6, int)),
        short_message_length=int(_num("short_message_length", 25, int)),
    )

    llm_verdict: Verdict | None = None
    spend_llm = _flag("always_llm", False) or result.needs_llm
    if spend_llm and _under_hourly_cap(state):
        llm_verdict = await _ask_llm(ctx, event, message, state, result)
        state.checks.append(time.time())

    verdict = combine(result, llm_verdict)
    verdict.details = {
        "heuristic": result.verdict,
        "heuristic_confidence": round(result.confidence, 3),
        "needs_llm": result.needs_llm,
        "llm_used": llm_verdict is not None,
        "mode": _mode(),
        "has_reply": bool(reply),
        "cost_ms": int((time.perf_counter() - started) * 1000),
    }

    verdict = _apply_threshold(verdict)
    _log_verdict(umo, message, verdict, state)
    _record_judgment(umo, message, verdict, via_hook=via_hook)
    _trace(
        "VERDICT",
        umo,
        f"| {'NEW-TOPIC' if verdict.is_new_topic else 'CONTINUE'} "
        f"| via={'hook' if via_hook else 'event'} "
        f"| confidence={verdict.confidence:.2f} | source={verdict.source} "
        f"| turns={state.turns + 1} | msg={message[:60]!r}",
    )
    if _debug():
        # 调试模式：把判定内部细节全量落盘。刻意只写 trace.log、不写 info.log——
        # 逐条判定的原始输出量很大，混进主日志会把正常日志淹掉。
        _trace(
            "DEBUG",
            umo,
            f"| msg_len={len(message)} | norm={normalize_text(message)[:80]!r} "
            f"| dedup_key={(event_key or text_key)[:70]!r} "
            f"| heuristic={result.verdict}/{result.confidence:.3f} "
            f"| needs_llm={result.needs_llm} | llm_called={llm_verdict is not None} "
            f"| llm_raw={(llm_verdict.confidence if llm_verdict else '-')} "
            f"| final={verdict.verdict}/{verdict.confidence:.3f} "
            f"| reason={verdict.reason[:80]!r}",
        )

    # 钩子路：把指纹持久化，方便排障与状态核对（事件路的去重不依赖它，见上面的说明）。
    # 这一步必须独立落盘：`CONTINUE`（延续）判定下面不会触发状态变更，若只靠函数
    # 末尾那次 `maybe_save()`，标记就写不进磁盘。
    if via_hook:
        _record_hook_detection(state, text_key, now)
        _store().mark_dirty()
        _store().maybe_save()

    # 状态推进：无论判定结果如何，这条消息都成为「上文」
    recent = _recent_for(umo)
    recent.append(message)
    if len(recent) > RECENT_KEEP:
        del recent[: len(recent) - RECENT_KEEP]
    state.last_user_text = message
    state.last_ts = time.time()
    state.turns += 1
    if verdict.is_new_topic:
        state.switches += 1
        # 模型没参与时用消息本身当话题名，保证「当前话题」永远不是空的
        state.label = verdict.topic_label or derive_label(message) or state.label
        if verdict.topic_summary:
            state.summary = verdict.topic_summary
    elif verdict.topic_label:
        # 模型顺手刷新了当前话题的名称/摘要，采纳它
        state.label = verdict.topic_label
        state.summary = verdict.topic_summary or state.summary
    _store().mark_dirty()
    _store().maybe_save()

    if verdict.is_new_topic and _mode() in {MODE_HINT, MODE_AUTO}:
        # 需要改写请求：等请求钩子拿到 ProviderRequest 再执行
        RUNTIME.pending_actions[umo] = (message, verdict)
        logger.info(
            f"[{PLUGIN_NAME}] {_mode()} 的请求改写已挂起，等请求钩子执行"
        )
    return verdict


async def _apply_request_actions(ctx: Context | None, event: Any, req: Any) -> None:
    """执行挂起的请求改写（`hint` / `auto`）。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        req: 即将发出的请求。
    """
    umo = _umo(event)
    pending = RUNTIME.pending_actions.pop(umo, None)
    if pending is None:
        return
    message, verdict = pending
    state = _store().get(umo)
    if not verdict.is_new_topic:
        return
    logger.info(
        f"[{PLUGIN_NAME}] 执行挂起的动作 | mode={_mode()} "
        f"| msg={message[:40]} | confidence={verdict.confidence:.2f}"
    )
    await _act(ctx, event, req, umo, state, verdict)


async def _act(
    ctx: Context | None,
    event: Any,
    req: Any,
    umo: str,
    state: Any,
    verdict: Verdict,
) -> None:
    """按模式和判定结果执行动作。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        req: 即将发出的请求。
        umo: 统一消息来源。
        state: 话题状态。
        verdict: 最终判定。
    """
    if not verdict.is_new_topic or req is None:
        return

    # 调试模式：一律空跑。判定已经做完了，这里只报告"本想做什么"就返回，
    # 绝不碰上下文（不新建对话）也不碰请求（不注入 hint）。
    if _debug():
        logger.info(
            f"[{PLUGIN_NAME}] [调试模式] 空跑，不执行动作 | 本想按 mode={_mode()} 处理"
            f" | 判定={'NEW-TOPIC' if verdict.is_new_topic else 'CONTINUE'}"
            f" | 置信度={verdict.confidence:.2f} | 话题={verdict.topic_label or '未命名'}"
        )
        _trace(
            "DRYRUN",
            umo,
            f"| mode={_mode()} | would_act=new_topic | confidence={verdict.confidence:.2f}"
            f" | topic={verdict.topic_label or '-'} | switched=no | hint_injected=no",
        )
        return

    if _mode() == MODE_AUTO:
        threshold = _num("auto_threshold", 0.8)
        if verdict.confidence < threshold:
            logger.info(
                f"[{PLUGIN_NAME}] 判为新话题但置信度 {verdict.confidence:.2f} "
                f"< auto_threshold {threshold:.2f}，不自动切换"
            )
            return
        await _switch_conversation(ctx, event, req, umo, verdict)
        return

    if _mode() == MODE_HINT:
        hint = make_hint(verdict)
        if hint and TextPart is not None:
            try:
                req.extra_user_content_parts.append(
                    TextPart(text=hint).mark_as_temp()
                )
                logger.info(
                    f"[{PLUGIN_NAME}] 已给本轮注入新话题提示"
                    f"（{verdict.topic_label or '未命名'}）"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 注入提示失败: {exc}")
        return

    # mark：什么都不改，日志已经写了


async def _switch_conversation(
    ctx: Context | None,
    event: Any,
    req: Any,
    umo: str,
    verdict: Verdict,
) -> None:
    """新建一个对话并把本轮请求挂过去。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        req: 即将发出的请求。
        umo: 统一消息来源。
        verdict: 最终判定。
    """
    manager = getattr(ctx, "conversation_manager", None)
    if manager is None:
        logger.warning(f"[{PLUGIN_NAME}] 没有会话管理器，无法切换对话")
        return
    persona_id = None
    try:
        if req.conversation is not None:
            persona_id = getattr(req.conversation, "persona_id", None)
    except Exception:  # noqa: BLE001
        persona_id = None
    try:
        new_cid = await manager.new_conversation(
            umo,
            event.get_platform_id(),
            title=(verdict.topic_label or None),
            persona_id=persona_id,
        )
    except Exception as exc:  # noqa: BLE001 - 切换失败时保持原对话继续
        logger.error(f"[{PLUGIN_NAME}] 新建对话失败，本轮保持原上下文: {exc}")
        return

    # 让本轮消息存进新对话，并从空上下文开始
    try:
        conversation = await manager.get_conversation(umo, new_cid)
        if conversation is not None:
            req.conversation = conversation
        req.contexts = []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[{PLUGIN_NAME}] 新对话 {str(new_cid)[:8]} 已创建，但绑定请求失败: {exc}"
        )

    state = _store().get(umo)
    state.conv_id = str(new_cid)
    state.label = verdict.topic_label or state.label
    state.summary = verdict.topic_summary or ""
    state.turns = 0
    RUNTIME.recent[umo] = [state.last_user_text] if state.last_user_text else []
    _store().mark_dirty()

    logger.info(
        f"[{PLUGIN_NAME}] 已自动开启新对话 {str(new_cid)[:8]}"
        f"（话题：{verdict.topic_label or '未命名'}，置信度 {verdict.confidence:.2f}）"
    )

    if _flag("new_conversation_notice", True):
        try:
            await event.send(
                event.plain_result(
                    f"检测到新话题：{verdict.topic_label or '未命名'}，已为你开一段新对话。"
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 发送新话题提示失败: {exc}")


# --------------------------------------------------------------------------- 插件


class TopicRouterPlugin(Star):
    """话题路由器插件本体。

    三个 handler 都只负责"认参数 → 转调模块级函数"，方法体里不出现任何 `self.xxx`
    （原因见文件头）。判定逻辑全在模块级函数里，因此可离线测试。
    """

    def __init__(self, context: Context | None = None, config: dict | None = None) -> None:
        """初始化插件。

        Args:
            context: AstrBot 注入的插件上下文。
            config: 插件配置（来自 WebUI）。
        """
        try:
            super().__init__(context)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - 构造失败也要能给出线索
            logger.warning(f"[{PLUGIN_NAME}] Star 基类初始化异常: {exc}")

        RUNTIME.config = config if isinstance(config, dict) else {}
        # 上下文也放模块级：handler 的 self 可能不是本实例
        global _CTX
        _CTX = context
        data_dir = _data_root() / "plugin_data" / PLUGIN_NAME
        RUNTIME.store = TopicStore(data_dir / "topic_state.json")
        RUNTIME.recent = {}
        RUNTIME.pending_actions = {}
        RUNTIME.aggregation = None
        RUNTIME.aggregation_error = ""
        RUNTIME.extras_tasks = []

        # 阶梯放权 / 每日复盘：与聚合无关，单独初始化
        _init_extras()

        # 内建消息聚合：默认开启，初始化失败自动退化为逐条判定
        _init_aggregation(context)

        logger.info(
            f"[{PLUGIN_NAME}] 实例已构造 {PLUGIN_VERSION} "
            f"| file={Path(__file__).resolve()} | mode={_mode()} "
            f"| 聚合={'开' if RUNTIME.aggregation is not None else '关'}"
        )

    async def initialize(self) -> None:
        """插件加载完成：启动聚合后台任务并注册可视化页面后端。

        AstrBot 的调用点是 `await metadata.star_cls.initialize()`（实例方法），
        所以这里必须是协程；`service.initialize()` 本身是同步的（注册路由 + 建后台任务），
        不需要再套一层 await。
        """
        # 阶梯放权 / 每日复盘：不依赖聚合，先启动
        _init_extras()
        try:
            _apply_promotion()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 启动时推进放权失败（已忽略）：{exc}")
        _start_extras_tasks()

        service = RUNTIME.aggregation
        if service is None:
            if RUNTIME.aggregation_error:
                logger.warning(
                    f"[{PLUGIN_NAME}] 聚合不可用，继续以逐条判定运行："
                    f"{RUNTIME.aggregation_error}"
                )
            return
        try:
            service.initialize()
        except Exception as exc:  # noqa: BLE001 - 页面/后台任务失败不能拖垮判定
            logger.warning(
                f"[{PLUGIN_NAME}] 聚合阶段初始化失败（判定不受影响）：{exc}"
            )

    async def terminate(self) -> None:
        """插件卸载/停用时保存状态，并优雅关停聚合。"""
        await _stop_extras_tasks()
        service = RUNTIME.aggregation
        if service is not None:
            try:
                await service.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 关停聚合失败: {exc}")
        try:
            _store().save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 退出时保存状态失败: {exc}")


# ===========================================================================
# 下面四个 handler 是**模块级函数**，故意不写成类的方法。
#
# 为什么（实机证据，别再改回方法）：
#
#   core.core_lifecycle:372  hook(on_astrbot_loaded) -> None - on_loaded_v017
#   TypeError: TopicRouterPlugin.on_loaded_v017() missing 1 required positional argument: 'self'
#
# AstrBot 的调用是 `await handler.handler()`（`core/core_lifecycle.py:375`），**不传
# self**。正常环境里注册表存的是**绑定方法**（self 已绑定），所以没事；但在这个环境
# 里注册进去的是**未绑定的类函数**，于是：
#
#   * 要求 self 的 handler 直接报 missing 'self'（上面那条）；
#   * `(self, event, req)` 这种签名会报 missing 'req'（event 落进 self）；
#   * `(self, *args)` 会把事件塞进 self（日志里 self=WebChatMessageEvent）。
#
# 模块级函数没有 self 这个位置，AstrBot 怎么调都落对：调用方多传的 self 会被
# `*args/**kwargs` 吃掉，事件与请求由 `_extract` 按类型认出来。
# ===========================================================================


@filter.event_message_type(filter.EventMessageType.ALL, priority=MESSAGE_PRIORITY)
async def handle_message_v090(*args: Any, **kwargs: Any):
    """消息入口：聚合（默认）→ 在合并文本上判定一次。

    这是 v0.9.0 的**第一阶段**：优先级 1000，尽最大可能先于普通插件拿到消息。
    聚合开启时把连续消息缓冲成一轮（见 `aggregation/service.py`），重构事件后
    通过 `on_settled` 回调在合并文本上判定一次；聚合关闭时退化成逐条即时判定。

    **入口日志是必需的**：AstrBot 的 `star_request.py` 是
    `for handler in activated_handlers: if event.is_stopped(): break`，任何一个
    handler 抛异常都会 `stop_event()`，排在后面的 handler 全部不执行。所以这里
    既要在入口留下证据，也要自己兜住所有异常。

    Args:
        *args: 派发方给的位置参数（事件，或先来一个被误当 self 的对象）。
        **kwargs: 关键字形式的情况。

    Yields:
        None: 只做判定与记录，不占用消息（不回复、不阻断）。
    """
    event, _req = _extract(*args, **kwargs)
    message = (getattr(event, "message_str", "") or "").strip()
    msg_obj = getattr(event, "message_obj", None)
    _trace(
        "ENTRY",
        str(getattr(event, "unified_msg_origin", "-")),
        f"| v={PLUGIN_VERSION} | msg={message[:60]!r} "
        f"| msg_id={getattr(msg_obj, 'message_id', '')!r} "
        f"| stopped={getattr(event, '_force_stopped', '?')}",
    )
    stopped = "?"
    try:
        stopped = str(event.is_stopped())
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        f"[{PLUGIN_NAME}] handle_message_v090 被调用 | {PLUGIN_VERSION} "
        f"| umo={getattr(event, 'unified_msg_origin', '?')} "
        f"| msg={message[:40]!r} | stopped={stopped} "
        f"| 聚合={'开' if RUNTIME.aggregation is not None else '关'} "
        f"| args={[type(a).__name__ for a in args]}"
    )
    if event is not None:
        # 先打"入口执行过"标记：抢占检测靠它区分"没被派发到"与"不需要处理"。
        _mark_event(event, SEEN_ATTR)
        service = RUNTIME.aggregation
        if service is not None:
            try:
                await service.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 绝不让异常外泄
                # 聚合层绝不能把异常抛出去：会被 stop_event()，连累同一条消息上的
                # 其它插件（这个坑我们踩过）。
                logger.error(
                    f"[{PLUGIN_NAME}] 聚合异常（已兜住，不影响其它插件）：{exc}",
                    exc_info=True,
                )
                try:
                    service.panic_release(event)
                except Exception:  # noqa: BLE001
                    pass
        elif message and not _looks_like_command(message):
            # 聚合关闭（或初始化失败）：保持 v0.8.6 的逐条判定语义
            _mark_event(event, DETECTED_ATTR)
            try:
                await _detect(_ctx(), event, message)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    f"[{PLUGIN_NAME}] 判定异常（已兜住，不影响其它插件）：{exc}",
                    exc_info=True,
                )
    return
    yield  # pragma: no cover - 让它是异步生成器，框架两种形态都接受


@filter.on_llm_request(priority=MESSAGE_PRIORITY)
async def hook_llm_request_v090(*args: Any, **kwargs: Any) -> None:
    """请求钩子：**判定兜底** + `hint` / `auto` 的请求改写。

    ## v0.9.0 的职责变化

    判定主路径已经前移到消息入口（`handle_message_v090` 的聚合阶段）：合并完成、
    事件重构之后立即在合并文本上判一次。本钩子现在负责两件事：

    1. **兜底判定**：消息入口没被派发（被更高优先级者 `stop_event()` 了）时，
       这里仍然能判定，并把这次抢占落成 `aggregate.preempted` 告警。本钩子不在
       `star_request.py` 的 `is_stopped()` break 循环里，任何优先级都挡不住它；
    2. **请求改写**：`hint` / `auto` 的动作要落到 `ProviderRequest` 上，只能在
       这里执行（`_detect` 先把动作挂进 `RUNTIME.pending_actions`）。

    ## 为什么不能在这里无条件重判

    聚合开启时，入口已经在合并文本上判过（`DETECTED_ATTR`）；再判一次会把
    `turns` 推快、把模型调用翻倍。所以只有"入口没被派发"或"聚合关闭且入口没判"
    时才走兜底。去重仍然是第二道保险（见 `_detect` 的 `_batch_key`）。

    ## 本钩子能拿到事件对象

    `call_event_hook` 是 `await handler.handler(event, *args)`，实机为
    `args=['WeixinOCMessageEvent','ProviderRequest']`、`has_event=True`。
    所以这里优先用事件取消息与 `umo`，只有老框架拿不到事件时才退回从 prompt 抠。

    Args:
        *args: 派发方给的位置参数（实机上只有 `ProviderRequest`）。
        **kwargs: 关键字形式的情况。
    """
    event, req = _extract(*args, **kwargs)
    event_umo = _umo(event) if event is not None else ""
    _trace(
        "HOOK",
        event_umo or "-",
        f"| v={PLUGIN_VERSION} | role=fallback "
        f"| has_event={event is not None} "
        f"| has_request={req is not None} "
        f"| prompt_len={len(getattr(req, 'prompt', '') or '') if req is not None else 0}",
    )
    logger.info(
        f"[{PLUGIN_NAME}] hook_llm_request_v090 被调用 | {PLUGIN_VERSION} "
        f"| has_event={event is not None} | has_request={req is not None} "
        f"| args={[type(a).__name__ for a in args]} | kwargs={sorted(kwargs)}"
    )
    if req is None:
        return
    ctx = _ctx()
    service = RUNTIME.aggregation

    # --- 1) 抢占检测：入口没跑就等于被更高优先级者挤掉了 ---
    seen = event is not None and _event_marked(event, SEEN_ATTR)
    if event is not None and not seen:
        # 先区分"没进本次派发计划"与"进了计划但没执行"：
        # 前者是注册/metadata 层面的问题（v0.8.x 起实际发生的那种），后者才是抢占。
        _warn_if_entry_absent_from_plan(event)
    if event is not None and not seen and service is not None:
        try:
            if service.check_preempted(event):
                _trace(
                    "PREEMPTED",
                    event_umo,
                    f"| v={PLUGIN_VERSION} | priority={MESSAGE_PRIORITY} "
                    f"| msg={(getattr(event, 'message_str', '') or '')[:60]!r}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 抢占检测异常（已忽略）：{exc}")

    # --- 2) 兜底判定：只在入口没判过时执行 ---
    detected = event is not None and _event_marked(event, DETECTED_ATTR)
    aggregation_active = service is not None and bool(
        getattr(getattr(service, "cfg", None), "enabled", False)
    )
    fallback = (
        event is None
        or not seen
        or (not aggregation_active and not detected)
    )
    if _flag("detect_in_hook", True) and fallback:
        message = _hook_user_message(event, req)
        # 有事件对象时**以事件为准**：`_umo(event)` 是权威的会话标识，
        # `_batch_key` 也能算出消息 ID 键，两路因此共用同一把去重锁。
        # 拿不到事件（老版本框架）才退回从 prompt 抠文本、按文本去重。
        umo = event_umo or _umo_from_request(req)
        if message and not _looks_like_command(message):
            if event is not None:
                _mark_event(event, DETECTED_ATTR)
            try:
                await _detect(ctx, event, message, umo=umo, via_hook=True)
            except Exception as exc:  # noqa: BLE001 - 判定失败绝不影响正常回复
                logger.warning(
                    f"[{PLUGIN_NAME}] 钩子内判定异常（已兜住）：{type(exc).__name__}: {exc}"
                )
        else:
            logger.debug(f"[{PLUGIN_NAME}] 钩子里没取到可判定的用户消息，跳过本轮判定")

    # --- 3) 请求改写：执行 `_detect` 挂起的 hint / auto 动作 ---
    # 这一条**必须有事件对象**（改写要落到具体会话上）；拿不到就跳过，
    # 等消息事件那条路（如果它还活着）或下一轮处理。
    if event is None:
        return
    try:
        await _apply_request_actions(ctx, event, req)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{PLUGIN_NAME}] 请求改写被跳过：{type(exc).__name__}: {exc}")


@filter.on_llm_response()
async def hook_llm_response_v090(*args: Any, **kwargs: Any) -> None:
    """记下机器人这一轮的回复，供下一次判定使用。

    判定模型需要"上一轮答了什么"才能判断承接关系：很多换话题发生在回答之后
    （"好的，那再问个别的"）。AstrBot 的调用点是
    `call_event_hook(event, EventType.OnLLMResponseEvent, llm_response)`，但这里
    同样不假设参数位置/名称：用 `_extract` 认事件，再在其余参数里找带文本的对象。

    Args:
        *args: 派发方给的位置参数（事件、`LLMResponse` 等）。
        **kwargs: 关键字形式的情况。
    """
    event, _req = _extract(*args, **kwargs)
    reply = ""
    for candidate in args:
        if _looks_like_event(candidate):
            continue
        text = _extract_response_text(candidate)
        if text:
            reply = text
            break
    if not reply:
        for candidate in kwargs.values():
            if _looks_like_event(candidate):
                continue
            text = _extract_response_text(candidate)
            if text:
                reply = text
                break
    if event is None or not reply:
        return
    RUNTIME.pending_replies[_umo(event)] = reply[:1000]


@filter.command("topicweave", alias={"话题织机"}, priority=MESSAGE_PRIORITY)
async def command_topic_v090(*args: Any, **kwargs: Any):
    """查看/管理当前会话的话题状态。

    指令名为什么不是 `topic`：`topic` 太通用，AstrBot 的「检测到指令冲突」会把它和
    别的插件判成同一指令、一次触发多个插件响应。别名只留一个中文长名（`话题织机`）：
    `topicrouter` 这类英文常用词同样会撞名（实测踩过）。

    Args:
        *args: 派发方给的位置参数（事件）。

    Yields:
        指令回复。
    """
    event, _req = _extract(*args, **kwargs)
    if event is None:
        return
    try:
        result = await _topic_status(_ctx(), event)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[{PLUGIN_NAME}] /topicweave 执行失败: {exc}", exc_info=True)
        return
    yield event.plain_result(result)


@filter.on_astrbot_loaded()
async def on_loaded_v090(*args: Any, **kwargs: Any) -> None:
    """启动后把可用的模型 ID 打进日志，方便填 provider_id。

    签名不带任何必填参数：实机上是 `await handler.handler()` 直接调用的（连 self 都
    不传），多一个必填参数就会 `missing ...` 报错。

    Args:
        *args: 派发方给的位置参数。
        **kwargs: 关键字形式的情况。
    """
    ids = _list_provider_ids(_ctx())
    configured = str(RUNTIME.config.get("provider_id", "") or "").strip()

    logger.info(
        f"[{PLUGIN_NAME}] 已加载 {PLUGIN_VERSION} | 模式 {_mode()} | 判定模型 "
        f"{configured or '（跟随当前会话）'} | 状态文件 {_store().path}"
    )
    if configured and ids and configured not in ids:
        logger.warning(
            f"[{PLUGIN_NAME}] ⚠ 配置的判定模型 '{configured}' 不在可用列表里，"
            "判定模型那层会失效（只剩启发式）。请从下面的 ID 里挑一个填进 provider_id，"
            "或把 provider_id 清空以跟随当前会话模型。"
        )
    if ids:
        logger.info(
            f"[{PLUGIN_NAME}] 可用的模型 ID（可直接填进 provider_id）："
            + "、".join(ids[:20])
        )

    # 入口注册自检：把"框架到底认不认我们的 handler"在启动时就变成一行明确结论。
    # v0.8.x 到 v0.9.1 的问题恰恰是这里没有任何输出——入口一次都没执行，日志里却
    # 只有"加载成功"。
    report = _registration_report()
    if report.get("ok"):
        logger.info(
            f"[{PLUGIN_NAME}] 入口自检通过 | module={report.get('handler_module_path')} "
            f"| 元数据={report.get('metadata_name')} "
            f"| filters={report.get('filters')} | 元数据条目="
            f"{report.get('metadata_count')}"
        )
    elif report.get("available", True):
        logger.warning(
            f"[{PLUGIN_NAME}] ⚠ 入口自检未通过："
            + "；".join(report.get("problems") or ["未知"])
            + " —— 聚合会整层空转，消息入口与 /topicweave 都不会被派发"
        )


# --------------------------------------------------------------------- 工具


async def _topic_status(ctx: Context | None, event: Any) -> str:
    """处理 `/topic` 指令并返回要发送的文本。

    Args:
        ctx: 插件上下文。
        event: 消息事件。

    Returns:
        回复文本。
    """
    raw = (getattr(event, "message_str", "") or "").strip()
    parts = raw.split(maxsplit=2)
    action = parts[1].strip().lower() if len(parts) > 1 else ""
    argument = parts[2].strip() if len(parts) > 2 else ""
    umo = _umo(event)
    conv_id = await _conversation_id(ctx, event)
    state = _state(ctx, umo, conv_id)
    store = _store()

    if action in {"", "status", "状态"}:
        return "\n".join(
            [
                f"当前话题状态（插件 {PLUGIN_VERSION}）",
                f"话题：{state.label or '（还没建立）'}",
                f"摘要：{state.summary or '（无）'}",
                f"当前话题已聊：{state.turns} 轮；累计判定新话题：{state.switches} 次",
                f"本小时模型判定：{state.checks_last_hour()} 次",
                f"本会话自动判断：{'已关闭' if state.disabled else '开启'}",
                f"模式：{_mode()}",
            ]
        )

    manager = getattr(ctx, "conversation_manager", None)

    if action in {"new", "切换", "新话题"}:
        label = argument or "手动切换的话题"
        if manager is None:
            return "没有可用的会话管理器，无法新建对话。"
        cid = await manager.new_conversation(umo, event.get_platform_id())
        state = store.reset(umo)
        state.conv_id = str(cid)
        state.label = label
        state.last_ts = time.time()
        RUNTIME.recent[umo] = []
        store.mark_dirty()
        store.save()
        return f"已开始新对话（{str(cid)[:8]}），话题：{label}"

    if action in {"forget", "忘记", "clear"}:
        store.reset(umo)
        RUNTIME.recent[umo] = []
        store.save()
        return "已忘记当前话题，下一条消息会重新建档。"

    if action in {"off", "关闭"}:
        state.disabled = True
        store.mark_dirty()
        store.save()
        return "本会话已关闭话题自动判断。"

    if action in {"on", "开启"}:
        state.disabled = False
        store.mark_dirty()
        store.save()
        return "本会话已开启话题自动判断。"

    if action in {"conflicts", "冲突"}:
        return _command_conflicts()

    return (
        "用法：/topicweave 查看状态；/topicweave new [名称] 手动翻篇；"
        "/topicweave forget 忘记当前话题；/topicweave off|on 本会话开关；"
        "/topicweave conflicts 列出指令冲突。"
    )


def _command_conflicts() -> str:
    """列出注册表里所有指令冲突（含是哪个插件注册的）。

    AstrBot 面板会提示"检测到指令冲突"，但不一定方便复制。这个函数直接读内存里的
    handler 注册表，把冲突双方的**插件名 + 指令名**打出来，便于定位该改谁。

    返回内容同时写进日志（面板提示往往看不到细节）。

    Returns:
        可读的文本报告。
    """
    try:
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception as exc:  # noqa: BLE001
        return f"无法读取注册表：{exc}"

    groups: dict[str, list[str]] = {}
    for handler in star_handlers_registry:
        names: list[str] = []
        for flt in getattr(handler, "event_filters", []) or []:
            if isinstance(flt, CommandFilter):
                base = getattr(flt, "command_name", None) or getattr(
                    flt, "command", None
                )
                if base:
                    names.append(str(base))
                for alias in getattr(flt, "alias", None) or []:
                    names.append(str(alias))
        if not names:
            continue
        module_path = getattr(handler, "handler_module_path", "") or ""
        meta = star_map.get(module_path)
        plugin = getattr(meta, "name", None) or module_path
        for name in names:
            groups.setdefault(name, []).append(
                f"{plugin}.{getattr(handler, 'handler_name', '?')}"
            )

    conflicts = {
        name: owners
        for name, owners in groups.items()
        if len({owner.split(".", 1)[0] for owner in owners}) > 1
    }

    lines = [f"指令冲突检查（插件 {PLUGIN_VERSION}）"]
    lines.append(f"注册表里的指令名共 {len(groups)} 个，冲突 {len(conflicts)} 个")
    if not conflicts:
        lines.append("当前没有冲突。")
    for name, owners in sorted(conflicts.items()):
        lines.append(f"· /{name} ← " + "、".join(sorted(set(owners))))
    report = "\n".join(lines)
    logger.info(f"[{PLUGIN_NAME}] {report.replace(chr(10), ' | ')}")
    return report


__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "TopicRouterPlugin"]

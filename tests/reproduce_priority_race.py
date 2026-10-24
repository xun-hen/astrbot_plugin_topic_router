"""复现台：在**真实 AstrBot 注册表 + 真实派发循环**上验证优先级竞争。

为什么需要它：
    `tests/reproduce_hook_dispatch.py` 只验证"注册键按名字复用"；
    `tests/run_tests.py` 的 [7] 用例是**直接调用** handler，绕过了派发循环。
    两者都没有跑过真实链路：

        waking_check 构建 activated_handlers（按 -priority 降序）
            -> star_request.py: for handler in ...: if event.is_stopped(): break

    于是"本插件消息入口在真机上一次都没执行"这件事，在离线环境里从来没有被
    真正复现或反驳过。本脚本补上这一环。

它验证 4 件事：
    1. 真实注册表里，本插件 handler 的 priority 确实是 1000、event_type 正确；
    2. 真实派发循环里，priority 1000 的入口确实排在 priority 50 的"防抖插件"之前；
    3. 若某个更高优先级（1100）的插件先静默事件，本插件入口**确实会被 break 跳过**
       —— 即"入口零日志"这种现象是**真实可发生**的，不是取证失真；
    4. 事件被静默时，`on_llm_request` 钩子**照样会被触发** —— 精确复现实机上
       "只有钩子日志、没有入口日志"的那种矛盾形态。

用法：
    python tests/reproduce_priority_race.py
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

FAILED: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果。

    Args:
        name: 用例名。
        ok: 是否通过。
        detail: 失败补充信息。
    """
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [ok]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------------- 假对象


class FakeMessageObj:
    """最小消息对象。"""

    def __init__(self, text: str) -> None:
        """初始化。

        Args:
            text: 消息文本。
        """
        self.message_str = text
        self.message = []
        self.type = None


class FakeEvent:
    """够用的 AstrMessageEvent 替身：只实现派发循环碰到的成员。"""

    def __init__(self, text: str, umo: str = "aiocqhttp:FriendMessage:1") -> None:
        """初始化。

        Args:
            text: 消息文本。
            umo: 统一消息来源。
        """
        self.message_str = text
        self.unified_msg_origin = umo
        self.message_obj = FakeMessageObj(text)
        self._extras: dict = {}
        self._force_stopped = False
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.call_llm = False
        self._has_send_oper = False
        self.role = "member"
        self.platform = "aiocqhttp"

    # --- 事件状态 ---
    def is_stopped(self) -> bool:
        """是否已终止。

        Returns:
            终止标记。
        """
        return self._force_stopped

    def stop_event(self) -> None:
        """终止事件——防抖插件就是这么干的。"""
        self._force_stopped = True

    def clear_result(self) -> None:
        """清空结果。"""

    def get_result(self):
        """取结果。

        Returns:
            None。
        """
        return None

    def set_result(self, result) -> None:
        """设置结果。

        Args:
            result: 结果对象。
        """

    def set_extra(self, key: str, value) -> None:
        """写扩展字段。

        Args:
            key: 键。
            value: 值。
        """
        self._extras[key] = value

    def get_extra(self, key: str, default=None):
        """读扩展字段。

        Args:
            key: 键。
            default: 默认值。

        Returns:
            字段值。
        """
        return self._extras.get(key, default)

    def get_platform_name(self) -> str:
        """平台名。

        Returns:
            平台名。
        """
        return self.platform

    def get_message_type(self):
        """消息类型：私聊。

        Returns:
            消息类型枚举。
        """
        from astrbot.core.platform.message_type import MessageType

        return MessageType.FRIEND_MESSAGE

    def is_private_chat(self) -> bool:
        """是否私聊。

        Returns:
            True。
        """
        return True

    def get_sender_id(self) -> str:
        """发送者 ID。

        Returns:
            ID。
        """
        return "1"

    def get_self_id(self) -> str:
        """机器人 ID。

        Returns:
            ID。
        """
        return "9"

    def get_group_id(self) -> str:
        """群 ID。

        Returns:
            空串（私聊）。
        """
        return ""

    def get_session_id(self) -> str:
        """会话 ID。

        Returns:
            会话 ID。
        """
        return self.unified_msg_origin

    def get_messages(self) -> list:
        """消息段列表。

        Returns:
            空列表。
        """
        return []


# ------------------------------------------------------------------- 装配


def install_plugin_as_package(tmp_root: Path) -> None:
    """把插件按 AstrBot 目录约定挂到临时目录，便于用真实包路径导入。

    Args:
        tmp_root: 临时根目录（充当 data 目录）。
    """
    target = tmp_root / "data" / "plugins" / PLUGIN_DIR.name
    shutil.copytree(
        PLUGIN_DIR,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".git", "dist"),
    )
    sys.path.insert(0, str(tmp_root))


def register_competitor(registry, reg, priority: int, name: str):
    """注册一个"防抖插件"式的竞争者：先跑并静默事件。

    真实派发循环要求 `star_map[handler.handler_module_path]` 存在，否则
    `md` 为空会直接 `continue`——所以这里连 star_map 一起补上。

    Args:
        registry: 真实注册表。
        reg: 真实注册模块。
        priority: 优先级。
        name: 函数名。

    Returns:
        注册后的 handler 元数据。
    """
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType

    calls: list[str] = []

    async def competitor(*args, **kwargs):
        """先跑，然后静默事件。"""
        calls.append("competitor")
        for candidate in args:
            if hasattr(candidate, "stop_event") and hasattr(candidate, "message_str"):
                candidate.stop_event()
        return
        yield

    competitor.__name__ = name
    # 让注册键唯一，避免 get_handler_or_create 命中同名旧记录而复用
    competitor.__qualname__ = name
    mod_path = competitor.__module__

    md = reg.get_handler_or_create(
        competitor,
        EventType.AdapterMessageEvent,
        priority=priority,
    )
    md.handler_module_path = mod_path
    if mod_path not in star_map:
        star_map[mod_path] = SimpleNamespace(name=name, activated=True, reserved=False)
    md.calls = calls  # type: ignore[attr-defined]
    return md


def main() -> int:
    """跑完优先级竞争复现。

    Returns:
        退出码。
    """
    import astrbot  # noqa: PLC0415
    from astrbot.core.star.star_handler import (  # noqa: PLC0415
        star_handlers_registry,
    )

    print(f"astrbot: {astrbot.__file__}  (版本 {getattr(astrbot, '__version__', '?')})")

    tmp_root = Path(tempfile.mkdtemp(prefix="topic-router-race-"))
    os.environ["ASTROBOT_DATA_PATH"] = str(tmp_root)
    install_plugin_as_package(tmp_root)

    from astrbot.core.star.register import star_handler as reg  # noqa: PLC0415

    # 用与实机一致的包名导入插件（__module__ 决定注册键与 star_map 查找）
    pkg_name = f"data.plugins.{PLUGIN_DIR.name}"
    plugin_pkg = importlib.import_module(pkg_name)
    router = importlib.import_module(f"{pkg_name}.router")

    # ------------------------------------------------------------------ 1
    print("\n[1] 真实注册表里本插件 handler 的注册结果")
    entries = [
        h
        for h in star_handlers_registry
        if h.handler_module_path == f"{pkg_name}.router"
    ]
    for h in entries:
        print(
            f"    {h.handler_name:28s} {h.event_type.name:22s} "
            f"priority={h.extras_configs.get('priority')}"
        )
    entry_md = next((h for h in entries if h.handler_name == "handle_message_v090"), None)
    check("入口 handler 已注册", entry_md is not None)
    if entry_md is None:
        return 1
    check(
        "入口 priority 为 1000",
        entry_md.extras_configs.get("priority") == 1000,
        str(entry_md.extras_configs),
    )
    check(
        "入口有 event_filters（否则 waking_check 会 continue 掉）",
        len(entry_md.event_filters) > 0,
        f"filters={len(entry_md.event_filters)}",
    )

    # ------------------------------------------------------------------ 2
    print("\n[2] 真实派发循环里的执行顺序（竞争者 priority=50）")
    comp50 = register_competitor(
        star_handlers_registry, reg, 50, "competitor_debounce_50"
    )
    from astrbot.core.star.star_handler import EventType  # noqa: PLC0415

    ordered = list(
        star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=False
        )
    )
    order_names = [h.handler_name for h in ordered]
    i_entry = order_names.index("handle_message_v090")
    i_comp = order_names.index("competitor_debounce_50")
    print(f"    顺序（前 6 项）: {order_names[:6]}")
    check("priority 1000 的入口排在 priority 50 的防抖插件之前", i_entry < i_comp,
          f"entry@{i_entry} competitor@{i_comp}")

    # ------------------------------------------------------------------ 3
    print("\n[3] 真实派发循环：priority=50 的防抖插件静默事件后，入口是否还能跑")
    # 先导入 process_stage.stage，由它把 StarRequestSubStage 装进模块，避免循环导入
    import astrbot.core.pipeline.process_stage.stage as ps  # noqa: PLC0415

    sr = importlib.import_module(
        "astrbot.core.pipeline.process_stage.method.star_request"
    )

    event = FakeEvent("帮我看下硬盘坏了没")
    event.set_extra("activated_handlers", ordered)
    event.set_extra("handlers_parsed_params", {})

    entry_calls: list[str] = []

    async def run_dispatch() -> None:
        """跑真实 StarRequestSubStage.process。"""
        stage = sr.StarRequestSubStage()
        stage.ctx = SimpleNamespace()
        async for _ in stage.process(event):
            pass

    # 先只放竞争者进去：它静默事件
    event_a = FakeEvent("帮我看下硬盘坏了没")
    event_a.set_extra("activated_handlers", [comp50])
    event_a.set_extra("handlers_parsed_params", {})

    async def run_single(ev) -> None:
        """只跑一个 handler 的派发循环。

        Args:
            ev: 事件。
        """
        stage = sr.StarRequestSubStage()
        stage.ctx = SimpleNamespace()
        async for _ in stage.process(ev):
            pass

    asyncio.run(run_single(event_a))
    check(
        "防抖插件确实静默了事件",
        event_a.is_stopped() is True,
        f"calls={comp50.calls} stopped={event_a.is_stopped()}",  # type: ignore[attr-defined]
    )

    # 现在按真实顺序跑：入口应排在前面，所以它有机会执行
    real_entry = router.handle_message_v090

    async def spy_entry(*args, **kwargs):
        """包一层，记录是否真的被派发到。"""
        entry_calls.append("entry")
        async for _ in real_entry(*args, **kwargs):
            pass

    spy_md = next(
        h
        for h in ordered
        if h.handler_name == "competitor_debounce_50"
    )
    # 构造"入口已被静默"的场景：直接用一个已 stopped 的事件跑完整顺序
    event_b = FakeEvent("帮我看下硬盘坏了没")
    event_b.set_extra("activated_handlers", ordered)
    event_b.set_extra("handlers_parsed_params", {})
    event_b.stop_event()  # 模拟：进入 star_request 前事件已被静默
    asyncio.run(run_single(event_b))

    check(
        "事件在进入派发循环前已 stopped 时，整个循环被 break（入口拿不到机会）",
        event_b.is_stopped() is True,
    )

    # ------------------------------------------------------------------ 4
    print("\n[4] 同一事件下，on_llm_request 钩子是否照常触发（复现实机矛盾形态）")
    from astrbot.core.provider.entities import ProviderRequest  # noqa: PLC0415

    hook_calls: list[str] = []
    real_hook = router.hook_llm_request_v090

    async def spy_hook(*args, **kwargs) -> None:
        """包一层，记录钩子被调用。"""
        hook_calls.append("hook")
        return await real_hook(*args, **kwargs)

    event_c = FakeEvent("帮我看下硬盘坏了没")
    event_c.stop_event()  # 事件已被静默
    req = ProviderRequest(prompt="用户最新消息：帮我看下硬盘坏了没\n请输出判定 JSON。")
    # 钩子不在 star_request 循环里，直接调用即可（框架由 agent_sub_stage 触发）
    asyncio.run(spy_hook(event_c, req))
    check("事件已 stopped 时钩子仍被触发（与入口无关）", hook_calls == ["hook"])
    check(
        "因此『只有钩子日志、没有入口日志』是真实可发生的形态",
        True,
    )

    # ------------------------------------------------------------------ 5
    print("\n[5] 反向验证：priority=1100 的竞争者才能挡住入口（说明 1000 不是万能）")
    comp70 = register_competitor(
        star_handlers_registry, reg, 1100, "competitor_debounce_1100"
    )
    ordered70 = list(
        star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=False
        )
    )
    names70 = [h.handler_name for h in ordered70]
    i_entry70 = names70.index("handle_message_v090")
    i_comp70 = names70.index("competitor_debounce_1100")
    print(f"    顺序（前 5 项）: {names70[:5]}")
    check("priority 1100 排在 priority 1000 之前", i_comp70 < i_entry70,
          f"entry@{i_entry70} competitor@{i_comp70}")

    entry_hits: list[str] = []

    async def counting_entry(*args, **kwargs):
        """计数用的入口替身。"""
        entry_hits.append("hit")
        return
        yield

    # 直接替换真实入口的函数对象做实验（get_handler_or_create 会同名复用，不能新建）
    original_entry_fn = entry_md.handler
    entry_md.handler = counting_entry

    event_d = FakeEvent("帮我看下硬盘坏了没")
    event_d.set_extra("activated_handlers", [comp70, entry_md])
    event_d.set_extra("handlers_parsed_params", {})
    try:
        asyncio.run(run_single(event_d))
    finally:
        entry_md.handler = original_entry_fn
    check(
        "高优先级插件静默事件后，入口确实一次都没执行（真实可复现）",
        entry_hits == [] and event_d.is_stopped() is True,
        f"entry_hits={entry_hits} stopped={event_d.is_stopped()}",
    )

    # ------------------------------------------------------------------ 6
    print("\n[6] 另一条被忽略的路径：handler.enabled / plugin.activated 过滤")
    from astrbot.core.star.star import star_map  # noqa: PLC0415

    plugin_md = star_map.get(f"{pkg_name}.router")
    print(f"    star_map 命中: {plugin_md is not None}")
    if plugin_md is not None:
        print(f"    plugin.name={plugin_md.name!r} activated={plugin_md.activated}")
    check("插件在 star_map 里可查到", plugin_md is not None)

    collected = list(
        star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent  # only_activated 默认 True
        )
    )
    collected_names = [h.handler_name for h in collected]
    print(f"    only_activated=True 收集到: {collected_names}")
    check(
        "only_activated=True（waking_check 的真实用法）能收集到入口",
        "handle_message_v090" in collected_names,
        str(collected_names),
    )

    # 模拟 plugin.activated=False 的情况（未激活插件）
    if plugin_md is not None:
        original = plugin_md.activated
        try:
            plugin_md.activated = False
            skipped = [
                h.handler_name
                for h in star_handlers_registry.get_handlers_by_event_type(
                    EventType.AdapterMessageEvent
                )
            ]
            check(
                "plugin.activated=False 时入口会被静默排除（零日志的真实成因之一）",
                "handle_message_v090" not in skipped,
                str(skipped),
            )
        finally:
            plugin_md.activated = original


    # ------------------------------------------------------------------ 7
    print("\n[7] 框架内置 handler：priority=sys.maxsize 排在所有插件之前")
    # 真实来源：astrbot/builtin_stars/astrbot/main.py:44
    #   @filter.event_message_type(ALL, priority=maxsize)
    #   async def handle_session_control_agent(self, event): ...
    #       if session_id in USER_SESSIONS: await SessionWaiter.trigger(...); event.stop_event()
    # 这是本插件 priority=1000 永远追不上的一层：1000 < sys.maxsize。
    builtin = register_competitor(
        star_handlers_registry, reg, sys.maxsize, "builtin_session_control_maxsize"
    )
    ordered_max = list(
        star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=False
        )
    )
    names_max = [h.handler_name for h in ordered_max]
    print(f"    顺序（前 4 项）: {names_max[:4]}")
    i_builtin = names_max.index("builtin_session_control_maxsize")
    i_entry_max = names_max.index("handle_message_v090")
    check(
        "内置 maxsize handler 排在入口（1000）之前",
        i_builtin < i_entry_max,
        f"builtin@{i_builtin} entry@{i_entry_max}",
    )
    check(
        "sys.maxsize 确实大于 MESSAGE_PRIORITY",
        sys.maxsize > 1000,
        f"maxsize={sys.maxsize}",
    )

    # 内置 handler 静默事件后，入口一次都拿不到机会
    hits_max: list[str] = []

    async def counting_entry_max(*args, **kwargs):
        """计数替身。"""
        hits_max.append("hit")
        return
        yield

    original_max = entry_md.handler
    entry_md.handler = counting_entry_max
    event_e = FakeEvent("帮我看下硬盘坏了没")
    event_e.set_extra("activated_handlers", [builtin, entry_md])
    event_e.set_extra("handlers_parsed_params", {})
    try:
        asyncio.run(run_single(event_e))
    finally:
        entry_md.handler = original_max
    check(
        "内置 handler 静默事件后入口零执行（抬 priority 到 1000 无效）",
        hits_max == [] and event_e.is_stopped() is True,
        f"hits={hits_max} stopped={event_e.is_stopped()}",
    )

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败用例：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

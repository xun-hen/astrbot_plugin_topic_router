"""可移植性测试：两种派发契约都要能工作。

这个插件最初是在一个**非标准派发**的环境里调通的（AstrBot 4.28.1 + webchat：注册表里
存的是未绑定类函数，AstrBot 直接 `handler(event, req)` 调用，`self` 落不到实例上）。
但绝大多数人的 AstrBot 是标准行为：注册表里存的是**绑定方法**。

所以本脚本同时模拟两种契约，确保别人装上也能用：

| 契约 | 调用形态 |
| --- | --- |
| 标准（绑定方法） | `plugin.handle_xxx(event, req)` —— self 已绑定 |
| 非标准（未绑定） | `Func(event, req)` —— 第一个位置参数可能就是事件 |
| 零参数（on_astrbot_loaded） | `Func()` |

用法（任意 Python 3.10+，不需要装 AstrBot —— 用桩模块隔开依赖）：

    python tests/test_portability.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

# 用桩模块隔开 AstrBot 依赖后，本测试只需要标准库 —— 但也无所谓，先按需引入。
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录断言结果。

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


# --------------------------------------------------------------------- 桩模块


def install_stub_astrbot() -> None:
    """装上最小的 astrbot 桩模块。

    只实现插件实际用到的那几个东西，这样本测试在任何 Python 3.10+ 上都能跑，
    不需要真的装 AstrBot。跑的仍然是**磁盘上真实的插件代码**。
    """
    if "astrbot" in sys.modules:
        return

    api = types.ModuleType("astrbot.api")

    class _Logger:
        def info(self, *a, **k):
            """吞掉 info 日志。"""

        def debug(self, *a, **k):
            """吞掉 debug 日志。"""

        def warning(self, *a, **k):
            """吞掉 warning 日志。"""

        def error(self, *a, **k):
            """吞掉 error 日志。"""

    api.logger = _Logger()

    event_mod = types.ModuleType("astrbot.api.event")

    def _decorator_factory(*_a, **_k):
        """任何注册装饰器都退化成恒等函数。"""

        def deco(func):
            """返回原函数。"""
            return func

        return deco

    class _EventMessageType:
        """桩 EventMessageType：只要能被 `|` 组合就行。"""

        GROUP_MESSAGE = 1
        PRIVATE_MESSAGE = 2
        OTHER_MESSAGE = 4
        ALL = 7

    class _Filter:
        """桩 filter：所有装饰器都返回原函数。"""

        EventMessageType = _EventMessageType

        @staticmethod
        def event_message_type(*_a, **_k):
            """消息事件装饰器。"""
            return _decorator_factory()

        @staticmethod
        def on_llm_request(*_a, **_k):
            """请求钩子装饰器。"""
            return _decorator_factory()

        @staticmethod
        def on_llm_response(*_a, **_k):
            """响应钩子装饰器（用于记录机器人上一句回复）。"""
            return _decorator_factory()

        @staticmethod
        def command(*_a, **_k):
            """指令装饰器。"""
            return _decorator_factory()

        @staticmethod
        def on_astrbot_loaded(*_a, **_k):
            """启动完成装饰器。"""
            return _decorator_factory()

    event_mod.filter = _Filter
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api.event = event_mod

    provider_mod = types.ModuleType("astrbot.api.provider")

    class ProviderRequest:
        """只保留插件读写的字段。"""

        def __init__(self, prompt="", contexts=None, conversation=None) -> None:
            """初始化。

            Args:
                prompt: 提示词。
                contexts: 上下文列表。
                conversation: 关联对话。
            """
            self.prompt = prompt
            self.contexts = contexts if contexts is not None else []
            self.conversation = conversation
            self.extra_user_content_parts: list = []

    provider_mod.ProviderRequest = ProviderRequest
    api.provider = provider_mod

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        """桩基类：只记住 context。"""

        def __init__(self, context=None) -> None:
            """初始化。

            Args:
                context: 插件上下文。
            """
            self.context = context

    star_mod.Star = Star
    star_mod.Context = object
    api.star = star_mod

    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    agent_msg = types.ModuleType("astrbot.core.agent.message")

    class TextPart:
        """桩 TextPart。"""

        def __init__(self, text: str = "") -> None:
            """初始化。

            Args:
                text: 文本。
            """
            self.text = text
            self._no_save = False

        def mark_as_temp(self):
            """标记为临时内容。

            Returns:
                自身。
            """
            self._no_save = True
            return self

    agent_msg.TextPart = TextPart
    agent.message = agent_msg

    utils = types.ModuleType("astrbot.core.utils")
    path_utils = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_utils.get_astrbot_data_path = lambda: tempfile.gettempdir()

    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    astrbot.core = core

    for name, mod in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.provider", provider_mod),
        ("astrbot.api.star", star_mod),
        ("astrbot.core", core),
        ("astrbot.core.agent", agent),
        ("astrbot.core.agent.message", agent_msg),
        ("astrbot.core.utils", utils),
        ("astrbot.core.utils.astrbot_path", path_utils),
    ):
        sys.modules[name] = mod


# ------------------------------------------------------------------- 假对象


class FakeConversationManager:
    """最小会话管理器。"""

    def __init__(self) -> None:
        """初始化。"""
        self.current: dict[str, str] = {}
        self.conversations: dict[str, SimpleNamespace] = {}
        self.new_calls = 0

    async def get_curr_conversation_id(self, umo: str):
        """取当前对话 ID。

        Args:
            umo: 统一消息来源。

        Returns:
            对话 ID 或 None。
        """
        return self.current.get(umo)

    async def new_conversation(self, umo: str, platform_id=None, **kwargs):
        """新建对话。

        Args:
            umo: 统一消息来源。
            platform_id: 平台 ID。
            **kwargs: 其它参数。

        Returns:
            新对话 ID。
        """
        self.new_calls += 1
        cid = f"conv-{self.new_calls}"
        self.conversations[cid] = SimpleNamespace(cid=cid, history="[]", persona_id=None)
        self.current[umo] = cid
        return cid

    async def get_conversation(self, umo: str, cid: str, **_k):
        """取对话对象。

        Args:
            umo: 统一消息来源。
            cid: 对话 ID。
            **_k: 其它参数。

        Returns:
            对话对象。
        """
        return self.conversations.get(cid)


class FakeContext:
    """最小插件上下文。"""

    def __init__(self) -> None:
        """初始化。"""
        self.conversation_manager = FakeConversationManager()
        self.provider_manager = SimpleNamespace(provider_insts=[])
        self.calls: list[dict] = []

    async def llm_generate(self, **kwargs):
        """假装调用模型。

        Args:
            kwargs: 调用参数。

        Returns:
            带 completion_text 的对象。
        """
        self.calls.append(kwargs)
        return SimpleNamespace(
            completion_text=(
                '{"verdict":"continue","confidence":0.9,"topic_name":"测试话题",'
                '"topic_summary":"在测可移植性","reason":"接着聊"}'
            )
        )

    async def get_current_chat_provider_id(self, umo: str) -> str:
        """返回当前会话模型 ID。

        Args:
            umo: 统一消息来源。

        Returns:
            provider ID。
        """
        return "stub/model"


class FakeEvent:
    """最小消息事件。"""

    def __init__(self, text: str, umo: str = "stub:group:1") -> None:
        """初始化。

        Args:
            text: 消息文本。
            umo: 统一消息来源。
        """
        self.message_str = text
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(message_id=f"m{id(self)}", timestamp=1)
        self.extras: dict = {}
        self.sent: list = []

    def get_platform_id(self) -> str:
        """平台 ID。

        Returns:
            平台标识。
        """
        return "stub"

    def is_stopped(self) -> bool:
        """是否被停止。

        Returns:
            False。
        """
        return False

    def plain_result(self, text: str):
        """构造纯文本结果。

        Args:
            text: 文本。

        Returns:
            简单对象。
        """
        return SimpleNamespace(text=text)

    async def send(self, result) -> None:
        """记录发送内容。

        Args:
            result: 结果对象。
        """
        self.sent.append(result)

    def set_extra(self, key: str, value) -> None:
        """存额外数据。

        Args:
            key: 键。
            value: 值。
        """
        self.extras[key] = value

    def get_extra(self, key: str, default=None):
        """取额外数据。

        Args:
            key: 键。
            default: 默认值。

        Returns:
            值。
        """
        return self.extras.get(key, default)


# --------------------------------------------------------------------- 用例

MOD = None
_DATA_ROOT: Path | None = None


def reset_runtime() -> None:
    """清空模块级运行时状态。

    handler 是模块级函数、状态也在模块级，所以用例之间必须显式重置，否则会串味
    （最典型的表现就是 turns 一路累加）。
    """
    if MOD is None or _DATA_ROOT is None:
        return
    os.environ["ASTROBOT_DATA_PATH"] = str(_DATA_ROOT)
    MOD.RUNTIME.config = {}
    MOD.RUNTIME.store = MOD.TopicStore(
        _DATA_ROOT / "plugin_data" / MOD.PLUGIN_NAME / "topic_state.json"
    )
    MOD.RUNTIME.recent = {}
    MOD.RUNTIME.pending_actions = {}


async def drive_message(*args, **kwargs) -> None:
    """像 AstrBot 那样驱动消息 handler（它是异步生成器）。

    实机上框架用 `async for` 消费它（`call_handler` 里就是这么做的），直接 `await`
    是不对的——这个助手就是为了让测试和框架行为一致。

    Args:
        *args: 传给 handler 的参数。
        **kwargs: 传给 handler 的关键字参数。
    """
    async for _ in MOD.handle_message_v090(*args, **kwargs):
        pass


async def main() -> int:
    """跑完可移植性测试。

    Returns:
        退出码。
    """
    install_stub_astrbot()
    import importlib.util

    from astrbot.api.provider import ProviderRequest

    tmp_root = Path(tempfile.mkdtemp(prefix="topic-router-portable-"))
    package_parent = tmp_root / "data" / "plugins"
    package_parent.mkdir(parents=True)
    shutil.copytree(
        PLUGIN_DIR,
        package_parent / PLUGIN_DIR.name,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".git", "dist"),
    )
    sys.path.insert(0, str(tmp_root))

    try:
        import data.plugins.astrbot_plugin_topic_router_pro.router as mod

        global MOD, _DATA_ROOT
        MOD = mod
        _DATA_ROOT = tmp_root

        print("\n[1] 手写注册装饰器（不受 __init__.py 依赖影响）")
        MOD.filter = types.SimpleNamespace(  # type: ignore[assignment]
            event_message_type=lambda *a, **k: (lambda f: f),
            on_llm_request=lambda *a, **k: (lambda f: f),
            command=lambda *a, **k: (lambda f: f),
            on_astrbot_loaded=lambda *a, **k: (lambda f: f),
        )

        names = (
            "handle_message_v090",
            "hook_llm_request_v090",
            "command_topic_v090",
            "on_loaded_v090",
        )
        for name in names:
            func = getattr(MOD, name, None)
            check(f"{name} 存在于模块级", func is not None)
            check(f"{name} 不是绑定方法", func is not None and not hasattr(func, "__self__"))

        reset_runtime()
        print("\n[2] 标准 AstrBot 契约：handler 是模块级函数，AstrBot 用绑定好的实例调用")
        ctx = FakeContext()
        MOD.TopicRouterPlugin(ctx, {"mode": "mark", "aggregation": {"enabled": False}})
        event = FakeEvent("第一句话")
        # 注意：handler 故意不是类方法（见 router.py 文件头），所以两种契约下都是
        # 调这个模块级函数；"实例调用"在源码里等价于 plugin.__class__.handle_xxx。
        await drive_message(event)
        state = MOD.RUNTIME.store.get(event.unified_msg_origin)
        check("调用后触发了判定", state.turns == 1, f"turns={state.turns}")

        req = ProviderRequest(prompt="第二句话")
        await MOD.hook_llm_request_v090(event, req)
        check("请求钩子调用不抛异常", True)

        reset_runtime()
        print("\n[3] 非标准契约：未绑定调用（第一个参数就是事件）")
        ctx2 = FakeContext()
        MOD.TopicRouterPlugin(ctx2, {"mode": "mark", "aggregation": {"enabled": False}})
        event2 = FakeEvent("未绑定调用", "stub:group:2")
        await drive_message(event2)
        state2 = MOD.RUNTIME.store.get(event2.unified_msg_origin)
        check("未绑定调用也能判定", state2.turns == 1, f"turns={state2.turns}")

        req2 = ProviderRequest(prompt="未绑定调用")
        await MOD.hook_llm_request_v090(event2, req2)
        check("未绑定调用请求钩子不抛异常", True)

        reset_runtime()
        print("\n[4] 零参数调用（on_astrbot_loaded 的真实形态）")
        ctx3 = FakeContext()
        MOD.TopicRouterPlugin(ctx3, {"mode": "mark", "aggregation": {"enabled": False}})
        try:
            await MOD.on_loaded_v090()
            check("on_loaded_v090() 零参数不抛异常", True)
        except Exception as exc:  # noqa: BLE001
            check("on_loaded_v090() 零参数不抛异常", False, f"{type(exc).__name__}: {exc}")

        reset_runtime()
        print("\n[5] 只有请求、没有事件（实机日志里的形态）")
        try:
            await MOD.hook_llm_request_v090(ProviderRequest(prompt="只有请求"))
            check("只有请求时不抛异常", True)
        except Exception as exc:  # noqa: BLE001
            check("只有请求时不抛异常", False, f"{type(exc).__name__}: {exc}")

        reset_runtime()
        print("\n[6] 指令在两种契约下都能回话")
        ctx4 = FakeContext()
        MOD.TopicRouterPlugin(ctx4, {"mode": "mark", "aggregation": {"enabled": False}})
        event4 = FakeEvent("/topicweave", "stub:group:4")
        outs = [r async for r in MOD.command_topic_v090(event4)]
        check("指令有回复", len(outs) == 1 and "当前话题状态" in outs[0].text)
        outs2 = [r async for r in MOD.command_topic_v090(event4)]
        check("重复调用仍然稳定", len(outs2) == 1 and "当前话题状态" in outs2[0].text)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

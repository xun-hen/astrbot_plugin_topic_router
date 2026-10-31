"""离线测试：不启动 AstrBot，用假 context/event 跑通判定与动作层。

跑法（需要用 AstrBot 自带的 Python 运行时，才能 import 到真实的 astrbot 模块）：

    # Windows（AstrBot 桌面版）
    & "$env:LOCALAPPDATA\\AstrBot\\backend\\python\\python.exe" tests/run_tests.py

    # Linux / macOS（用装了 astrbot 的解释器；桌面版在 ~/.astrbot 下）
    python3 tests/run_tests.py

不想装 AstrBot 也能验证核心逻辑与两种 handler 契约：
`python3 tests/test_portability.py`（用桩模块，任意 Python 3.10+ 可跑）。

它做四件事：
1. 按 AstrBot 真实的加载方式（`data.plugins.<目录名>.main`）导入插件，验证相对导入、
   `metadata.yaml`、`_conf_schema.json` 都是 AstrBot 认的格式；
2. 用真实 `ProviderRequest` / `TextPart` 对象跑 mark / hint / auto 三种模式；
3. 覆盖两个入口的组合：消息事件（主判定）与请求钩子（可选的请求改写）、
   同一条消息重复到达时的去重；
4. 覆盖状态作废、每小时上限、置信度门槛、模型异常/超时、`/topicweave` 指令等边界。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

# 每个用例换一个数据目录，避免状态文件串味（第一版就栽在这里）
_DATA_ROOT = Path(tempfile.mkdtemp(prefix="topic-router-data-"))
os.environ["ASTROBOT_DATA_PATH"] = str(_DATA_ROOT)

FAILED: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。

    Args:
        name: 用例名。
        condition: 是否通过。
        detail: 失败时的补充信息。
    """
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [ok]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def fresh_data_root(label: str) -> Path:
    """给一个用例准备独立的数据目录。

    Args:
        label: 用例名，用于目录命名。

    Returns:
        新的数据目录。
    """
    path = _DATA_ROOT / label / f"run-{PASSED}-{len(FAILED)}"
    path.mkdir(parents=True, exist_ok=True)
    os.environ["ASTROBOT_DATA_PATH"] = str(path)
    reset_runtime()
    return path


def install_plugin_as_package(tmp_root: Path) -> None:
    """把插件按 AstrBot 的目录约定挂到临时目录里，便于用真实包路径导入。

    Args:
        tmp_root: 临时根目录（充当 AstrBot 的 data 目录）。
    """
    target = tmp_root / "data" / "plugins" / PLUGIN_DIR.name
    shutil.copytree(
        PLUGIN_DIR,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".git", "dist"),
    )
    sys.path.insert(0, str(tmp_root))


# --------------------------------------------------------------------------- 假对象


class FakeConversationManager:
    """只实现插件用到的那几个方法。"""

    def __init__(self) -> None:
        """初始化。"""
        self.conversations: dict[str, SimpleNamespace] = {}
        self.current: dict[str, str] = {}
        self.new_calls: list[dict] = []

    async def get_curr_conversation_id(self, umo: str) -> str | None:
        """取当前对话 ID。

        Args:
            umo: 统一消息来源。

        Returns:
            对话 ID。
        """
        return self.current.get(umo)

    async def new_conversation(
        self,
        umo: str,
        platform_id: str | None = None,
        content=None,
        title=None,
        persona_id=None,
    ) -> str:
        """新建对话。

        Args:
            umo: 统一消息来源。
            platform_id: 平台 ID。
            content: 初始内容。
            title: 标题。
            persona_id: 人格 ID。

        Returns:
            新对话 ID。
        """
        cid = f"conv-{len(self.conversations) + 1:04d}"
        self.conversations[cid] = SimpleNamespace(
            cid=cid, history="[]", persona_id=persona_id, title=title
        )
        self.current[umo] = cid
        self.new_calls.append(
            {
                "umo": umo,
                "platform_id": platform_id,
                "title": title,
                "persona_id": persona_id,
            }
        )
        return cid

    async def get_conversation(
        self, umo: str, cid: str, create_if_not_exists: bool = False
    ):
        """取对话对象。

        Args:
            umo: 统一消息来源。
            cid: 对话 ID。
            create_if_not_exists: 不存在时是否创建。

        Returns:
            对话对象或 None。
        """
        return self.conversations.get(cid)


class FakeProvider:
    """假的 LLM provider。"""

    def __init__(self, provider_id: str) -> None:
        """初始化。

        Args:
            provider_id: provider ID。
        """
        self.provider_config = {"id": provider_id}


class FakeProviderManager:
    """假的 provider 管理器。"""

    def __init__(self) -> None:
        """初始化。"""
        self.provider_insts = [
            FakeProvider("deepseek/deepseek-v4-flash"),
            FakeProvider("nvidia/deepseek-ai/deepseek-v4-pro"),
        ]
        # 只有注册在 inst_map 里的 ID 才算"真实存在"，与 AstrBot 的行为一致
        self.inst_map = {p.provider_config["id"]: p for p in self.provider_insts}

    async def get_provider_by_id(self, provider_id: str):
        """按 ID 查 provider（复刻 AstrBot 的接口）。

        Args:
            provider_id: 待查 ID。

        Returns:
            provider 实例或 None。
        """
        return self.inst_map.get(provider_id)


class FakeContext:
    """假的插件上下文。"""

    def __init__(self) -> None:
        """初始化。"""
        self.conversation_manager = FakeConversationManager()
        self.provider_manager = FakeProviderManager()
        self.calls: list[dict] = []
        self.answer = (
            '{"verdict":"new_topic","confidence":0.9,"topic_name":"电路板",'
            '"topic_summary":"在聊电子吧唧的板子","reason":"换了件事"}'
        )
        self.fail: Exception | None = None

    async def llm_generate(self, **kwargs):
        """假装调用模型。

        Args:
            kwargs: 调用参数。

        Returns:
            带 completion_text 的对象。

        Raises:
            Exception: 由 self.fail 指定时抛出。
        """
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(completion_text=self.answer)

    async def get_current_chat_provider_id(self, umo: str) -> str:
        """返回当前会话模型 ID。

        Args:
            umo: 统一消息来源。

        Returns:
            provider ID。
        """
        return "deepseek/deepseek-v4-flash"


class FakeEvent:
    """假的消息事件。"""

    _seq = 0

    def __init__(
        self,
        text: str,
        umo: str = "aiocqhttp:group:123",
        message_id: str | None = None,
        timestamp: int | None = None,
        sender: str = "u1",
    ) -> None:
        """初始化。

        Args:
            text: 消息文本。
            umo: 统一消息来源。
            message_id: 平台消息 ID；不传时自动生成一个递增 ID。
            timestamp: 消息时间戳；不传时用当前时间。
            sender: 发送者 ID（聚合按「会话 + 发送者」分桶）。
        """
        FakeEvent._seq += 1
        self.message_str = text
        self.unified_msg_origin = umo
        self.sent: list[str] = []
        self.extras: dict = {}
        self._stopped = False
        self._sender = sender
        # 真实的 AstrMessageEvent 带 message_obj（含 message_id / timestamp），
        # 去重逻辑依赖它；这里照实模拟。`message=[]` 让解析层退回 message_str。
        self.message_obj = SimpleNamespace(
            message_id=message_id if message_id is not None else f"m{FakeEvent._seq}",
            timestamp=timestamp if timestamp is not None else FakeEvent._seq,
            message=[],
            raw_message=None,
        )

    def stop_event(self) -> None:
        """静默事件（聚合插件对非 Owner 消息的公开 API）。"""
        self._stopped = True

    def is_stopped(self) -> bool:
        """事件是否已被静默。

        Returns:
            True 表示已停止传播。
        """
        return self._stopped

    def get_sender_id(self) -> str:
        """发送者 ID（聚合分桶用）。

        Returns:
            发送者标识。
        """
        return self._sender

    def get_self_id(self) -> str:
        """机器人自己的 ID（剥除 @ 用）。

        Returns:
            固定值。
        """
        return "bot"

    def is_private_chat(self) -> bool:
        """是否私聊（聚合的渠道统计用）。

        Returns:
            True 表示私聊。
        """
        return ":FriendMessage:" in self.unified_msg_origin or "private" in self.unified_msg_origin

    def get_platform_id(self) -> str:
        """平台 ID。

        Returns:
            平台标识。
        """
        return "aiocqhttp"

    def plain_result(self, text: str):
        """构造纯文本结果。

        Args:
            text: 文本。

        Returns:
            简单对象。
        """
        return {"text": text}

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


# --------------------------------------------------------------------------- 用例


def make_plugin(module, context: FakeContext, config: dict):
    """构造插件实例。

    v0.9.0 起插件默认开启内建消息聚合；本文件绝大多数用例验证的是"逐条判定"
    的语义（去重、模型调用、模式动作），因此默认显式关闭聚合，保持用例口径不变。
    聚合整合本身的用例在 `[19]`，会显式传开启的配置。

    Args:
        module: 插件模块。
        context: 假上下文。
        config: 配置。

    Returns:
        插件实例。
    """
    merged = dict(config)
    merged.setdefault("aggregation", {"enabled": False})
    return module.TopicRouterPlugin(context, merged)


MODULE = None

# 在模块级导入一次：多个用例都要用它构造请求对象，局部重复导入会让新用例漏掉
from astrbot.api.provider import ProviderRequest  # noqa: E402


def state_store():
    """取当前模块级话题状态存储。

    新架构里 store 挂在模块级 RUNTIME 上（不再挂在插件实例上，因为实机派发 handler
    时 self 可能不是插件实例），测试也要跟着改。

    Returns:
        TopicStore 实例。
    """
    return MODULE.RUNTIME.store


def reset_runtime() -> None:
    """把模块级运行时状态清空。

    新架构的状态放在模块级单例 RUNTIME 里（不再挂在实例上，因为实机派发 handler
    时 self 可能不是插件实例），所以每个用例必须显式重置，否则会串味。
    """
    if MODULE is None:
        return
    module = MODULE
    data_dir = (
        Path(os.environ["ASTROBOT_DATA_PATH"]) / "plugin_data" / module.PLUGIN_NAME
    )
    module.RUNTIME.config = {}
    module.RUNTIME.store = module.TopicStore(data_dir / "topic_state.json")
    module.RUNTIME.recent = {}
    module.RUNTIME.pending_actions = {}
    module.RUNTIME.aggregation = None
    module.RUNTIME.aggregation_error = ""


async def run_detect(plugin, event, req=None):
    """按实机的调用顺序驱动一次判定。

    实机上有两个入口：

    * 消息事件（`on_message`）—— 主判定入口，调用约定确定，永远只传 event；
    * LLM 请求钩子（`on_llm_request`）—— 只负责在能拿到 `ProviderRequest` 时
      执行 `hint` / `auto` 需要的请求改写。

    所以这里先跑消息事件，再（可选地）跑钩子，模拟真实顺序。

    Args:
        plugin: 插件实例。
        event: 消息事件。
        req: `ProviderRequest`，为 None 时只走消息事件路径。
    """
    async for _ in MODULE.handle_message_v090(event):
        pass
    if req is not None:
        await MODULE.hook_llm_request_v090(event, req)


async def test_metadata(module, tmp_root: Path) -> None:
    """校验 metadata 与配置 schema。

    Args:
        module: 插件模块。
        tmp_root: 临时根目录。
    """
    print("\n[1] 加载与元数据")
    check("按 data.plugins.<name>.main 导入成功", module is not None)

    import yaml

    meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    check(
        "metadata.name 与目录名一致",
        meta.get("name") == PLUGIN_DIR.name,
        f"got {meta.get('name')}",
    )
    check("metadata.version 存在", bool(meta.get("version")))
    check("插件导出 PLUGIN_VERSION", bool(getattr(module, "PLUGIN_VERSION", "")))

    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    check("schema 默认 mode 是 mark", schema["mode"]["default"] == "mark")
    for key, item in schema.items():
        if not isinstance(item, dict) or "description" not in item or "type" not in item:
            check(f"schema.{key} 字段合法", False, str(item)[:80])
            continue
        # 对象型分组（如 aggregation）没有顶层 default，默认值在 items 里
        ok = "default" in item or (
            item.get("type") == "object" and isinstance(item.get("items"), dict)
        )
        check(f"schema.{key} 字段合法", ok, str(item)[:80])
    agg = schema.get("aggregation", {})
    check("schema 含 aggregation 分组", agg.get("type") == "object")
    check(
        "聚合默认开启（整合后的默认形态）",
        agg.get("items", {}).get("enabled", {}).get("default") is True,
    )
    check(
        "聚合配置项与独立插件对齐（至少 25 项）",
        len(agg.get("items", {})) >= 25,
        str(len(agg.get("items", {}))),
    )
    check(
        "mode 选项包含四档",
        schema["mode"]["options"] == ["off", "mark", "hint", "auto"],
        str(schema["mode"].get("options")),
    )


async def test_heuristics(module) -> None:
    """校验零成本启发式层。

    Args:
        module: 插件模块。
    """
    print("\n[2] 启发式判定")
    detection = sys.modules["data.plugins.astrbot_plugin_topic_router_pro.detection"]
    state = SimpleNamespace(
        label="电子吧唧的电路板设计",
        summary="在聊怎么画板子、选元件",
        last_user_text="那这个电容选多大的？",
        last_ts=1000.0,
    )

    first = detection.heuristic(
        "你好", SimpleNamespace(label="", summary="", last_user_text="", last_ts=0.0)
    )
    check("首个话题直接建档", first.verdict == detection.VERDICT_NEW_TOPIC and first.needs_llm)

    short = detection.heuristic("嗯", state, now=1001.0)
    check("短消息不调用模型", short.verdict == detection.VERDICT_CONTINUE and not short.needs_llm)

    same = detection.heuristic("那这个电容选多大的？还有电阻呢", state, now=1002.0)
    check(
        "高度重叠判延续",
        same.verdict == detection.VERDICT_CONTINUE and not same.needs_llm,
        same.reason,
    )

    other = detection.heuristic(
        "帮我写个 Python 脚本，把这一批 csv 文件批量转成 excel 表格", state, now=1002.0
    )
    check("几乎不重叠判新话题", other.verdict == detection.VERDICT_NEW_TOPIC, other.reason)

    marked = detection.heuristic("换个话题，帮我看看 NAS 的硬盘", state, now=1002.0)
    check("显式换话题词直接定性", marked.verdict == detection.VERDICT_NEW_TOPIC and not marked.needs_llm)

    # 长时间未回复（v0.8.6 改）：不再凭时间硬判新话题，而是**交给模型判是否延续**。
    # 时间只是弱证据——"隔一小时回来接着做同一件事"很常见，而把延续判成新话题
    # 正是误判代价最大的那一类。
    cold = detection.heuristic("今天天气怎么样", state, now=1000.0 + 3600, time_gap_minutes=45)
    check(
        "长时间未回复 → 交模型判，而不是凭时间硬判新话题",
        cold.needs_llm and cold.verdict == detection.VERDICT_CONTINUE,
        cold.reason,
    )
    check("长时间未回复时把间隔写进 reason（模型需要这个线索）",
          "60" in cold.reason or "过" in cold.reason, cold.reason)

    # 隔很久但字面高度重叠（接着聊同一件事）→ 不必花模型调用。
    # 用"与上一条消息完全相同"的文本来保证相似度确定性地达到 1.0，
    # 不去假设 state 里到底存了哪句话。
    cold_same = detection.heuristic(
        state.last_user_text,
        state,
        now=1000.0 + 3600,
        time_gap_minutes=45,
    )
    check(
        "隔很久但高度重叠 → 直接判延续、不花模型调用",
        (not cold_same.needs_llm) and cold_same.verdict == detection.VERDICT_CONTINUE,
        cold_same.reason,
    )

    # 真实数据带来的修正：短句 + 字面零重叠，不再单凭字面判新话题
    # （实测样本里这类有 30/65 轮，如「我没有代理啊」「等等吧还没做好」，几乎都是延续）
    followup = detection.heuristic("我没有代理啊", state, now=1003.0)
    check(
        "短追问句交给模型判语义（不凭字面判新话题）",
        followup.needs_llm and followup.verdict == detection.VERDICT_CONTINUE,
        followup.reason,
    )
    check(
        "短追问句的相似度确实接近 0（说明字面信号无信息量）",
        followup.similarity <= 0.12,
        str(followup.similarity),
    )

    # 短句 + 纯指代：明确延续，不必花模型调用
    pronoun = detection.heuristic("那它呢", state, now=1003.0)
    check(
        "短句含指代词直接判延续且不调模型",
        pronoun.verdict == detection.VERDICT_CONTINUE and not pronoun.needs_llm,
        pronoun.reason,
    )

    # 长句 + 零重叠：仍然由启发式直接判新话题（省一次模型调用）
    long_new = detection.heuristic(
        "顺便问一下，飞牛 NAS 上怎么做定时备份到另一台机器，有推荐方案吗", state, now=1003.0
    )
    check(
        "长句零重叠仍直接判新话题",
        long_new.verdict == detection.VERDICT_NEW_TOPIC and not long_new.needs_llm,
        long_new.reason,
    )

    vague = detection.heuristic(
        "那块板子上原来那个元件现在还能不能继续用啊", state, now=1002.0, sim_continue=0.99
    )
    check(
        "模糊区间交给模型",
        vague.needs_llm and vague.verdict == detection.VERDICT_CONTINUE,
        vague.reason,
    )


async def test_user_prompt_contains_reply(module) -> None:
    """判定提示词里必须带上"机器人上一句回复"。

    实测结论：很多换话题发生在机器人回答之后，模型看不到上一轮答了什么就没有承接关系的
    锚点。这条用例把"回复会进提示词"钉死，避免以后重构时被悄悄去掉。

    Args:
        module: 插件模块。
    """
    print("\n[14] 判定提示词内容")
    detection = sys.modules["data.plugins.astrbot_plugin_topic_router_pro.detection"]
    state = SimpleNamespace(
        label="飞牛 NAS 安装",
        summary="在装飞牛系统",
        last_user_text="格式化好慢啊",
        last_assistant_text="1T 机械盘那正常，慢慢等着吧",
        last_ts=0.0,
    )

    with_reply = detection.build_user_prompt(
        "那我现在咋退出去",
        state,
        ["格式化好慢啊"],
        3,
        last_assistant="按 Ctrl+Alt+Del 重启就行",
    )
    check("提示词带上了机器人上一句回复", "按 Ctrl+Alt+Del 重启就行" in with_reply)
    check("提示词仍然带当前话题", "飞牛 NAS 安装" in with_reply)
    check("提示词带上了用户最新消息", "那我现在咋退出去" in with_reply)

    from_state = detection.build_user_prompt("那我现在咋退出去", state, [], 3)
    check(
        "不显式传回复时，回退到 state 里的 last_assistant_text",
        "1T 机械盘那正常" in from_state,
    )

    no_reply = detection.build_user_prompt(
        "那我现在咋退出去",
        SimpleNamespace(label="某话题", summary="", last_user_text="x", last_ts=0.0),
        [],
        3,
    )
    check("没有回复时不出现空行标签", "机器人上一句回复" not in no_reply)


async def test_provider_id_fallback(module) -> None:
    """配错 provider_id 时必须回退，而不是静默失效。

    实机教训（2026-10-11 日志）：配置里写的是 `deepseek/deepseek-v4-flash`，但用户环境里
    真实存在的是 `deepseek/deepseek-flash`。`llm_generate` 抛 ProviderNotFoundError 后被
    兜底逻辑吞掉，判定模型那层**静默失效**，只剩启发式在跑 —— 从日志上几乎看不出来。

    Args:
        module: 插件模块。
    """
    print("\n[15] provider_id 配错时的回退")
    detection = sys.modules["data.plugins.astrbot_plugin_topic_router_pro.detection"]

    # 配了一个不存在的 ID → 应该回退到当前会话模型
    fresh_data_root("provider-fallback")
    ctx = FakeContext()
    make_plugin(module, ctx, {"mode": "mark", "provider_id": "deepseek/deepseek-v4-flash-NOT-EXIST"})
    event = FakeEvent("帮我看下硬盘", "aiocqhttp:group:pf")
    pid, note = await MODULE._resolve_provider(MODULE._ctx(), event)
    check(
        "配错 provider_id 时回退到当前会话模型",
        pid == "deepseek/deepseek-v4-flash" and note == "跟随当前会话",
        f"{pid!r} / {note!r}",
    )

    # 配了存在的 ID → 就用它
    fresh_data_root("provider-ok")
    ctx2 = FakeContext()
    make_plugin(module, ctx2, {"mode": "mark", "provider_id": "nvidia/deepseek-ai/deepseek-v4-pro"})
    event2 = FakeEvent("帮我看下硬盘", "aiocqhttp:group:po")
    pid2, note2 = await MODULE._resolve_provider(MODULE._ctx(), event2)
    check(
        "provider_id 存在时按配置使用",
        pid2 == "nvidia/deepseek-ai/deepseek-v4-pro" and note2 == "配置指定",
        f"{pid2!r} / {note2!r}",
    )

    # 留空 → 跟随当前会话
    fresh_data_root("provider-empty")
    ctx3 = FakeContext()
    make_plugin(module, ctx3, {"mode": "mark", "provider_id": ""})
    event3 = FakeEvent("帮我看下硬盘", "aiocqhttp:group:pe")
    pid3, note3 = await MODULE._resolve_provider(MODULE._ctx(), event3)
    check(
        "provider_id 留空时跟随当前会话（新默认值）",
        pid3 == "deepseek/deepseek-v4-flash" and note3 == "跟随当前会话",
        f"{pid3!r} / {note3!r}",
    )

    # 配错时最终仍然能完成一次模型判定（回退后照常工作）
    fresh_data_root("provider-fallback-run")
    ctx4 = FakeContext()
    make_plugin(module, ctx4, {"mode": "mark", "provider_id": "不存在的模型"})
    event4 = FakeEvent("帮我看下硬盘坏了没", "aiocqhttp:group:pf2")
    async for _ in MODULE.handle_message_v090(event4):
        pass
    check(
        "回退之后模型判定依然执行（不是静默失效）",
        any(call.get("chat_provider_id") == "deepseek/deepseek-v4-flash" for call in ctx4.calls),
        str([c.get("chat_provider_id") for c in ctx4.calls]),
    )
    del detection


async def test_json_parsing(module) -> None:
    """校验模型输出解析的健壮性。

    Args:
        module: 插件模块。
    """
    print("\n[3] 模型输出解析")
    detection = sys.modules["data.plugins.astrbot_plugin_topic_router_pro.detection"]
    heur = detection.HeuristicResult(
        verdict=detection.VERDICT_CONTINUE,
        confidence=0.5,
        similarity=0.3,
        needs_llm=True,
        reason="x",
    )

    plain = detection.parse_verdict(
        '{"verdict":"new_topic","confidence":0.88,"topic_name":"NAS"}', heur
    )
    check("纯 JSON 可解析", plain is not None and plain.is_new_topic and plain.topic_label == "NAS")

    fenced = detection.parse_verdict(
        '好的，判定如下：\n```json\n{"verdict":"continue","confidence":0.7}\n```\n以上。', heur
    )
    check("被 markdown 包裹也能解析", fenced is not None and not fenced.is_new_topic)
    check("缺少 topic_name 不报错", fenced is not None and fenced.topic_label == "")

    bad = detection.parse_verdict("我觉得应该算延续吧", heur)
    check("没有 JSON 返回 None", bad is None)

    weird = detection.parse_verdict('{"verdict":"NEW_TOPIC","confidence":"0.9"}', heur)
    check("大写与字符串置信度可容错", weird is not None and weird.is_new_topic)

    out_of_range = detection.parse_verdict('{"verdict":"new_topic","confidence":9}', heur)
    check("置信度被夹到 0~1", out_of_range is not None and out_of_range.confidence == 1.0)

    merged = detection.combine(heur, plain)
    check("模型结果覆盖启发式", merged.is_new_topic and merged.source == "llm")

    # 不对称原则：启发式说延续时，模型必须很确定才能翻成新话题
    unsure = detection.parse_verdict('{"verdict":"new_topic","confidence":0.66}', heur)
    check("模型不够确定时能解析", unsure is not None and unsure.is_new_topic)
    check("启发式延续 + 模型半信半疑 = 延续", not detection.combine(heur, unsure).is_new_topic)

    sure = detection.parse_verdict('{"verdict":"new_topic","confidence":0.95}', heur)
    strong = detection.combine(heur, sure)
    check("模型非常确定时可以翻成新话题", strong.is_new_topic and strong.confidence <= 0.8)

    weak_new = detection.HeuristicResult(
        verdict=detection.VERDICT_NEW_TOPIC,
        confidence=0.2,
        similarity=0.1,
        needs_llm=True,
        reason="低重叠但没把握",
    )
    keep = detection.combine(weak_new, unsure)
    check(
        "两个信号一致时取较低置信度、能被阈值门挡住",
        keep.is_new_topic and keep.confidence < 0.6,
        f"{keep.verdict}/{keep.confidence}",
    )

    firm_new = detection.HeuristicResult(
        verdict=detection.VERDICT_NEW_TOPIC,
        confidence=0.7,
        similarity=0.05,
        needs_llm=True,
        reason="低重叠",
    )
    check("两个信号一致且模型有把握时判新话题", detection.combine(firm_new, plain).is_new_topic)


async def test_mark_mode(module) -> None:
    """mark 模式：只记录，绝不改请求。

    Args:
        module: 插件模块。
    """
    print("\n[4] mark 模式（默认）")
    fresh_data_root("mark")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark"})
    event = FakeEvent("帮我写个爬虫抓豆瓣")
    from astrbot.api.provider import ProviderRequest

    req = ProviderRequest(
        prompt=event.message_str, contexts=[{"role": "user", "content": "在聊电路板"}]
    )

    await run_detect(plugin, event, req)

    check("没有新建对话", context.conversation_manager.new_calls == [])
    check("contexts 未被改动", len(req.contexts) == 1)
    check("没有注入额外内容块", len(req.extra_user_content_parts) == 0)
    check("没有向用户发言", event.sent == [])
    check("状态里记下了话题名称", state_store().get(event.unified_msg_origin).label == "电路板")
    check("调用了判定模型", len(context.calls) == 1, str(context.calls))
    prompt = context.calls[0]["prompt"] if context.calls else ""
    check("提示词带上了最新消息", "豆瓣" in prompt, prompt[:120])
    check("判定了但没动上下文", state_store().get(event.unified_msg_origin).switches == 1)
    check(
        "同一条消息不会被判定两次（两个入口去重）",
        state_store().get(event.unified_msg_origin).turns == 1,
        f"turns={state_store().get(event.unified_msg_origin).turns}",
    )


async def test_auto_mode(module) -> None:
    """auto 模式：高置信度时切换对话。

    Args:
        module: 插件模块。
    """
    print("\n[5] auto 模式")
    fresh_data_root("auto")
    context = FakeContext()
    plugin = make_plugin(
        module,
        context,
        # new_conversation_notice 的默认值已改为 false（v0.8.5），
        # 这个用例专门验证"通知"这条路径，所以显式开启。
        {"mode": "auto", "auto_threshold": 0.8, "new_conversation_notice": True},
    )
    event = FakeEvent("帮我写个 Python 脚本")
    from astrbot.api.provider import ProviderRequest

    old = await context.conversation_manager.new_conversation(event.unified_msg_origin)
    context.calls.clear()
    calls_before = len(context.conversation_manager.new_calls)
    req = ProviderRequest(
        prompt=event.message_str,
        contexts=[{"role": "user", "content": "电路板怎么画"}],
        conversation=context.conversation_manager.conversations[old],
    )

    await run_detect(plugin, event, req)

    check("新建了一个对话", len(context.conversation_manager.new_calls) == calls_before + 1)
    new_cid = context.conversation_manager.current[event.unified_msg_origin]
    check("请求已挂到新对话", getattr(req.conversation, "cid", "") == new_cid)
    check("上下文被清空", req.contexts == [])
    check("向用户提示了换话题", len(event.sent) == 1, str(event.sent))
    check("旧对话仍在（没删）", old in context.conversation_manager.conversations)

    # 低置信度不切换
    fresh_data_root("auto-low")
    context2 = FakeContext()
    context2.answer = '{"verdict":"new_topic","confidence":0.62,"topic_name":"随便"}'
    plugin2 = make_plugin(module, context2, {"mode": "auto", "auto_threshold": 0.8})
    event2 = FakeEvent("帮我写个爬虫")
    await run_detect(plugin2, event2, ProviderRequest(prompt=event2.message_str))
    check("置信度不够时不切换", context2.conversation_manager.new_calls == [])

    # 阈值下限：低于 min_new_topic_confidence 直接压回延续
    fresh_data_root("auto-floor")
    context3 = FakeContext()
    context3.answer = '{"verdict":"new_topic","confidence":0.4,"topic_name":"随便"}'
    plugin3 = make_plugin(module, context3, {"mode": "auto", "min_new_topic_confidence": 0.6})
    event3 = FakeEvent("帮我写个爬虫")
    await run_detect(plugin3, event3, ProviderRequest(prompt=event3.message_str))
    check("低置信度被压回延续", context3.conversation_manager.new_calls == [])

    context3.answer = '{"verdict":"continue","confidence":0.9,"topic_name":"爬虫"}'
    same = "帮我写个爬虫，要支持代理"
    await run_detect(plugin3, FakeEvent(same), ProviderRequest(prompt=same))
    switches_before = state_store().get(event3.unified_msg_origin).switches
    context3.answer = '{"verdict":"new_topic","confidence":0.4,"topic_name":"随便"}'
    low = "帮我写个爬虫，再加个去重"
    await run_detect(plugin3, FakeEvent(low), ProviderRequest(prompt=low))
    check(
        "被压回的判定不计入新话题数",
        state_store().get(event3.unified_msg_origin).switches == switches_before,
        f"before={switches_before} after={state_store().get(event3.unified_msg_origin).switches}",
    )


async def test_hint_mode(module) -> None:
    """hint 模式：注入本轮临时提示，但不换对话。

    Args:
        module: 插件模块。
    """
    print("\n[6] hint 模式")
    fresh_data_root("hint")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "hint"})
    event = FakeEvent("帮我写个爬虫")
    from astrbot.api.provider import ProviderRequest

    req = ProviderRequest(
        prompt=event.message_str, contexts=[{"role": "user", "content": "旧话题"}]
    )
    await run_detect(plugin, event, req)

    check("注入了 1 个内容块", len(req.extra_user_content_parts) == 1)
    if req.extra_user_content_parts:
        part = req.extra_user_content_parts[0]
        check("注入的是临时内容（不进历史）", getattr(part, "_no_save", False) is True)
        check(
            "提示里点了新话题名",
            "电路板" in getattr(part, "text", ""),
            getattr(part, "text", "")[:80],
        )
    check("没有换对话", context.conversation_manager.new_calls == [])
    check("上下文保持原样", len(req.contexts) == 1)


async def test_request_hook_alone(module) -> None:
    """只有请求钩子被调用时（消息事件没到），判定也应该发生。

    v0.8.0 的架构性修复：实机上装了「消息防抖动」类插件后，消息事件处理器会被
    `event.is_stopped()` 挡在循环外，一次都不执行；而请求钩子不在那个循环里，照常触发。
    所以判定必须能在钩子里独立完成。

    Args:
        module: 插件模块。
    """
    print("\n[7] 只有 on_llm_request 被调用的路径（事件被别的插件静默时）")
    fresh_data_root("hook-only")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "hint"})
    umo = "aiocqhttp:group:hookonly"
    event = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    from astrbot.api.provider import ProviderRequest

    # 提示词按判定器的格式拼，钩子才抠得出用户消息
    prompt = f"当前话题：（还没有话题）\n用户最新消息：{event.message_str}\n请输出判定 JSON。"
    req = ProviderRequest(prompt=prompt)
    await MODULE.hook_llm_request_v090(event, req)

    check(
        "钩子单独调用也完成了判定（不依赖消息事件）",
        state_store().get(umo).turns == 1,
        f"turns={state_store().get(umo).turns}",
    )
    check(
        "同一轮里挂起的 hint 动作被本次钩子就地执行",
        len(req.extra_user_content_parts) == 1,
        str(req.extra_user_content_parts),
    )

    # 事件路先跑同一条消息：钩子不该把它再判一次
    turns_before = state_store().get(umo).turns
    req2 = ProviderRequest(prompt=prompt)
    await MODULE.hook_llm_request_v090(event, req2)
    check(
        "事件路已判过的消息，钩子不重复判定",
        state_store().get(umo).turns == turns_before,
        f"turns={state_store().get(umo).turns}",
    )

    # 消息事件路本身：同一条消息重复到达按消息 ID 去重
    async for _ in MODULE.handle_message_v090(event):
        pass
    check(
        "重复到达的消息被跳过",
        state_store().get(umo).turns == turns_before,
        f"turns={state_store().get(umo).turns}",
    )

    # 用户真的把同一句话又发了一遍（新的消息 id）→ 应该算新的一轮
    same_text_again = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    async for _ in MODULE.handle_message_v090(same_text_again):
        pass
    check(
        "用户重发同样的话算新一轮",
        state_store().get(umo).turns == turns_before + 1,
        f"turns={state_store().get(umo).turns}",
    )
    del plugin


async def test_state_lifecycle(module) -> None:
    """状态：对话作废、写盘、每小时上限、off 开关。

    Args:
        module: 插件模块。
    """
    print("\n[8] 状态生命周期")
    fresh_data_root("lifecycle")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark", "max_checks_per_hour": 2})
    umo = "aiocqhttp:group:999"
    from astrbot.api.provider import ProviderRequest

    await context.conversation_manager.new_conversation(umo)
    for text in ("帮我写个爬虫抓豆瓣", "再来个代理池", "换个话题，聊聊 NAS"):
        await run_detect(plugin, FakeEvent(text, umo), ProviderRequest(prompt=text))
    check("超过每小时上限后不再调模型", len(context.calls) == 2, f"calls={len(context.calls)}")

    state = state_store().get(umo)
    check("状态绑定了当前对话", state.conv_id == context.conversation_manager.current[umo])
    check("消息计入轮数", state.turns >= 3)
    check("三轮后话题名还在", bool(state.label), f"label={state.label!r}")

    # 换对话后旧话题作废（统计与开关要留着）
    switches_before = state_store().get(umo).switches
    context.conversation_manager.current[umo] = "manual-new"
    await run_detect(plugin, FakeEvent("全新的一句话", umo), ProviderRequest(prompt="全新的一句话"))
    check(
        "换对话后旧话题被清掉",
        state_store().get(umo).label != "电路板",
        f"label={state_store().get(umo).label}",
    )
    check("换对话不清空累计统计", state_store().get(umo).switches >= switches_before)

    # /topic off 后完全不动
    state_store().get(umo).disabled = True
    state_store().mark_dirty()
    state_store().save()
    before = len(context.calls)
    await run_detect(plugin, FakeEvent("再聊点别的", umo), ProviderRequest(prompt="再聊点别的"))
    check("off 之后不再调用模型", len(context.calls) == before)

    saved = json.loads(state_store().path.read_text(encoding="utf-8"))
    check("状态文件已写出", "states" in saved and umo in saved["states"])
    check("状态文件有版本号", saved.get("version") == 1)

    from data.plugins.astrbot_plugin_topic_router_pro.state import TopicStore

    reloaded = TopicStore(state_store().path)
    check("重新载入后开关还在", reloaded.get(umo).disabled is True)
    check(
        "重新载入后话题还在",
        bool(reloaded.get(umo).label),
        f"内存 label={state_store().get(umo).label!r} "
        f"磁盘 label={saved['states'][umo].get('label')!r}",
    )


async def test_failure_paths(module) -> None:
    """判定模型异常时不能影响正常回复。

    Args:
        module: 插件模块。
    """
    print("\n[9] 失败路径")
    fresh_data_root("failure")
    from astrbot.api.provider import ProviderRequest

    for name, exc in (
        ("模型抛异常", RuntimeError("boom")),
        ("模型超时", TimeoutError()),
    ):
        fresh_data_root(f"failure-{name.replace(' ', '-')}")
        context = FakeContext()
        context.answer = '{"verdict":"continue","confidence":0.9,"topic_name":"爬虫"}'
        plugin = make_plugin(module, context, {"mode": "auto", "timeout_seconds": 5})
        umo = f"aiocqhttp:group:{name}"
        await run_detect(
            plugin,
            FakeEvent("帮我写个爬虫抓豆瓣", umo),
            ProviderRequest(prompt="帮我写个爬虫抓豆瓣"),
        )
        check(f"{name}前状态已建立", bool(state_store().get(umo).label))

        context.fail = exc
        context.answer = '{"verdict":"new_topic","confidence":0.95,"topic_name":"别的事"}'
        before_calls = len(context.conversation_manager.new_calls)
        event = FakeEvent("帮我写个爬虫抓豆瓣，顺便加个去重", umo)
        req = ProviderRequest(
            prompt=event.message_str, contexts=[{"role": "user", "content": "旧"}]
        )
        await run_detect(plugin, event, req)
        check(f"{name}时不换对话", len(context.conversation_manager.new_calls) == before_calls)
        check(f"{name}时不发消息", event.sent == [])
        check(f"{name}时上下文未被改动", len(req.contexts) == 1)
        check(
            f"{name}时按延续推进状态",
            state_store().get(umo).turns == 2,
            f"turns={state_store().get(umo).turns}",
        )

    fresh_data_root("failure-unparseable")
    context = FakeContext()
    context.answer = '{"verdict":"continue","confidence":0.9,"topic_name":"爬虫"}'
    plugin = make_plugin(module, context, {"mode": "auto"})
    umo = "aiocqhttp:group:unparseable"
    await run_detect(plugin, FakeEvent("帮我写个爬虫抓豆瓣", umo), ProviderRequest(prompt="帮我写个爬虫抓豆瓣"))
    context.answer = "我不知道该怎么判断"
    before_calls = len(context.conversation_manager.new_calls)
    event = FakeEvent("帮我写个爬虫抓豆瓣，再加个日志", umo)
    req = ProviderRequest(prompt=event.message_str, contexts=[{"role": "user", "content": "旧"}])
    await run_detect(plugin, event, req)
    check("输出无法解析时不换对话", len(context.conversation_manager.new_calls) == before_calls)
    check("输出无法解析时不改上下文", len(req.contexts) == 1)


async def test_topic_command(module) -> None:
    """/topic 指令。

    Args:
        module: 插件模块。
    """
    print("\n[10] /topicweave 指令")
    fresh_data_root("command")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark"})
    umo = "aiocqhttp:group:cmd"

    async def collect(message: str) -> list[str]:
        """跑一次指令并收集输出。

        Args:
            message: 指令文本。

        Returns:
            输出文本列表。
        """
        ev = FakeEvent(message, umo)
        out = []
        async for result in MODULE.command_topic_v090(ev):
            out.append(result["text"])
        return out

    status = await collect("/topicweave")
    check("status 有输出", len(status) == 1 and "当前话题状态" in status[0])
    check("status 里带版本号", any(module.PLUGIN_VERSION in line for line in status))

    await collect("/topicweave new 手动话题")
    check("/topicweave new 建了新对话", len(context.conversation_manager.new_calls) == 1)
    check("/topicweave new 记下了话题名", state_store().get(umo).label == "手动话题")

    await collect("/topicweave off")
    check("/topic off 生效", state_store().get(umo).disabled is True)
    await collect("/topicweave on")
    check("/topic on 生效", state_store().get(umo).disabled is False)

    await collect("/topicweave forget")
    check("/topic forget 清空话题", state_store().get(umo).label == "")

    unknown = await collect("/topicweave 胡说八道")
    check("未知子命令给用法提示", "用法" in unknown[0])


async def test_entry_points(module) -> None:
    """两个入口的签名与兼容性。

    Args:
        module: 插件模块。
    """
    print("\n[11] 入口签名与调用方式")
    import inspect

    from astrbot.api.provider import ProviderRequest

    mod = MODULE
    seen = inspect.signature(mod.handle_message_v090)
    seen_params = list(seen.parameters.values())
    seen_required = [
        p.name
        for p in seen_params
        if p.default is inspect.Parameter.empty
        and p.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    check(
        "消息处理器没有任何必填参数（AstrBot 不传 self 也不会报错）",
        seen_required == [],
        str(seen_required),
    )
    check(
        "消息处理器用 *args/**kwargs 接住事件",
        any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in seen_params)
        and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in seen_params),
        str([(p.name, p.kind.name) for p in seen_params]),
    )

    hook = inspect.signature(mod.hook_llm_request_v090)
    params = list(hook.parameters.values())
    required = [
        p.name
        for p in params
        if p.default is inspect.Parameter.empty
        and p.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    check(
        "请求钩子没有任何必填参数（少传/多传都不会报错）",
        required == [],
        str(required),
    )
    check(
        "请求钩子用 *args/**kwargs 兜住任何派发方式",
        any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
        and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params),
        str([(p.name, p.kind.name) for p in params]),
    )
    # 这条是给未来改代码的人（包括我自己）的警告：AstrBot 按"模块+函数名"复用
    # handler 元数据，方法名一旦叫回 on_llm_request，就会命中旧条目、新代码永不生效。
    hook_name = MODULE.hook_llm_request_v090.__name__
    check("请求钩子的方法名不是 on_llm_request", hook_name != "on_llm_request", hook_name)
    check(
        "请求钩子的方法名带版本号",
        any(ch.isdigit() for ch in hook_name),
        hook_name,
    )

    umo = "aiocqhttp:group:entries"

    # 只传 event 调钩子（实机报错的那条路径）
    fresh_data_root("entries-bare")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark"})
    await MODULE.hook_llm_request_v090(FakeEvent("你好", umo))
    check("钩子只传 event 不抛异常", True)

    # 位置参数 / 关键字参数里的请求都能认出来
    fresh_data_root("entries-positional")
    ctx_pos = FakeContext()
    plugin_pos = make_plugin(module, ctx_pos, {"mode": "hint"})
    ev_pos = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    real_req = ProviderRequest(prompt=ev_pos.message_str)
    async for _ in MODULE.handle_message_v090(ev_pos):
        pass
    await MODULE.hook_llm_request_v090(ev_pos, real_req)
    check(
        "位置参数里的请求被认出来",
        len(real_req.extra_user_content_parts) == 1,
        str(real_req.extra_user_content_parts),
    )

    fresh_data_root("entries-keyword")
    ctx_kw = FakeContext()
    plugin_kw = make_plugin(module, ctx_kw, {"mode": "hint"})
    ev_kw = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    kw_req = ProviderRequest(prompt=ev_kw.message_str)
    async for _ in MODULE.handle_message_v090(ev_kw):
        pass
    await MODULE.hook_llm_request_v090(ev_kw, req=kw_req)
    check(
        "关键字参数里的请求被认出来",
        len(kw_req.extra_user_content_parts) == 1,
        str(kw_req.extra_user_content_parts),
    )

    # 能从事件 extra 里找回 ProviderRequest
    # 实机上是「未绑定」调用：注册表里存的是类函数，AstrBot 直接 handler(event, req)。
    # handler 现在是模块级函数，所以这种调用会**正常工作**（这正是修复目标）。
    fresh_data_root("entries-unbound")
    ctx_unbound = FakeContext()
    plugin_unbound = make_plugin(module, ctx_unbound, {"mode": "hint"})
    ev_unbound = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    req_unbound = ProviderRequest(prompt=ev_unbound.message_str)
    async for _ in MODULE.handle_message_v090(ev_unbound):
        pass
    unbound_ok = True
    try:
        await MODULE.hook_llm_request_v090(ev_unbound, req_unbound)
    except Exception as exc:  # noqa: BLE001 - 就是要看它会不会炸
        unbound_ok = False
        print(f"       [debug] 未绑定调用抛了: {type(exc).__name__}: {exc}")
    check("未绑定调用（handler(event, req)）不抛异常", unbound_ok)
    check(
        "未绑定调用也能正常完成请求改写",
        len(req_unbound.extra_user_content_parts) == 1,
        str(req_unbound.extra_user_content_parts),
    )

    # 只拿到请求、拿不到事件（实机日志里的形态）：安静返回，不炸
    fresh_data_root("entries-req-only")
    ctx_reqonly = FakeContext()
    make_plugin(module, ctx_reqonly, {"mode": "hint"})
    req_only = ProviderRequest(prompt="只有请求")
    try:
        await MODULE.hook_llm_request_v090(req_only)
        check("只有 ProviderRequest 时不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check("只有 ProviderRequest 时不抛异常", False, f"{type(exc).__name__}: {exc}")

    # 钩子只拿到事件（没有请求对象）时安全退出
    fresh_data_root("entries-no-request")
    ctx_noreq = FakeContext()
    plugin_noreq = make_plugin(module, ctx_noreq, {"mode": "hint"})
    ev_noreq = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    async for _ in MODULE.handle_message_v090(ev_noreq):
        pass
    await MODULE.hook_llm_request_v090(ev_noreq)
    check("钩子只拿到事件时不抛异常", True)
    check("拿不到请求时不换对话", ctx_noreq.conversation_manager.new_calls == [])


async def test_handlers_are_module_level(module) -> None:
    """handler 必须是模块级、且任何调用方式都不会抛参数错误。

    实机证据（AstrBot 4.28.1）：

        hook(on_astrbot_loaded) -> None - on_loaded_v090
        TypeError: TopicRouterPlugin.on_loaded_v090() missing 1 required positional
                   argument: 'self'

    AstrBot 用 `await handler.handler()` 直接调用、**不传 self**。正常环境里注册表存的
    是绑定方法所以没事，这个环境里存的是未绑定类函数。所以 handler 必须是模块级函数
    且不声明任何必填参数——多一个就 `missing ...`。
    """
    import inspect

    print("\n[12] handler 的形态（实机踩过的坑）")
    for name in (
        "handle_message_v090",
        "hook_llm_request_v090",
        "command_topic_v090",
        "on_loaded_v090",
    ):
        func = getattr(MODULE, name)
        params = list(inspect.signature(func).parameters.values())
        required = [
            p.name
            for p in params
            if p.default is inspect.Parameter.empty
            and p.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        check(f"{name} 是模块级函数（不是类方法）", not inspect.ismethod(func) and func.__qualname__ == name, func.__qualname__)
        check(f"{name} 没有任何必填参数", required == [], str(required))

    # 最关键的一条：像 AstrBot 那样零参数调用 on_loaded
    fresh_data_root("module-level-onloaded")
    ok = True
    try:
        await MODULE.on_loaded_v090()
    except Exception as exc:  # noqa: BLE001 - 就是要看它炸不炸
        ok = False
        print(f"       [debug] on_loaded_v090() 抛了: {type(exc).__name__}: {exc}")
    check("on_loaded_v090() 零参数调用不报错（复现 missing 'self' 的那条路径）", ok)

    # 消息 handler：零参数、只传事件、多传一个 self 都要安全
    for label, call_args in (
        ("零参数", ()),
        ("只传事件", (FakeEvent("测试零参数", "aiocqhttp:group:ml"),)),
        (
            "多传一个 self（未绑定调用的形态）",
            (SimpleNamespace(unified_msg_origin="x", message_str="y"), FakeEvent("测试未绑定", "aiocqhttp:group:ml")),
        ),
    ):
        fresh_data_root("module-level-msg")
        try:
            async for _ in MODULE.handle_message_v090(*call_args):
                pass
            check(f"handle_message_v090 {label} 不抛异常", True)
        except Exception as exc:  # noqa: BLE001
            check(f"handle_message_v090 {label} 不抛异常", False, f"{type(exc).__name__}: {exc}")


async def test_cross_path_dedup(module) -> None:
    """跨路去重：同一条消息不能被判两次，但用户重发必须算新一轮。

    修复前的缺陷：钩子路拿不到事件对象，`_batch_key()` 必然返回 None，所以它无法写
    `state.last_detected`，判完只写内存里的 `RUNTIME.last_detect`。于是"钩子先判、
    事件路后到"时事件路认不出来，同一条消息被判两次，`turns` 直接 +2。

    修复后钩子路会把指纹落到 `state.last_detected_hook`（持久化、可诊断）。但**事件路
    不按文本拦**：它带平台消息 ID，是权威的消息身份，用户重发同一句话必须算新一轮
    —— 这一条曾经被第一版修复误伤，`[7]` 用例当场抓出来了，所以这里两个方向都要钉住。

    Args:
        module: 插件模块。
    """
    print("\n[17] 跨路去重：不重复判定，但重发算新一轮")

    # --- 方向一：事件路自己重复到达（同一消息 ID）→ 只算一轮
    fresh_data_root("cross-path-event-repeat")
    make_plugin(module, FakeContext(), {"mode": "mark"})
    umo = "aiocqhttp:FriendMessage:crosspath"
    event = FakeEvent("帮我看下硬盘坏了没", umo, message_id="m100", timestamp=100)
    async for _ in MODULE.handle_message_v090(event):
        pass
    turns_a = state_store().get(umo).turns
    async for _ in MODULE.handle_message_v090(event):
        pass
    turns_b = state_store().get(umo).turns
    check("同一消息 ID 重复到达只算一轮", turns_a == 1 and turns_b == 1, f"{turns_a} -> {turns_b}")

    # --- 方向二：事件路先判、钩子后到 → 钩子不重复判
    req = ProviderRequest(
        prompt=f"当前话题：x\n用户最新消息：{event.message_str}\n请输出判定 JSON。"
    )
    await MODULE.hook_llm_request_v090(event, req)
    check("事件路刚判过，钩子不重复判", state_store().get(umo).turns == 1,
          f"turns={state_store().get(umo).turns}")

    # --- 方向三：用户重发同一句话（新的消息 ID）→ 必须算新一轮
    resent = FakeEvent("帮我看下硬盘坏了没", umo, message_id="m101", timestamp=101)
    async for _ in MODULE.handle_message_v090(resent):
        pass
    check(
        "用户重发同一句话算新一轮（不能被文本去重吞掉）",
        state_store().get(umo).turns == 2,
        f"turns={state_store().get(umo).turns}",
    )

    # --- 方向四：钩子路判过的消息，在状态里留下可诊断的指纹
    fresh_data_root("cross-path-hook-marker")
    make_plugin(module, FakeContext(), {"mode": "mark"})
    umo2 = "aiocqhttp:FriendMessage:crosspath2"
    event2 = FakeEvent("帮我写个爬虫抓豆瓣", umo2, message_id="m200", timestamp=200)
    await MODULE.hook_llm_request_v090(
        event2,
        ProviderRequest(prompt=f"用户最新消息：{event2.message_str}\n请输出判定 JSON。"),
    )
    marker = state_store().get(umo2).last_detected_hook
    check("钩子路判过之后留下了 last_detected_hook 指纹", bool(marker), repr(marker))
    check(
        "指纹里带着归一化文本（排障时能看出判的是哪句话）",
        "爬虫" in marker,
        repr(marker),
    )


async def test_hook_with_event(module) -> None:
    """钩子**同时**拿到事件与请求时（AstrBot 4.28.1 的实测形态），判定与去重都要成立。

    为什么单独立一条：`_extract` 按类型认参数，事件 + 请求同时出现时钩子路会变成
    "有事件对象"，于是 `_batch_key()` 能算出真键、走的是事件路的去重分支。这与
    "钩子只有 ProviderRequest"（老版本形态）是两条不同的代码路径，必须分别钉住。

    实机证据（用户 v2.0 报告 §8.5）：
        hook_llm_request_v090 被调用 | has_event=True | has_request=True
        | args=['WeixinOCMessageEvent', 'ProviderRequest']
    框架侧对应 `call_event_hook` 的 `await handler.handler(event, *args)`。

    Args:
        module: 插件模块。
    """
    print("\n[19] 钩子同时拿到事件与请求（4.28.1 形态）")
    fresh_data_root("hook-with-event")
    context = FakeContext()
    make_plugin(module, context, {"mode": "mark"})
    umo = "weixin_personal_pxjc:FriendMessage:hookevent"
    event = FakeEvent("看看修好了吗", umo, message_id="mwh1", timestamp=900)

    req = ProviderRequest(
        prompt=f"当前话题：（还没有话题）\n用户最新消息：{event.message_str}\n请输出判定 JSON。"
    )
    # 与框架一致：事件在前，请求在后
    await MODULE.hook_llm_request_v090(event, req)
    turns = state_store().get(umo).turns
    check("钩子带事件也能完成判定", turns == 1, f"turns={turns}")

    st = state_store().get(umo)
    check(
        "带事件时钩子路也会写 last_detected_hook（排障指纹）",
        bool(st.last_detected_hook),
        repr(st.last_detected_hook),
    )

    # 同一事件再次从钩子到达 → 不该重复判定
    await MODULE.hook_llm_request_v090(event, ProviderRequest(prompt=req.prompt))
    check(
        "同一条消息重复到达钩子不重复判定",
        state_store().get(umo).turns == 1,
        f"turns={state_store().get(umo).turns}",
    )

    # 用户重发同一句话（新消息 ID）→ 必须算新一轮
    again = FakeEvent("看看修好了吗", umo, message_id="mwh2", timestamp=901)
    await MODULE.hook_llm_request_v090(again, ProviderRequest(prompt=req.prompt))
    check(
        "钩子路下重发同一句话也算新一轮",
        state_store().get(umo).turns == 2,
        f"turns={state_store().get(umo).turns}",
    )


async def test_hook_prompt_fallback(module) -> None:
    """钩子**只拿到 ProviderRequest**（老框架形态）时，仍能从提示词里兜底抠出消息。

    v0.8.4 把钩子改成了判定主战场，并优先从事件读消息（`_hook_user_message`）。
    但老版本框架的钩子拿不到事件，只能从 prompt 抠——这条路依赖提示词格式，
    必须单独钉住，否则一旦 `build_user_prompt` 的标记改了，兜底会静默失效。

    Args:
        module: 插件模块。
    """
    print("\n[20] 钩子只有请求、没有事件（老框架兜底路径）")
    fresh_data_root("hook-prompt-only")
    make_plugin(module, FakeContext(), {"mode": "mark"})
    umo = "aiocqhttp:group:promptonly"

    # 不出事件，只给请求：走 _hook_user_message 的 prompt 抠取分支
    prompt = "当前话题：（还没有话题）\n用户最新消息：帮我看看硬盘挂了没\n请输出判定 JSON。"
    req = ProviderRequest(prompt=prompt)
    await MODULE.hook_llm_request_v090(req)

    # 没有事件时 umo 只能从 req.conversation 推，推不到就按 cid 隔离；
    # 这里只断言"判定确实发生了"，即状态里出现任意一条带轮数的记录
    store = MODULE.RUNTIME.store
    states = getattr(store, "_states", {})
    judged = [s for s in states.values() if s.turns > 0]
    check(
        "只有 prompt 时也能完成判定（不再依赖事件对象）",
        len(judged) == 1,
        f"states={ {k: v.turns for k, v in states.items()} }",
    )
    if judged:
        check(
            "抠出来的正是标记后的那句话",
            "硬盘" in (judged[0].last_user_text or ""),
            repr(judged[0].last_user_text),
        )

    # 提示词里没有标记时，退回"整段提示词足够短就当作消息"
    fresh_data_root("hook-prompt-no-marker")
    make_plugin(module, FakeContext(), {"mode": "mark"})
    await MODULE.hook_llm_request_v090(ProviderRequest(prompt="顺便看看代理池"))
    states2 = getattr(MODULE.RUNTIME.store, "_states", {})
    judged2 = [s for s in states2.values() if s.turns > 0]
    check(
        "没有标记时退回整段提示词",
        len(judged2) == 1 and "代理池" in (judged2[0].last_user_text or ""),
        f"{[(k, v.turns, v.last_user_text) for k, v in states2.items()]}",
    )


async def test_debug_mode(module) -> None:
    """调试模式：判定照常跑，但**一个副作用都不许有**（空跑）。

    为什么需要它：`auto` 模式会真的新建对话、清空上下文，而判定的准确性尚未在真实
    会话里标定过。调试模式要能让人在**完全不动真实会话**的前提下观察判定。

    这里同时确认"详细日志"那一半：trace.log 里要出现 DEBUG 行。

    Args:
        module: 插件模块。
    """
    print("\n[21] 调试模式：空跑（不切对话、不注入提示）+ 详细日志")
    root = fresh_data_root("debug-dryrun")
    context = FakeContext()
    # auto 模式 + 通知开启 + 调试模式 → 判定照旧，但什么都不该发生
    plugin = make_plugin(
        module,
        context,
        {
            "mode": "auto",
            "auto_threshold": 0.8,
            "new_conversation_notice": True,
            "trace_enabled": True,
            "debug_mode": True,
        },
    )
    event = FakeEvent("帮我写个 Python 脚本")
    from astrbot.api.provider import ProviderRequest  # noqa: PLC0415

    old = await context.conversation_manager.new_conversation(event.unified_msg_origin)
    context.calls.clear()
    new_calls_before = len(context.conversation_manager.new_calls)
    req = ProviderRequest(
        prompt=event.message_str,
        contexts=[{"role": "user", "content": "电路板怎么画"}],
        conversation=context.conversation_manager.conversations[old],
    )
    await run_detect(plugin, event, req)

    check(
        "调试模式不新建对话",
        len(context.conversation_manager.new_calls) == new_calls_before,
        str(context.conversation_manager.new_calls),
    )
    check("调试模式不清空上下文", req.contexts != [], str(req.contexts))
    check("调试模式不向用户发提示", event.sent == [], str(event.sent))
    check(
        "调试模式下判定仍然执行（状态被推进）",
        state_store().get(event.unified_msg_origin).turns == 1,
        f"turns={state_store().get(event.unified_msg_origin).turns}",
    )

    trace_path = root / "plugin_data" / "astrbot_plugin_topic_router_pro" / "trace.log"
    body = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
    check("详细日志：trace.log 里有 DEBUG 行", "DEBUG" in body, body[:200])
    check(
        "详细日志：DEBUG 行含启发式得分与模型原始输出字段",
        "heuristic=" in body and "llm_called=" in body,
        body[:400],
    )
    check("空跑时留下 DRYRUN 记录", "DRYRUN" in body, body[-300:])

    # hint 模式走的是同一个闸门：必须同样被抑制（别只顾着 auto）
    fresh_data_root("debug-dryrun-hint")
    context_h = FakeContext()
    plugin_h = make_plugin(
        module,
        context_h,
        {
            "mode": "hint",
            "auto_threshold": 0.8,
            "trace_enabled": True,
            "debug_mode": True,
        },
    )
    event_h = FakeEvent("帮我写个 Rust 服务")
    req_h = ProviderRequest(
        prompt=event_h.message_str,
        contexts=[{"role": "user", "content": "电路板怎么画"}],
    )
    await run_detect(plugin_h, event_h, req_h)
    check(
        "调试模式也不注入 hint（同一个闸门覆盖 auto 与 hint）",
        len(req_h.extra_user_content_parts) == 0,
        str(req_h.extra_user_content_parts),
    )

    # 对照：同样的输入关掉调试模式 → 必须真的切换（证明差别来自调试模式）
    fresh_data_root("debug-off-control")
    context2 = FakeContext()
    plugin2 = make_plugin(
        module,
        context2,
        {
            "mode": "auto",
            "auto_threshold": 0.8,
            "new_conversation_notice": True,
            "debug_mode": False,
        },
    )
    event2 = FakeEvent("帮我写个 Python 脚本")
    old2 = await context2.conversation_manager.new_conversation(event2.unified_msg_origin)
    context2.calls.clear()
    new_calls_before2 = len(context2.conversation_manager.new_calls)
    req2 = ProviderRequest(
        prompt=event2.message_str,
        contexts=[{"role": "user", "content": "电路板怎么画"}],
        conversation=context2.conversation_manager.conversations[old2],
    )
    await run_detect(plugin2, event2, req2)
    check(
        "对照组：关掉调试模式后确实切换了对话（证明差异源自调试模式）",
        len(context2.conversation_manager.new_calls) == new_calls_before2 + 1,
        str(context2.conversation_manager.new_calls),
    )


async def test_long_idle_gap(module) -> None:
    """长时间未回复后的处理（v0.8.6）：由模型判"下一句是否延续"。

    设计要点（每一步都有对应断言）：

    1. 时间间隔**不再**直接判新话题（旧行为），而是触发一次模型咨询；
    2. 模型说延续 → 就延续（这正是用户流程图里"可能性高就连续"那一支）；
    3. 模型说新话题且很确定 → 才算新话题；
    4. **模型失败/超时 → 按延续**（本项目的铁律：暂时性故障不该切开用户上下文）；
    5. 提示词里必须带上"隔了多久"，否则模型手上只有语义相似度，等于白问；
    6. 兜底抠取（只有 prompt、没有事件）不能被这行说明污染。

    Args:
        module: 插件模块。
    """
    print("\n[22] 长时间未回复：交模型判是否延续")
    umo = "aiocqhttp:group:idle"
    long_gap = "今天天气怎么样"

    async def setup(state_turns: int = 3):
        """建会话并把它推到"已经有话题、且刚过很久"的状态。

        Args:
            state_turns: 当前轮数。

        Returns:
            (plugin, context, state)。
        """
        fresh_data_root(f"idle-{state_turns}")
        context = FakeContext()
        plugin = make_plugin(
            module, context, {"mode": "mark", "time_gap_minutes": 45}
        )
        st = state_store().get(umo)
        st.label = "Python 批量转表格"
        st.last_user_text = "帮我写个 Python 脚本，把这一批 csv 批量转成 excel"
        st.turns = state_turns
        # 把"上一条消息时间"推到两小时前
        st.last_ts = time.time() - 2 * 3600
        return plugin, context, st

    # --- 模型说延续 → 延续 ---
    plugin, context, st = await setup()
    context.answer = '{"verdict":"continue","confidence":0.9,"topic_name":"Python 批量转表格"}'
    # 注意：_detect 是**原地修改** state 对象的，所以不能拿 st.turns 当基线
    # （它会跟着一起变）。必须取数值快照。
    turns_before_continue = state_store().get(umo).turns
    await run_detect(plugin, FakeEvent(long_gap, umo), ProviderRequest(prompt=long_gap))
    turns_after_continue = state_store().get(umo).turns
    check(
        "模型判延续时就是延续（隔得久也不硬切）",
        turns_after_continue == turns_before_continue + 1,
        f"turns {turns_before_continue} -> {turns_after_continue}",
    )
    check(
        "模型确实被调用了（说明时间间隔走了'交模型判'这条路）",
        len(context.calls) > 0,
        f"calls={len(context.calls)}",
    )

    # --- 提示词里必须带上间隔 ---
    prompt_text = ""
    for call in context.calls:
        text = str(call)
        if "距上一条消息" in text:
            prompt_text = text
            break
    check(
        "送给模型的提示词带上了'距上一条消息已过 N 分钟'",
        "距上一条消息" in prompt_text,
        prompt_text[:200],
    )
    check(
        "提示词里说明了'隔得久不代表换话题'",
        "不代表换话题" in prompt_text,
        prompt_text[:300],
    )

    # --- 模型说新话题且确定 → 新话题 ---
    plugin2, context2, st2 = await setup()
    context2.answer = '{"verdict":"new_topic","confidence":0.9,"topic_name":"天气"}'
    before_switches = state_store().get(umo).switches
    await run_detect(plugin2, FakeEvent(long_gap, umo), ProviderRequest(prompt=long_gap))
    check(
        "模型很确定判新话题时才切",
        state_store().get(umo).switches > before_switches,
        f"switches {before_switches} -> {state_store().get(umo).switches}",
    )

    # --- 模型失败 → 按延续（铁律）---
    plugin3, context3, st3 = await setup()
    context3.fail = RuntimeError("模拟判定模型超时/报错")
    turns_before = state_store().get(umo).turns
    await run_detect(plugin3, FakeEvent(long_gap, umo), ProviderRequest(prompt=long_gap))
    st_after = state_store().get(umo)
    check(
        "模型失败时按延续（暂时性故障不切开上下文）",
        st_after.turns == turns_before + 1,
        f"turns {turns_before} -> {st_after.turns}",
    )
    check(
        "模型失败时不新建对话",
        context3.conversation_manager.new_calls == [],
        str(context3.conversation_manager.new_calls),
    )

    # --- 兜底抠取不被空闲说明污染 ---
    check(
        "只有 prompt 时抠出的仍是用户消息（不含'距上一条消息'说明）",
        "距上一条消息" not in (st_after.last_user_text or ""),
        repr(st_after.last_user_text[:80]),
    )
    check(
        "抠出的就是那句话本身",
        (st_after.last_user_text or "").strip() == long_gap,
        repr(st_after.last_user_text),
    )


async def test_trace_landing(module) -> None:
    """落盘取证：开启 trace_enabled 后，入口/钩子/判定都要在 trace.log 留痕。

    Args:
        module: 插件模块。
    """
    print("\n[18] 排障证据落盘（不依赖日志级别与格式）")
    root = fresh_data_root("trace")
    make_plugin(module, FakeContext(), {"mode": "mark", "trace_enabled": True})
    umo = "aiocqhttp:FriendMessage:trace"
    event = FakeEvent("帮我看下硬盘坏了没", umo, message_id="m400", timestamp=400)
    async for _ in MODULE.handle_message_v090(event):
        pass
    trace_path = root / "plugin_data" / "astrbot_plugin_topic_router_pro" / "trace.log"
    check("trace.log 已生成", trace_path.exists(), str(trace_path))
    body = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
    check("留了 ENTRY 行（证明入口被执行）", "ENTRY" in body, body[:200])
    check("留了 VERDICT 行（带 via= 标明走的哪条路）", "VERDICT" in body and "via=event" in body, body[:300])

    # 关掉开关就不该有任何落盘
    root2 = fresh_data_root("trace-off")
    make_plugin(module, FakeContext(), {"mode": "mark", "trace_enabled": False})
    event2 = FakeEvent("关掉开关的消息", "aiocqhttp:FriendMessage:notrace", message_id="m401", timestamp=401)
    async for _ in MODULE.handle_message_v090(event2):
        pass
    off_path = root2 / "plugin_data" / "astrbot_plugin_topic_router_pro" / "trace.log"
    check("关闭 trace_enabled 后不产生 trace.log", not off_path.exists(), str(off_path))


async def test_promotion_and_review(module) -> None:
    """验证阶梯放权与每日复盘（v0.10.0）。

    Args:
        module: 插件模块（`...router`）。
    """
    print("\n[21] 阶梯放权 + 每日复盘")
    journey_mod = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.journey"
    ]
    daily_mod = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.daily"
    ]

    plan = journey_mod.Plan(enabled=True, mark_days=3.0, hint_days=3.0, to_auto=True)
    check(
        "天数 -> 档位（2.9 天仍是 mark）",
        journey_mod.target_stage(2.9, plan) == "mark",
        journey_mod.target_stage(2.9, plan),
    )
    check(
        "天数 -> 档位（3.0 天进入 hint）",
        journey_mod.target_stage(3.0, plan) == "hint",
        journey_mod.target_stage(3.0, plan),
    )
    check(
        "天数 -> 档位（6.0 天进入 auto）",
        journey_mod.target_stage(6.0, plan) == "auto",
        journey_mod.target_stage(6.0, plan),
    )
    check(
        "to_auto=False 时停在 hint",
        journey_mod.target_stage(99.0, journey_mod.Plan(True, 3, 3, False)) == "hint",
    )

    tmp = Path(tempfile.mkdtemp(prefix="topic-router-journey-"))
    try:
        journey = journey_mod.Journey(tmp / "promotion.json")
        start = 1_000_000.0
        journey.start(start, "mark")
        check("刚启动时不动", journey.step(start, plan, "mark") is None)
        check(
            "第 4 天自动切 hint",
            journey.step(start + 4 * 86400, plan, "mark") == "hint",
        )
        check(
            "切 hint 之后不会重复切",
            journey.step(start + 4.5 * 86400, plan, "hint") is None,
        )
        check(
            "第 7 天自动切 auto",
            journey.step(start + 7 * 86400, plan, "hint") == "auto",
        )

        # 用户手动改档：暂停，不再覆盖
        journey2 = journey_mod.Journey(tmp / "promotion2.json")
        journey2.start(start, "mark")
        journey2.step(start + 4 * 86400, plan, "mark")  # -> hint（自动写入，last_applied=hint）
        paused = journey2.step(start + 5 * 86400, plan, "mark")
        check("检测到手动改档后暂停", paused is None and journey2.paused is True,
              f"{paused} paused={journey2.paused}")

        # 页面设置写回（白名单 + 夹紧 + 落盘）
        original_settings_cfg = MODULE.RUNTIME.config

        class _SettingsCfg(dict):
            """带 save_config 的假配置。"""

            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.saved = 0

            def save_config(self) -> None:
                """记录落盘次数。"""
                self.saved += 1

        settings_cfg = _SettingsCfg({'promotion': {}, 'review': {}})
        MODULE.RUNTIME.config = settings_cfg
        try:
            saved = MODULE.update_settings(
                {
                    'promotion': {'enabled': True, 'mark_days': -5, 'junk': 1},
                    'review': {'hour': 99},
                }
            )
            check(
                '设置：白名单外的键被忽略',
                'promotion.junk' not in saved['changed'],
                str(saved['changed']),
            )
            check(
                '设置：负天数夹紧到 0',
                settings_cfg['promotion']['mark_days'] == 0.0,
                str(settings_cfg['promotion']),
            )
            check(
                '设置：小时夹紧到 23',
                settings_cfg['review']['hour'] == 23,
                str(settings_cfg['review']),
            )
            check('设置：写回触发 save_config', settings_cfg.saved == 1,
                  str(settings_cfg.saved))
            MODULE.update_settings({'mode': 'hint'})
            check(
                '设置：顶层标量按 schema 写入',
                settings_cfg.get('mode') == 'hint',
                str(settings_cfg.get('mode')),
            )
            check(
                '设置：schema 里没有的顶层键被忽略',
                'no_such_key' not in MODULE.update_settings({'no_such_key': 1})['changed'],
            )
        finally:
            MODULE.RUNTIME.config = original_settings_cfg

        # 每日复盘：流水 + 统计 + 兜底摘要
        log = daily_mod.JudgmentLog(tmp / "review" / "logs", keep_days=3)
        for idx in range(4):
            log.append(
                {
                    "ts": time.time() + idx,
                    "umo": "aiocqhttp:FriendMessage:r",
                    "text": f"消息{idx}",
                    "verdict": "new" if idx % 2 == 0 else "continue",
                    "confidence": 0.9 if idx == 0 else 0.5,
                    "source": "heuristic",
                    "mode": "mark",
                    "via": "event",
                },
            )
        records = log.read_day()
        check("判定流水写入并读回", len(records) == 4, str(len(records)))
        stats = daily_mod.summarize(records, "2026-10-25")
        check("统计：新话题 2 条", stats["new_topic"] == 2, str(stats["new_topic"]))
        check("统计：低置信度样例被收集", len(stats["samples"]) == 1,
              str(len(stats["samples"])))
        text = daily_mod.compose_review(stats, "")
        check("模型不可用时退回本地摘要", "本地摘要" in text, text[:40])

        store = daily_mod.ReviewStore(tmp / "review")
        store.save("2026-10-25", text)
        latest = store.latest()
        check(
            "复盘报告可读回",
            latest is not None and latest["day"] == "2026-10-25",
            str(latest and latest["day"]),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_aggregation_integration(module) -> None:
    """验证内建消息聚合与话题判定的流水线衔接。

    覆盖四件事：

    1. 聚合默认开启、配置默认值与 `_conf_schema.json` 逐项一致；
    2. 连续两条消息只推进**一轮**话题，且 Owner 事件被重构为合并文本；
    3. 补充消息被静默（不产生第二次 LLM 调用），请求钩子也不会重复判定；
    4. `aggregation.enabled=false` 时退回逐条判定。

    Args:
        module: 插件模块。
    """
    print("\n[19] 内建消息聚合（整合自 astrbot_plugin_msg_aggregator）")
    root = fresh_data_root("aggregation")
    ctx = FakeContext()
    plugin = make_plugin(
        module,
        ctx,
        {
            "mode": "mark",
            "provider_id": "",
            "aggregation": {
                "enabled": True,
                "debounce_time": 0.3,
                "adaptive": False,
                "group_enabled": True,
                "page_enabled": False,
            },
        },
    )
    service = MODULE.RUNTIME.aggregation
    check("聚合开启时创建服务", service is not None)
    if service is None:
        return
    await plugin.initialize()
    check("插件 initialize 启动了聚合后台任务", service._started)

    check(
        "聚合数据落在插件自己的 plugin_data 下",
        str(service.stats.path).startswith(str(root)),
        str(service.stats.path),
    )

    # 配置默认值与 schema 必须逐项一致（独立插件里这条是硬约束，迁入后保持）
    agg_cfg = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.aggregation.config"
    ]
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["aggregation"]["items"]
    defaults = agg_cfg.Config()
    mismatch = []
    for name in agg_cfg.Config.__dataclass_fields__:
        want = getattr(defaults, name)
        got = items.get(name, {}).get("default")
        if want != got:
            mismatch.append(f"{name}: code={want!r} schema={got!r}")
    check("聚合 Config 默认值与 schema 一致", not mismatch, "; ".join(mismatch[:5]))

    umo = "aiocqhttp:FriendMessage:agg"
    e1 = FakeEvent("帮我看看 NAS 硬盘", umo, message_id="agg1", timestamp=1)
    e2 = FakeEvent("好像有点响", umo, message_id="agg2", timestamp=2)

    async def run(ev) -> None:
        """驱动一次消息入口。

        Args:
            ev: 消息事件。
        """
        async for _ in MODULE.handle_message_v090(ev):
            pass

    task1 = asyncio.create_task(run(e1))
    await asyncio.sleep(0.05)
    task2 = asyncio.create_task(run(e2))
    await asyncio.wait_for(asyncio.gather(task1, task2), timeout=3.0)

    check("补充消息被静默（不进入下游）", e2.is_stopped())
    check("Owner 未被静默（继续流向 LLM）", not e1.is_stopped())
    check(
        "事件被重构为合并文本",
        e1.message_str == "帮我看看 NAS 硬盘\n好像有点响",
        repr(e1.message_str),
    )
    state = MODULE.RUNTIME.store.get(umo)
    check("两句话只推进一轮话题", state.turns == 1, f"turns={state.turns}")

    # 钩子在同一事件上再触发：已判过，不得把 turns 推成 2
    req = ProviderRequest(prompt="用户最新消息：帮我看看 NAS 硬盘\n好像有点响\n请输出判定 JSON。")
    await MODULE.hook_llm_request_v090(e1, req)
    check(
        "聚合后的请求钩子不重复判定",
        MODULE.RUNTIME.store.get(umo).turns == 1,
        f"turns={MODULE.RUNTIME.store.get(umo).turns}",
    )

    check(
        "统计记录了一次结算",
        service.stats.flush_total == 1,
        f"flush_total={service.stats.flush_total}",
    )
    check(
        "统计记录了两条入桶消息",
        service.stats.items_total == 2,
        f"items_total={service.stats.items_total}",
    )

    # ---- v0.9.3：自适应窗口必须能覆盖"话没说完"的连发 ----
    strategy = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.aggregation.strategy"
    ]

    def _wait(text_len: int, tail: str) -> float:
        """按默认配置算一次等待秒数。"""
        return strategy.compute(
            now=0.0,
            started_at=0.0,
            text_len=text_len,
            tail_char=tail,
            short_streak=0,
            cooldown_remaining=0.0,
            cfg=agg_cfg.Config(),
        )

    finished = _wait(12, "。")
    # 用真实的"句末不是标点"的字符（不是空串）——v0.9.3 就是栽在只判空串上，
    # 窗口在实机上仍然是 2.5s，扫间隔才发现。
    unfinished = _wait(12, "略")
    short_unfinished = _wait(4, "略")
    check(
        "话没说完（句末不是标点）比已说完的句子多等 ≥0.8s",
        unfinished >= finished + 0.8,
        f"unfinished={unfinished} finished={finished}",
    )
    check(
        "短句且句末不是标点的有效窗口约 3.5s",
        3.3 <= short_unfinished <= 3.7,
        str(short_unfinished),
    )

    # ---- v0.9.3：页面切预设必须写回插件配置（否则重载即回退）----
    class _PersistConfig(dict):
        """带 save_config 的假配置（模拟 AstrBotConfig）。"""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.saved = 0

        def save_config(self) -> None:
            """记录落盘次数。"""
            self.saved += 1

    persist_cfg = _PersistConfig({"aggregation": {"preset": "balanced"}})
    original_config = MODULE.RUNTIME.config
    MODULE.RUNTIME.config = persist_cfg
    try:
        service.apply_preset("patient")
        check(
            "apply_preset 把生效值写回配置",
            persist_cfg["aggregation"].get("debounce_time") == 3.5
            and persist_cfg["aggregation"].get("preset") == "patient",
            str(persist_cfg["aggregation"]),
        )
        check(
            "写回后调用了 save_config",
            persist_cfg.saved == 1,
            str(persist_cfg.saved),
        )
    finally:
        MODULE.RUNTIME.config = original_config


    # 页面后端：只验证路由注册（真实渲染由 Dashboard 负责）
    class FakeWebCtx:
        """只提供 register_web_api 的假上下文。"""

        def __init__(self) -> None:
            self.routes: list[tuple[str, tuple[str, ...]]] = []

        def register_web_api(self, route, handler, methods, desc) -> None:
            self.routes.append((route, tuple(methods)))

    from data.plugins.astrbot_plugin_topic_router_pro.aggregation import web_api

    service.cfg.page_enabled = True
    web_ctx = FakeWebCtx()
    web_api.register(web_ctx, service)
    check(
        "页面后端注册 14 个路由（含 schema/config）",
        len(web_ctx.routes) == 14,
        str(web_ctx.routes),
    )
    check(
        "放权与复盘的 GET 路由都在",
        {
            "/astrbot_plugin_topic_router_pro/promotion",
            "/astrbot_plugin_topic_router_pro/review",
        }
        <= {route for route, _ in web_ctx.routes},
        str(web_ctx.routes),
    )
    check(
        "路由以插件名开头",
        all(
            route.startswith("/astrbot_plugin_topic_router_pro/")
            for route, _ in web_ctx.routes
        ),
        str(web_ctx.routes),
    )

    await service.shutdown()
    MODULE.RUNTIME.aggregation = None

    # ---- 入口被抢占：钩子兜底判定 + 抢占计数 ----
    root2 = fresh_data_root("aggregation-preempted")
    make_plugin(
        MODULE,
        FakeContext(),
        {
            "mode": "mark",
            "trace_enabled": True,
            "aggregation": {
                "enabled": True,
                "debounce_time": 0.05,
                "adaptive": False,
                "page_enabled": False,
            },
        },
    )
    service2 = MODULE.RUNTIME.aggregation
    check("抢占场景下聚合服务仍在", service2 is not None)
    if service2 is not None:
        class _DispatchEntry:
            """模拟 waking_check 写入的 activated_handlers 条目。"""

            handler_name = "handle_message_v090"
            extras_configs = {"priority": 1000}

        stolen = FakeEvent(
            "被更高优先级插件截走的消息",
            "aiocqhttp:FriendMessage:preempted",
            message_id="pre1",
            timestamp=4,
        )
        # 关键：事件必须真的在派发计划里含本插件，才算"被抢占"。
        stolen.set_extra("activated_handlers", [_DispatchEntry()])
        # 不调用消息入口，直接走请求钩子：模拟"入口在派发循环里没被调用"
        await MODULE.hook_llm_request_v090(
            stolen,
            ProviderRequest(prompt="用户最新消息：被更高优先级插件截走的消息\n请输出判定 JSON。"),
        )
        check(
            "入口未跑时钩子仍完成判定",
            MODULE.RUNTIME.store.get("aiocqhttp:FriendMessage:preempted").turns == 1,
        )
        check(
            "抢占计数被记录",
            service2.stats.preempted_total == 1,
            str(getattr(service2.stats, "preempted_total", "?")),
        )

        # 合成事件（主动回复 / 其它插件直接 request_llm）没有派发计划，
        # 不能报成"被抢占"——这正是 v0.9.0 线上误报的来源。
        synthetic = FakeEvent(
            "主动回复触发的合成请求",
            "aiocqhttp:FriendMessage:synthetic",
            message_id="syn1",
            timestamp=5,
        )
        await MODULE.hook_llm_request_v090(
            synthetic,
            ProviderRequest(prompt="用户最新消息：主动回复触发的合成请求\n请输出判定 JSON。"),
        )
        check(
            "合成事件不计入抢占",
            service2.stats.preempted_total == 1,
            str(service2.stats.preempted_total),
        )
        trace_path = (
            root2
            / "plugin_data"
            / "astrbot_plugin_topic_router_pro"
            / "trace.log"
        )
        body = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
        check("抢占证据落盘（PREEMPTED）", "PREEMPTED" in body, body[-300:])
        await service2.shutdown()
        MODULE.RUNTIME.aggregation = None

    # ---- 关闭聚合：退回逐条判定 ----
    fresh_data_root("aggregation-off")
    make_plugin(
        module,
        FakeContext(),
        {"mode": "mark", "aggregation": {"enabled": False}},
    )
    check("关闭聚合时不创建服务", MODULE.RUNTIME.aggregation is None)
    event = FakeEvent(
        "聚合关闭后的消息",
        "aiocqhttp:FriendMessage:noagg",
        message_id="noagg1",
        timestamp=3,
    )
    async for _ in MODULE.handle_message_v090(event):
        pass
    check(
        "关闭聚合后仍逐条判定",
        MODULE.RUNTIME.store.get("aiocqhttp:FriendMessage:noagg").turns == 1,
    )

    # ---- 聚合后 hint 动作仍然要执行（“先聚合、再判定、最后改写请求”全链路） ----
    fresh_data_root("aggregation-hint")
    ctx_h = FakeContext()
    make_plugin(
        MODULE,
        ctx_h,
        {
            "mode": "hint",
            "provider_id": "",
            "aggregation": {
                "enabled": True,
                "debounce_time": 0.05,
                "adaptive": False,
                "page_enabled": False,
            },
        },
    )
    service_h = MODULE.RUNTIME.aggregation
    check("hint 场景下聚合服务已创建", service_h is not None)
    if service_h is not None:
        hint_event = FakeEvent(
            "帮我写个 Python 脚本",
            "aiocqhttp:FriendMessage:agghint",
            message_id="hint1",
            timestamp=5,
        )
        async for _ in MODULE.handle_message_v090(hint_event):
            pass
        check(
            "聚合后新话题判定已挂起 hint 动作",
            "aiocqhttp:FriendMessage:agghint" in MODULE.RUNTIME.pending_actions,
            str(MODULE.RUNTIME.pending_actions.keys()),
        )
        hint_req = ProviderRequest(prompt="用户最新消息：帮我写个 Python 脚本\n请输出判定 JSON。")
        await MODULE.hook_llm_request_v090(hint_event, hint_req)
        check(
            "请求钩子执行了 hint 注入",
            len(hint_req.extra_user_content_parts) >= 1,
            str(hint_req.extra_user_content_parts),
        )
        check(
            "hint 动作执行后被消费",
            "aiocqhttp:FriendMessage:agghint" not in MODULE.RUNTIME.pending_actions,
        )
        await service_h.shutdown()
        MODULE.RUNTIME.aggregation = None

    # ---- 总开关关闭：聚合也不能偷偷缓冲/静默消息 ----
    fresh_data_root("plugin-off")
    make_plugin(
        MODULE,
        FakeContext(),
        {"enabled": False, "aggregation": {"enabled": True}},
    )
    check(
        "插件总开关关闭时聚合不启动",
        MODULE.RUNTIME.aggregation is None,
    )


async def test_adaptive_interval(module) -> None:
    """验证「自适应间隔时间」（v1.1.4 新增，默认开启）。

    三件事：

    1. 纯函数：样本不足不参与、样本足够时按分位 + 余量拉长、关闭后完全不参与、
       一轮总等待上限仍然压得住；
    2. 存储：同桶相邻消息的真实间隔被记住、跨轮的超长间隔不进样本、样本条数按配置截断；
    3. 配置：默认开启、非法值被夹紧（与 schema 的逐项一致由 [19] 的比对覆盖）。

    Args:
        module: 插件模块（本用例只碰聚合子模块，保持签名与其它用例一致）。
    """
    print("\n[19b] 自适应间隔时间（按真实发送节奏拉长窗口）")
    strategy = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.aggregation.strategy"
    ]
    agg_cfg = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.aggregation.config"
    ]
    session_mod = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.aggregation.session"
    ]

    cfg = agg_cfg.Config()
    check("自适应间隔默认开启", cfg.adaptive_interval is True)
    check("间隔余量默认 0.5s", cfg.adaptive_interval_margin == 0.5)
    check("间隔样本默认 6 条", cfg.adaptive_interval_samples == 6)

    check(
        "样本不足时自适应间隔不参与",
        strategy.interval_wait(gaps=[3.0], cfg=cfg) == 0.0,
    )
    learned = strategy.interval_wait(gaps=[2.8, 3.2, 3.9], cfg=cfg)
    check(
        "有样本时取 75% 分位 + 余量",
        abs(learned - 4.4) < 1e-6,
        str(learned),
    )
    check(
        "关闭后完全不参与",
        strategy.interval_wait(
            gaps=[2.8, 3.2, 3.9], cfg=agg_cfg.replace(cfg, adaptive_interval=False)
        )
        == 0.0,
    )

    base = strategy.compute(
        now=0.0, started_at=0.0, text_len=12, tail_char="。",
        short_streak=0, cooldown_remaining=0.0, cfg=cfg,
    )
    extended = strategy.compute(
        now=0.0, started_at=0.0, text_len=12, tail_char="。",
        short_streak=0, cooldown_remaining=0.0, cfg=cfg,
        interval_gaps=[2.8, 3.2, 3.9],
    )
    check(
        "学到的间隔只拉长、不缩短窗口",
        extended > base,
        f"base={base} extended={extended}",
    )
    capped = strategy.compute(
        now=11.0, started_at=0.0, text_len=12, tail_char="。",
        short_streak=0, cooldown_remaining=0.0, cfg=cfg,
        interval_gaps=[2.8, 3.2, 3.9],
    )
    check("一轮总等待上限仍然优先", abs(capped - 1.0) < 1e-6, str(capped))

    clamped = agg_cfg.validate(
        agg_cfg.replace(
            cfg, adaptive_interval_margin=99.0, adaptive_interval_samples=1
        )
    )
    check(
        "非法余量/样本数被夹紧",
        clamped.adaptive_interval_margin == 10.0
        and clamped.adaptive_interval_samples == 2,
        f"margin={clamped.adaptive_interval_margin}"
        f" samples={clamped.adaptive_interval_samples}",
    )

    class _StubClock:
        """可手拨的时间源（间隔样本按 arrived_at 落账，不需要真等）。"""

        def __init__(self) -> None:
            self.value = 100.0

        def now(self) -> float:
            return self.value

    def _item(clock, mid: str, text: str):
        """造一条最小消息条目（只带文本）。

        Args:
            clock: 时间源（决定 arrived_at）。
            mid: 消息 ID。
            text: 文本。

        Returns:
            `session.Item`。
        """
        return session_mod.Item(
            message_id=mid,
            parts=(session_mod.Part(kind="text", value=text),),
            arrived_at=clock.now(),
        )

    clock = _StubClock()
    store = session_mod.SessionStore(clock=clock, cfg=cfg)
    key = ("aiocqhttp:FriendMessage:interval", "u1")
    rnd = store.get_or_create(key, object())
    for index, text in enumerate(("第一句", "第二句", "第三句", "第四句")):
        if index:
            clock.value += (2.8, 3.2, 3.9)[index - 1]
        store.append(rnd, _item(clock, f"m{index}", text))
    gaps = store.recent_gaps(key)
    check(
        "同桶相邻消息的真实间隔被记住",
        len(gaps) == 3
        and all(abs(a - b) < 1e-6 for a, b in zip(gaps, (2.8, 3.2, 3.9))),
        str(gaps),
    )

    clock.value += 90.0
    store.append(rnd, _item(clock, "m4", "很久以后的一句"))
    check(
        "跨轮的超长间隔不进样本",
        len(store.recent_gaps(key)) == 3,
        str(store.recent_gaps(key)),
    )

    small = agg_cfg.replace(cfg, adaptive_interval_samples=2)
    clock2 = _StubClock()
    store2 = session_mod.SessionStore(clock=clock2, cfg=small)
    key2 = (key[0], "u2")
    rnd2 = store2.get_or_create(key2, object())
    for index, text in enumerate(("a", "b", "c", "d")):
        if index:
            clock2.value += (1.0, 2.0, 3.0)[index - 1]
        store2.append(rnd2, _item(clock2, f"n{index}", text))
    check(
        "样本条数按配置截断（只留最近 N 条）",
        len(store2.recent_gaps(key2)) == 2,
        str(store2.recent_gaps(key2)),
    )

    # ---- 服务层接线：学到的间隔必须真的进到 _compute_wait ----
    service_mod = sys.modules[
        "data.plugins.astrbot_plugin_topic_router_pro.aggregation.service"
    ]
    wiring_clock = _StubClock()
    service = service_mod.AggregationService(
        None,
        {
            "enabled": True,
            "adaptive": True,
            "debounce_time": 2.0,
            "adaptive_interval": True,
            "adaptive_interval_margin": 0.5,
            "adaptive_interval_samples": 6,
        },
        data_dir=fresh_data_root("adaptive-interval-wiring"),
        clock=wiring_clock,
    )
    key3 = ("aiocqhttp:FriendMessage:wiring", "u3")
    rnd3 = service.store.get_or_create(key3, object())
    for index, text in enumerate(("a", "b", "c")):
        if index:
            wiring_clock.value += 3.4
        service.store.append(rnd3, _item(wiring_clock, f"w{index}", text))
    service_wait = service._compute_wait(rnd3)
    check(
        "服务层把学到的间隔接进了等待计算",
        3.8 <= service_wait <= 4.0,
        str(service_wait),
    )


async def main() -> int:
    """跑完所有用例。

    Returns:
        进程退出码。
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="topic-router-test-"))
    try:
        install_plugin_as_package(tmp_root)
        import data.plugins.astrbot_plugin_topic_router_pro.main as plugin_main

        global MODULE
        MODULE = sys.modules["data.plugins.astrbot_plugin_topic_router_pro.router"]

        await test_metadata(plugin_main, tmp_root)
        await test_heuristics(plugin_main)
        await test_json_parsing(plugin_main)
        await test_mark_mode(plugin_main)
        await test_auto_mode(plugin_main)
        await test_hint_mode(plugin_main)
        await test_request_hook_alone(plugin_main)
        await test_state_lifecycle(plugin_main)
        await test_failure_paths(plugin_main)
        await test_topic_command(plugin_main)
        await test_entry_points(plugin_main)
        await test_handlers_are_module_level(plugin_main)
        await test_user_prompt_contains_reply(plugin_main)
        await test_provider_id_fallback(plugin_main)
        await test_cross_path_dedup(plugin_main)
        await test_hook_with_event(plugin_main)
        await test_hook_prompt_fallback(plugin_main)
        await test_debug_mode(plugin_main)
        await test_long_idle_gap(plugin_main)
        await test_trace_landing(plugin_main)
        await test_aggregation_integration(plugin_main)
        await test_adaptive_interval(MODULE)
        await test_promotion_and_review(MODULE)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED.append("测试脚本本身抛异常")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        shutil.rmtree(_DATA_ROOT, ignore_errors=True)

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))




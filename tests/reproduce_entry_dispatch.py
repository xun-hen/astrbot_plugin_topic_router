"""回归测试：消息入口必须通过 waking_check 的两道筛选。

## 为什么需要这个脚本

v0.8.x 到 v0.9.1 的真实故障是**完全静默**的：

1. 插件入口是 `main.py`，框架只以 `data.plugins.<目录名>.main` 登记插件元数据；
2. 所有 handler 定义在 `router.py`，`handler_module_path` 因此是 `...router`；
3. `Star.__init_subclass__` 按 `router` 路径留下一条**匿名**元数据（`name=None`），
   框架查 handler 时正好命中它；
4. `SessionPluginManager.filter_handlers_by_session` 里 `if plugin.name is None: continue`
   —— 静默丢弃，不写日志、不抛异常。

于是消息入口与 `/topicweave` 永远进不了 `activated_handlers`，聚合层整层空转；
`on_llm_request` 钩子不走这条过滤，所以功能看起来"没坏"，一年多没人发现。

本脚本按实机方式导入 `data.plugins.<目录名>.main`，逐条走
`get_handlers_by_event_type(AdapterMessageEvent)` → `filter_handlers_by_session`，
断言入口在最终名单里；最后再把 `handler_module_path` 改回 `router` 复现静默丢弃，
证明这个断言真的能抓住回归。

用法：

    <AstrBot 的 python> tests/reproduce_entry_dispatch.py
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent


def _astrbot_paths() -> list[Path]:
    """猜出本机 AstrBot 的 import 路径。

    Returns:
        存在的候选路径列表。
    """
    candidates: list[Path] = []
    for env_key in ("ASTRBOT_APP_PATH", "ASTRBOT_SITE_PACKAGES"):
        value = os.environ.get(env_key)
        if value:
            candidates.append(Path(value))
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += [
        Path(local) / "AstrBot" / "backend" / "app" if local else None,
        Path(local) / "AstrBot" / "backend" / "python" / "Lib" / "site-packages"
        if local
        else None,
        home / ".astrbot" / "data" / "site-packages",
        Path("/Applications/AstrBot.app/Contents/Resources/app"),
        home / ".local" / "share" / "astrbot",
    ]
    return [c for c in candidates if c is not None and c.is_dir()]


for candidate in _astrbot_paths():
    if str(candidate) not in sys.path:
        sys.path.append(str(candidate))

FAILED: list[str] = []
PASSED = 0


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


def install_plugin_as_package(tmp_root: Path) -> None:
    """把插件按 AstrBot 的目录约定挂进临时目录。

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


async def main() -> int:
    """跑完回归断言。

    Returns:
        退出码。
    """
    import astrbot  # noqa: PLC0415
    from astrbot.core.star.session_plugin_manager import (  # noqa: PLC0415
        SessionPluginManager,
    )
    from astrbot.core.star.star import StarMetadata, star_map  # noqa: PLC0415
    from astrbot.core.star.star_handler import (  # noqa: PLC0415
        EventType,
        star_handlers_registry,
    )

    print(f"astrbot: {astrbot.__file__}  (版本 {getattr(astrbot, '__version__', '?')})")

    tmp_root = Path(tempfile.mkdtemp(prefix="topic-router-entry-"))
    try:
        install_plugin_as_package(tmp_root)
        pkg_name = f"data.plugins.{PLUGIN_DIR.name}"
        entry_path = f"{pkg_name}.main"

        print("\n[1] 按实机方式导入 main（AstrBot 只导入它）")
        importlib.import_module(entry_path)
        router = importlib.import_module(f"{pkg_name}.router")

        ours = [
            h
            for h in star_handlers_registry
            if "topic_router" in getattr(h, "handler_full_name", "")
            and h.handler_name in {"handle_message_v090", "hook_llm_request_v090"}
        ]
        check("注册表里能查到消息入口与请求钩子", len(ours) == 2, str(len(ours)))

        print("\n[2] handler 必须挂在框架登记插件元数据的那个模块路径下")
        wrong = [h.handler_name for h in ours if h.handler_module_path != entry_path]
        check(
            "两个 handler 都归位到 main 模块路径",
            not wrong,
            f"仍在别处: {wrong}",
        )
        check(
            "router 路径下没有残留的匿名元数据（否则会被静默丢弃）",
            f"{pkg_name}.router" not in star_map,
        )

        # star_manager 导入后才建元数据；这里补上它做的那一步（名字来自 metadata.yaml）
        if entry_path not in star_map:
            star_map[entry_path] = StarMetadata(
                module_path=entry_path,
                name=PLUGIN_DIR.name,
                activated=True,
            )
        meta = star_map[entry_path]
        check("插件元数据有名字（不是 None）", bool(meta.name), repr(meta.name))
        check(
            "面板能列出本插件注册的 handler（star_handler_full_names 非空）",
            len(star_handlers_registry.get_handlers_by_module_name(entry_path)) >= 2,
        )

        print("\n[3] waking_check 第一道：get_handlers_by_event_type")
        candidates = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
        names = [h.handler_name for h in candidates]
        check("消息入口在第一道筛选后仍然存在", "handle_message_v090" in names, str(names))
        check("/topicweave 在第一道筛选后仍然存在", "command_topic_v090" in names, str(names))

        print("\n[4] waking_check 第二道：SessionPluginManager.filter_handlers_by_session")
        import astrbot.core.star.session_plugin_manager as spm  # noqa: PLC0415

        async def fake_get_async(**_kwargs):
            """不碰真实 DB：会话级插件配置一律当作"没配置"。"""
            return {}

        spm.sp.get_async = fake_get_async

        class FakeEvent:
            """只提供会话过滤用得到的字段。"""

            unified_msg_origin = "default:FriendMessage:entry-dispatch-probe"

        mine = [h for h in candidates if "topic_router" in h.handler_full_name]
        kept = await SessionPluginManager.filter_handlers_by_session(FakeEvent(), mine)
        kept_names = [h.handler_name for h in kept]
        check("入口与 /topicweave 都通过了会话级过滤", len(kept_names) == len(mine), str(kept_names))
        check("入口在最终派发名单里", "handle_message_v090" in kept_names, str(kept_names))

        print("\n[5] 注册表自检接口（启动日志用的那份报告）")
        report = router._registration_report()
        check("自检结论为通过", bool(report.get("ok")), str(report.get("problems")))
        check(
            "自检看到元数据名字",
            report.get("metadata_name") == PLUGIN_DIR.name,
            repr(report.get("metadata_name")),
        )

        print("\n[6] 反证：把入口改回 router 路径 → 会话过滤静默丢弃")
        entry_md = next(h for h in ours if h.handler_name == "handle_message_v090")
        original = entry_md.handler_module_path
        try:
            entry_md.handler_module_path = f"{pkg_name}.router"
            star_map[f"{pkg_name}.router"] = StarMetadata(
                module_path=f"{pkg_name}.router"
            )
            broken = await SessionPluginManager.filter_handlers_by_session(
                FakeEvent(),
                [entry_md],
            )
            check(
                "复现成功：匿名元数据名下的入口被丢弃",
                broken == [],
                f"kept={[h.handler_name for h in broken]}",
            )
            check(
                "复现成功：自检能报出 name 为空",
                bool(router._registration_report().get("problems")),
            )
        finally:
            entry_md.handler_module_path = original
            star_map.pop(f"{pkg_name}.router", None)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))

"""插件入口。

AstrBot 按 `data.plugins.<目录名>.main` 加载插件，所以这里只做转出，实现都在 `router.py`。
拆成两个文件是为了让核心逻辑可以被离线测试直接 import，不用起整个 AstrBot。

## 为什么要在这里"认领 handler"（v0.9.2）

AstrBot 用一个 **模块路径 -> 插件元数据** 的字典（`core/star/star.py` 的 `star_map`）
把 handler 归到插件名下。插件的元数据是以 `data.plugins.<目录名>.main` 为键登记的
（`star_manager` 只导入 `main`），而 handler 的 `handler_module_path` 取的是
`handler.__module__`——本插件所有 handler 都定义在 `router.py` 里，于是键是
`...router`。

两边对不上时，框架里那条按 `__init_subclass__` 自动登记的**匿名**元数据会把
handler 接住（`name=None`），插件面板因此看不到任何指令/钩子；更致命的是
`SessionPluginManager.filter_handlers_by_session`（AstrBot 4.27+）有一行

    if plugin.name is None:
        continue

——它会把所有 `AdapterMessageEvent` handler **静默丢掉**，不写日志、不报错。
实测后果：`handle_message_v090`（消息入口）和 `/topicweave` 从 v0.8.x 起一次
都没被派发过，聚合能力整层空转；只有 `on_llm_request` 钩子（不走这条过滤）还活着，
所以功能看起来"没坏"。详见 `docs/DESIGN.md` 的 v0.9.2 小节。

修法：在 `main` 导入时把这些 handler 的 `handler_module_path` 归位到本模块，
顺手清掉那条匿名元数据。这样 AstrBot 走的是"元数据 = main、handler 也 = main"的
正常形态，面板、指令权限、会话级插件开关、消息派发全部对得上。

归位失败（框架改了内部结构）不会让插件起不来：`_adopt_handlers()` 返回 0，
`router.on_loaded_v090` 会在启动日志里大声警告并把注册表现状打出来。
"""

from __future__ import annotations

from .router import PLUGIN_NAME, PLUGIN_VERSION, TopicRouterPlugin

__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "TopicRouterPlugin"]

_ROUTER_MODULE_PATH = f"{__package__}.router"


def _adopt_handlers() -> int:
    """把 `router.py` 里注册的 handler 归到本模块路径下。

    只改 `handler_module_path` 这一个字段，不动 `handler_full_name`、事件类型、
    过滤器或函数对象——派发语义因此保持不变，变的只是"框架认为它属于哪个插件"。

    Returns:
        归位的 handler 数量；框架内部结构对不上时为 0（不影响插件加载）。
    """
    try:
        from astrbot.core.star.star import star_map, star_registry
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception:  # noqa: BLE001 - 离线测试/老框架没有这些模块，正常降级
        return 0

    moved = 0
    try:
        for handler in list(star_handlers_registry):
            if getattr(handler, "handler_module_path", "") == _ROUTER_MODULE_PATH:
                handler.handler_module_path = __name__
                moved += 1
    except Exception:  # noqa: BLE001
        return 0

    # `Star.__init_subclass__` 在 class 定义时就登记了一条以 `router` 为键的匿名
    # 元数据。handler 归位后它就没有用户了，留着会让面板/插件列表多出一条无名条目。
    ghost = star_map.pop(_ROUTER_MODULE_PATH, None)
    if ghost is not None:
        try:
            star_registry.remove(ghost)
        except ValueError:
            pass
    return moved


ADOPTED_HANDLERS = _adopt_handlers()
"""被归位到本模块的 handler 数量（自检用，正常应为 5）。"""

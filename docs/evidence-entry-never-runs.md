# 入口零执行 · 定案证据（2026-10-14）

> **v0.9.2 补充（2026-10-25）**：本文记录的**现象**（入口一次都没进函数体）是准确的，
> 但当时给出的**原因**（优先级被 `sys.maxsize` 的内置会话控制抢走）不成立。真因是
> handler 注册在 `router` 模块路径、插件元数据却登记在 `main`，框架命中的是
> `Star.__init_subclass__` 留下的匿名元数据（`name=None`），被 AstrBot 4.27+
> `SessionPluginManager.filter_handlers_by_session` 静默丢弃。修法与复现见
> `docs/DESIGN.md` §5.1 与 `tests/reproduce_entry_dispatch.py`。本文其余证据仍可作为
> "为什么当时看不出问题"的排查史料。

## 结论

**`handle_message_v017`（消息事件主入口）一次都没有进入函数体。**
本插件的全部判定都由 `on_llm_request` 钩子路径完成。

## 决定性证据：`trace.log`

文件：`/vol1/@appdata/astrbot/data/plugin_data/astrbot_plugin_topic_router_pro/trace.log`
（v0.8.2 的 `_trace()` 落盘探针，写在 handler 第一行、直接写文件、不经过日志系统）

统计结果（14 行 / 2762 字节）：

| 标记 | 行数 | 说明 |
| --- | --- | --- |
| `HOOK` | 7 | 请求钩子正常触发 |
| `VERDICT` | 7 | 判定层正常产出 |
| **`ENTRY`** | **0** | **入口从未进入函数体** |
| 合计 | 14 | 与总行数精确持平，无遗漏 |

**第二个独立证据源**：7 条 `VERDICT` 行的 `via=` 字段**全部是 `hook`**，
没有一条 `via=event`。该字段由判定函数内部写入，与 `ENTRY` 相互独立。

## 为什么这不是"取证失真"

1. `ENTRY` 探针用 `path.open("a")` 直接写文件，**不经过 logger** →
   不受日志级别、格式、WebUI 命令回显影响；
2. 同文件里 `HOOK`/`VERDICT` 都写出来了 → 证明探针机制本身在跑、配置已生效
   （`trace_enabled=True`，`log_level=null` 跟随全局）；
3. 入口的 `logger.info` 是**无条件**的（router.py:1200），且框架的插件日志
   格式化器带 `[%(source_file)s:%(source_line)d]`（log.py:426）→ 若入口执行过，
   `info.log` 里必有该行。此前多轮前缀锚定统计均为 0 条。

## 排除项（本轮用 API + 源码核实）

| 假设 | 结论 | 依据 |
| --- | --- | --- |
| `trace_enabled` 没生效 | 排除 | 配置实测为 `True`；`HOOK`/`VERDICT` 已落盘 |
| 插件日志级别被调高 | 排除 | `log_level = null`（跟随全局） |
| 第三方插件 priority > 60 | 排除 | API 查得 8 个插件，最高 `continuous_message=50` |
| 防抖插件（50）挡住入口 | 排除 | 真实注册表实测 `priority 60` 排在 50 之前 |
| 框架内置 maxsize handler 挡住 | **机制成立，现场未证实** | `builtin_stars/astrbot/main.py:44` 的 `handle_session_control_agent` 挂在 `sys.maxsize` 且会 `stop_event()`；但触发条件是在 `USER_SESSIONS` 中，需先有"仅 @ / 仅唤醒词"的 60 秒会话等待，现场时间线（间隔 1270 分钟、进程已重启）不支持 |
| 注册失败 | 排除 | 注册表隔离复现 5 个 handler 全部注册成功 |
| 会话级禁用 / 白名单 / `plugin.activated` | 排除 | `plugin_set=["*"]`、`inactivated_plugins` 不含本插件 |

## 因此

根因定位到"**入口在进入函数体之前就被挡掉**"，但**究竟是哪一层挡的仍未确认**。
已确认的是：`priority=60` 这个修法对它无效（对 `sys.maxsize` 无效），
而钩子路径 41 次触发、7 次落盘判定全部正常。

## 待办

1. 把判定主战场移到 `on_llm_request`（方案 B）——钩子**能拿到事件对象**
   （`has_event=True`，`args=['WeixinOCMessageEvent','ProviderRequest']`），
   判定输入齐备，且不在派发循环内，不受任何优先级影响；
2. 修正 `router.py` 里"钩子路拿不到事件对象"的过时前提；
3. 入口路径保留但降级为可选快通道，并明确其不可靠性。

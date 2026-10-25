# 更新日志

## v0.9.2

**修「消息入口从未被派发」：聚合层自 v0.8.x 起一直空转，原因在插件与框架的模块路径错位。**

### 一、线上现象

- 消息入口 `handle_message_v090` 自插件加载以来执行 0 次（全量日志 31 万行无一条
  "被调用"），只有 `on_llm_request` 钩子在兜底判定，所以功能看起来正常；
- `/topicweave` 指令同样从未生效；
- 聚合统计 `stats.json` 的 `saved_at` 长期停在旧时间，`items_total`/`flush_total`
  恒为 0，`by_channel` 恒为空；
- 跨平台一致（weixin 私聊 / webchat / QQ Official），属部署级现象。

### 二、根因

AstrBot 用 `star_map`（模块路径 → 插件元数据）把 handler 归到插件名下：

1. 插件入口是 `main.py`，框架只以 `data.plugins.<目录名>.main` 登记插件元数据；
2. 本插件所有 handler 都定义在 `router.py`，`handler_module_path` 因此是
   `data.plugins.<目录名>.router`；
3. `Star.__init_subclass__` 在 `class TopicRouterPlugin` 定义时按 `router` 模块路径
   登记了一条**匿名**元数据（`name=None`），框架查 handler 时正好命中它；
4. AstrBot 4.27+ 的 `SessionPluginManager.filter_handlers_by_session` 里有

   ```python
   if plugin.name is None:
       continue
   ```

   —— 匿名元数据名下的 handler 被**静默丢弃**，不写日志、不抛异常。

于是：所有 `AdapterMessageEvent` handler（消息入口、`/topicweave`）永远进不了
`activated_handlers`；`OnLLMRequestEvent` 钩子不走这条过滤，所以还活着。

同一根因的旁证：插件详情接口里本插件只有 `page` 组件，`command`/`hook` 组件全缺
（`star_handler_full_names` 为空）——因为框架按 `main` 路径去查 handler，一条也查不到。

> v0.9.1 的抢占检测没能暴露它，反而被它吃掉了：检测条件是"`activated_handlers`
> 存在且**包含**本插件"，而本插件恰恰永远不在名单里，于是条件永假、零告警。

### 三、修法

1. `main.py` 导入时把 `router` 名下的 handler 的 `handler_module_path` 归位到
   `main`，并清掉那条匿名元数据（`_adopt_handlers()`）。只改归属字段，不动事件类型、
   过滤器、函数对象，派发语义不变；
2. `on_loaded_v090` 增加**入口注册自检**：启动日志里直接给出
   `入口自检通过 | module=... | 元数据=... | filters=...`，不通过则 WARNING 列出原因；
3. `hook_llm_request_v090` 增加另一半抢占判据：派发计划里有别的 handler 却没有我们
   → 记 `ENTRY_MISSING` 告警（10 分钟限流一次），把"静默失效"变成可见故障。

### 四、验证

- 新增 `tests/reproduce_entry_dispatch.py`：按 `data.plugins.<目录名>.main` 真实导入，
  断言 `SessionPluginManager.filter_handlers_by_session` 不再丢弃入口；
- 五套既有测试（207 / 16 / 9 / 16 / 23）保持全绿。

## v0.9.1

**修正抢占误报：合成事件不再被当成"被更高优先级插件截走"。**

### 一、线上现象

v0.9.0 在真机上出现过一条 `aggregate.preempted`：

```
aggregate.preempted umo=default:FriendMessage:... priority=1000
```

但同一条消息的统计里 `preempted_total` 可能为 0（插件重载后内存清零），
且没有任何 `aggregate.start` 记录——说明这条 LLM 请求根本不是一次新的
消息派发。

### 二、根因

`OnLLMRequestEvent` 钩子不只在新消息到达时触发，以下场景同样会触发：

- 主动回复 / 主动聊天（`provider_ltm_settings.active_reply`）；
- 其它插件直接调用 `event.request_llm()`；
- Agent 内部重放、会话等待器把事件复制后重新入队。

这些事件的 `SEEN_ATTR` 当然不存在，但 `check_preempted()` 只看了
"消息有内容 + 没有标记"，于是把正常行为报成了抢占。

### 三、修法

抢占的**必要条件**改成：这条事件真的在本次派发计划里，且计划中包含本插件的
消息 handler。`waking_check` 会在派发前把 `activated_handlers` 写进事件
（已按优先级降序、已按会话插件过滤），所以：

1. `activated_handlers` 缺失（合成事件）→ 不报抢占；
2. 列表里没有 `handle_message_v090`（本就不该派发给我们）→ 不报抢占；
3. 列表里有、但我们没执行（`SEEN_ATTR` 缺失）→ 才报抢占。

告警同时带上直接证据，下一次不用再猜：

```
aggregate.preempted ... dispatched=7 position=3
  prior=handle_session_control_agent@9223372036854775807
  msg_id=...
```

- `position`：本插件在派发计划里的下标；
- `prior`：紧挨在本插件前面的最多 3 个 handler 及其优先级；
- `dispatched`：本次派发计划里的 handler 总数。

### 四、测试

- 抢占用例显式构造 `activated_handlers`，覆盖真实派发计划；
- 新增"合成事件不计数"用例；
- `tests/run_tests.py`：**207 项**，其余既有用例不变。

## v0.9.0

**把「消息聚合」（`astrbot_plugin_msg_aggregator`）内建为判定流水线的第一阶段。**

### 一、为什么整合

上一版按「每条消息」判定。用户把一句话拆成三条发时会出现两个问题：

1. 同一件事被判定三次，`turns` 被推快三倍，话题边界彻底失真；
2. 后两条消息各自触发一次 LLM 调用，既费 token 又让回复割裂。

独立插件 `astrbot_plugin_msg_aggregator` 已经解决了缓冲与重构，但它和话题路由器
是两个 Star 插件，谁先拿到消息取决于优先级——这是不可控的环境输入。所以本版把它的
核心（配置、状态机、自适应等待、附件保活、统计、页面）整体迁入 `aggregation/`，
使「先聚合、再判定」成为同一个 handler 内的确定性流程。

### 二、流水线

```
消息到达
  └─ handle_message_v090（priority=1000）
       ├─ 聚合开启：缓冲/自适应等待/重构 → on_settled(合并文本)
       │    └─ 在合并文本上判定一次 → hint/auto 动作挂起
       └─ 聚合关闭：直接判定（v0.8.6 行为）
  └─ hook_llm_request_v090（请求钩子）
       ├─ 兜底：入口没被派发（被抢占）时仍判定，并写 aggregate.preempted
       └─ 执行 hint / auto 的请求改写
```

关键点：

- **消息入口优先级从 60 提到 1000**（插件层顶格）。聚合必须尽最大可能先于普通插件
  看到消息，否则别人先 `stop_event()` 后连缓冲的机会都没有；1000 仍不提供绝对保证，
  所以保留请求钩子兜底与抢占告警；
- **判定只做一次，且发生在合并文本上**。`DETECTED_ATTR` 标记 + 原有消息 ID 去重
  双层保证钩子不会重复判定；
- **补充消息被静默**：非 Owner 事件不再流向下游，用户只会收到一次回复；
- **`aggregation.enabled=false` 可完整退回 v0.8.6 语义**（无额外等待、逐条判定）。

### 三、随迁功能

| 功能 | 说明 |
| --- | --- |
| 三档预设 | `fast` / `balanced`（默认）/ `patient`，自适应等待与总上限 |
| 输入状态感知 | 支持平台（如 NapCat `input_status`）暂停计时，`max_typing_wait` 兜底 |
| 撤回过滤 | 等待期内撤回的消息不再进入合并结果（仅支持撤回通知的平台） |
| 附件保活 | 本地临时图片在事件结束前复制到插件缓存，避免第 2+ 条消息的图丢失 |
| 群聊分桶 | 按「会话 + 发送者」分桶，同群不同人不会互相合并 |
| 指令打断 | 指令到达时先把该桶结算完再放行，超时放弃该轮 |
| 统计与页面 | 合并率、渠道/丢弃原因分布、延迟直方图、最近轮次；页面复用 AstrBot 插件视图 |
| 抢占检测 | 入口没跑时 `aggregate.preempted` + `preempted_total`，把静默失效变成可见告警 |

### 四、配置

新增 `aggregation` 对象分组，默认 `enabled=true`（整合后的默认形态）。字段与独立插件
逐项对齐；`aggregation.enabled=false` 即恢复逐条判定。

`astrbot_version` 提高到 `>=4.26`：聚合用到的能力（如 `input_status` 探测、附件
`untrack` 降级路径）以 4.26 为基线。

> ⚠️ **升级注意**：如果同时装了独立版 `astrbot_plugin_msg_aggregator`，请先卸载它。
> 两者都取 priority 1000 缓冲消息，同时启用会互相抢事件；本插件已内建同样的能力，
> 不需要再装独立版。

### 五、测试

- `tests/run_tests.py`：**206 项**（新增 `[19]` 聚合整合 24 项：两句话一轮、事件重构、
  钩子不重复判定、抢占兜底、配置默认值一致性、关闭回退）；
- `tests/test_portability.py`：16 项；
- `tests/reproduce_priority_race.py`：16 项（优先级基线更新为 1000，反向用例改用 1100）。

## v0.8.6

**长时间未回复不再凭时间硬判新话题，改为请模型判「下一句是否延续」。**

### 一、为什么改

旧行为是：距上一条消息超过 `time_gap_minutes`（默认 45 分钟）且字面重叠低，**直接判
新话题**（置信度恰好 0.8，不调模型）。

这个规则有两个问题：

1. **把时间当成强证据，但它其实很弱**。真实场景里大量存在「隔一小时回来接着做同一件事」
   （「那个 bug 我复现了」「继续刚才那个脚本」）。凭时间硬切，切掉的正是用户正在做的事；
2. **而误判代价是不对称的**：把延续判成新话题 = 用户觉得机器人失忆，代价最大。
   这条规则恰好制造的是这一类误判。

还有一个隐藏的脆弱点：它的置信度**恰好等于 0.8**，而 `auto_threshold` 默认也是 0.8、
门槛是 `>=`。也就是说"能不能自动切换"悬在一个等号上——`auto_threshold` 一旦调到
0.95（这正是为避免卡边值而推荐的），长时间未回复的自动切换就会**静默失效**。

### 二、新行为

```
距上一条消息 ≥ time_gap_minutes
  ├─ 新消息与当前话题字面高度重叠 → 直接判延续（不花模型调用）
  └─ 否则 → needs_llm=True，请模型判「这一句是不是接着上一件事」
             ├─ 模型说延续        → 延续
             ├─ 模型说新话题且确定 → 新话题
             └─ 模型不可用/超时    → 按延续（失败即延续）
```

关键的一步是**把"隔了多久"写进提示词**（`build_user_prompt(idle_minutes=...)`）：

```
距上一条消息已过 120 分钟（隔得久不代表换话题：如果这一句是在接着做同一件事，
就算隔了很久也判 continue）
```

不写这句，模型手上只有语义相似度、没有新信息，等于白问一次。

### 三、模型失败时的退路：按延续

模型超时/报错/未配 provider 时，按本项目铁律**失败即延续**。理由：模型不可用是
**暂时性故障**，为此切开用户上下文，代价比"少切一次"大得多。

### 四、连带修正（改动过程中发现的）

**① 提示词新增一行会污染兜底抠取。** `_hook_user_message` 在拿不到事件时用
`prompt.rsplit("用户最新消息：", 1)[-1]` 抠正文，而新加的说明行带**句号**、又排在标记
**之前**，导致抠出的"用户消息"里混进了说明文字（约 60 字，恰好超过 6 字门槛，会悄悄
污染一次判定）。已改成不影响标记切分的写法，并加注释说明约束。

**② 配置说明里的错误承诺。** 我一度在 `time_gap_minutes` 的说明里写"填 0 或负数可关闭
检查"——**实测是错的**：填 0 会让条件恒真，导致**每条消息**都触发一次模型判定（费 token）。
已改为正确说明。

### 五、测试

新增 `[22] 长时间未回复`（9 项断言）：模型判延续就是延续、模型确定才算新话题、
**模型失败按延续且不新建对话**、提示词确实带上间隔与"隔得久不代表换话题"、
兜底抠取不被说明污染。

`[2]` 里"冷启动 + 低重叠 = 新话题"这条旧断言按新行为改写，并补了
"隔很久但高度重叠 → 直接延续、不花模型调用"。

测试：178 + 16 + 9 + 16 + 21 项全部通过。

## v0.8.5

**换话题提示默认关闭；新增「调试模式」（空跑 + 详细日志，默认关闭）。**

### 一、`new_conversation_notice` 默认值 true → false

原来 `auto` 模式真的切换对话时，机器人会回一句提示。默认开启有两个问题：

1. **判定的准确性从未在真实会话里标定过**（`docs/DESIGN.md` §11 把"手工标注真值"列为
   第一优先待办）。在这种状态下默认往用户脸上打提示，是在为一个未验证的判断背书；
2. 静默切换本身就是可接受的行为——用户感受到的是"机器人翻篇了"，而不是被弹一句机械提示。

需要提示的用户可以自己开。**同时修掉一处文案错误**：配置说明写的是
「已开始新话题」，而代码实际发的是「检测到新话题：<话题名>，已为你开一段新对话。」，
两者对不上，已更正。

### 二、新增 `debug_mode`：调试模式（默认关闭）

语义只有两条，刻意保持简单：

1. **只看不做**：一切会改变上下文的动作一律不执行——`auto` 不新建对话、`hint` 不注入
   提示，只在日志与 `trace.log` 里写「本想做什么」（`DRYRUN` 行）；
2. **看得更细**：把启发式得分、归一化文本、去重键、是否调用了模型、模型原始置信度、
   最终结论与理由全量落盘（`DEBUG` 行）。

**判定本身照常执行**——不然就没得观察了。

为什么需要它：`auto` 会真的动用户上下文，而判定质量尚未标定。调试模式让人能在
**完全不动真实会话**的前提下验证判定准不准、阈值该调到多少——这正是当前最需要的能力。

实现上只在 `_act()` 的开头加了一道闸门。选这个位置是因为它是所有"改变现状"的动作
（`auto` 切换、`hint` 注入）的**单点收口**，加一处就能全覆盖，不必在每个分支里各写一遍。

`DEBUG` 明细**只写 `trace.log`、不写 `info.log`**：逐条判定的原始输出量很大，
混进主日志会把正常日志淹掉。

### 三、测试

新增 `[21] 调试模式`（9 项断言），覆盖：

- 不新建对话 / 不清空上下文 / 不发提示；
- 判定仍然执行（`turns` 被推进）；
- `DEBUG` 行与 `DRYRUN` 行都落盘，且 `DEBUG` 含 `heuristic=` / `llm_called=` 字段；
- **`hint` 模式同样被抑制**（同一闸门覆盖 `auto` 与 `hint`，别只顾一个）；
- **对照组**：同样输入关掉调试模式 → 确实切换了对话（证明差异源自调试模式，
  而不是用例写错了）。

既有 `[5] auto 模式` 里"向用户提示了换话题"那条断言，改为显式开启
`new_conversation_notice`——它验证的是"通知"这条路径，不该依赖默认值。

测试：167 + 16 + 9 + 16 + 21 项全部通过。

## v0.8.4

**入口零执行定案：判定主战场迁到 `on_llm_request` 钩子。**

### 一、定案证据（2026-10-14，AstrBot 4.28.1 实机）

用 v0.8.2 的 `_trace` 落盘探针（写在 handler 第一行、直接写文件、不经过 logger，
不受日志级别/格式/命令回显影响）取到的 `trace.log`：

| 标记 | 行数 | 含义 |
| --- | --- | --- |
| `HOOK` | 7 | 钩子路正常 |
| `VERDICT` | 7 | 判定正常产出 |
| **`ENTRY`** | **0** | **`handle_message_v017` 从未进入函数体** |
| 合计 | 14 | 与总行数持平，无遗漏 |

第二个**独立**证据源：7 条 `VERDICT` 行的 `via=` 字段**全部是 `hook`**，
没有一条 `via=event`。两个标记互相印证——**入口是死的**。

排除项（用 OpenAPI + 源码逐一核实）：

- `trace_enabled` 未生效 —— 排除（探针已落盘，配置实测为 `True`）；
- 插件日志级别被调高 —— 排除（`log_level = null`，跟随全局）；
- 第三方插件 priority > 60 —— 排除（8 个插件，最高 `continuous_message = 50`）；
- 防抖插件挡住入口 —— 排除（真实注册表实测 60 排在 50 之前）；
- 注册失败 / 会话级禁用 / 白名单 / `plugin.activated` —— 排除。

**根因收窄为"入口在进入函数体之前被挡掉"，具体哪一层仍未确认。**
但有一点已经确定：`priority=60` 这条路走不通——框架内置
`handle_session_control_agent` 挂在 `sys.maxsize`（`builtin_stars/astrbot/main.py:44`），
**60 永远追不上**，任何"再抬一点优先级"的思路都是错的。

### 二、改动：判定主战场改为请求钩子

不再把判定押在消息事件路径上。该路径要同时穿过「优先级排序」「`is_stopped()`
截断」「第三方插件静默」三道关，任何环境都可能被拦；而 `on_llm_request`
不在那个循环里。

关键依据：**钩子能拿到事件对象**。`call_event_hook` 是
`await handler.handler(event, *args)`，实机为
`args=['WeixinOCMessageEvent','ProviderRequest']`、`has_event=True`。
这一条推翻了代码里长期存在的旧前提（"钩子路拿不到事件对象，只能从 prompt 抠"）。

具体改动：

1. `_extract_user_message(req)` → **`_hook_user_message(event, req)`**：
   优先直接从事件读消息，只有老框架拿不到事件时才退回从 prompt 抠
   （那条路依赖提示词格式，任何改写 prompt 的插件都会让它失效）；
2. 两路**共用同一把去重锁**：钩子既然也能算出 `消息ID|时间戳`，就用同一个权威键，
   不再退化成"按文本猜"；没有事件时才走文本兜底；
3. `_umo_from_request` 降级为兜底（有事件时一律用 `_umo(event)`）；
4. 修掉文件头那句已失效的 `has_event=False` 记载，并把定案写进
   `hook_llm_request_v017` 的文档串与 `MESSAGE_PRIORITY` 的注释；
5. `HOOK` 探针加 `role=primary` 标记，便于事后区分主/辅路径。

消息事件入口**保留**为"可选快通道"（它更快、更早拿到事件），但**不得再作为
判定的唯一依赖**——实测它可能一条都不执行。

### 三、新增测试

- `[19] 钩子同时拿到事件与请求（4.28.1 形态）`：判定、指纹、重复不重判、重发算新一轮；
- `[20] 钩子只有请求、没有事件（老框架兜底路径）`：prompt 标记抠取与整段兜底；
- `[7]` 跨路去重与 `[17]` 的既有断言全部保持通过（**重发必须算新一轮**这条铁律没被破坏）。

测试：157 + 16 + 9 + 16 + 21 项全部通过。

### 四、取证材料

完整证据链见 [`docs/evidence-entry-never-runs.md`](docs/evidence-entry-never-runs.md)。

## v0.8.3

**打包产物改到插件工作区的 `dist/` 目录，不再写到桌面。**

### 为什么改

`build_zip.py` 原先固定往桌面写。桌面在工作区之外，虽然"能稳定留下"，但有两个坏处：

1. 交付物散落在工作区外，版本一多就不好找，也容易和别的东西混在一起；
2. 打包脚本把一个**工作区外的绝对路径**硬编码在代码里，换台机器就写到别人的桌面上。

而当初之所以不敢放工作区内，是因为两条真实的清理/打包陷阱（见 `default_output()` 的注释）：

- 放在 `F:\deep seek` 根下或插件目录根下，zip 会被工作区的清理规则扫走
  （文件名以 `astrbot_` 开头、或就在插件目录里，实测都会消失）；
- 放在插件目录根下，重新打包会把上一个包一起压进新包里。

`dist/` 同时绕开这两条：它在 `.gitignore` 里（`dist/`），也被 `tests/run_tests.py` 的
`ignore_patterns(..., "dist")` 排除；同时它又不是仓库根目录，不会被清理规则命中。

### 改动

- `build_zip.py` 的 `default_output()` 改为 `<插件目录>/dist/<包名>-<版本>.zip`，
  并更新模块文档串与 `default_output()` 的注释；
- `tests/verify_zip_install.py` 改为**直接问** `build_zip.default_output()` 拿路径，
  不再自己拼一份桌面路径——两处路径逻辑合一，以后改打包位置不会再出现"改了打包器
  却忘了改测试"；
- README、CONTRIBUTING、docs/DESIGN.md 里"输出到桌面"的说明同步更正；
- 交付包仍然可以直接在命令行传路径覆盖（`python build_zip.py <输出路径>`）。

测试：150 + 16 + 9 + 13 + 21 项全部通过。

## v0.8.2

**更正 v0.8.0 的错误根因 + 修一处真的会算错轮次的去重缺陷 + 新增不依赖日志的落盘取证。**

### 一、v0.8.0 的根因归错了（重要更正）

v0.8.0 把"消息入口一次都不执行"归因于「消息防抖动插件（priority=50）先跑并静默了事件」。
**这个归因是错的**：本项目新增的 `tests/reproduce_priority_race.py` 在**真实 AstrBot 注册表**
上验证了执行顺序，priority=50 的插件**排在**本插件（60）**之后**，根本没有机会先静默事件。

顺带发现：文档 §3.3 那种"隔离复现"只读取注册表里的 `priority` 字段值，
**验证不了真实派发循环里的相对顺序**——这正是错误根因能存活至今的原因。

更正后的结论：

- priority 60 **确实**比 50 高，这条修复本身有效，保留；
- 但 60 只保证"比 50 高"，**不保证比所有插件都高**。真实环境若仍有入口零调用，
  应优先怀疑存在**优先级 > 60** 的第三方插件（`tests/reproduce_priority_race.py` 已把
  "priority 70 才能挡住入口"这一反向结论也钉成用例）；
- 事件被静默时 `on_llm_request` 钩子**照样触发**——这个形态已被复现，所以
  「只有钩子日志、没有入口日志」是真实可发生的，不需要用"取证失真"来解释。

涉及更正的注释与配置说明：`router.py` 的 `MESSAGE_PRIORITY`、`hook_llm_request_v017`
文档串、`_conf_schema.json` 的 `detect_in_hook` 说明。

### 二、修去重缺陷：钩子先判、事件路后到会算两轮

`_detect` 的两路去重是**不对称**的：钩子路拿不到事件对象，`_batch_key()` 必然返回 `None`，
所以它判完**只写内存里的** `RUNTIME.last_detect`，从不写 `state.last_detected`。
于是"钩子先判、事件路后到"时事件路认不出来，同一条消息被判两次，`turns` 直接 +2。

修法：钩子路额外把指纹落到 `TopicState.last_detected_hook`（**持久化**，重启后仍有效）。

> 修这一版时踩了自己的坑：第一版实现拿这个指纹去**拦**事件路，结果把"用户重发同一句话"
> 也吞掉了（`turns` 少算一轮），被 `tests/run_tests.py` 的 `[7]` 用例当场抓住。
> 最终语义：**事件路带平台消息 ID，是权威的消息身份，绝不按文本拦**；文本级跳过只对
> 钩子路有意义。这条已写进 `_detect` 的注释，避免以后有人再"顺手优化"回去。

### 三、新增落盘取证（`trace_enabled`）

判断"某个 handler 到底有没有被调用"时，日志有三个不可靠之处：

1. **级别**：入口日志是 INFO，日志级别调到 WARNING 以上就什么都看不到；
2. **格式**：AstrBot 会给插件日志加注 `[文件名:行号]`，不同版本格式不同，
   用正则 grep 很容易"看起来是 0 次"，实际是模式写错被筛掉了；
3. **污染**：WebUI 的日志面板会把**你敲的命令本身**也记进 `info.log`，
   于是 `grep -c` 会统计到自己，得到假阳性（v0.8.0 的排查就栽在这里）。

新增配置 `trace_enabled`（默认关闭）。开启后在
`plugin_data/astrbot_plugin_topic_router_pro/trace.log` 追加三类行，**不受以上任何一条影响**：

```
ENTRY   ... umo=... | msg='...' | msg_id=... | stopped=...   ← 消息入口被执行
HOOK    ... has_event=... | has_request=... | prompt_len=...  ← 请求钩子被触发
VERDICT ... NEW-TOPIC/CONTINUE | via=event|hook | turns=...   ← 判定走了哪条路
```

单文件封顶 512KB 后轮转为 `.log.1`，平时可关闭。

### 四、新增测试

- `tests/reproduce_priority_race.py`（13 项，**新文件**）：在真实注册表 + 真实
  `StarRequestSubStage` 派发循环上验证优先级竞争、事件静默后的 break 语义、
  钩子不受影响、以及 `plugin.activated` 过滤路径；
- `tests/run_tests.py`：新增 `[17] 跨路去重`（10 项断言，覆盖"同 ID 重复只算一轮 /
  事件先判钩子不重复 / **重发必须算新一轮** / 钩子指纹留痕"）与
  `[18] 排障证据落盘`（含"关掉开关不产生文件"）。

测试：150 + 16 + 9 + 13 + 21 项全部通过。

## v0.8.1

**新增完整技术文档 `docs/DESIGN.md`（约 700 行）**，并清理一处死代码。

技术文档内容：

1. **设计哲学**：误判代价不对称、"失败即延续"、"先观察后行动"三条铁律，以及它们如何直接
   推出默认模式与合并规则；
2. **判定原理**：相似度算法（含真实语料实测数值）、启发式九条优先级链、非对称合并规则、
   送进模型的提示词结构；`不拿字面不重叠当证据` 那次修正的完整数据（39→13 轮）；
3. **执行架构**：为什么需要消息事件 + 请求钩子两条路、优先级 60 的由来、两路去重键为何不同；
4. **六个实机踩坑**（本文档最有价值的部分）：消息入口被静默、异常连累其它插件、
   handler 拿不到 self、注册表按"模块名+函数名"复用、指令撞名、provider 配错静默失效——
   每条都写清症状、迷惑点、根因、修法；
5. **数据模型、配置项设计、测试与评测体系**（含"真实数据评测台"和它催生的修正）；
6. **疑难排查案例集**：把真实排查历程整理成"症状 → 定位手段"，附本项目积累的诊断手段清单；
7. **未来方向**：按价值排序的九项（手工标注真值、英文支持、话题名刷新、并行判定…）。

同时：

- 清理 `router.py` 里一处死代码（`_detect_key`，去重逻辑改为双键后已无引用）；
- README 的代码结构表与贡献章节指向技术文档。

测试：140 + 16 + 9 + 21 项全部通过。

## v0.8.0

**修一个从第一版起就存在的严重缺陷：消息入口可能一次都不执行（被别的插件静默）。**

### 故障现象

插件显示已加载、配置正确、日志里也有活动记录，但**聊天中完全没有任何话题判定**：
没有日志、没有状态文件、没有新话题提示，像没装一样。群聊正常，**私聊复现**。

### 根本原因（实机日志 + 代码级证据）

AstrBot 的处理器循环是这样的（`core/pipeline/process_stage/method/star_request.py`）：

```python
for handler in activated_handlers:
    if event.is_stopped():
        break        # 后面的处理器全部不执行
```

而注册表按优先级降序排序，**未声明优先级时默认为 0**（`core/star/star_handler.py`）。
装了「消息防抖动 / 连续消息合并」类插件时，它们以 `priority=50` 先执行，并在每条私聊消息上
把事件静默掉（`event._force_stopped = True`），于是默认优先级 0 的本插件**永远轮不到**。

实测数据：4 条消息（3 QQ + 1 微信），`hook_llm_request_v017` 触发 **4 次**，
`handle_message_v017` 触发 **0 次**；`topic_state.json` 始终不存在。

> 这也解释了为什么"日志看起来正常"：`on_llm_request` 这类钩子**不在那个循环里**，
> 照常触发，制造了"插件在工作"的假象。

### 两项修复

1. **消息入口与命令提升优先级**：`priority=60`（高于实测的防抖插件 50）。该处理器只判定与
   记录、不 `stop_event`、不发消息，提前执行不会干扰别人的合并逻辑；
2. **判定迁移到请求钩子（架构性修复，更重要）**：`hook_llm_request_v017` 现在也执行判定。
   它不在处理器循环内，**不受任何插件的优先级与事件静默影响**——即使将来出现优先级 100 的
   插件，判定依然稳定。

两条路通过去重互斥，不会重复判定：

- **事件路**（有事件对象）按 `消息ID|时间戳` 去重——用户重发同一句话仍算新的一轮；
- **钩子路**（只有 `ProviderRequest`）按提示词里的用户消息文本去重，且只跳过"事件路刚刚
  判过（10 秒内）"的文本，避免同一条消息被判两次。

请求里抠不出用户消息时（例如 `prompt` 不是判定器那种格式）钩子会安静跳过，不影响回复。

新增配置 `detect_in_hook`（默认开，强烈建议保持开启）。

### 测试

- 原用例 `[7] 只有 on_llm_request 被调用的路径` 重写为验证新架构：**钩子单独调用也能完成
  判定**、同一轮里挂起的 hint 动作就地执行、事件路已判过的消息钩子不重复判、用户重发同一句
  话仍算新一轮（这条用例正是本轮改去重时抓出 bug 的那条）；
- 139 项全部通过。

### 升级建议

**所有用户都应升级**。旧版本在这个插件组合下完全不可用，而日志上看不出问题。

## v0.7.0

**改名为「话题路由器 PRO」（内部标识 `astrbot_plugin_topic_router_pro`）。**

展示名与内部名一起改了。内部名变化会连带影响这些位置（都已同步）：

| 位置 | 变化 |
| --- | --- |
| 插件目录 | `data/plugins/astrbot_plugin_topic_router_pro/` |
| 导入路径 | `data.plugins.astrbot_plugin_topic_router_pro.router` |
| 状态文件 | `data/plugin_data/astrbot_plugin_topic_router_pro/topic_state.json` |
| 打包产物 | `astrbot_plugin_topic_router_pro-<版本>.zip` |
| 日志前缀 | `[astrbot_plugin_topic_router_pro]` |
| 市场展示名 | 话题路由器PRO |

⚠️ **这是一次不兼容变更，老用户需要重新装一次（见 README 的升级说明）**：

- 旧插件 `astrbot_plugin_topic_router` 与新版是**两个不同的插件条目**，会同时出现在插件列表里，
  必须先**卸载旧版**，否则两个都会加载、重复判定；
- 话题状态文件路径变了，旧的状态（每个会话的话题名/轮数）**不会自动迁移**，会从零开始建档
  （不影响任何对话内容）；
- 想保留旧状态，可在卸载前把
  `data/plugin_data/astrbot_plugin_topic_router/topic_state.json`
  复制成 `data/plugin_data/astrbot_plugin_topic_router_pro/topic_state.json`。

**仓库地址没有变**（GitHub 仓库名仍是 `astrbot_plugin_topic_router`），因为改仓库名需要仓库设置
权限；AstrBot 并不要求仓库名与插件名一致，所以不影响安装与更新。

## v0.6.1

**修掉 README 里一处死链，并补上 AstrBot 版本要求。**

- README 原来写"zip 从 Releases 下载"，但本仓库**没有发布任何 Release**（0 个 tag / 0 个 release），
  那个链接是 404。改为说明两种真实可行的取包方式：市场安装，或本地 `python build_zip.py`；
  并确认了 **GitHub 的「Download ZIP」产物也能直接装**（见下）。
- 实测三种压缩包结构都能被 AstrBot 正确识别（复刻上游 v4.28.1 的
  `_resolve_archive_root_dir` + `find_plugin_metadata_entry` 验证）：

  | 包结构 | 解析出的包根 | 结果 |
  | --- | --- | --- |
  | `build_zip.py` 产物（插件目录当根） | `astrbot_plugin_topic_router` | ✅ |
  | GitHub Download ZIP（仓库名-分支当根） | `astrbot_plugin_topic_router-main` | ✅ |
  | 文件直接放包根 | 空 | ✅ |

- **填上 `astrbot_version: ">=4.24"`**：之前一直留空，理由是"没验证过的下限不该乱填"。
  现在有依据了——`hint` 模式依赖的 `TextPart.mark_as_temp()` 是 4.24+ 才有，且 4.28.1 上已跑通。
  填上后，过老的 AstrBot 会在安装时就被拦下，而不是运行时莫名失败。

## v0.6.0

**已上架 AstrBot 插件市场；仓库地址迁移到 `xun-hen`；README 安装章重排。**

### 插件已上架市场

README 的安装部分重写：**方式 A = 从插件市场安装**（最简单，推荐），
**方式 B = WebUI 上传 zip**（离线/内网/指定版本），
**方式 C = 按部署类型装到对应目录**（Docker / Windows 桌面 / 飞牛 NAS / 源码，降为 C1~C4 子节）。

原来的编号有问题：A（上传）和 B（Docker）在同一个层级，但一个是"装法"、一个是"部署类型"，
读者按顺序看会乱。现在改成先讲装法、再讲"你的部署类型该放哪个目录"。

### 仓库地址迁移到 xun-hen

仓库已从 `3559413914-spec` 迁移到 **`xun-hen/astrbot_plugin_topic_router`**（旧地址 GitHub 会
重定向，但文档里不该再写旧地址）。同步更新：

- `metadata.yaml` 的 `repo` 与 `social_link`；
- README 里的 Releases 链接；
- 作者署名统一为 **[@xun-hen](https://github.com/xun-hen)**（寻痕）。

> 注意：`xunhen`（无连字符）是**另一个人的账号**，之前误用过这个名字，本版已全部纠正。

### 本地 git 身份

这个仓库里曾经有仓库级的 `user.name = 3559413914-spec`，会覆盖全局身份，导致提交作者一直是旧名字。
本版删掉该仓库级配置，之后的提交使用全局身份 `xun-hen`。

## v0.5.3

**修：`provider_id` 配错时判定模型静默失效（实机踩到）。**

实机日志（2026-10-11）暴露的问题：插件启动时打印

```
[话题路由器] 已加载 v0.5.1 | 模式 mark | 判定模型 deepseek/deepseek-v4-flash | ...
```

但同一份日志里 AstrBot 加载的 Provider 实际是 `deepseek/deepseek-flash`（没有 `v4`）。
这个 ID 不存在，于是 `context.llm_generate()` 抛 `ProviderNotFoundError`，被"失败即延续"
的兜底逻辑吞掉 —— 结果是**判定模型那层完全没工作，只剩启发式**，而日志上几乎看不出来
（只在 `_ask_llm` 里留一行 warning）。

三处修改：

1. **`_resolve_provider` 增加存在性校验**：配置的 ID 会先用
   `provider_manager.get_provider_by_id()` 查一遍；查不到就告警并**回退到当前会话模型**，
   不再静默失效。顺带把可用的 ID 一起打进告警，方便直接抄。
2. **启动日志主动告警**：`on_loaded_v017` 里比对"配置的 ID"与"可用列表"，不一致时打印
   `⚠ 配置的判定模型 'xxx' 不在可用列表里 …`。
3. **默认值改为留空**（`_conf_schema.json`）：留空 = 跟随当前会话模型，最不容易配错。
   原来硬写 `deepseek/deepseek-v4-flash` 是在没有对应 Provider 的环境里保证出错。

新增测试 `[15] provider_id 配错时的回退`：配错时回退到当前会话模型、配对时按配置使用、
留空时跟随会话，以及**回退之后模型判定确实执行了**（不是静默失效）。
测试 135 → 139 项。

## v0.5.2

- 修 CHANGELOG 的 Markdown 格式：v0.5.1 小节和 v0.5.0 标题之间补上空行（之前贴在一起，
  渲染出来会粘成一坨）；
- 打包器新增 `check_changelog_format()`：打包前检查首行标题、每个版本小节前后是否有
  空行、同一版本号是否重复出现、以及是否包含当前版本小节，不合格拒绝打包。

## v0.5.1

- 作者署名从 `dsh` 改为 GitHub 账号 **[@xun-hen](https://github.com/xun-hen)**（`metadata.yaml`
  的 `author` 字段与 README 同步）；插件市场上展示的作者名即取此字段。

## v0.5.0

**为发布到 AstrBot 插件市场做收尾：元数据补全、图标、贡献指南。**

### 元数据补全（`metadata.yaml`）

对照官方[插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)补齐可选字段——
必填字段（`name` / `desc` / `version` / `author`）本来就合法，缺的是让市场卡片能看的那批：

- `display_name: 话题路由器`（展示名，之前卡片上只有 `astrbot_plugin_topic_router`）；
- `short_desc`（卡片短描述，缺了会回退显示一大段 `desc`）；
- `tags`（市场分类与搜索用）；
- `social_link`；
- `desc` 改成多行，写清特性。

**刻意没填 `astrbot_version`**：官方格式是 PEP 440（如 `">=4.17.0"`），但本插件只在
AstrBot 4.28.1 上验证过，`hint` 模式依赖的 `TextPart.mark_as_temp()` 是 4.24+ 才有。
填一个没验证过的下限，会把本来能用的用户直接挡在门外，所以等有实际不兼容反馈再加。

### 新增插件图标 `logo.png`

256×256、1:1、圆角。图形是两个话题分支：下方亮线是当前话题，上方虚线从分岔点离开代表
新话题，分岔处的白点表示"上下文在这里切换"。

注意 AstrBot 的机制是**把 `logo.png` 放在插件目录**，元数据里**没有** logo 字段。

### 打包器加了图标自检

`check_logo()` 会在打包前校验：PNG 有效性、是否 1:1、是否 256×256、是否超过 512KB。
和版本一致性自检一样，不合格直接拒绝打包。

### `AGENTS.md` → `CONTRIBUTING.md`

原来的 `AGENTS.md` 是给 AI 会话用的项目指令，里面写着推送冻结记录、本机路径等只对本地环境
有意义的内容，不适合放在公开仓库。现在改成面向贡献者的 `CONTRIBUTING.md`：讲清环境、五套
测试怎么跑、提交规范，并把六条"被实机踩出来的约束"整理成「改代码前必读」
（handler 必须是模块级、入口要吞异常、指令名避开通用词、失败即延续等）。

"""

## v0.4.1

**README 顶部新增「原理」一节**：一张流程图 + 三个关键设计，放在最前面，读者不必翻到
后面才知道它怎么判的。

- 流程图讲清顺序：启发式（免费、每条都跑）→ 结论明确就直接用 → 拿不准才问一次便宜模型
  → 模型失败一律当「延续」；
- 三个关键设计：① 两个信号都指向新话题才敢判新话题；② 不拿"字面不重叠"当证据（中文追问
  字面相似度天然为 0）；③ 判定是旁路，失败不影响正常聊天；
- 原来的「它能做什么」里那段较细的两层机制说明删掉，改为指向「原理」，避免重复。

## v0.4.0

**用真实聊天记录建评测，并据此修掉一个高频误判；判定模型现在能看到机器人上一句回复。**

这一版的两个改动都是"先测再改"，不是拍脑袋。

### 新增：真实数据评测台 `tests/eval_topic_detection.py`

- 直接读 AstrBot 的 `data_v4.db`，把**真实会话**（本次实测 5 个会话 / 65 轮，最长一段
  42 轮）灌进判定流水线，输出混淆矩阵、精确率/召回率、模型调用占比、置信度分布、
  误判样例；
- 支持换阈值跑敏感性分析（`--sim-new-topic` 等），结果可写 JSON 便于前后对比；
- `--compare-legacy` 一条命令跑出"旧启发式 vs 当前"的行为对比，防止以后回退；
- 零网络：纯启发式评估不发任何请求；真值口径（时间间隔代理 / 手工标注 / 保底口径）
  在报告里写明，不假装是真值。

### 修掉：短句被凭"字面不重叠"判成新话题

评测第一轮就暴露了问题：65 轮里**启发式直接判了 38 轮新话题、模型一次都没问**，
其中 **30 轮是短句**——「我没有代理啊」「等等吧还没做完」「为啥直接闪退？」这类追问
与话题名的字符相似度是 0.00（中文追问换词彻底，字面重叠天然趋近 0），却被当成新话题。

改动：**字面低相似在短句上没有信息量，这类消息不再由启发式定性，而是交给模型判语义。**

| 指标（同一份 65 轮真实数据） | 旧 | 新 |
| --- | --- | --- |
| 判定为新话题的轮数 | 39 | **13** |
| 其中短句（≤25 字）被判新话题 | 31 | **5** |
| 会去问模型的轮数 | 13 | 37 |

另外调整了判定优先级，避免新规则带来副作用：

- **冷启动提到最前**：隔了 45 分钟再开口，即使只有「嗯」「在吗」两三个字也判新话题，
  不会为了短句白问一次模型；
- **极短纯指代（≤12 字，如「那它呢」）直接判延续**，不花模型调用；
- 长句零重叠仍由启发式直接判新话题（保留原有的省钱路径）。

新增配置 `short_message_length`（默认 25）：设成 0 即恢复旧行为（不推荐）。

### 新增：机器人上一句回复进入判定提示词

判定模型此前只看到"当前话题 + 最近几条**用户**消息"，看不到上一轮机器人答了什么，
缺少承接关系的锚点——而很多换话题恰好发生在回答之后（"好的，那再问个别的"）。

- 新增 `on_llm_response` 钩子捕获回复，按会话临时中转，落进 `TopicState.last_assistant_text`
  （截断 1000 字，避免状态文件被长回复撑大）；
- 判定提示词新增一行 `机器人上一句回复：…`；
- 新增配置 `include_assistant_reply`（默认开），关掉可略微省 token；
- 日志的判定明细里多一个 `has_reply` 字段，便于确认回复确实被用上了。

### 测试

- `tests/run_tests.py` 130 项 → **135 项**：新增短追问句交模型、极短指代不调模型、
  长句零重叠仍直接判新话题、冷启动优先于短句规则、提示词包含回复等用例；
- `tests/test_portability.py` 补 `on_llm_response` 桩装饰器。

## v0.3.2

**按官方文档与源码核实，修正部署文档里几处不准的说法。**

- **数据目录口径统一**：官方文档对"数据目录"的写法不统一（`data/`、`AstrBot/data`、
  `~/.astrbot`…）。实际规则只有一条：根目录 = `ASTRBOT_ROOT` 环境变量 > 桌面打包运行时
  `~/.astrbot` > 其他情况**当前工作目录**，再拼 `data/`。目录表据此补齐了
  **一键启动器的两种数据位置**（新版可视化在 `~/.astrbot_launcher`，旧版 PowerShell
  脚本在与 `.bat` 同级目录）。
- **桌面客户端默认只监听本机**：WebUI 是 `127.0.0.1:6185`，要让别的设备访问得改
  `cmd_config.json` 的 `dashboard.host` 为 `0.0.0.0`（或用 `ASTRBOT_DASHBOARD_HOST`）。
- **Python 要求写准**：AstrBot v4 要求 **Python ≥ 3.12**（旧版安装脚本的 3.10–3.13 是脚本
  自身要求，不是 AstrBot 的要求）；补上 `uv run --no-sync main.py` 与 `uv tool install
  astrbot` 两种官方装法及其限制（uv tool 方式不能在 WebUI 里升级版本）。
- **Docker 补全**：贴出官方 `compose.yml` 要点（唯一环境变量是 `TZ`、6185/6199 两个端口、
  `./data:/AstrBot/data`）；补上容器内代理该怎么写（`host.docker.internal` / docker0 网关）；
  说明群晖·威联通官方没有专章，CasaOS·宝塔·1Panel·K8s 有。
- **zip 规则按源码写精确**：`metadata.yaml` 必须在根目录、四个必填字段、UTF-8、≤1MB、
  `name` 必须是合法 Python 标识符；**外层套一层目录可以，多层没有官方承诺**；
  安装后立即加载，不需要重启。
- 修正上一版 fnOS 章节（见 v0.3.1）：该环境是 fnOS **原生应用**而非 Docker，重启方式不同。

## v0.3.1

## v0.3.0

**面向所有用户重写文档：不再只讲飞牛 NAS，覆盖各种部署方式。**

- README 重写为使用者视角：开头讲清它能做什么，然后是**五种部署方式**的完整步骤：
  - 方式 A：WebUI 上传（所有平台通用，推荐）
  - 方式 B：Docker 部署的 AstrBot（docker cp / 宿主机挂载目录 / 群晖·威联通·CasaOS·1Panel）
  - 方式 C：Windows 桌面客户端（含 %USERPROFILE%\.astrbot\data\plugins 路径）
  - 方式 D：飞牛 NAS（fnOS）
  - 方式 E：macOS / Linux 源码部署（含 ASTRBOT_ROOT）
- 新增「插件到底装在哪个目录」对照表：Docker 是容器内 /AstrBot/data/plugins（官方 compose
  的挂载点），桌面版是 ~/.astrbot/data/plugins，源码部署是 <启动目录>/data/plugins。
- 新增速查：装完怎么配置、怎么验证真的在工作、使用条件与边界、完整配置表、指令表、排错表。
- metadata.yaml：描述与 help 改为面向使用者的文案，补上 
epo 地址，help 用多行格式
  列出全部子指令。
- 测试脚本去掉剩余的本机绝对路径写法，测试说明同时给出 Windows / Linux-macOS 两种跑法。

## v0.2.2

**可移植性收尾：确认别人也能用。**

- 测试脚本不再写死本机绝对路径：AstrBot 运行路径改为自动探测
  （ASTRBOT_APP_PATH / ASTRBOT_SITE_PACKAGES 环境变量优先，其次桌面版默认位置）；
  
erify_zip_install.py 找不到上游 v4.28.1 源码时给出明确指引并跳过，而不是报错。
- 新增 	ests/test_portability.py：用**桩模块**隔离 AstrBot 依赖，因此任意 Python 3.10+
  都能跑。它同时模拟两种 handler 派发契约（绑定方法 / 未绑定函数）与零参数调用
  （on_astrbot_loaded 的真实形态），16 项断言全过 —— 这是别人装上也能用的证据。
- 新增 
equirements.txt（本插件零第三方依赖）与 README「谁能用 / 使用条件」一节，
  写清版本、Runner、Provider 三个前置条件。

## v0.2.1

**根治：所有 handler 改成模块级函数、不声明 self。**

实机重启后的纯信号日志（僵尸条目已清空，只剩真相）：

`
core.core_lifecycle:372  hook(on_astrbot_loaded) -> None - on_loaded_v017
TypeError: TopicRouterPlugin.on_loaded_v017() missing 1 required positional argument: 'self'
`

AstrBot 的调用是 wait handler.handler()（core/core_lifecycle.py:375），**不传 self**。
正常环境里注册表存的是**绑定方法**（self 已绑定）所以没事；这个环境里注册进去的是
**未绑定的类函数**，于是：

- 要求 self 的 handler → missing 1 required positional argument: 'self'；
- (self, event, req) → missing 'req'（event 落进 self）；
- (self, *args) → 事件被塞进 self（日志里 self=WebChatMessageEvent）。

**修法**：handle_message_v017 / hook_llm_request_v017 / command_topic_v017 /
on_loaded_v017 全部改为**模块级函数**，签名是 (*args, **kwargs)，用 isinstance
自己认出事件与请求。模块级函数没有 self 这个位置，AstrBot 怎么调都落对。

- _extract() 允许零参数（on_astrbot_loaded 就是 handler() 零参调用）。
- 新增回归用例 [12] handler 的形态：逐个断言四个 handler 是模块级、无必填参数，
  并像 AstrBot 那样**零参数调用 on_loaded_v017()**、以三种方式调用消息 handler。
- 状态仍全部在模块级 RUNTIME / _CTX 上。

## v0.2.0

- **改：去掉英文别名 	opicrouter**（只留 话题织机）。	opic 和 	opicrouter 都是
  容易与别的插件撞名的英文常用词；指令冲突是 AstrBot **实时读内存注册表**判定的
  （command_management._collect_descriptors），撞了就会一次触发多个插件。
- **新增：/topicweave conflicts** —— 直接读 handler 注册表，把当前所有冲突的
  「指令名 ← 是哪些插件注册的」列出来并写进日志，方便定位到底该改谁。
- 说明：面板上如果仍显示 /topic 冲突，那一条**不是本插件当前版本注册的**
  （v0.1.9 起注册的是 	opicweave）。AstrBot 的卸载/热重载不会清掉内存注册表里的
  旧条目，**重启进程**后才会消失。

## v0.1.9

- **改：指令名 `/topic` → `/topicweave`**（别名 `话题织机`、`topicrouter`）。
  AstrBot 的「检测到指令冲突」把 `/topic` 和别的插件判成了同一指令，一次触发会让
  多个插件同时响应。`topic` 太通用，换成不会撞的名字；两个别名方便输入。
- `/topicweave` 的用法与之前完全一致：`/topicweave`、`/topicweave new [名称]`、
  `/topicweave forget`、`/topicweave off|on`。

## v0.1.8

**架构修正：状态与逻辑从 `self` 搬到模块级——这是本插件能在这个环境跑起来的关键。**

实机（4.28.1 + webchat）反复出现同一个现象：插件加载正常、版本日志正常，但判定
日志一条都没有。日志最终给出了铁证：

```
hook_llm_request_v016 被调用 | self=WebChatMessageEvent | has_event=False | args=['ProviderRequest']
AttributeError: 'WebChatMessageEvent' object has no attribute '_request_from_event'
```

**这个环境派发插件 handler 时没有把实例绑到 `self`，`self` 拿到的是事件对象。**
于是任何 `self.xxx` 都会抛 `AttributeError`；而 `handle_message` 的入口日志外面正好
包着 `except`，异常被静默吞掉 → 表现就是"插件在跑，但什么都不输出"。

- 状态（config / store / recent / pending_actions / ctx）全部移到模块级单例
  `RUNTIME` 与 `_CTX`；
- 判定逻辑改为**模块级函数**（显式接收 `ctx`），可离线测试；
- 三个 handler（`handle_message_v017` / `hook_llm_request_v017` /
  `command_topic_v017`）只做"认参数 → 转调模块函数"，参数识别只用 `isinstance`，
  方法体里**不出现任何 `self.xxx`**；
- `on_loaded_v017` 同样不依赖 `self`；
- 测试同步到新架构（模块级状态需要显式重置），111 项全过。

## v0.1.7

- **修：判定处理器可能压根没轮到执行**。AstrBot 的 `star_request.py` 是
  `for handler in activated_handlers: if event.is_stopped(): break`，而任何一个
  handler 抛异常都会 `event.stop_event()`。实机上注册表里残留的旧 handler 先抛
  `TypeError` → 事件被停掉 → 排在后面的 `on_message` **一次都没执行**，日志里因此
  只有报错、没有判定结果。
  - 判定处理器改名为 `handle_message_v017`（同样避开"按名复用旧条目"），并加**入口
    日志**，一眼看出"轮到我没有"；
  - `_detect` 外层加兜底：判定层任何异常都自己吞掉并记全栈，绝不 `stop_event()`
    连累同一条消息上的其它插件。
- 附：那两条零点报错来自注册表里的**内存僵尸条目**，与磁盘上的代码无关，**重启
  AstrBot 进程**后消失。

## v0.1.6

- **修：钩子的 `self` 可能不是插件实例**。实机日志（4.28.1 + WebChat）显示，改名后的
  钩子确实被调用了，但第一个位置参数是事件对象：
  `AttributeError: 'WebChatMessageEvent' object has no attribute '_request_from_event'`
  ——也就是这个派发链把函数按**未绑定**方式存/调用，`self` 落到了事件上。
  现在钩子用 `*args/**kwargs` 接住一切，自己按类型认出"事件"与"请求"，并且
  **不依赖 `self` 做任何判断**（`_looks_like_event` 提升为模块级函数），方法体加了
  总兜底：任何异常只记一行 warning，不再往日志里刷 traceback。
- 判定主路径（消息事件）不受影响；`hint` / `auto` 在这类派发下会自动降级。

## v0.1.5

**找到并修掉了真正的根因：AstrBot 的 handler 注册表按"模块+函数名"复用旧条目。**

`astrbot/core/star/register/star_handler.py:38`（4.28.1）：

```python
handler_full_name = f"{awaitable.__module__}_{awaitable.__name__}"
md = star_handlers_registry.get_handler_by_full_name(handler_full_name)
if md:
    return md          # 命中就复用旧元数据，新函数根本不注册
```

这个键**不含插件版本**。于是从 v0.1.0 起，卸载重装时模块路径与方法名都没变，
注册表命中的始终是**第一次安装**留下的那条记录——里面存着旧的 `on_llm_request`
函数对象。结果是：新代码确实被加载、实例也构造了（日志能看到 `模块已导入 v0.1.4`），
但被派发的永远是旧的 `(self, event, req)`，所以怎么改签名都没用，连函数入口的日志
都一次不会出现。

- **修**：把请求钩子改名为 `hook_llm_request_v015`。名字变了 → full name 变了 →
  拿不到旧条目 → 注册到的是本类当前的方法。
- **验证**：新增 `tests/reproduce_hook_dispatch.py`，用官方注册代码复现"同名被复用"，
  并验证改名后新方法真的被执行（9 项断言）。

## v0.1.4

- **改：判定入口从 LLM 请求钩子换成消息事件**（`@filter.event_message_type(ALL)`）。
  消息事件的调用约定是确定的（永远 `handler(event)`），不受上面的注册表复用问题影响。
- **`on_llm_request` 降级为可选增强**：只在能拿到 `ProviderRequest` 时执行 `hint` /
  `auto` 需要的请求改写；拿不到就先挂起，等下一次钩子拿到请求再执行。
- **新增去重**：去重键用平台的 `message_id + timestamp`（拿不到时才退回正文），
  所以用户真的重发同一句话仍然算新的一轮。
- 判定日志新增 `has_request` 字段。

## v0.1.3

- **新增诊断**：模块导入时打印版本号与磁盘真实路径，并在插件数据目录写
  `imported.txt` 标记文件；每次请求钩子被调用也打印调用现场。
  正是这两样定位到了"包是新的、跑的却是旧函数"。
- 新增 `CHANGELOG.md`（AstrBot 会提示插件缺少 changelog）。

## v0.1.2

- `on_llm_request` 的 `req` 改为可选参数并加多路回退（事后看无效，因为压根没进函数）。
- 新增 `build_zip.py`（正斜杠条目 + 目录条目，输出到桌面）、
  `tests/verify_zip_install.py`（用 AstrBot v4.28.1 官方解包代码在 POSIX 语义下
  验证安装流程）。

## v0.1.1

- 首次尝试修复，无效：报错是"参数没被传进来"，而我只改了参数默认值。

## v0.1.0

- 首个版本：启发式 + 便宜模型的二层话题判定，`off / mark / hint / auto` 四种模式，
  `/topic` 指令，按会话保存的话题状态。

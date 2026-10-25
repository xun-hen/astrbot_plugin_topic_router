<img src="logo.png" alt="话题路由器 PRO" width="120" align="right">

# 话题路由器 PRO · Topic Router

> 先合并连发消息，再判断用户这句话是**接着上一个话题**，还是**开了新话题**。

AstrBot 的对话是一整条上下文。当用户在同一个会话里从"帮我画个电路板"跳到"顺便看看 NAS 硬盘"
时，模型会把两件事混在一起回答；等到上下文攒到 50 条被压缩，话题边界就彻底糊了。
而用户把一句话拆成三条发时，逐条判定又会把同一件事判成三次。

v0.9.0 起，插件把「消息聚合」内建为判定的第一阶段：先按自适应防抖把连续消息缓冲成一轮，
再在合并后的文本上做一次轻量判定，输出「延续 / 新话题」+ 置信度，然后按你选的模式行动。
**默认只记录，不改任何上下文**——先看几天它判得准不准，再决定要不要自动翻篇。

```
[话题路由器] NEW-TOPIC | webchat:... | confidence=0.90 | source=llm | topic=硬盘扩容 | ...
[话题路由器] CONTINUE  | webchat:... | confidence=0.95 | source=heuristic | topic=电路板设计 | ...
```

| | |
| --- | --- |
| 适用 | AstrBot ≥ 4.26（v4.x） |
| 依赖 | **零第三方依赖**（纯标准库） |
| 许可 | MIT |
| 模式 | `off` / `mark`（默认）/ `hint` / `auto` |
| 指令 | `/topicweave`（别名 `话题织机`） |
| 聚合 | 内建、默认开启；`aggregation.enabled=false` 可退回逐条判定 |

---

## 原理

一句话：**能白判就白判，判不准就问模型，问不出来就当延续。**

```
用户消息
   │
   ├─ 启发式（本地、免费、每条都跑）
   │    时间间隔 / 承接词 / 显式换话题词 / 消息长度 / 与会话内文字的相似度
   │
   ├─ 结论明确 ──────────────► 直接用（多数消息走这条，不花钱）
   │
   ├─ 拿不准 ──► 问一次便宜模型（默认 deepseek-v4-flash）
   │              把「当前话题 + 最近几条消息 + 机器人上一句回复」给它，要一个 JSON
   │
   └─ 模型超时/报错/输出不合法 ──► 一律当「延续」
```

三个关键设计：

1. **两个信号都指向新话题，才敢判新话题**。启发式说延续、模型说新话题但不太确定时，
   保守按延续——误判新话题会把用户正在聊的上下文切走，代价比漏判大得多。
2. **不拿"字面不重叠"当证据**。中文追问换词很彻底（"我没有代理啊"和话题的字符相似度是 0），
   所以短句一律交给模型判语义，不让免费层草率定性。
3. **失败不影响正常聊天**。判定是旁路：超时、异常、JSON 解析失败都退化成「延续」，
   该回的消息照常回。

**v0.9.0 的流水线：先聚合，再判定**：

| 阶段 | 位置 | 角色 | 说明 |
| --- | --- | --- | --- |
| ① 聚合 | `handle_message_v090`，`priority=1000` | **第一阶段** | 缓冲连续消息、自适应等待、重构事件；非 Owner 消息被静默 |
| ② 判定 | 聚合完成后的 `on_settled(合并文本)` | **主路径** | 在**合并后**的文本上判一次，`turns` 只推进一轮 |
| ③ 兜底 | `hook_llm_request_v090` | **被抢占时仍判定** | 不在插件排队循环里，任何优先级都挡不住；同时执行 `hint`/`auto` 改写 |

AstrBot 的处理器循环遇到 `event.is_stopped()` 就会 `break`。只要有**优先级高于本插件**的
执行者先跑并静默事件，消息入口就一次都执行不到。所以：

- 优先级取插件层顶格 **1000**，尽最大可能先拿到消息（聚合必须如此）；
- 但仍**不把"先跑"当保证**：请求钩子会检查入口标记，没跑就兜底判定，并落
  `aggregate.preempted` 告警与 `preempted_total` 计数——失败从静默变成可见。

> ⚠️ 更正（v0.9.2）：上面的"优先级被抢"不是 v0.8.4 那台机器上入口零执行的真正原因。
> 真因是 **handler 注册在 `router` 模块路径、插件元数据却登记在 `main`**，框架命中的
> 是 `Star.__init_subclass__` 留下的匿名元数据（`name=None`），AstrBot 4.27+ 的
> `SessionPluginManager.filter_handlers_by_session` 会**静默丢弃**它——没有异常、没有警告，
> 钩子（不走这条过滤）照常工作，所以看起来"插件在跑"。v0.9.2 在 `main.py` 里把 handler
> 归位到 `main` 并把这条链路写进启动自检；历史分析见
> [docs/evidence-entry-never-runs.md](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/docs/evidence-entry-never-runs.md)
> 与 [docs/DESIGN.md §5.1](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/docs/DESIGN.md)。

判定结果按会话（UMO）隔离保存，`auto` 模式只是替用户执行了一次 `/new`，旧对话完整留档。

---

## 目录

- [原理](#原理)
- [它能做什么](#它能做什么)
- [消息聚合（内建）](#消息聚合内建)
- [安装：先选你的部署方式](#安装先选你的部署方式)
- [从旧版升级（原「话题路由器」）](#从旧版升级原话题路由器)
  - [方式 A：WebUI 上传（所有平台通用，推荐）](#方式-a从插件市场安装最简单推荐)
  - [方式 B：Docker 部署的 AstrBot](#方式-bdocker-部署的-astrbot)
  - [方式 C：Windows 桌面客户端](#方式-cwindows-桌面客户端)
  - [方式 D：飞牛 NAS（fnOS）](#方式-d飞牛-nasfnos)
  - [方式 E：macOS / Linux 源码部署](#方式-emacos--linux-源码部署)
- [装完之后的配置](#装完之后的配置)
- [验证它真的在工作](#验证它真的在工作)
- [使用条件与边界](#使用条件与边界)
- [配置项](#配置项)
- [指令](#指令)
- [用真实数据调参](#用真实数据调参)
- [排错](#排错)
- [开发与测试](#开发与测试)

---

## 它能做什么

**四种模式，按侵入性从小到大：**

| mode | 行为 | 适合 |
| --- | --- | --- |
| `off` | 完全不判定 | 临时关掉 |
| `mark` | **默认**。只写日志、维护话题状态，请求一个字都不改 | 先跑这个，观察准确率 |
| `hint` | 额外往本轮注入一段临时提示（不进历史），告诉模型"已换话题" | 不想换对话，但想让模型别串味 |
| `auto` | 置信度 ≥ `auto_threshold` 时新建一个对话并切过去，旧对话完整留档 | 确认判定准了再开 |

`auto` 做的事和用户手打 `/new` 完全等价：旧对话不删、随时能用 `/ls` `/switch` 切回去，
区别只是由插件在后台完成。**切换默认是静默的**——想让机器人回一句提示，把
`new_conversation_notice` 打开（默认关闭）。

> 💡 **想先验证判定准不准、又不想动真实会话**：打开 `debug_mode`（调试模式）。
> 它会进入「空跑」——判定照常执行并记日志，但**一个副作用都不产生**：
> 即使 `mode=auto` 也不会切对话、不会注入提示。适合在调 `auto_threshold` 之前先观察几天。

**为什么默认不自动切？** 因为误判的代价不对称：

- 把「延续」误判成「延续」→ 没损失；
- 把「延续」误判成「新话题」→ 会把用户正在聊的上下文切走，用户会觉得机器人失忆了。

所以插件的默认姿势是「先观察，后行动」。

判定怎么做的、为什么这样设计，见上面的[原理](#原理)。

模型说错话、超时、报错**一律按「延续」处理**——判定失败绝不影响正常回复。

---

## 消息聚合（内建）

v0.9.0 把独立插件 [`astrbot_plugin_msg_aggregator`](https://github.com/xun-hen/astrbot_plugin_msg_aggregator)
的核心整体迁入本插件（`aggregation/` 目录），**默认开启**。

**它解决什么**：

```
你: 帮我看看 NAS 硬盘        ┐
你: 好像有点响               ├─ 合并成一轮 → 只回一次，判定只推进 1 轮
你: 是不是要换一个           ┘
```

不开聚合时，上面三句话会触发三次判定、三次 LLM 调用；开了之后只触发一次。

**默认行为**：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| 等待策略 | `balanced` | 2.0 秒自适应防抖；`fast`=1.0s 固定，`patient`=3.5s 自适应 |
| 总等待上限 | 12 秒 | 从第一条算起，防止用户一直发导致永不结算 |
| 群聊合并 | 开 | 按「会话 + 发送者」分桶，同群不同人不会互相合并 |
| 输入状态感知 | 开 | 支持平台（如 NapCat `input_status`）上，用户打字时暂停计时 |
| 撤回过滤 | 开 | 等待期内撤回的消息不进入合并结果（仅支持撤回通知的平台） |
| 图片保活 | 开 | 本地临时图片复制到插件缓存，避免第 2+ 条消息的图丢失 |
| 统计与页面 | 开 | 插件详情页可看合并率、延迟、最近轮次；数据落 `plugin_data/.../stats.json` |

**群聊的可达性限制（平台级，必须知道）**：未被唤醒的群消息在 AstrBot 的唤醒阶段就被
拦截，插件根本看不到。因此群聊合并只在**每条补充消息都 @ 机器人（或带唤醒前缀）**时生效。
私聊不受此限制。

**想关掉聚合**：把 `aggregation.enabled` 设为 `false`。行为回到 v0.8.6——每条消息即时判定、
没有额外等待。其它聚合参数（防抖、预设、群聊、附件配额、统计）都在 `aggregation` 分组下。

**被抢占时会怎样**：入口优先级已取插件层顶格 1000，但仍可能被 `sys.maxsize` 的内置会话
控制或别的 1000 插件挤掉。此时请求钩子仍会判定，并写 `aggregate.preempted` 警告、把
`preempted_total` 计入统计——不会静默失效。

---

## 安装：先选你的部署方式

不管你用哪种方式跑 AstrBot，插件最终都装在同一个地方：

| 部署方式 | AstrBot 数据目录 | 插件目录 |
| --- | --- | --- |
| Docker | 容器内 `/AstrBot/data`（你挂载的宿主机目录） | `/AstrBot/data/plugins/` |
| 桌面客户端 | Windows `C:\Users\<你>\.astrbot\data`；macOS / Linux `~/.astrbot/data` | 同上 `plugins/` |
| 一键启动器（新版可视化） | `~/.astrbot_launcher` 下 | 同上 `plugins/` |
| 一键启动器（旧版 PowerShell 脚本） | 与 `launcher_astrbot_en.bat` 同级的 `AstrBot\data` | 同上 `plugins/` |
| 源码 / CLI / uv | `<启动目录>/data`（可用 `ASTRBOT_ROOT` 改） | `<启动目录>/data/plugins/` |
| 飞牛 NAS 原生应用 | 应用私有目录（fnOS 上通常是 `/vol1/@appdata/astrbot/data`） | 同上 `plugins/` |

> 官方文档里"数据目录"的写法不统一（`data/`、`AstrBot/data`、`~/.astrbot`…）。实际规则
> 只有一条：**根目录 = `ASTRBOT_ROOT` 环境变量 > 桌面打包运行时用 `~/.astrbot` > 其他情况用
> 当前工作目录**，再拼 `data/`；`data` 下的子路径固定是 `plugins`、`config`、
> `cmd_config.json`、`plugin_data`、`dist`。找到你的根目录，插件目录就确定了。

> Windows 的 `~` 是 `C:\Users\<你的用户名>`；macOS / Linux 的 `~` 是 `/Users/<你>`、
> `/home/<你>`。

**装完生效方式**：市场安装和 WebUI 上传都会**自动加载**，一般不需要重启；但升级已装的版本时，
建议**重启一次 AstrBot 进程**（原因见 [排错](#排错)：热重载不会清掉内存里的旧注册项）。

> ⚠️ **从 v0.8.x 升级到 v0.9.0**：消息聚合**默认开启**，所以升级后你会先感觉到约 2 秒的
> 合并等待。这是预期行为；不想要就在插件配置的 `aggregation.enabled` 关掉。旧配置项
> 原地保留，不需要迁移。
>
> ⚠️ **如果你同时装了独立版「消息聚合」（`astrbot_plugin_msg_aggregator`），请先卸载它**：
> 两者都用 priority 1000 缓冲消息，同时启用会互相抢事件。本插件已经内建同样的能力。

---

### 方式 A：从插件市场安装（最简单，推荐）

插件已上架 AstrBot 插件市场，**一行命令都不需要**：

1. 打开 AstrBot 管理面板（默认 `http://<你的IP>:6185`，Docker 部署时**不能用 localhost**）；
2. 左侧进 **插件 → 插件**，切到顶部的 **插件市场** 标签（地址 `/extension/plugins/market`）；
3. 搜索 **话题路由器**（或 `topic_router`），点安装；
4. 装完在 **插件配置** 里确认 `provider_id`（见[装完之后的配置](#装完之后的配置)）。

也可以直接在市场网页端看：[AstrBot 插件市场](https://cloud.astrbot.app/)。

> 市场装的是 release 包，会自动加载，一般不需要重启。

---

### 方式 B：WebUI 上传 zip（离线 / 内网 / 想装指定版本时用）

不用碰命令行、不用进容器，**所有部署方式都适用**。

1. 管理面板 → **插件 → 插件**（`/extension/plugins`）；
2. 右上角 **安装插件 → 从文件安装**，选择 `astrbot_plugin_topic_router_pro-<版本>.zip`；
3. 等日志出现 `Installed plugin astrbot_plugin_topic_router_pro-... successfully.`；
4. 在插件列表确认已**启用**，再进 **插件配置** 填 `provider_id`。

zip 从哪来：本仓库**不提供预编译的 zip 附件**（市场装的就是最新版）。要从文件安装就自己打一个：

```bash
git clone https://github.com/xun-hen/astrbot_plugin_topic_router
cd astrbot_plugin_topic_router_pro
python build_zip.py            # 在插件工作区的 dist/ 生成 astrbot_plugin_topic_router_pro-<版本>.zip
```

也可以直接下 GitHub 的源码压缩包（Code → Download ZIP）——仓库根目录就是插件目录，
AstrBot 会自己把那层目录拍平，能正常识别。

> ⚠️ **不要用系统自带的"压缩文件夹"重新打包**。Windows 的压缩工具会写反斜杠条目名，
> 装到 Linux 上的 AstrBot 会直接失败（`FileNotFoundError`）。要自己打包请用
> `python build_zip.py`，详见 [README 末尾](#自己打包)。
>
> **AstrBot 认可的包结构**（源码核实）：`metadata.yaml`（或 `.yml`）必须在**插件根目录**，
> 且至少包含非空的 `name` / `desc` / `version` / `author` 四个字段，UTF-8 编码、≤1MB；
> `name` 必须是合法 Python 标识符（它会变成 `data/plugins/<name>` 目录名）。**外层套一层
> 目录可以**（AstrBot 会把那层拍平），多层嵌套没有官方承诺。
> 安装成功后会**立即加载**；插件页卡片上的刷新图标是"重载插件"，加载失败的插件会出现在
> "加载失败插件"列表里，旁边有"重载"按钮。

---

### 方式 C：按你的部署类型装到对应目录

如果你的 AstrBot 是 Docker / 桌面客户端 / 飞牛 NAS / 源码部署，且不想用上面两种方式，
就按下面的对照表找到数据目录，把插件放进去，然后跳到对应小节。

**装完生效方式**：市场安装和 WebUI 上传都会**自动加载**，一般不需要重启；但升级已装的版本时，
建议**重启一次 AstrBot 进程**（原因见 [排错](#排错)：热重载不会清掉内存里的旧注册项）。

#### C1. Docker 部署的 AstrBot

官方镜像 `soulter/astrbot`，数据卷挂到容器内 `/AstrBot/data`。

**最省事的做法**：直接照 [方式 A](#方式-a从插件市场安装最简单推荐) 走，
宿主机不需要任何操作。

**放进容器**：

```bash
# 1) 看容器名（官方示例里叫 astrbot）
docker ps

# 2) 把解压出来的插件目录拷进去
docker cp astrbot_plugin_topic_router_pro <容器名>:/AstrBot/data/plugins/

# 3) 重启容器让它加载
docker restart <容器名>

# 4) 看日志确认加载
docker logs -f <容器名> | grep -i topic
```

**直接放宿主机（不用进容器）**：因为 `/AstrBot/data` 就是挂载点，找到你启动容器时
`-v` 的那一侧目录，把插件目录放进去即可：

```bash
# 例如启动命令是 -v $PWD/data:/AstrBot/data
cp -r astrbot_plugin_topic_router_pro ./data/plugins/
docker restart <容器名>
```

**官方的 `compose.yml` 长这样**（仓库根目录就有，可直接用）：

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: always
    ports:
      - "6185:6185"   # WebUI
      - "6199:6199"   # 可选：OneBot v11 / NapCat 反向 WS
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - ./data:/AstrBot/data
      - /etc/localtime:/etc/localtime:ro
```

- `volumes:` 的左边（`./data`）就是宿主机上的那个目录，插件放进它的 `plugins/` 即可；
- 官方 compose 唯一的环境变量是 `TZ`（IANA 时区，如 `Asia/Shanghai`）；
- 国内网络拉不动镜像时，把镜像换成 `m.daocloud.io/docker.io/soulter/astrbot:latest`；
- **代理**：WebUI `设置 → 网络 → 代理与依赖源 → HTTP 代理` 里填的地址是**从容器内部**访问的，
  所以 `127.0.0.1:7890` 会连到容器自己。宿主机上的代理要写
  `http://host.docker.internal:7890`（Mac/Windows Docker Desktop）或
  `http://172.17.0.1:7890`（Linux，docker0 网关）。

**群晖 / 威联通 / Unraid**：官方**没有**专门章节，套用上面的通用 Docker 写法（同样的镜像、端口、`/AstrBot/data` 卷）即可；**CasaOS / 宝塔面板 / 1Panel / Kubernetes** 官方有独立教程。

这些 NAS 面板的本质都是"找到映射到 `/AstrBot/data` 的宿主机
目录 → 把插件目录放进 `plugins/` → 重启容器"。图形界面里找"容器 → 详情 → 卷/存储"就能看到映射。

---

#### C2. Windows 桌面客户端

[AstrBot-desktop](https://github.com/AstrBotDevs/AstrBot-desktop) 支持 Windows / macOS / Linux，
下载对应安装包（`.exe` / `.dmg` / `.deb` / `.rpm`）装完启动即可，数据在 `~/.astrbot`。

**推荐做法**：桌面客户端自带管理面板，按 [方式 A](#方式-a从插件市场安装最简单推荐)
操作。

> ⚠️ 桌面客户端的管理面板默认**只监听本机**（`http://127.0.0.1:6185`）。想让别的设备访问，
> 改 `C:\Users\<你>\.astrbot\data\cmd_config.json` 里的 `dashboard.host` 为 `0.0.0.0`
> （端口保持 `6185`），完全退出并重启客户端，再从其他设备访问 `http://<本机IP>:6185/`。
> 也可以用环境变量 `ASTRBOT_DASHBOARD_HOST` / `ASTRBOT_DASHBOARD_PORT` 覆盖（优先级更高）。

**直接放目录**：

1. 完全退出 AstrBot 桌面客户端（托盘图标也要退出）；
2. 把 `astrbot_plugin_topic_router_pro` 整个目录复制到：
   ```
   C:\Users\<你的用户名>\.astrbot\data\plugins\
   ```
   最终结构必须是 `.astrbot\data\plugins\astrbot_plugin_topic_router_pro\metadata.yaml`；
3. 重新启动客户端。

**C3. 只想在本地试一下、不启动 AstrBot**：仓库的 `tests/test_portability.py` 用桩模块跑，
任意 Python 3.10+ 都能执行，用来确认插件逻辑本身没问题：

```powershell
python tests\test_portability.py
```

---

#### C3. 飞牛 NAS（fnOS）

fnOS 上的 AstrBot 有两种常见装法，**先确认你是哪种**，因为重启方式完全不同：

| 你的情况 | 怎么判断 | 重启方式 |
| --- | --- | --- |
| **fnOS 原生应用（FPK）** | 应用中心里有 AstrBot 卡片；SSH 里能看到 `/vol1/@apphome/astrbot/` | 应用中心卡片里的**停用 → 启用**（原生应用不支持 `systemctl`，也不要用 docker 命令） |
| **Docker 部署** | 自己用 compose / `docker run` 起的 | `docker restart <容器名>` |

**装插件**

1. 浏览器打开 `http://<NAS 的 IP>:6185`，用户名默认 `astrbot`；
   - 首次登录的初始密码在日志里：`/vol1/@appdata/astrbot/data/logs/astrbot.log` 搜 `Initial password`；
   - 登入后请立刻改密码。
2. **插件 → 插件 → 安装插件 → 从文件安装** → 上传 zip；
3. 在 **插件配置** 里填 `provider_id`，保存；
4. 如果日志里没有出现 `模块已导入 <版本>`，按上表**停用再启用**（或重启容器）。

**手动放文件（不推荐）**

原生应用的运行时数据在应用私有目录里，**文件管理器的「应用文件」进不去**（它只展示应用
用 `data-share` 声明的共享目录）。要手动放就得走 SSH：

```bash
# 1) 登录 NAS（设置 → SSH 打开），提权
ssh <管理员>@<NAS_IP>
sudo -i

# 2) 确认应用数据真实路径（不同 fnOS 版本可能不同，先确认再动手）
readlink -f /vol1/@appdata/astrbot

# 3) 解压并放进插件目录
unzip astrbot_plugin_topic_router_pro-*.zip -d /tmp/
cp -r /tmp/astrbot_plugin_topic_router_pro \
      /vol1/@appdata/astrbot/data/plugins/

# 4) 关键：把属主改回应用用户，否则 AstrBot 读不到 / 写不了
chown -R astrbot:astrbot /vol1/@appdata/astrbot/data/plugins/astrbot_plugin_topic_router_pro

# 5) 确认结构
ls /vol1/@appdata/astrbot/data/plugins/astrbot_plugin_topic_router_pro/metadata.yaml
```

然后回应用中心**停用 → 启用**。

> ⚠️ 用 `sudo cp` 拷进去的文件属主是 `root`，**不做第 4 步的 chown 就会失败**——这是
> 原生应用手拷文件最常见的坑。
> ⚠️ 部分 fnOS 版本的应用数据根目录不在 `/vol1/@appdata`，所以第 2 步的 `readlink` 不要跳过。

**数据安全（升级 / 卸载 / 备份）**

- **升级 AstrBot 应用不会删数据**：升级流程只做数据与配置迁移，并会先停应用再启；
- **卸载时要选「保留数据」**：fnOS 卸载向导通常给「保留数据（推荐）/ 删除所有数据」二选一，
  选删除就真的没了（有用户因此重装后登录不上，只能去 `cmd_config.json` 里删认证字段恢复）；
- **备份建议**：SSH 打包整个 `/vol1/@appdata/astrbot/`，里面包含 `data/`（插件、
  插件配置与 `plugin_data/` 状态）、`logs/`。`@appdata` 在数据卷上，换盘或重装系统前必须备份；
- ⚠️ **恢复时权限要一起恢复**（属主/属组），否则数据回来也用不了。

> fnOS 目前**没有官方 AstrBot 文档**。网上流传最广的那篇飞牛 AstrBot 教程是 **Docker 版**，
> 路径是 `/vol1/1000/docker/AstrBot/data`，和原生应用完全不同，别照着做。

---

#### C4. macOS / Linux 源码部署

**还没装 AstrBot 的话**，两种更省事的选择：

- **一键启动器**（Windows / macOS / Linux）：从
  [AstrBotDevs/astrbot-launcher](https://github.com/AstrBotDevs/astrbot-launcher) 的 Releases
  下载对应安装包（Windows x64/arm64、macOS aarch64 等）装上即可。macOS 若提示"已损坏"，
  终端执行 `xattr -dr com.apple.quarantine "/Applications/AstrBot Launcher.app"` 后重开。
- **手动部署**（下面这段）：适合服务器与想完全掌控环境的人。

**手动部署**（官方 [手动部署文档](https://docs.astrbot.app/deploy/astrbot/cli.html)）：

要求 **Python ≥ 3.12**（AstrBot v4 的硬性要求；旧版安装脚本另要求 3.10–3.13，两者不是一回事）。

```bash
git clone https://github.com/AstrBotDevs/AstrBot
cd AstrBot
uv sync              # 或 pip install -r requirements.txt
uv run main.py       # 启动；数据目录就是当前目录下的 data/
# 装了插件之后建议这样启动，避免每次同步依赖：
uv run --no-sync main.py
```

也可以完全不用 clone，用 uv 全局安装（官方 [包管理器部署](https://docs.astrbot.app/deploy/astrbot/package.html)）：

```bash
uv tool install astrbot --python 3.12
astrbot init         # 仅首次，会在当前目录创建数据目录与配置
astrbot run
```

> 注意：`uv tool` 这种方式**不支持在 WebUI 里升级 AstrBot 版本**，升级要用
> `uv tool upgrade astrbot --python 3.12`。

然后装插件：

```bash
# 把插件放进数据目录（注意：这里是 <AstrBot 启动目录>/data，不是 ~/.astrbot）
cp -r astrbot_plugin_topic_router_pro ./data/plugins/
# 重启 AstrBot
```

源码部署时数据目录跟随**启动时的工作目录**，想固定位置可以设环境变量：

```bash
export ASTRBOT_ROOT=/opt/astrbot      # 之后数据都在 /opt/astrbot/data
```

> 启动器/脚本部署同样落在 `<AstrBot 目录>/data/plugins/`，WebUI 默认也是 `:6185`。

---

## 从旧版升级（原「话题路由器」）

这个插件在 v0.7.0 改名为 **话题路由器 PRO**，内部标识从 `astrbot_plugin_topic_router` 变成
`astrbot_plugin_topic_router_pro`。对 AstrBot 来说这是**两个不同的插件**，所以要按下面做：

1. **先卸载旧版**：插件 → 插件 → 找到「话题路由器」（`astrbot_plugin_topic_router`）→ 卸载。
   ⚠️ 不卸载的话两个都会加载、对同一条消息判定两次；
2. **再装新版**：从插件市场搜「话题路由器PRO」安装，或上传
   `astrbot_plugin_topic_router_pro-<版本>.zip`；
3. **想保留旧的话题状态**（每个会话记住的话题名、轮数），在卸载前复制一份：

   ```bash
   cd <你的数据目录>/plugin_data
   mkdir -p astrbot_plugin_topic_router_pro
   cp astrbot_plugin_topic_router/topic_state.json \
      astrbot_plugin_topic_router_pro/topic_state.json
   ```

   不迁移也没关系：只是话题状态从零开始建档，**对话内容完全不受影响**；
4. 装完确认一下插件配置里的 `provider_id`（新插件是独立的配置项，旧配置不会带过来）。

> 仓库地址仍是 `xun-hen/astrbot_plugin_topic_router`（改仓库名需要仓库设置权限，AstrBot 也不要求
> 仓库名与插件名一致）。

---

## 装完之后的配置

进 **插件 → 插件配置 → 话题路由器**，至少确认这三项：

| 配置 | 建议值 | 说明 |
| --- | --- | --- |
| `mode` | `mark` | 先观察。确认判定准确后再考虑 `auto` |
| `provider_id` | `deepseek/deepseek-v4-flash` | 判定用的模型，格式 `provider_source_id/model`；**填错不影响回复**，只是判定失败 |
| `debug_log` | 打开 | 不然日志里看不到判定结果 |
| `debug_mode` | 想验证判定时打开 | 空跑：不切对话、不注入提示，只在日志与 `trace.log` 记录"本想做什么" |

**不知道 `provider_id` 填什么？** 插件每次加载都会把可用的模型 ID 打进日志，抄一个即可：

```
[话题路由器] 可用的模型 ID（可直接填进 provider_id）：deepseek/deepseek-v4-flash、...
```

> 判定用的模型和你聊天用的模型是分开的。用便宜的快模型即可，成本可以忽略（每轮最多
> 一次单轮调用，且只有启发式拿不准时才触发）。

---

## 验证它真的在工作

发一句话给机器人，然后在 AstrBot 日志里找这两行：

```
[话题路由器] 入口自检通过 | module=data.plugins...main | 元数据=astrbot_plugin_topic_router_pro | filters=1
[话题路由器] handle_message_v090 被调用 | vX.Y.Z | umo=... | msg='你好' | stopped=False | 聚合=开
[话题路由器] NEW-TOPIC | webchat:... | confidence=0.90 | source=llm | topic=... | ...
```

再发一句**接着刚才话题**的话，第三行应该变成 `CONTINUE`。

也可以用指令自查：发给机器人 `/topicweave`，会回一份当前话题状态。

如果只看到 `hook_llm_request_v090 被调用`、看不到 `handle_message_v090 被调用`，
说明消息入口没被派发（v0.9.2 之前的老版本会**静默**这样失败）——先看启动日志里的
`入口自检` 那一行，它会把原因直接写出来。

---

## 使用条件与边界

| 条件 | 说明 |
| --- | --- |
| AstrBot ≥ 4.26 | 聚合阶段以 4.26 为基线（`input_status` 探测、附件 `untrack` 降级路径）；`hint` 模式另需 `TextPart.mark_as_temp`（4.24+） |
| **内置 Agent Runner** | `provider_settings.agent_runner_type` 必须是 `local`。切到 Dify / Coze / DashScope / DeerFlow 时钩子不触发，插件静默不工作 |
| 一个可用的 LLM Provider | 判定要一次模型调用（可指定便宜模型）。不配也能跑，退化成纯启发式 |
| 平台无关 | QQ / 微信 / Telegram / Discord / WebChat 等所有平台都适用，只要消息能到达 LLM |

**已知边界：**

- **相似度是字面级的**：中文用字符 2-gram，同义改写的相似度会偏低，这类情况靠模型判定兜住；
- **判定会占用一点时间**：只有需要问模型的那一轮会多等一次单轮调用（约几百毫秒），
  其余轮次是纯本地计算；
- **状态按会话（UMO）隔离**：群聊里开 `unique_session` 时每个人是独立会话，互不干扰；
- **插件不会删除任何对话**：`auto` 只新建对话，旧对话完整保留。

---

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `mode` | `mark` | 见上文四种模式 |
| `provider_id` | `deepseek/deepseek-v4-flash` | 判定用模型；留空则跟随当前会话模型 |
| `auto_threshold` | `0.8` | `auto` 模式自动切换所需置信度 |
| `min_new_topic_confidence` | `0.6` | 低于它一律当延续，宁可不切 |
| `always_llm` | `false` | 每轮都问模型（贵，一般不用） |
| `sim_continue` | `0.5` | 相似度高于它就判延续，不问模型 |
| `sim_new_topic` | `0.12` | 相似度低于它且无承接词就判新话题 |
| `time_gap_minutes` | `45` | 距上条消息超过它，就**请模型判一次**「这句是不是接着上一件事」。不再凭时间硬判新话题（隔一小时回来继续同一件事很常见）|
| `min_message_length` | `6` | 短于它的消息（如"嗯""继续"）不判定 |
| `history_turns` | `3` | 送给判定模型的近期消息条数 |
| `include_assistant_reply` | `true` | 把机器人上一句回复也给判定模型看（很多换话题发生在回答之后） |
| `short_message_length` | `25` | 不超过这个长度且字面不重叠的消息，交给模型判语义而不是直接判新话题 |
| `timeout_seconds` | `8` | 判定模型超时，超时按延续处理 |
| `max_checks_per_hour` | `30` | 每个会话每小时的模型判定上限（0 不限） |
| `new_conversation_notice` | `false` | `auto` 切换时是否回一句提示。默认关闭（判定质量未标定前不打扰用户）|
| `debug_log` | `true` | 把判定明细写进日志 |
| `debug_mode` | `false` | **调试模式：只看不做 + 详细日志**。开启后不切对话、不注入提示，只在日志与 `trace.log` 写「本想做什么」 |
| `trace_enabled` | `false` | 把入口调用证据落盘到 `trace.log`（排障用，见故障排查表） |

**聚合分组（`aggregation.*`，默认开启）**：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `aggregation.enabled` | `true` | 聚合总开关；关闭后逐条判定 |
| `aggregation.preset` | `balanced` | `fast` / `balanced` / `patient` |
| `aggregation.debounce_time` | `2.0` | 最后一条消息后等多久结算 |
| `aggregation.adaptive` | `true` | 按长度、结尾标点、连续短句动态延长等待 |
| `aggregation.adaptive_max_wait` | `6.0` | 单次等待上限 |
| `aggregation.adaptive_max_total_wait` | `12.0` | 一轮总等待上限 |
| `aggregation.group_enabled` | `true` | 群聊是否参与合并（按「会话+发送者」分桶） |
| `aggregation.typing_detection` | `true` | 输入状态感知（平台支持时生效） |
| `aggregation.recall_filter` | `true` | 过滤等待期内撤回的消息 |
| `aggregation.preserve_part_order` | `false` | 保留图文交错顺序（实验性） |
| `aggregation.page_enabled` | `true` | 是否注册插件详情页后端 |

完整字段（图片配额、指令中断、统计落盘、页面推送间隔等）见 `_conf_schema.json` 里的
`aggregation` 分组；默认值在代码里只写一次，并由测试断言与 schema 逐项一致。

状态文件：`<数据目录>/plugin_data/astrbot_plugin_topic_router_pro/topic_state.json`
（每个会话一条记录，直接删文件等于全部重置）。
聚合统计与附件缓存：同目录下的 `stats.json` 与 `attachments/`。

---

## 指令

| 指令 | 作用 |
| --- | --- |
| `/topicweave` | 查看当前话题、轮数、本小时判定次数、开关状态 |
| `/topicweave new [名称]` | 手动翻篇：新建对话并重置话题（等于 `/new` + 重置状态） |
| `/topicweave forget` | 忘记当前话题，下一条消息重新建档 |
| `/topicweave off` / `/topicweave on` | 关掉 / 打开**本会话**的自动判断 |
| `/topicweave conflicts` | 列出当前所有指令冲突（哪个指令被哪几个插件注册） |

别名：`话题织机`。

> 指令名为什么是 `topicweave` 而不是 `topic`：AstrBot 用**实时内存注册表**判定指令冲突，
> `topic`、`topicrouter` 这类通用英文词很容易和别的插件撞名，撞了会一次触发多个插件。

---

## 排错

| 现象 | 处理 |
| --- | --- |
| 安装时报 `FileNotFoundError: .../plugin/astrbot_plugin_topic_router_pro` | 包是用系统压缩工具打的（反斜杠条目名 / 缺目录条目）。用 `python build_zip.py` 重新打包，或直接用 release 里的包 |
| 日志里完全没有插件的行 | 插件没装上或被禁用：去 **插件 → 插件** 看列表状态与版本 |
| 有 `hook_llm_request_v090 被调用`，但搜不到 `handle_message_v090 被调用` | **v0.9.2 已修**（根因是 handler 注册在 `router` 模块路径、插件元数据却在 `main`，被 AstrBot 4.27+ 的会话级插件过滤静默丢弃）。仍出现就说明没升级到 v0.9.2，或又出现了同类注册问题：看启动日志的 `入口自检` 那一行，它会列出模块路径/元数据名字/filters 三处的实际值 |
| 连发几条消息却没被合并 | 确认 `aggregation.enabled` 为 `true`；看日志 `aggregate.start` / `aggregate.flush`。群聊里只有每条都 @ 机器人（或带唤醒前缀）的消息才可能合并——未唤醒消息在唤醒阶段就被拦掉了 |
| 机器人迟迟不回复 | 聚合在等最后一条消息：检查 `aggregation.adaptive_max_total_wait` 是否被调得极大；用户一直处于"正在输入"时由 `max_typing_wait`（默认 60s）兜底 |
| 图片丢了 | 看日志 `attach.persist_failed`：临时文件复制失败且平台不支持 `untrack` 时会退化为 base64；仍失败则只丢该图、文本保留 |
| 合并率/统计看起来不对 | 面板里的耗时是**桶近似**（不是精确分位）；合并率 = 结算总条数 / 结算轮次。跨重启累计值在 `stats.json`，可在页面点"清空统计" |
| 有 `模块已导入` 但没有判定日志 | `mode` 是 `off`、或 `enabled` 是 `false`、或 `debug_log` 关了；也可能是这个会话被 `/topicweave off` 关了 |
| 判定结果和实际不符，老是判延续 | 看日志里的 `heuristic_confidence`：启发式就判了延续的话，调低 `sim_continue` 或调高 `sim_new_topic` |
| `[WARN] 判定模型调用失败` | `provider_id` 写错或该 provider 不可用；去日志里抄一个可用的 ID |
| `[WARN] 判定模型超时` | 网络慢或模型慢：调大 `timeout_seconds`，或换成更快的模型 |
| 每轮都被判成新话题 | 说明话题名一直是空的：确认判定模型能正常返回 JSON（换成 flash 这类快的、非推理模型） |
| 升级版本后行为没变 / 报旧版本的错 | **重启 AstrBot 进程**：Docker 用 `docker restart <容器>`；fnOS 原生应用在应用中心**停用 → 启用**；桌面客户端退出后重开。热重载不会清掉内存注册表里的旧 handler，只有重启才彻底 |
| 提示"检测到指令冲突" | 发 `/topicweave conflicts` 看是谁跟谁撞了；如果是别的插件之间撞名，跟本插件无关 |
| 群聊里几个人互相干扰 | 本插件按会话（UMO）隔离；平台开 `unique_session` 后每人独立 |

---

## 用真实数据调参

插件带一个评测台：把**你自己的真实聊天记录**灌进判定流水线，输出可对比的数字。
阈值该调多少，用它看，别猜。

```bash
# 读 AstrBot 的数据库（~/.astrbot/data/data_v4.db），纯启发式、零网络
python tests/eval_topic_detection.py

# 同时跑"旧启发式 vs 当前"，看改动到底有没有用
python tests/eval_topic_detection.py --compare-legacy

# 换阈值做敏感性分析
python tests/eval_topic_detection.py --sim-new-topic 0.3 --gap-minutes 30

# 把指标存下来，改前改后各存一份对比
python tests/eval_topic_detection.py --json-out before.json
```

报告里有：混淆矩阵、精确率 / 召回率 / F1、**模型调用占比**（判断省钱与否）、
判为"新话题"的置信度分布（决定 `auto_threshold` 定在哪）、以及误判样例。

**关于真值**：真实数据没有人工标注的"话题边界"，所以脚本默认用**时间间隔代理**
（相邻消息间隔超过 `--gap-minutes` 记为新话题），也可以用 `--labels` 传手工标注。
代理口径本身有噪声，**它的绝对值只当参考，真正有用的是同一口径下的前后对比**。

## 开发与测试

六套离线测试，都不需要启动 AstrBot（第一、四、五、六套需要 AstrBot 的 Python 环境来 import 真实代码）：

```bash
# 1) 插件逻辑：207 项（含跨路去重、钩子两种形态、调试模式、长空闲、落盘取证、聚合整合；需要 AstrBot 的 python）
python tests/run_tests.py

# 2) 可移植性：16 项（用桩模块，任意 Python 3.10+）
python tests/test_portability.py

# 3) 真实数据评测台（零网络，任意 Python 3.10+）
python tests/eval_topic_detection.py --compare-legacy

# 4) 注册表复用复现：9 项（验证 AstrBot 的 handler 注册表行为）
python tests/reproduce_hook_dispatch.py

# 5) 真实派发循环里的优先级竞争：18 项（含 sys.maxsize 内置 handler；需要 AstrBot 的 python）
python tests/reproduce_priority_race.py

# 6) 入口派发回归：13 项（验证 handler 归位 main 后能过会话级过滤；含反证，需要 AstrBot 的 python）
python tests/reproduce_entry_dispatch.py

# 7) zip 安装流程：23 项（需要 AstrBot v4.28.1 源码，见脚本内提示）
python tests/verify_zip_install.py
```

### 自己打包

```bash
python build_zip.py            # 输出 astrbot_plugin_topic_router_pro-<版本>.zip 到插件工作区的 dist/
```

**必须用这个脚本，不要用系统右键压缩**：Windows 的压缩工具写反斜杠条目名、且不写目录条目，
Linux 上的 AstrBot 解包后会抛 `FileNotFoundError`；此外它还会把 `tests/` 带进去，导致安装时
"把目录往自己里面搬"的错乱。

### 代码结构

| 文件 | 作用 |
| --- | --- |
| `main.py` / `__init__.py` | 插件入口，只做转出 |
| `router.py` | handler（模块级）+ 聚合/判定流程编排 + 指令 |
| `detection.py` | 启发式打分、模型提示词、结果解析与合并（纯函数，易测） |
| `state.py` | 话题状态持久化（按会话分条） |
| `aggregation/` | 内建消息聚合：配置、状态机、计时器、事件重构、附件保活、统计、页面后端 |
| `pages/status/` | 插件详情页（零依赖前端，读聚合统计） |
| `_conf_schema.json` | WebUI 配置项定义（含 `aggregation` 分组） |
| `metadata.yaml` | 插件元信息 |
| `tests/eval_topic_detection.py` | 真实数据评测台（阈值调参用，不进安装包） |
| `docs/DESIGN.md` | **技术文档（源）**：设计取舍、判定原理、执行架构、七个实机踩坑、测试体系 |
| `docs/topic-router-pro-technical-doc.docx` | 上面那份的 **Word 版**（从 DESIGN.md 生成，方便分发与打印）|
| `docs/evidence-entry-never-runs.md` | 「消息入口零执行」的完整证据链（实测数据 + 排除项）|

> 📄 上面三份文档**不在安装包里**（包只含运行所需文件）。要读请在仓库里看：
> [技术文档](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/docs/DESIGN.md) ·
> [Word 版](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/docs/topic-router-pro-technical-doc.docx) ·
> [证据链](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/docs/evidence-entry-never-runs.md)

> **给改代码的人**：这个插件的 handler 是**模块级函数**（不是类方法），状态放在模块级
> `RUNTIME` / `_CTX` 上，方法体里不允许出现 `self.xxx`。原因写在 `router.py` 文件头——
> 少数环境的 AstrBot 派发 handler 时不绑定实例，`self` 会是事件对象，任何 `self.xxx` 都会抛
> `AttributeError`，而入口的 `except` 会把它静默吞掉（表现为"插件在跑但什么都不输出"）。
> 模块级写法对两种派发契约都安全。

## 贡献

改代码前请先读 [CONTRIBUTING.md](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/CONTRIBUTING.md)
的「改代码前必读」——里面六条约束都是线上事故换来的（比如 handler 为什么必须是模块级函数）。
（这份文件不在安装包里，所以这里用仓库绝对链接。）

想理解**为什么这样设计**（而不是只知道规则），看
[技术文档](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/docs/DESIGN.md)：
它写清了判定算法、执行架构的实测由来、七个实机踩坑的完整根因，以及测试与评测方法。
需要 Word 版就下
[技术文档 Word 版](https://github.com/xun-hen/astrbot_plugin_topic_router/blob/main/docs/topic-router-pro-technical-doc.docx)
（由 DESIGN.md 生成，改完 Markdown 用 `python tools/md_to_docx.py docs/DESIGN.md <输出>` 重新生成）。

作者：[@xun-hen](https://github.com/xun-hen)。欢迎提 Issue 和 PR。

## 许可

MIT，见 [LICENSE](LICENSE)。

# 贡献指南

欢迎提 Issue 和 PR。这个插件的代码量不大，但有几条**被实机踩出来的约束**，改代码前请先看
[改代码前必读](#改代码前必读)——它们看着奇怪，但每一条都是线上事故换来的。

---

## 环境

- Python **3.10+**（AstrBot 本体要求 3.12+，但本插件的代码只用 3.10 语法）；
- 插件**零第三方依赖**，全部标准库。请不要为了省几行代码引入新依赖；
- 跑测试需要一个能 `import astrbot` 的解释器（用 AstrBot 自带的那个最省事）。

## 测试

五套测试，都不需要启动 AstrBot（第一套除外，它要 import 真实的 AstrBot API 对象）：

```bash
# 1) 插件逻辑与边界：135 项
python tests/run_tests.py

# 2) 可移植性：16 项。用桩模块隔开 AstrBot，任意 Python 3.10+ 都能跑
python tests/test_portability.py

# 3) 真实数据评测台：把真实聊天记录灌进判定流水线，看指标
python tests/eval_topic_detection.py --compare-legacy

# 4) handler 注册表行为复现：9 项（依赖 AstrBot 源码，见脚本内提示）
python tests/reproduce_hook_dispatch.py

# 5) zip 安装流程：21 项（依赖 AstrBot v4.28.1 源码，见脚本内提示）
python tests/verify_zip_install.py
```

**改了判定逻辑（`detection.py`）务必跑第 1 和第 3 套**：第 3 套会打印新旧行为对比，
能直接看出改动是让判定更准还是更糟。只凭感觉调阈值是不行的——这一点我们已经吃过亏
（见 CHANGELOG 的 v0.4.0）。

## 提交规范

- 提交信息：`<type>: <一句话概括>（vX.Y.Z）`，`type` 取 `feat` / `fix` / `refactor` / `docs` / `chore`，
  概括用中文说清"改了什么、为什么"；
- 发版时三处版本号必须一致：`metadata.yaml` 的 `version`、`CHANGELOG.md` 顶部的 `## vX.Y.Z`、
  `README.md` 里若出现的版本号。`build_zip.py` 会在打包前自检，不一致会直接拒绝打包；
- **不要提交 zip 安装包**（`.gitignore` 已排除）；
- 不要 `git push --force`。

## 自己打包

```bash
python build_zip.py     # 输出到插件工作区的 dist/ 目录
```

**必须用这个脚本，不要用系统右键压缩**：Windows 的压缩工具写反斜杠条目名、且不写目录条目，
Linux 上的 AstrBot 解包后会抛 `FileNotFoundError`；它还会把 `tests/` 带进包里。

---

## 改代码前必读

### 1. handler 必须是模块级函数，不能是类方法

`router.py` 里的 `handle_message_v090` / `hook_llm_request_v090` / `hook_llm_response_v090` /
`command_topic_v090` / `on_loaded_v090` 全是**模块级函数**，签名是 `(*args, **kwargs)`，
参数靠 `_extract()` 用 `isinstance` 自己认。

原因（实机日志）：

```
hook(on_astrbot_loaded) -> None - on_loaded_v017
TypeError: TopicRouterPlugin.on_loaded_v017() missing 1 required positional argument: 'self'
```

AstrBot 用 `await handler.handler()` 直接调用、**不传 self**。多数环境里注册表存的是**绑定方法**
所以没事，但至少有一个真实环境存的是**未绑定类函数**，于是：

- 要求 `self` 的 handler → `missing 1 required positional argument: 'self'`；
- `(self, event, req)` → `missing 'req'`（event 落进了 self）；
- `(self, *args)` → 事件被塞进 `self`。

**所以：状态一律放模块级 `RUNTIME` / `_CTX`，方法体里不允许出现 `self.xxx`。**
任何 `self.xxx` 都会抛 `AttributeError`，而入口的 `except` 会把它静默吞掉——
表现为"插件加载了但什么都不输出"，极难排查。

### 2. 入口 handler 必须自己吞掉所有异常

AstrBot 的派发是 `for handler in activated_handlers: if event.is_stopped(): break`，
任何一个 handler 抛异常都会 `stop_event()`，**同一条消息上排在后面的插件全部不执行**。
判定层出问题不能连累别的插件，所以入口一律 `try/except` 兜住。

### 3. 指令名不要用通用英文词

AstrBot 用**实时内存注册表**判定指令冲突（`star_handlers_registry`），`topic`、`topicrouter`
这类通用词很容易和别的插件撞名，撞了会一次触发多个插件。当前只用 `/topicweave` 和别名
`话题织机`。

另外：热重载**不会**清理内存里的旧注册项，改名后必须**重启进程**才彻底生效。

### 4. 判定层必须"失败即延续"

模型超时、报错、返回的不是合法 JSON——**一律按「延续」处理**。误判新话题会把用户正在聊的
上下文切走（用户觉得机器人失忆），代价不对称地高于漏判。任何新增的判定分支都要遵守这条。

### 5. 字符相似度在短句上没有信息量

中文追问会彻底换词（「我没有代理啊」与话题名的 2-gram 相似度是 **0.00**），
所以**不要把"字面不重叠"当成新话题的证据**，短句交给模型判语义。
数据见 `tests/eval_topic_detection.py --compare-legacy`。

### 6. 打包产物与状态数据的边界

- 代码只读插件自身目录；**持久化数据一律写 `data/plugin_data/<插件名>/`**，不写插件目录，
  否则更新/重装插件时会被覆盖；
- 不要把日志、状态文件、打包产物提交进仓库。

---

## 许可

MIT。提交 PR 即表示你同意以 MIT 许可发布你的贡献。

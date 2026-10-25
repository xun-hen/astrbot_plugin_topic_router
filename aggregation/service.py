"""消息聚合服务：独立插件 `astrbot_plugin_msg_aggregator` 的核心。

## 为什么是"服务"而不是另一个 Star 插件

话题路由器 PRO 把消息聚合**内建**成一个流水线阶段，而不是让两个插件在同一个
AstrBot 进程里各自注册 handler 互相抢消息：

1. 两个插件都注册 `event_message_type`，谁先停止事件取决于优先级，属于不可控的
   环境输入（聚合插件自己就吃过这个亏，见其设计文档 §25）；
2. 聚合之后**必须**在合并文本上做一次话题判定，而不是对每条碎片消息各判一次——
   否则"一句话拆三条发"会把 `turns` 推快三倍，话题边界直接失真；
3. 一个 handler、一个配置面板、一个数据目录，用户只需要维护一个插件。

所以本模块只保留**业务能力**（缓冲、自适应等待、重构、附件保活、统计、页面），
handler 全部由 `router.py` 注册，生命周期由 `TopicRouterPlugin` 驱动。

## 与独立插件的关系

* 配置字段、预设、等待策略、状态机、统计口径与独立插件保持一致；
* `cfg.enabled` 只表示"聚合阶段是否参与"，插件是否加载仍由话题路由器的顶层
  `enabled` 决定；
* `data_dir` 由调用方显式传入（跟随话题路由器的 `plugin_data`），不再自己猜路径；
* 独立插件里的 `on_llm_request` 抢占检测保留为 `check_preempted()`，由话题路由器
  的请求钩子调用——它现在同时是"聚合是否被抢占"和"话题判定是否被抢占"的探针。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from . import attachments as attachments_mod
from . import capabilities as caps_mod
from . import event_io, stats as stats_mod, strategy, web_api
from .config import (
    PLUGIN_NAME,
    PRESETS,
    PRESET_NAMES,
    Config,
    apply_preset as _apply_preset,
    describe_effective,
    load_config,
    validate,
)
from .observability import log_event, snapshot_state
from .session import Round, RoundState, SessionStore
from .timer import DebounceTimer, RealClock

MESSAGE_PRIORITY = 1000
"""聚合阶段在插件层的顶格优先级（与独立插件一致，理由见其设计文档 §25）。

`router.py` 用同一个数字注册消息 handler：聚合必须**先于**普通插件看到消息，
否则别人先 `stop_event()` 后我们连缓冲的机会都没有。取 1000 只降低被抢占的
概率，不提供保证——保证由 `check_preempted()` 把失败变成可见告警。
"""

SEEN_ATTR = "_topic_router_seen_v090"
"""事件对象上的"本插件消息入口执行过"标记（由 router 与聚合服务共用）。"""

SHUTDOWN_GRACE = 2.0
"""shutdown 时等待进行中轮次收尾的秒数。"""

OnSettled = Callable[[Any, str], Awaitable[None]]


class AggregationService:
    """一次插件生命周期内的聚合服务。"""

    def __init__(
        self,
        context: Any,
        config: dict[str, Any] | None,
        *,
        data_dir: Path,
        seen_attr: str = SEEN_ATTR,
        message_handler_name: str = "handle_message_v090",
        clock: Any = None,
        persist_preset: Any = None,
    ) -> None:
        """初始化。

        Args:
            context: AstrBot 上下文（页面注册用，可为 None）。
            config: 聚合配置字典（`config.aggregation` 子项）。
            data_dir: 插件数据目录（统计、附件缓存都放这里）。
            seen_attr: 事件上的"入口执行过"标记名。
            message_handler_name: 消息 handler 的函数名，用于核对该事件是否
                真的被派发到了本插件（抢占检测的必要条件）。
            clock: 注入的时间源（测试用 FakeClock）。
            persist_preset: 页面切换预设后把显式值写回插件配置的回调。
                v0.9.3 之前只改内存里的 Config，重载插件就回到旧设置。
        """
        self.context = context
        self.cfg: Config = load_config(config)
        self.clock = clock or RealClock()
        self.seen_attr = seen_attr
        self.message_handler_name = message_handler_name
        self.persist_preset = persist_preset
        self.on_settled: OnSettled | None = None
        """合并并重构事件成功后的回调（话题判定挂在这里）。

        回调在 `end_finalize` 之后、handler 返回之前执行，因此：
        * 事件已经带着合并后的文本，下游拿到的是**一轮**完整消息；
        * 不持有 `finalize_lock`，不会阻塞同桶后续轮次；
        * 回调异常只记日志，绝不放掉一个未重构的事件。
        """

        self._shutting_down = False
        self._started = False
        self._task_list: list[asyncio.Task] = []
        self._caps_probed = False

        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        self.stats = stats_mod.Stats(data_dir=data_dir, cfg=self.cfg, clock=self.clock)
        self.stats.load()
        self.stats.track_config(stats_mod.config_hash(self.cfg))

        self.store = SessionStore(
            clock=self.clock, cfg=self.cfg, on_round_end=self._on_round_end
        )
        self.attachments = attachments_mod.Attachments(
            data_dir=data_dir, cfg=self.cfg, clock=self.clock
        )
        self.caps = caps_mod.Capabilities()

        logger.info(
            f"[{PLUGIN_NAME}] 聚合阶段已构造 {describe_effective(self.cfg)}"
            f" | 数据目录={data_dir} | 优先级={MESSAGE_PRIORITY}"
        )

    # ------------------------------------------------------------------ 生命周期
    def initialize(self) -> None:
        """插件加载完成：注册页面后端、启动后台任务。

        刻意是**同步**方法：AstrBot 的 `initialize()` 调用点没有对本方法做特殊
        包装，同步注册路由与建任务更不容易出竞态；真正的异步启动在
        `_start_tasks()` 里完成。
        """
        if self.context is not None:
            web_api.register(self.context, self)
        self._start_tasks()
        logger.info(
            f"[{PLUGIN_NAME}] 聚合阶段已加载 | 群聊合并且"
            f"{'开' if self.cfg.group_enabled else '关'}"
            f" | 页面={'开' if self.cfg.page_enabled else '关'}"
        )
        logger.info(
            f"[{PLUGIN_NAME}] 聚合统计已恢复：flush={self.stats.flush_total}"
            f" abort={self.stats.abort_total} 合并率={self.stats.merge_ratio():.2f}"
        )

    async def shutdown(self) -> None:
        """优雅关停（进行中的轮次静默，不写 cooldown，统计落盘）。"""
        if self._shutting_down:
            return
        self._shutting_down = True

        pending = list(self.store.active.values()) + list(
            self.store.finalizing.values()
        )
        for rnd in pending:
            self.store.abort(rnd, cause="shutdown")
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(r.done.wait() for r in pending)),
                    timeout=SHUTDOWN_GRACE,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{PLUGIN_NAME}] 聚合 shutdown 等待 {len(pending)} 个轮次超时"
                    f"（{SHUTDOWN_GRACE}s），已强制继续"
                )

        for task in self._task_list:
            task.cancel()
        if self._task_list:
            await asyncio.gather(*self._task_list, return_exceptions=True)
        self._task_list.clear()

        self.stats.save()

    def _start_tasks(self) -> None:
        """启动后台任务（统计落盘、缓存清理）。"""
        if self._started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(f"[{PLUGIN_NAME}] 无运行中的事件循环，聚合后台任务延后启动")
            return
        self._started = True
        self._task_list.append(loop.create_task(self._stats_loop()))
        self._task_list.append(loop.create_task(self._housekeeping_loop()))

    async def _stats_loop(self) -> None:
        """按间隔落盘统计（也响应"累计 N 轮"的即时落盘）。"""
        try:
            while not self._shutting_down:
                await asyncio.sleep(max(5.0, float(self.cfg.stats_flush_interval)))
                if self._shutting_down:
                    return
                if (
                    self.stats.need_flush()
                    or self.stats.revision != self.stats._last_flush_revision
                ):
                    self.stats.save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 聚合统计落盘任务异常退出：{exc}")

    async def _housekeeping_loop(self) -> None:
        """定期清理过期缓存与 cooldown。"""
        try:
            while not self._shutting_down:
                await asyncio.sleep(3600.0)
                if self._shutting_down:
                    return
                self.attachments.cleanup()
                self.store.gc_cooldown()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 聚合清理任务异常退出：{exc}")

    # ------------------------------------------------------------------ 主流程
    async def handle(self, event: Any) -> None:
        """分流与编排：拦截、缓冲、重构。

        **调用方必须保证本方法不抛异常**（或者自己兜住）：AstrBot 的派发层在
        handler 抛异常时 `stop_event()`，会连累同一条消息上排在后面的插件。

        Args:
            event: 消息事件。
        """
        if not self.cfg.enabled or self._shutting_down:
            return
        if not self._started:
            self._start_tasks()

        # 打标记：只要进了这里，就说明"我们看到了这条消息"。
        # 抢占检测靠它区分"没被派发到"与"派发到了但没内容可合并"。
        self._mark_seen(event)

        # 通知类事件在 `_accepts` **之前**处理：群聊 input_status 是 notice，
        # `is_wake` 未必为真，先做过滤会把群聊输入感知整体误杀。
        if self.cfg.typing_detection and caps_mod.is_typing_notice(event):
            self._on_typing(event)
            return
        if self.cfg.recall_filter and caps_mod.is_recall_notice(event):
            self._on_recall(event)
            return

        if not self._accepts(event):
            return

        self._probe_caps(event)
        parsed = event_io.parse_incoming(event, self.cfg)

        # 先判指令，再持久化附件，避免命令消息产生孤儿缓存
        if event_io.is_command(parsed.text(), self.cfg.command_prefixes):
            await self._interrupt(event_io.key_of(event))
            return
        if not parsed.has_content():
            return

        parsed = await self.attachments.persist(parsed, event)

        key = event_io.key_of(event)
        channel = event_io.channel_of(event)
        rnd = self.store.get_or_create(key, event, channel=channel)
        item = parsed.to_item(self.clock.now())
        if not self.store.append(rnd, item):
            # 追加失败时，非 Owner 事件仍然必须静默：否则它带着原始内容流到
            # 下游，用户会收到一条多余的回复（由"单轮配额满且本条无文本"触发）。
            if rnd.owner_event is not event:
                self._silence(event)
            return

        log_event(
            "aggregate.append",
            umo=key[0],
            items=len(rnd.items),
            images=len(parsed.images()),
            wait=round(rnd.timer.remaining(), 2) if rnd.timer else 0,
        )

        # Owner 与非 Owner 都要延长等待（从最后一条起算），
        # 例外是 TYPING_PAUSED（此时不得 reset，否则等于解除暂停）。
        if rnd.state is not RoundState.TYPING_PAUSED:
            wait = self._compute_wait(rnd)
            self._arm_timer(rnd, wait)

        if rnd.owner_event is not event:
            self._silence(event)
            return

        try:
            await rnd.flush.wait()
            await self._after_flush(rnd)
            await self._notify_settled(rnd)
        except asyncio.CancelledError:
            self.store.abort(rnd, cause="cancelled")
            raise

    async def _notify_settled(self, rnd: Round) -> None:
        """把"这一轮合并成功"通知给上层（话题判定）。

        只在真正重构过事件、且没有被放弃时调用。回调里的任何异常都只记日志：
        话题判定失败不应该影响用户已经拿到的回复。

        Args:
            rnd: 已结束的轮次。
        """
        callback = self.on_settled
        if callback is None or not rnd.merged_text:
            return
        if rnd.shutting_down or self._shutting_down:
            return
        try:
            await callback(rnd.owner_event, rnd.merged_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 判定失败绝不影响正常回复
            logger.warning(
                f"[{PLUGIN_NAME}] 聚合后回调失败（已兜住）：{type(exc).__name__}: {exc}"
            )

    def _compute_wait(self, rnd: Round) -> float:
        """算本轮下一次等待。

        Args:
            rnd: 轮次。

        Returns:
            秒数（<= 0 表示立即结算）。
        """
        return strategy.compute(
            now=self.clock.now(),
            started_at=rnd.started_at,
            text_len=rnd.text_len(),
            tail_char=rnd.tail_char(),
            short_streak=rnd.short_streak,
            cooldown_remaining=self.store.cooldown_remaining(rnd.key),
            cfg=self.cfg,
        )

    def _arm_timer(self, rnd: Round, seconds: float) -> None:
        """武装计时器（复用 Round 自带对象）。

        Args:
            rnd: 轮次。
            seconds: 等待秒数。
        """
        if rnd.timer is None:
            rnd.timer = DebounceTimer(self.clock, on_fire=lambda: self._on_fire(rnd))
        wait = max(0.0, float(seconds))
        if rnd.started_at <= 0:
            rnd.started_at = self.clock.now()
        log_event(
            "aggregate.start",
            umo=rnd.key[0],
            sender=rnd.key[1],
            wait=round(wait, 2),
            reason="adaptive" if self.cfg.adaptive else "fixed",
        )
        rnd.timer.reset(wait)

    def _on_fire(self, rnd: Round) -> None:
        """计时器到期：同步退役 + 放行（中间无 await）。

        Args:
            rnd: 轮次。
        """
        if not self.store.begin_finalize(rnd):
            log_event("aggregate.stale_timer", key=rnd.key, round_id=rnd.round_id)
            return
        rnd.flush.set()

    async def _after_flush(self, rnd: Round) -> None:
        """Owner 从挂起返回后的唯一分支点。

        注意：abort 之后必须由 Owner **自己**静默，不能裸 return，
        否则该事件会带着首条消息继续流向下游，产生一条多余的回复。

        Args:
            rnd: 轮次。
        """
        if rnd.shutting_down or self._shutting_down:
            self._silence(rnd.owner_event, cause="abandoned")
            self.store.end_finalize(rnd, cooldown=0.0)
            return
        await self._finalize(rnd)

    async def _finalize(self, rnd: Round) -> None:
        """结算（检查点化：每个可能挂起的位置之后都复查放弃）。

        Args:
            rnd: 轮次。
        """
        acquired = False
        started = time.perf_counter()
        try:
            async with self.store.finalize_lock(rnd.key):
                acquired = True
                snap = rnd.frozen or rnd.snapshot()

                # 检查点 ①：入口
                if self._abandoned(rnd):
                    self._silence(rnd.owner_event, cause="abandoned")
                    return

                try:
                    text = snap.text(self.cfg.separator)
                    images = list(snap.images())
                    images = self.attachments.normalize(images)
                    images = _dedupe(images)

                    # 检查点 ②：附件校验之后
                    if self._abandoned(rnd):
                        self._silence(rnd.owner_event, cause="abandoned")
                        return

                    # 检查点 ③：reconstruct 之前（最后一道闸）
                    if self._abandoned(rnd):
                        self._silence(rnd.owner_event, cause="abandoned")
                        return

                    if not text.strip() and not images:
                        self._silence(rnd.owner_event, cause="empty")
                        return

                    if self.cfg.preserve_part_order:
                        event_io.reconstruct_ordered(
                            rnd.owner_event, snap, images, self.cfg
                        )
                    else:
                        event_io.reconstruct(
                            rnd.owner_event, text, images, self.cfg
                        )
                    # 只有重构成功后才给上层"可判定文本"——它同时是话题判定的输入
                    rnd.merged_text = text
                except Exception:
                    logger.exception(f"[{PLUGIN_NAME}] aggregate.finalize_failed")
                    self.store.abort(rnd, cause="finalize_failed")
                    self._silence(rnd.owner_event, cause="failed")
        finally:
            rnd.elapsed_ms = (time.perf_counter() - started) * 1000.0
            # shutdown 期间不写 cooldown
            cooldown = 0.0
            if acquired and not rnd.shutting_down and not self._shutting_down:
                cooldown = self._cooldown_for(rnd)
            self.store.end_finalize(rnd, cooldown)

    def _abandoned(self, rnd: Round) -> bool:
        """放弃判定：单轮 abort 或整插件 shutdown。

        Args:
            rnd: 轮次。

        Returns:
            True 表示应放弃。
        """
        return rnd.shutting_down or self._shutting_down

    def _cooldown_for(self, rnd: Round) -> float:
        """按渠道取 cooldown 秒数（仅当 debounce < min_interval 时才有意义）。

        Args:
            rnd: 轮次。

        Returns:
            秒数。
        """
        interval = (
            self.cfg.min_interval_private
            if rnd.channel == "private"
            else self.cfg.min_interval_group
        )
        if interval <= 0 or self.cfg.debounce_time >= interval:
            return 0.0
        return float(interval)

    def _on_round_end(self, rnd: Round) -> None:
        """轮次结束回调（交给统计，幂等）。

        Args:
            rnd: 轮次。
        """
        self.stats.add_end(rnd)

    # ------------------------------------------------------------------ 通知事件
    def _on_typing(self, event: Any) -> None:
        """输入状态通知：只查表、不建表（无副作用）。

        Args:
            event: 通知事件。
        """
        key = event_io.key_of(event)
        rnd = self.store.active.get(key)
        if rnd is None or rnd.timer is None:
            return
        state = caps_mod.probe_typing(caps_mod.raw_of(event))
        if state is None:
            # 判定失败只打 debug：部分适配器每次输入都发 notice，warning 会刷屏
            self.stats.add_typing_unknown()
            log_event("aggregate.typing_unknown", umo=key[0])
            return
        if state:
            if rnd.typing:
                return
            rnd.typing = True
            rnd.state = RoundState.TYPING_PAUSED
            rnd.timer.pause()
            rnd.typing_guard = asyncio.create_task(self._typing_watchdog(rnd))
            log_event(
                "aggregate.pause",
                umo=key[0],
                cause="typing",
                max_wait=self.cfg.max_typing_wait,
            )
        elif rnd.typing:
            rnd.typing = False
            rnd.state = RoundState.COLLECTING
            guard = rnd.typing_guard
            if guard is not None and not guard.done():
                current = asyncio.current_task()
                if guard is not current:
                    guard.cancel()
            rnd.typing_guard = None
            rnd.timer.resume()

    async def _typing_watchdog(self, rnd: Round) -> None:
        """输入状态的最长暂停兜底。

        走注入的时钟而不是 `asyncio.sleep`：否则测试里 60 秒就只能真等。

        Args:
            rnd: 轮次。
        """
        try:
            await self.clock.sleep(max(1.0, float(self.cfg.max_typing_wait)))
        except asyncio.CancelledError:
            return
        self._on_fire(rnd)

    def _on_recall(self, event: Any) -> None:
        """撤回通知：按索引定位桶并标记（只查表，无副作用）。

        Args:
            event: 通知事件。
        """
        mid = caps_mod.recalled_message_id(event)
        if mid is None:
            return
        key = self.store.index.get(mid)
        if key is None:
            return
        rnd = self.store.active.get(key)
        if rnd is None:
            # 已退役：冻结快照不再接受撤回
            return
        for idx, item in enumerate(rnd.items):
            if item.message_id == mid:
                rnd.items[idx] = replace(item, recalled=True)
                self.caps.mark_recall_seen()
                log_event(
                    "aggregate.recall",
                    umo=key[0],
                    message_id=mid,
                    remaining=len(rnd.items) - 1,
                )
                return

    # ------------------------------------------------------------------ 指令中断
    async def _interrupt(self, key: tuple[str, str]) -> None:
        """指令打断：先把该桶结算完，再放行指令。

        Args:
            key: 桶键。
        """
        rnd = self.store.active.get(key)
        if rnd is not None:
            self.store.begin_finalize(rnd)
            rnd.flush.set()
        if not self.store.finalizing_rounds(key):
            return
        self.stats.add_interrupt()
        try:
            await asyncio.wait_for(
                self.store.wait_idle(key), timeout=float(self.cfg.interrupt_wait)
            )
        except asyncio.TimeoutError:
            pending = self.store.finalizing_rounds(key)
            self.stats.add_interrupt(timeout=True)
            log_event(
                "aggregate.interrupt_timeout", key=key, pending=len(pending)
            )
            for extra in pending:
                self.store.abort(extra, cause="interrupt_timeout")

    # ------------------------------------------------------------------ 杂项
    def _accepts(self, event: Any) -> bool:
        """是否处理该事件。

        Args:
            event: 消息事件。

        Returns:
            True 表示参与聚合。
        """
        if not self.cfg.group_enabled and event_io.channel_of(event) == "group":
            return False
        return True

    def _silence(self, event: Any, cause: str | None = None) -> None:
        """唯一静默入口。

        `cause=None` 表示**常规静默、不计数**——非 Owner 事件静默退出是聚合的
        正常路径（每条补充消息都会走一次）。把它计入 `drop_total`，计数就会被
        消息条数撑爆，"丢弃"这个指标当场失去意义。

        Args:
            event: 事件。
            cause: 丢弃原因；None 表示常规静默。
        """
        try:
            event_io.silence(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 聚合静默失败（已忽略）：{exc}")
        if cause is not None:
            self.stats.add_drop(cause)
            log_event("aggregate.drop", umo=event_io.umo_of(event), cause=cause)

    def panic_release(self, event: Any) -> None:
        """异常兜底：尽量保证事件不被卡住。

        Args:
            event: 事件。
        """
        try:
            key = event_io.key_of(event)
        except Exception:  # noqa: BLE001
            return
        rnd = self.store.active.get(key)
        if rnd is not None and rnd.owner_event is event:
            self.store.abort(rnd, cause="exception")
            self._silence(event, cause="failed")

    def _dispatch_snapshot(self, event: Any) -> tuple[list[Any], str]:
        """取"这次派发计划里有哪些 handler"以及本插件在其中的位置。

        这是抢占检测从"猜"变成"可证"的关键：`activated_handlers` 由
        `waking_check` 在派发前写入事件，里面是**这次真的会被调用的** handler
        （已按优先级降序）。如果我们的 handler 不在里面，说明这条事件根本没
        被派发给我们（例如其它插件直接 `request_llm()` 的合成事件），不能叫
        "被抢占"——那正是 v0.9.0 误报的来源。

        Args:
            event: 消息事件。

        Returns:
            `(handlers, self_index)`；`self_index` 为 -1 表示不在派发计划里。
        """
        try:
            handlers = list(event.get_extra("activated_handlers") or [])
        except Exception:  # noqa: BLE001
            return [], -1
        names = [str(getattr(h, "handler_name", "") or "") for h in handlers]
        try:
            index = names.index(self.message_handler_name)
        except ValueError:
            index = -1
        return handlers, index

    def _should_have_handled(self, event: Any) -> bool:
        """判断"这条消息本来该由聚合阶段处理"（抢占检测用，刻意保守）。

        必要条件：本插件的消息 handler **在这次的 `activated_handlers` 里**。
        合成事件（别的插件直接 `request_llm`、主动回复、Agent 重放）没有这份
        派发计划，或计划里没有我们，不能计为抢占——否则会把正常行为报成故障。

        Args:
            event: 消息事件。

        Returns:
            True 表示如果没被抢占，本阶段本应处理它。
        """
        _handlers, index = self._dispatch_snapshot(event)
        if index < 0:
            return False
        if caps_mod.is_typing_notice(event) or caps_mod.is_recall_notice(event):
            return False
        if not self._accepts(event):
            return False
        text = str(getattr(event, "message_str", "") or "")
        if event_io.is_command(text, self.cfg.command_prefixes):
            return False
        try:
            return event_io.parse_incoming(event, self.cfg).has_content()
        except Exception:  # noqa: BLE001
            return False

    def check_preempted(self, event: Any) -> bool:
        """抢占检测：本该处理却没处理到，说明被更高优先级者挤掉了。

        **不做任何补救动作**（补救会和对方抢，反而更乱），只告警与计数，
        让"优先级不足"从静默故障变成可观测现象。告警里带**排在我们前面的
        handler 名单与优先级**，下一次不必再猜是谁挡的。

        Args:
            event: 消息事件。

        Returns:
            True 表示这次确实判定为被抢占（调用方据此落 trace 证据）。
        """
        if getattr(event, self.seen_attr, False):
            return False
        if not self._should_have_handled(event):
            return False
        handlers, index = self._dispatch_snapshot(event)
        prior = []
        for handler in handlers[:index][-3:]:
            priority = (getattr(handler, "extras_configs", None) or {}).get(
                "priority", "?"
            )
            prior.append(f"{getattr(handler, 'handler_name', '?')}@{priority}")
        try:
            setattr(event, self.seen_attr, True)
        except Exception:  # noqa: BLE001
            pass
        self.stats.add_preempted()
        log_event(
            "aggregate.preempted",
            umo=event_io.umo_of(event),
            priority=MESSAGE_PRIORITY,
            dispatched=len(handlers),
            position=index,
            msg_id=getattr(getattr(event, "message_obj", None), "message_id", "?"),
            prior=",".join(prior) or "(none)",
            hint=(
                "本条消息未进入聚合：派发计划里有本插件，但入口没被执行"
                "（通常是排在前面的 handler 静默了事件）。"
                "prior= 是紧挨在本插件前面的执行者；若 position=0 仍被跳过，"
                "说明事件在派发循环之外被消费"
            ),
        )
        return True

    def state_rows(self) -> list[dict[str, Any]]:
        """页面用的状态行。

        Returns:
            行字典列表。
        """
        return snapshot_state(self.store)

    def apply_preset(self, name: str) -> dict[str, Any]:
        """切换预设并落到运行中的配置上。

        Args:
            name: 预设名（已由调用方校验）。

        Returns:
            生效值摘要。
        """
        if name not in PRESET_NAMES:
            raise ValueError(f"unknown preset: {name}")
        self.cfg = validate(_apply_preset(_replace_preset(self.cfg, name)))
        self.store.cfg = self.cfg
        self.stats.cfg = self.cfg
        self.attachments.cfg = self.cfg
        effective = dict(PRESETS[name])
        applied = {key: getattr(self.cfg, key) for key in PRESETS[name]}
        applied["preset"] = self.cfg.preset
        logger.info(f"[{PLUGIN_NAME}] 聚合预设已切换为 {name}：{effective}")

        # v0.9.3：把显式值写回插件配置。只改内存的话，重载插件/重启 AstrBot 就
        # 悄悄回到旧预设，页面显示的也是旧值（现场踩过：页面显示 patient，
        # 重建后变回 balanced）。写失败不影响本次生效，只记一条 warning。
        persist = self.persist_preset
        if callable(persist):
            try:
                persist(applied)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[{PLUGIN_NAME}] 聚合预设写回配置失败（本次仍生效）："
                    f"{type(exc).__name__}: {exc}"
                )

        return {
            "debounce_time": self.cfg.debounce_time,
            "adaptive": self.cfg.adaptive,
            "adaptive_max_wait": self.cfg.adaptive_max_wait,
            "adaptive_max_total_wait": self.cfg.adaptive_max_total_wait,
        }

    # ------------------------------------------------------------------ 内部
    def _mark_seen(self, event: Any) -> None:
        """给事件打"入口执行过"标记（失败不致命，去重还有第二层）。"""
        try:
            setattr(event, self.seen_attr, True)
        except Exception:  # noqa: BLE001 - 少数事件对象可能有 __slots__
            pass

    def _probe_caps(self, event: Any) -> None:
        """第一次见到真实消息时做平台能力探测（只做一次）。

        Args:
            event: 消息事件。
        """
        if self._caps_probed:
            return
        self._caps_probed = True
        try:
            probed = caps_mod.probe(event)
        except Exception:  # noqa: BLE001
            return
        self.caps.temp_file_tracking = probed.temp_file_tracking
        self.caps.temp_file_untrack = probed.temp_file_untrack
        self.caps.typing_feedback = probed.typing_feedback
        logger.info(
            f"[{PLUGIN_NAME}] 聚合能力探测：{self.caps.as_log_line()}"
        )


def _replace_preset(cfg: Config, name: str) -> Config:
    """只替换 `preset` 字段（其余由 `apply_preset` 覆盖）。

    Args:
        cfg: 配置。
        name: 预设名。

    Returns:
        新配置。
    """
    return replace(cfg, preset=name)


def _dedupe(images: list[str]) -> list[str]:
    """按引用去重（保持首次出现顺序；不用内容哈希——那要额外读文件）。

    Args:
        images: 引用列表。

    Returns:
        去重后的列表。
    """
    seen: set[str] = set()
    out: list[str] = []
    for ref in images:
        if ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
    return out

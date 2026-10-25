"""插件可视化页面的后端（设计文档 §24.3 / §24.4）。

**只读契约（硬约束）**：本模块只经 `stats.snapshot()` / `snapshot_state()` /
`stats.history_records()` 读取，**不持有 Round 引用**，也不调用
`get_or_create` / `begin_finalize` / `abort` / `finalize_lock`。
那几个都是状态机的转移函数，页面若成为第三条触发路径，§10 的不变式立刻失效。

**校验口径**（这里最容易写错，所以刻意区分）：

* `limit` 越界 → **clamp**：用户只是想多要点，不是错；
* `limit` 非整数 → **400**：那是前端 bug，静默 clamp 会把 bug 藏起来；
* `preset` 非法 → **400**：会改变状态，必须拒绝；
* `reset` 缺确认串 → **400**：不可逆操作。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from astrbot.api import logger

from .config import PLUGIN_NAME, PRESET_NAMES
from .observability import log_event, snapshot_state

try:  # pragma: no cover - 老版本没有 api.web 时页面整体降级
    from astrbot.api.web import error_response, json_response, request, stream_response

    WEB_API_AVAILABLE = True
except Exception:  # noqa: BLE001
    error_response = None  # type: ignore[assignment]
    json_response = None  # type: ignore[assignment]
    stream_response = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    WEB_API_AVAILABLE = False

HISTORY_MAX = 200
HISTORY_DEFAULT = 50


# --------------------------------------------------------------------- 纯校验函数
def parse_limit(raw: Any, *, default: int = HISTORY_DEFAULT,
                maximum: int = HISTORY_MAX) -> int:
    """解析并夹紧 `limit`。

    Args:
        raw: 原始值（字符串或数字）。
        default: 缺省值。
        maximum: 上限。

    Returns:
        夹到 `[1, maximum]` 的整数。

    Raises:
        ValueError: 传入的不是整数（**不静默夹紧**，因为那会掩盖前端 bug）。
    """
    if raw is None or raw == "":
        return max(1, min(maximum, default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    return max(1, min(maximum, value))


def validate_preset(name: Any) -> str:
    """校验预设名。

    Args:
        name: 传入的名字。

    Returns:
        合法的预设名。

    Raises:
        ValueError: 不在白名单内。
    """
    text = str(name or "").strip()
    if text not in PRESET_NAMES:
        raise ValueError(f"unknown preset: {text!r}")
    return text


def reset_allowed(payload: Any) -> bool:
    """判断清空统计的请求是否带了确认串。

    Args:
        payload: 请求体。

    Returns:
        True 表示允许执行。
    """
    return isinstance(payload, dict) and payload.get("confirm") == "reset"


# --------------------------------------------------------------------- 页面 API
class PageApi:
    """把插件内部状态暴露成页面的只读视图 + 两个写操作。"""

    def __init__(self, plugin: Any) -> None:
        """初始化。

        Args:
            plugin: 插件实例（读其 `stats` / `store` / `cfg`）。
        """
        self.plugin = plugin

    # ------------------------------------------------------------------ 读
    async def api_stats(self) -> Any:
        """汇总统计（不含全量历史）。

        Returns:
            JSON 响应。
        """
        snap = self.plugin.stats.snapshot(history_limit=0)
        return json_response(
            {
                "counters": dict(snap.counters),
                "merge_ratio": snap.merge_ratio,
                "by_channel": {k: v.to_dict() for k, v in snap.by_channel.items()},
                "by_umo_top": [list(item) for item in snap.by_umo_top],
                "by_sender_top": [list(item) for item in snap.by_sender_top],
                "drop_by_cause": dict(snap.drop_by_cause),
                "latency": {
                    "p50_ms": snap.latency.p50_ms,
                    "p95_ms": snap.latency.p95_ms,
                    "buckets": list(snap.latency.buckets),
                    "total": snap.latency.total,
                },
                "revision": snap.revision,
                "saved_at": snap.saved_at,
            }
        )

    async def api_rounds(self) -> Any:
        """实时轮次快照。

        Returns:
            JSON 响应。
        """
        rows = snapshot_state(self.plugin.store)
        return json_response({"rows": rows, "count": len(rows)})

    async def api_history(self) -> Any:
        """最近 N 条处理记录。

        Returns:
            JSON 响应或错误响应。
        """
        try:
            limit = parse_limit(request.query.get("limit", HISTORY_DEFAULT))
        except ValueError as exc:
            return self._bad_request("history", str(exc))
        return json_response(
            {
                "items": self.plugin.stats.history_records(limit),
                "limit": limit,
                "max": HISTORY_MAX,
                "revision": self.plugin.stats.revision,
            }
        )

    async def api_preset(self) -> Any:
        """当前预设与生效关键值。

        Returns:
            JSON 响应。
        """
        cfg = self.plugin.cfg
        return json_response(
            {
                "preset": cfg.preset,
                "presets": list(PRESET_NAMES),
                "effective": {
                    "debounce_time": cfg.debounce_time,
                    "adaptive": cfg.adaptive,
                    "adaptive_max_wait": cfg.adaptive_max_wait,
                    "adaptive_max_total_wait": cfg.adaptive_max_total_wait,
                },
            }
        )

    # ------------------------------------------------------------------ 写
    async def api_set_preset(self) -> Any:
        """切换预设（白名单校验 + 落成显式值）。

        Returns:
            JSON 响应或错误响应。
        """
        try:
            payload = await request.json(default={})
        except Exception as exc:  # noqa: BLE001
            return self._bad_request("preset", f"invalid body: {exc}")
        raw = payload.get("preset") if isinstance(payload, dict) else None
        try:
            name = validate_preset(raw)
        except ValueError as exc:
            return self._bad_request("preset", str(exc))
        try:
            applied = self.plugin.apply_preset(name)
        except Exception as exc:  # noqa: BLE001 - 配置写失败不能影响聚合
            logger.exception(f"[{PLUGIN_NAME}] 切换预设失败")
            return error_response(f"apply preset failed: {exc}", status_code=500)
        log_event("page.preset_changed", preset=name)
        return json_response({"preset": name, "effective": applied})

    async def api_reset(self) -> Any:
        """清空统计（需要确认串）。

        Returns:
            JSON 响应或错误响应。
        """
        try:
            payload = await request.json(default={})
        except Exception:  # noqa: BLE001
            payload = {}
        if not reset_allowed(payload):
            return self._bad_request("stats.reset", 'body must be {"confirm": "reset"}')
        self.plugin.stats.reset()
        return json_response({"reset": True, "revision": self.plugin.stats.revision})

    # ------------------------------------------------------------------ 放权 / 复盘
    def _extra(self, key: str) -> Any:
        """取 router 挂上来的旁路能力（放权/复盘），没有就返回 None。

        Args:
            key: `promotion` / `promotion_reset` / `review` / `run_review`。

        Returns:
            可调用对象或 None。
        """
        extras = getattr(self.plugin, "extras", None)
        if not isinstance(extras, dict):
            return None
        return extras.get(key)

    async def api_promotion(self) -> Any:
        """阶梯放权进度（GET）。

        Returns:
            JSON 响应。
        """
        view = self._extra("promotion")
        if not callable(view):
            return json_response({"enabled": False, "error": "promotion unavailable"})
        try:
            return json_response(view())
        except Exception as exc:  # noqa: BLE001
            return error_response(f"promotion view failed: {exc}", status_code=500)

    async def api_promotion_reset(self) -> Any:
        """重置放权计时（POST）。

        Returns:
            JSON 响应。
        """
        reset = self._extra("promotion_reset")
        if not callable(reset):
            return error_response("promotion unavailable", status_code=501)
        try:
            return json_response(await reset())
        except Exception as exc:  # noqa: BLE001
            return error_response(f"promotion reset failed: {exc}", status_code=500)

    async def api_review(self) -> Any:
        """最新一份每日复盘 + 复盘配置（GET）。

        Returns:
            JSON 响应。
        """
        view = self._extra("review")
        config = self._extra("review_config")
        if not callable(view):
            return json_response({"review": None, "config": None})
        try:
            return json_response(
                {
                    "review": view(),
                    "config": config() if callable(config) else None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return error_response(f"review view failed: {exc}", status_code=500)

    async def api_review_run(self) -> Any:
        """立即生成今天的复盘（POST）。

        Returns:
            JSON 响应。
        """
        run = self._extra("run_review")
        if not callable(run):
            return error_response("review unavailable", status_code=501)
        try:
            report = await run()
            return json_response({"review": report})
        except Exception as exc:  # noqa: BLE001
            return error_response(f"review failed: {exc}", status_code=500)

    async def api_settings(self) -> Any:
        """保存页面上的放权 / 复盘设置（POST）。

        Returns:
            JSON 响应或错误响应。
        """
        update = self._extra("settings")
        if not callable(update):
            return error_response("settings unavailable", status_code=501)
        try:
            payload = await request.json(default={})
        except Exception as exc:  # noqa: BLE001
            return self._bad_request("settings", f"invalid body: {exc}")
        try:
            return json_response(update(payload))
        except ValueError as exc:
            return self._bad_request("settings", str(exc))
        except Exception as exc:  # noqa: BLE001
            return error_response(f"settings failed: {exc}", status_code=500)

    # ------------------------------------------------------------------ SSE
    async def api_events(self) -> Any:
        """SSE：按 `page_push_interval` 推送摘要，**变化才推**（§24.4）。

        Returns:
            SSE 响应。
        """
        interval = float(self.plugin.cfg.page_push_interval)
        plugin = self.plugin

        async def frames():
            last_key: tuple[int, int] | None = None
            # 先立即给一帧，避免页面空白等待
            while True:
                stats = plugin.stats
                rows = snapshot_state(plugin.store)
                key = (stats.revision, len(rows))
                if key != last_key:
                    last_key = key
                    snap = stats.snapshot(history_limit=1)
                    payload = {
                        "revision": stats.revision,
                        "merge_ratio": snap.merge_ratio,
                        "flush_total": snap.counters.get("flush_total", 0),
                        "abort_total": snap.counters.get("abort_total", 0),
                        "drop_total": snap.counters.get("drop_total", 0),
                        "items_total": snap.counters.get("items_total", 0),
                        "active_rounds": len(rows),
                        "recent": [r.to_dict() for r in snap.history],
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(interval)

        return stream_response(frames())

    # ------------------------------------------------------------------ 内部
    def _bad_request(self, route: str, reason: str) -> Any:
        """统一的 400 + 日志。

        Args:
            route: 路由名。
            reason: 原因。

        Returns:
            错误响应。
        """
        log_event("page.api_error", route=route, reason=reason)
        return error_response(reason, status_code=400)


ROUTES = (
    ("/{plugin}/stats", "api_stats", ("GET",), "Aggregate stats"),
    ("/{plugin}/rounds", "api_rounds", ("GET",), "Live rounds"),
    ("/{plugin}/history", "api_history", ("GET",), "Flush history"),
    ("/{plugin}/preset", "api_preset", ("GET",), "Current preset"),
    ("/{plugin}/promotion", "api_promotion", ("GET",), "Promotion progress"),
    ("/{plugin}/review", "api_review", ("GET",), "Latest daily review"),
    ("/{plugin}/events", "api_events", ("GET",), "SSE summary"),
)


def register(context: Any, plugin: Any) -> bool:
    """注册页面后端路由。

    `page_enabled=False` 时**完全不注册**（而不是"注册了再拒绝"）：
    减少攻击面，且 404 比 403 更早暴露配置状态。

    Args:
        context: AstrBot Context。
        plugin: 插件实例。

    Returns:
        True 表示注册成功。
    """
    if not plugin.cfg.page_enabled:
        logger.info(f"[{PLUGIN_NAME}] 页面后端未注册（page_enabled=false）")
        return False
    if not WEB_API_AVAILABLE:
        log_event("capability.missing", name="astrbot.api.web",
                  fallback="页面后端关闭", hint="升级 AstrBot 到支持 api.web 的版本")
        return False
    api = PageApi(plugin)
    try:
        for route, handler_name, methods, desc in ROUTES:
            context.register_web_api(
                route.format(plugin=PLUGIN_NAME),
                getattr(api, handler_name),
                list(methods),
                desc,
            )
        # 两个写操作单独注册（方法不同）
        context.register_web_api(
            f"/{PLUGIN_NAME}/preset", api.api_set_preset, ["POST"], "Set preset"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/stats/reset", api.api_reset, ["POST"], "Reset stats"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/promotion/reset",
            api.api_promotion_reset,
            ["POST"],
            "Reset promotion clock",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/review/run",
            api.api_review_run,
            ["POST"],
            "Run daily review now",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings",
            api.api_settings,
            ["POST"],
            "Save promotion/review settings",
        )
    except Exception as exc:  # noqa: BLE001 - 页面不可用不能拖垮插件
        logger.exception(f"[{PLUGIN_NAME}] 注册页面后端失败：{exc}")
        return False
    logger.info(f"[{PLUGIN_NAME}] 页面后端已注册 {len(ROUTES) + 5} 个路由")
    return True

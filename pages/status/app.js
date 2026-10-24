// 消息聚合 · 运行状态面板
//
// 两条硬约束（来自技术设计文档 §24.5，都是踩过的真实限制）：
//   1. 数据一律走 bridge，**不做前端相对路径 fetch**：Dashboard 用正则重写 HTML/CSS/JS
//      里的静态引用注入资源令牌，运行时拼出来的 URL 不会被改写，也就拿不到令牌。
//   2. bridge 全局名两版不同（AstrBotPluginView / AstrBotPluginPage），且旧版可能没有
//      subscribeSSE —— 都必须特性探测，不能写死版本号。

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const HISTORY_LIMIT = 50;
const POLL_MS = 3000;

const $ = (id) => document.getElementById(id);
const el = {
  dot: $("dot"), title: $("title"), live: $("live-state"), fatal: $("fatal"),
  preset: $("preset"), reset: $("reset"), refresh: $("refresh"),
  merge: $("c-merge"), flush: $("c-flush"), abort: $("c-abort"), active: $("c-active"),
  flushHint: $("c-flush-hint"),
  channels: $("channels"), drops: $("drops"), preempted: $("preempted"),
  hist: $("hist"), p50: $("p50"), p95: $("p95"), lsample: $("lsample"),
  rounds: $("rounds"), roundsCount: $("rounds-count"), roundsEmpty: $("rounds-empty"),
  history: $("history"), historyEmpty: $("history-empty"),
};

let subscriptionId = null;
let pollTimer = null;

function setFatal(text) {
  el.fatal.hidden = false;
  el.fatal.textContent = text;
  el.dot.className = "dot bad";
}

function t(key, fallback) {
  if (bridge && typeof bridge.t === "function") {
    try {
      return bridge.t(key, fallback);
    } catch (_) {
      return fallback;
    }
  }
  return fallback;
}

function fmtRatio(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "–";
  return n.toFixed(2) + "×";
}

function fmtInt(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(n) : "–";
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  return td;
}

function renderBars(host, entries, total, emptyText) {
  clear(host);
  if (!entries.length || total <= 0) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = emptyText;
    host.appendChild(p);
    return;
  }
  const max = Math.max(...entries.map(([, v]) => v), 1);
  for (const [name, value] of entries) {
    const row = document.createElement("div");
    row.className = "bar-row";

    const label = document.createElement("span");
    label.className = "name";
    label.textContent = name;
    label.title = name;

    const track = document.createElement("div");
    track.className = "track";
    const fill = document.createElement("div");
    fill.className = "fill";
    fill.style.width = Math.max(2, (Number(value) / max) * 100).toFixed(1) + "%";
    track.appendChild(fill);

    const num = document.createElement("span");
    num.className = "num";
    num.textContent = String(value);

    row.append(label, track, num);
    host.appendChild(row);
  }
}

function renderHistogram(buckets, total) {
  clear(el.hist);
  const bounds = [10, 25, 50, 100, 250, 500, 1000, 3000, 12000];
  const counts = Array.isArray(buckets) ? buckets : [];
  const max = Math.max(1, ...counts);
  const n = counts.length;
  if (!n) return;
  const gap = 6;
  const width = (640 - gap * (n - 1)) / n;
  const labels = [...bounds.map((b) => "<" + b), "≥12000"];
  counts.forEach((count, idx) => {
    const h = Math.max(1, (Number(count) / max) * 120);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", String(idx * (width + gap)));
    rect.setAttribute("y", String(130 - h));
    rect.setAttribute("width", String(width));
    rect.setAttribute("height", String(h));
    rect.setAttribute("rx", "3");
    rect.setAttribute("fill", "var(--bar)");
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = labels[idx] + "ms: " + count;
    rect.appendChild(title);
    el.hist.appendChild(rect);
  });
}

function renderRounds(rows) {
  const tbody = el.rounds.querySelector("tbody");
  clear(tbody);
  el.roundsCount.textContent = rows.length ? `（${rows.length}）` : "";
  el.roundsEmpty.hidden = rows.length > 0;
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(String(row.umo || "").slice(0, 28)));
    tr.appendChild(cell(String(row.sender || "").slice(0, 16)));
    tr.appendChild(cell("#" + row.round_id, "num"));
    const state = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = row.phase === "finalizing" ? "结算中" : String(row.state || "");
    state.appendChild(tag);
    tr.appendChild(state);
    tr.appendChild(cell(fmtInt(row.items), "num"));
    tr.appendChild(cell(Number(row.remaining || 0).toFixed(1) + "s", "num"));
    tr.appendChild(cell(row.typing ? "是" : "–"));
    tr.appendChild(cell(Number(row.cooldown || 0) > 0 ? Number(row.cooldown).toFixed(1) + "s" : "–", "num"));
    tbody.appendChild(tr);
  }
}

function renderHistory(items) {
  const tbody = el.history.querySelector("tbody");
  clear(tbody);
  el.historyEmpty.hidden = items.length > 0;
  for (const item of items) {
    const tr = document.createElement("tr");
    const when = item.ts ? new Date(item.ts * 1000).toLocaleTimeString() : "–";
    tr.appendChild(cell(when));
    tr.appendChild(cell(String(item.umo || "").slice(0, 28)));
    tr.appendChild(cell(fmtInt(item.items), "num"));
    tr.appendChild(cell(fmtInt(item.images), "num"));
    tr.appendChild(cell(fmtInt(item.elapsed_ms) + "ms", "num"));
    const outcome = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag " + (item.outcome === "abort" ? "abort" : "done");
    tag.textContent = item.outcome === "abort" ? "放弃" : "完成";
    outcome.appendChild(tag);
    tr.appendChild(outcome);
    tbody.appendChild(tr);
  }
}

function renderStats(stats) {
  el.merge.textContent = fmtRatio(stats.merge_ratio);
  el.flush.textContent = fmtInt(stats.counters?.flush_total);
  el.abort.textContent = fmtInt(stats.counters?.abort_total);
  const overflow = stats.counters?.dim_overflow_total || 0;
  if (el.flushHint) {
    const items = stats.counters?.items_total;
    el.flushHint.textContent = Number.isFinite(Number(items))
      ? `共合并 ${items} 条消息`
      : "正常完成";
  }

  const channels = Object.entries(stats.by_channel || {}).map(([name, cs]) => [
    name === "group" ? "群聊" : "私聊",
    (cs && (cs.flush + cs.abort)) || 0,
  ]);
  renderBars(el.channels, channels, channels.reduce((a, [, v]) => a + v, 0), "暂无数据");
  if (overflow > 0) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = `维度已截断：另 ${overflow} 次未计入`;
    el.channels.appendChild(p);
  }

  const drops = Object.entries(stats.drop_by_cause || {});
  renderBars(el.drops, drops, drops.reduce((a, [, v]) => a + v, 0), "暂无丢弃");

  // 抢占提示：一旦非零，说明真有执行者排在本插件前面（优先级不足的实证）
  const preempted = Number(stats.counters?.preempted_total || 0);
  if (el.preempted) {
    if (preempted > 0) {
      el.preempted.hidden = false;
      el.preempted.textContent =
        `有 ${preempted} 条消息未被聚合：它们在本插件的消息入口之前就被拦截了。` +
        `这通常意味着另一个插件的 priority 更高，或框架内置的会话控制先处理了事件。` +
        `本插件已取插件层顶格优先级并记录了明细，日志里搜 aggregate.preempted。`;
    } else {
      el.preempted.hidden = true;
      el.preempted.textContent = "";
    }
  }

  const latency = stats.latency || {};
  el.p50.textContent = fmtInt(latency.p50_ms);
  el.p95.textContent = fmtInt(latency.p95_ms);
  el.lsample.textContent = fmtInt(latency.total);
  renderHistogram(latency.buckets || [], latency.total || 0);
}

function renderActive(count) {
  el.active.textContent = fmtInt(count);
}

async function loadAll() {
  if (!bridge) return;
  try {
    const [stats, rounds, history, preset] = await Promise.all([
      bridge.apiGet("stats"),
      bridge.apiGet("rounds"),
      bridge.apiGet("history", { limit: HISTORY_LIMIT }),
      bridge.apiGet("preset"),
    ]);
    renderStats(stats);
    renderRounds(rounds.rows || []);
    renderActive((rounds.rows || []).length);
    renderHistory(history.items || []);
    fillPresets(preset);
    el.dot.className = "dot ok";
  } catch (error) {
    setFatal("读取失败：" + (error?.message || error));
  }
}

let presetsFilled = false;
function fillPresets(preset) {
  const current = preset?.preset;
  if (!presetsFilled) {
    const presets = preset?.presets || [];
    clear(el.preset);
    for (const name of presets) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      el.preset.appendChild(opt);
    }
    presetsFilled = true;
  }
  if (current) el.preset.value = current;
}

async function onPresetChange() {
  const name = el.preset.value;
  el.preset.disabled = true;
  try {
    await bridge.apiPost("preset", { preset: name });
    await loadAll();
  } catch (error) {
    setFatal("切换预设失败：" + (error?.message || error));
  } finally {
    el.preset.disabled = false;
  }
}

async function onReset() {
  if (!window.confirm("清空统计？历史明细也会一并清空（不可恢复）。")) return;
  el.reset.disabled = true;
  try {
    await bridge.apiPost("stats/reset", { confirm: "reset" });
    await loadAll();
  } catch (error) {
    setFatal("清空失败：" + (error?.message || error));
  } finally {
    el.reset.disabled = false;
  }
}

function startPolling() {
  if (pollTimer) return;
  el.live.textContent = t("views.status.live_polling", "轮询中（实时推送不可用）");
  pollTimer = setInterval(loadAll, POLL_MS);
}

async function startLive() {
  if (!bridge || typeof bridge.subscribeSSE !== "function") {
    startPolling();
    return;
  }
  try {
    subscriptionId = await bridge.subscribeSSE("events", {
      onOpen() {
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
        el.live.textContent = t("views.status.live_on", "实时");
        el.dot.className = "dot ok";
      },
      onMessage(event) {
        const data = event.parsed;
        if (!data) return;
        renderActive(data.active_rounds || 0);
        if (Array.isArray(data.recent) && data.recent.length) {
          renderHistory(data.recent.slice(-HISTORY_LIMIT));
        }
        // 摘要里有计数，但图表需要全量分布，因此仍周期性拉一次
      },
      onError() {
        el.live.textContent = t("views.status.live_error", "实时中断，改用轮询");
        startPolling();
      },
    }, { limit: HISTORY_LIMIT });
  } catch (_) {
    startPolling();
  }
}

async function boot() {
  if (!bridge) {
    setFatal(t("views.status.no_bridge",
      "当前 Dashboard 不支持插件视图（缺少 bridge），请升级 AstrBot。"));
    return;
  }
  try {
    const ctx = await bridge.ready();
    if (ctx?.displayName) {
      el.title.textContent = ctx.displayName + " · " + t("views.status.heading", "运行状态");
    }
    document.title = (ctx?.pageTitle || t("views.status.title", "消息聚合")) + "";
  } catch (_) {
    /* ready() 失败也继续尝试读数据 */
  }

  el.preset.addEventListener("change", onPresetChange);
  el.reset.addEventListener("click", onReset);
  el.refresh.addEventListener("click", loadAll);

  await loadAll();
  await startLive();

  // 图表/表格不是每帧都要重画，每 5 秒补一次全量
  setInterval(loadAll, 5000);
}

boot();

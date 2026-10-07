/**
 * 后台任务运行状态栏（左侧菜单栏底部，健康状态条上方）
 *
 * 数据源：优先 SSE /system/activity/stream，失败/断线后回退到 GET /api/v1/system/activity（3s 轮询）
 *   - 任务：task_registry 表 + 后端内存实时态（进度/耗时/ETA/运行统计）合并
 *   - AI：provider_factory 内存实时计数（进行中/最近一次）+ 今日审计汇总
 *   - 服务：版本 / 进程运行时长
 *
 * 设计要点：
 *   - SSE 连接建立时先推完整快照，任务/AI 状态变化时立即刷新；
 *   - 30s 无事件看门狗自动断开并回退轮询，保证网络抖动后仍可恢复；
 *   - 页面隐藏时 SSE/轮询均暂停，切回时立即补偿拉取；
 *   - 轮询失败保持上次数据（常驻 UI，不弹错误打断用户）；
 *   - 支持 暂停/恢复/停止 任务控制（POST /sse/task/{id}/control）；
 *   - 暂停是**协作式**的：后端不再发起新的 AI 调用，已在飞的调用会跑完，因此
 *     面板显式标注「进行中的 AI 调用返回后即停」，且 elapsed 已在后端扣除暂停时长；
 *   - 折叠态只显示图标 + 运行数徽标，详情面板改为浮层。
 */
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { App, Button, Popconfirm, Progress, Tag, Tooltip } from "antd";
import {
  CaretRightOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownOutlined,
  LoadingOutlined,
  MinusCircleOutlined,
  PauseCircleOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { systemApi, tasksApi } from "../api";
import { useAntdMessageHub } from "../utils/activityCenter";
import { createSseBatcher, type SseBatcher } from "../utils/sseBatcher";

type TaskItem = {
  id: string;
  task_type: string;
  status: string;
  progress: number;
  message?: string;
  scheme_id?: string;
  scheme_name?: string;
  updated_at?: string;
  created_at?: string;
  live?: boolean;
  elapsed?: number;
  stats?: Record<string, any>;
};

type Activity = {
  server?: { version?: string; uptime?: number };
  tasks?: { running: TaskItem[]; recent: TaskItem[] };
  ai?: {
    in_flight?: number;
    total?: number;
    failed?: number;
    calls_today?: number;
    tokens_today?: number;
    ok_calls?: number;
    success_rate?: number | null;
    avg_duration?: number;
    last_provider?: string;
    last_model?: string;
    last_ok?: boolean;
    last_duration?: number;
    last_at?: number;
  };
  /** 文档解析统计（2026-10-06 新增，加法字段；旧后端不下发时整段不渲染） */
  documents?: {
    total?: number;
    parsed?: number;
    failed?: number;
    pending?: number;
    last_upload_at?: string;
    failure_rate?: number | null;
  };
};

const POLL_MS = 3000;

// ✅ 性能优化：SSE 断线自动重连的指数退避序列与上限。
// 旧实现「一次断线永久退化为 3s 轮询」——后端重启/网络抖动后要用户手动刷新页面
// 才能恢复 SSE，期间轮询频率是 SSE 的 3 倍（3s vs 10s 快照），后端压力翻 3 倍。
// 重试耗尽后仍回退轮询，与旧行为完全一致（不改变兜底语义）。
const SSE_BACKOFF_MS = [3000, 6000, 12000, 30000, 60000] as const;
const SSE_MAX_RETRIES = SSE_BACKOFF_MS.length;

/**
 * ✅ 性能优化：活动快照的轻量指纹，用于跳过「内容未变」的重复 setState。
 * 背景：后端 activity_broadcaster 每次 notify()（task_registry 每次进度/统计
 * 变化都会触发）都会推送一份快照，实测正文生成峰值约 3 份/秒。快照里除了
 * 真正变化的进度，还夹带每次序列化都新建的引用 —— 逐条 setActivity 会把
 * 整个详情面板（最多 5 条任务行 + 近期任务 + AI 面板）重建一遍。
 *
 * 这里按 1s 粒度取任务进度/耗时 + AI 关键计数拼成短字符串：
 *   - 进度按整数百分比（对状态栏显示够用，过滤掉 0.37→0.38 这类抖动）；
 *   - 耗时按整秒（后端在飞的 AI 调用可能 30s 不推新进度，但仍需显示"已耗时"）。
 * 指纹本身只遍历两次任务数组，代价远低于一次整面板重渲。
 */
export function activityFingerprint(a: Activity | null | undefined): string {
  if (!a) return "";
  const part = (arr?: TaskItem[]) =>
    (arr || []).map((x) =>
      `${x.id}:${x.status}:${Math.round((x.progress || 0) * 100)}:${Math.floor(x.elapsed || 0)}`
    ).join(",");
  const ai = a.ai || {};
  const docs = a.documents || {};
  return [
    part(a.tasks?.running),
    part(a.tasks?.recent),
    ai.in_flight ?? "",
    ai.calls_today ?? "",
    ai.tokens_today ?? "",
    ai.success_rate ?? "",
    ai.last_at ?? "",
    ai.last_ok ?? "",
    // 文档解析计数参与指纹：解析状态变化（如 failed→success）必须触发重渲
    `${docs.total ?? ""}:${docs.parsed ?? ""}:${docs.failed ?? ""}`,
    a.server?.version ?? "",
  ].join("|");
}

// ✅ 修复（2026-09-18）：补齐 bid_analysis（结构化提取）—— 旧实现缺失该映射，
//    任务栏直接显示原始英文类型名；同时统一 outline_generation 文案为「目录生成」
//    （与 useSchemeLiveTask 的 TYPE_LABELS 保持一致，旧实现一处叫"大纲生成"）。
const TASK_TYPE_LABELS: Record<string, string> = {
  outline_generation: "目录生成",
  content_generation: "正文生成",
  facts_generation: "事实提取",
  bid_analysis: "结构化提取",
};
const taskTypeLabel = (t: string) => TASK_TYPE_LABELS[t] || t || "后台任务";

const STATUS_META: Record<string, { label: string; color: string }> = {
  running: { label: "运行中", color: "#00D4FF" },
  paused: { label: "已暂停", color: "#FAAD14" },
  completed: { label: "已完成", color: "#52C41A" },
  failed: { label: "失败", color: "#FF4D4F" },
  stopped: { label: "已停止", color: "#8C8C8C" },
};
const statusMeta = (s: string) => STATUS_META[s] || { label: s || "未知", color: "#8C8C8C" };

function fmtDuration(sec?: number | null): string {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 60) return `${s}秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}分${s % 60 ? `${s % 60}秒` : ""}`;
  const h = Math.floor(m / 60);
  return `${h}小时${m % 60}分`;
}

function fmtTokens(n?: number): string {
  const v = Math.max(0, Math.floor(n || 0));
  if (v >= 100000000) return `${(v / 100000000).toFixed(1)}亿`;
  if (v >= 10000) return `${(v / 10000).toFixed(1)}万`;
  return String(v);
}

function fmtClock(iso?: string): string {
  if (!iso) return "";
  const t = iso.replace("T", " ").slice(11, 16);
  return t || "";
}

/** 单个任务行：进度条 + 运行统计 + 暂停/恢复/停止控制 */
function TaskRow({
  task,
  pendingAction,
  onControl,
}: {
  task: TaskItem;
  /** 正在下发中的控制动作（pause/resume/stop），用于按钮 loading 防重复点击 */
  pendingAction?: string;
  onControl: (taskId: string, action: string) => void;
}) {
  const meta = statusMeta(task.status);
  const active = task.status === "running" || task.status === "paused";
  const busy = !!pendingAction;
  const percent = Math.round((task.progress || 0) * 100);
  const stats = task.stats || {};
  // 后端 stats：elapsed_ms/eta_ms/done/total/failed/words/running/concurrency
  const elapsedSec = task.elapsed ?? (stats.elapsed_ms ? stats.elapsed_ms / 1000 : null);
  const etaSec = stats.eta_ms ? stats.eta_ms / 1000 : null;
  const runningChapters: any[] = Array.isArray(stats.running) ? stats.running : [];

  const statChips: string[] = [];
  if (elapsedSec != null) statChips.push(`已耗时 ${fmtDuration(elapsedSec as number)}`);
  if (etaSec != null) statChips.push(`剩余约 ${fmtDuration(etaSec as number)}`);
  if (stats.total) statChips.push(`章节 ${stats.done ?? 0}/${stats.total}`);
  if (stats.words) statChips.push(`字数 ${fmtTokens(stats.words)}`);
  if (stats.concurrency) statChips.push(`并发 ${stats.concurrency}`);

  return (
    <div style={{ padding: "8px 0", borderBottom: "1px solid rgba(0,212,255,0.08)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        <Tag
          style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: "18px", paddingInline: 6 }}
          bordered={false}
          color={task.status === "running" ? "processing" : task.status === "paused" ? "warning" : "default"}
        >
          {taskTypeLabel(task.task_type)}
        </Tag>
        <span style={{ flex: 1, fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {task.scheme_name || task.scheme_id || "未关联方案"}
        </span>
        <span style={{ fontSize: 11, color: meta.color, flexShrink: 0 }}>
          {active && task.status === "running" && <LoadingOutlined style={{ marginRight: 3 }} />}
          {meta.label}
        </span>
        {active && (
          <>
            {task.status === "running" ? (
              <Tooltip title="暂停任务：不再开始新的章节/目录，已在进行的 AI 调用会跑完">
                <Button
                  size="small"
                  type="text"
                  icon={<PauseCircleOutlined />}
                  loading={busy && pendingAction === "pause"}
                  disabled={busy}
                  onClick={() => onControl(task.id, "pause")}
                />
              </Tooltip>
            ) : (
              <Tooltip title="恢复任务">
                <Button
                  size="small"
                  type="text"
                  icon={<CaretRightOutlined />}
                  loading={busy && pendingAction === "resume"}
                  disabled={busy}
                  onClick={() => onControl(task.id, "resume")}
                />
              </Tooltip>
            )}
            <Popconfirm
              title="停止该任务？"
              description="已生成的部分会保留，正在进行的 AI 调用会被中断。"
              okText="停止"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => onControl(task.id, "stop")}
            >
              <Tooltip title="停止任务">
                <Button
                  size="small"
                  type="text"
                  danger
                  loading={busy && pendingAction === "stop"}
                  disabled={busy}
                  icon={<MinusCircleOutlined />}
                />
              </Tooltip>
            </Popconfirm>
          </>
        )}
      </div>
      {active && (
        <Progress
          percent={percent}
          size="small"
          status={task.status === "paused" ? "normal" : "active"}
          strokeColor={task.status === "paused" ? "#FAAD14" : "#00D4FF"}
          trailColor="rgba(0,212,255,0.12)"
        />
      )}
      {(task.message || statChips.length > 0 || task.status === "paused") && (
        <div style={{ fontSize: 11, color: "rgba(255,255,255,0.55)", lineHeight: 1.6 }}>
          {/* ✅ 暂停是协作式的：已在飞的 AI 调用会跑完。必须显式说明，
              否则用户点暂停后发现还有章节陆续完成，会误判为"暂停没生效"。 */}
          {task.status === "paused" && (
            <div style={{ color: "#FAAD14" }}>
              已暂停：不再开始新内容，进行中的 AI 调用返回后即停
            </div>
          )}
          {task.message && (
            <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {task.message}
            </div>
          )}
          {statChips.length > 0 && <div>{statChips.join(" · ")}</div>}
          {runningChapters.length > 0 && (
            <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              进行中：{runningChapters.map((c: any) => c?.title || c?.section_id || "").filter(Boolean).join("、")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TaskStatusBar({ collapsed }: { collapsed: boolean }) {
  const { message: _antdMsg } = App.useApp();
  const message = useAntdMessageHub(_antdMsg, "后台任务");
  const [activity, setActivity] = useState<Activity | null>(null);
  const [expanded, setExpanded] = useState(false);
  /** task_id -> 正在下发的控制动作，用于按钮 loading 与防重复点击 */
  const [pendingActions, setPendingActions] = useState<Record<string, string>>({});
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);
  // ✅ 性能优化：上一次已渲染快照的指纹，指纹相同则跳过 setActivity（见 activityFingerprint）
  const activityFpRef = useRef("");
  // ✅ 性能优化：SSE 快照 rAF 批量器（lazy 初始化；每个 effect 实例会在内部重建并接管）
  const batcherRef = useRef<SseBatcher | null>(null);
  if (batcherRef.current === null) batcherRef.current = createSseBatcher();

  /**
   * ✅ 性能优化：活动快照的统一写入入口 —— rAF 合帧 + 指纹去重。
   * 旧实现每条快照一次 setActivity：正文生成峰值约 3 份/秒，每次都重建整个
   * 详情面板（最多 5 条任务行 + 近期任务 + AI 面板）并跑一遍 CSS recalc。
   * 现在：指纹未变的快照连刷帧都不排；指纹变化时同帧内多次快照只合并成一次渲染。
   */
  const applyActivity = useCallback((next: Activity | null) => {
    if (!mountedRef.current) return;
    const fp = activityFingerprint(next);
    if (fp === activityFpRef.current) return;
    activityFpRef.current = fp;
    batcherRef.current?.schedule("activity", () => {
      if (mountedRef.current) setActivity(next);
    });
  }, []);
  const pollTimerRef = useRef<number | null>(null);
  const watchdogTimerRef = useRef<number | null>(null);
  const lastEventRef = useRef<number>(Date.now());
  // 优雅停止 SSE 的 resolve 函数 —— resolve 时 sseGetStream 会走 reader.cancel() 路径，
  // 不触发浏览器 net::ERR_ABORTED（AbortController.abort() 会触发该控制台噪音）。
  // 统一用于 StrictMode 开发态 mount→cleanup→mount、真正组件卸载、看门狗超时断连等场景。
  const softStopResolveRef = useRef<(() => void) | null>(null);

  const load = useCallback(async () => {
    if (inFlightRef.current || document.hidden) return;
    inFlightRef.current = true;
    try {
      const { data } = await systemApi.activity(8);
      applyActivity(data);
      lastEventRef.current = Date.now();
    } catch {
      // 请求失败保持上次数据（离线时健康条已负责提示）
    } finally {
      inFlightRef.current = false;
    }
  }, [applyActivity]);

  useEffect(() => {
    mountedRef.current = true;
    let active = true;
    // ✅ 性能优化：每个 effect 实例独立持有批量器 —— StrictMode 开发态
    // mount→cleanup→mount 时旧实例的批量器已在 cleanup 里 stop()，不能复用
    const batcher = createSseBatcher();
    batcherRef.current = batcher;
    let reconnectTimer: number | null = null;
    let reconnectAttempt = 0;

    const startPolling = () => {
      if (pollTimerRef.current) window.clearInterval(pollTimerRef.current);
      load();
      pollTimerRef.current = window.setInterval(load, POLL_MS);
    };

    const stopPolling = () => {
      if (pollTimerRef.current) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };

    /**
     * ✅ 性能修复：断线自动重连（指数退避 3s→6s→12s→30s→60s）。
     * 旧实现连接断开后直接 startPolling() 且永不再试 —— 后端重启、代理抖动、
     * 笔记本休眠唤醒之后，用户只能手动 F5 才能恢复 SSE；而期间轮询频率是
     * SSE 的 3 倍（3s vs 后端 10s 快照），后端快照 SQL 压力随之放大 3 倍。
     * 退避用尽后仍回退轮询，与旧行为一致。
     */
    const scheduleReconnect = () => {
      if (!active) return;
      if (reconnectAttempt >= SSE_MAX_RETRIES) {
        startPolling();
        return;
      }
      const delay = SSE_BACKOFF_MS[Math.min(reconnectAttempt, SSE_BACKOFF_MS.length - 1)];
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        if (active) void connectSSE();
      }, delay);
    };

    const connectSSE = async () => {
      if (!active) return;
      // 优雅关闭上一次 effect 遗留的连接（StrictMode 首次 cleanup 会走这条路径）
      softStopResolveRef.current?.();
      softStopResolveRef.current = null;
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      stopPolling();
      // 每次 connectSSE 创建新的 softStop Promise，resolve 函数存 ref 里
      let softStopResolve: () => void = () => {};
      const softStop = new Promise<void>((resolve) => { softStopResolve = resolve; });
      softStopResolveRef.current = softStopResolve;

      try {
        // 不传 signal —— 完全靠 softStop 做优雅断连（reader.cancel 路径，无 ERR_ABORTED）
        for await (const msg of systemApi.activityStream(8, undefined, softStop)) {
          if (!active) break;
          if (msg?.event === "snapshot") {
            // ✅ 性能优化：不再逐条 setActivity，统一走合帧 + 指纹去重
            applyActivity(msg.data);
            lastEventRef.current = Date.now();
          }
        }
      } catch {
        // SSE 不可用或连接断开 → 走下方重连调度
      }

      if (!active) return;
      // ✅ 主动 softStop（看门狗超时 / cleanup 触发）也算断线，同样交给退避重连
      scheduleReconnect();
    };

    // 看门狗：30s 内未收到任何事件/心跳则主动断连（由退避调度接管后续重连）
    watchdogTimerRef.current = window.setInterval(() => {
      if (Date.now() - lastEventRef.current > 30000) {
        softStopResolveRef.current?.();
        softStopResolveRef.current = null;
      }
    }, 5000);

    connectSSE();

    const onVisible = () => {
      if (!document.hidden) {
        // 已回退轮询或长时间无事件时立即补偿拉取
        if (pollTimerRef.current || Date.now() - lastEventRef.current > POLL_MS) {
          load();
        }
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      active = false;
      mountedRef.current = false;
      // 优雅停止 SSE：resolve softStop promise → sseGetStream Promise.race 捕获 → break 循环
      // → reader.cancel() → fetch 正常 resolve，浏览器不会记录 ERR_ABORTED
      // 统一处理 StrictMode 开发态 mount→cleanup→mount 和真正组件卸载
      softStopResolveRef.current?.();
      softStopResolveRef.current = null;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      stopPolling();
      if (watchdogTimerRef.current) window.clearInterval(watchdogTimerRef.current);
      document.removeEventListener("visibilitychange", onVisible);
      // 卸载：丢弃未刷的快照，避免 React 已卸载后 setState
      batcher.stop();
    };
  }, [load, applyActivity]);

  const handleControl = useCallback(
    async (taskId: string, action: string) => {
      setPendingActions((m) => ({ ...m, [taskId]: action }));
      try {
        const { data } = await tasksApi.control(taskId, action);
        if (data?.ok) {
          message.success(
            data.message ||
              (action === "pause" ? "已暂停" : action === "resume" ? "已恢复" : "已停止")
          );
        } else {
          message.warning(data?.message || "操作未生效（任务可能已结束）");
        }
      } catch (e: any) {
        message.error(e?.message || "操作失败");
      } finally {
        setPendingActions((m) => {
          const next = { ...m };
          delete next[taskId];
          return next;
        });
        // 控制结果落在 DB / 内存实时态上，主动补一次拉取让面板立刻变化
        load();
      }
    },
    [load, message]
  );

  const running = activity?.tasks?.running ?? [];
  const recentAll = activity?.tasks?.recent ?? [];
  const finishedRecent = recentAll
    .filter((t) => t.status !== "running" && t.status !== "paused")
    .slice(0, 5);
  const ai = activity?.ai;
  const docStats = activity?.documents;
  const aiBusy = (ai?.in_flight ?? 0) > 0;
  const busy = running.length > 0;
  const topTask = running[0];

  const summaryText = busy
    ? `${running.length} 个任务运行中${
        topTask ? ` · ${taskTypeLabel(topTask.task_type)} ${Math.round((topTask.progress || 0) * 100)}%` : ""
      }`
    : "后台空闲";
  const aiText = aiBusy
    ? `AI 调用中${ai && (ai.in_flight ?? 0) > 1 ? ` ×${ai.in_flight}` : ""}`
    : `AI 今日 ${ai?.calls_today ?? 0} 次`;

  const detailPanel = (
    <div
      className="bp-task-panel"
      style={
        collapsed
          ? // ✅ 折叠态必须用 fixed：Sider 上设了 `overflow: hidden`，absolute 定位的
            // 260px 面板若以 Sider 为包含块会被裁成 80px，暂停/停止按钮点不到。
            // fixed 的包含块是视口，天然绕开祖先裁剪；侧边栏贴左，left 即侧边起点。
            // ✅ 窄屏用 max-width + calc 而不是固定 260，避免手机/小窗口溢出。
            { position: "fixed", bottom: 84, left: 8, width: 260, maxWidth: "calc(100vw - 24px)", zIndex: 1050 }
          : undefined
      }
    >
      <div className="bp-task-panel-head">
        <span>后台任务</span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 2 }}>
          <Button size="small" type="text" icon={<ReloadOutlined />} onClick={load} />
          <Button
            size="small"
            type="text"
            icon={<DownOutlined />}
            onClick={() => setExpanded(false)}
          />
        </span>
      </div>

      <div style={{ padding: "4px 10px 8px", maxHeight: 380, overflowY: "auto" }}>
        {/* ---- 运行中任务 ---- */}
        <div className="bp-task-panel-section">运行中（{running.length}）</div>
        {running.length === 0 ? (
          <div className="bp-task-empty">暂无运行中的后台任务</div>
        ) : (
          running.map((t) => (
            <TaskRow
              key={t.id}
              task={t}
              pendingAction={pendingActions[t.id]}
              onControl={handleControl}
            />
          ))
        )}

        {/* ---- 最近任务（终态） ---- */}
        {finishedRecent.length > 0 && (
          <>
            <div className="bp-task-panel-section" style={{ marginTop: 6 }}>
              最近完成 / 失败
            </div>
            {finishedRecent.map((t) => {
              const meta = statusMeta(t.status);
              const done = t.status === "completed";
              return (
                <Tooltip key={t.id} title={t.message || undefined} placement="left">
                  <div className="bp-task-recent-row">
                    {done ? (
                      <CheckCircleOutlined style={{ color: meta.color }} />
                    ) : (
                      <CloseCircleOutlined style={{ color: meta.color }} />
                    )}
                    <span className="bp-task-recent-name">
                      {taskTypeLabel(t.task_type)} · {t.scheme_name || t.scheme_id || "未关联方案"}
                    </span>
                    <span style={{ marginLeft: "auto", flexShrink: 0, color: meta.color }}>
                      {meta.label}
                    </span>
                    <span className="bp-task-recent-time">{fmtClock(t.updated_at)}</span>
                  </div>
                </Tooltip>
              );
            })}
          </>
        )}

        {/* ---- AI 调用实时态 ---- */}
        <div className="bp-task-panel-section" style={{ marginTop: 6 }}>
          AI 调用
        </div>
        <div style={{ fontSize: 11, color: "rgba(255,255,255,0.65)", lineHeight: 1.9 }}>
          <div>
            {aiBusy ? (
              <span style={{ color: "#FAAD14" }}>
                <LoadingOutlined style={{ marginRight: 4 }} />
                {ai?.in_flight} 个调用进行中
              </span>
            ) : (
              <span>当前无进行中的 AI 调用</span>
            )}
            {ai?.last_provider && (
              <span style={{ color: "rgba(255,255,255,0.45)", marginLeft: 8 }}>
                最近：{ai.last_provider}
                {ai.last_model ? ` / ${ai.last_model}` : ""}
                {ai.last_duration ? ` · ${ai.last_duration.toFixed(1)}s` : ""}
                {ai.last_ok === false ? " · 失败" : ""}
              </span>
            )}
          </div>
          <div>
            今日 {ai?.calls_today ?? 0} 次
            {ai?.success_rate != null && <> · 成功率 {ai.success_rate}%</>}
            {ai && (ai.tokens_today ?? 0) > 0 && <> · Token {fmtTokens(ai.tokens_today)}</>}
            {ai && (ai.avg_duration ?? 0) > 0 && <> · 平均 {ai.avg_duration?.toFixed(1)}s</>}
            {ai && (ai.failed ?? 0) > 0 && (
              <span style={{ color: "#FF4D4F" }}> · 本会话失败 {ai.failed} 次</span>
            )}
          </div>
        </div>

        {/* ---- 文档解析统计（2026-10-06 新增；无文档时整段不显示，避免空噪） ---- */}
        {docStats && (docStats.total ?? 0) > 0 && (
          <>
            <div className="bp-task-panel-section" style={{ marginTop: 6 }}>
              文档解析
            </div>
            <div style={{ fontSize: 11, color: "rgba(255,255,255,0.65)", lineHeight: 1.9 }}>
              <div>
                共 {docStats.total} 份 · 已解析 {docStats.parsed ?? 0}
                {(docStats.pending ?? 0) > 0 && ` · 待解析 ${docStats.pending}`}
              </div>
              {(docStats.failed ?? 0) > 0 ? (
                <div style={{ color: "#FF4D4F" }}>
                  失败 {docStats.failed} 份 · 失败率 {docStats.failure_rate ?? "-"}%
                </div>
              ) : (
                <div style={{ color: "rgba(82,196,66,0.85)" }}>无解析失败</div>
              )}
            </div>
          </>
        )}

        {/* ---- 服务态 ---- */}
        {activity?.server && (
          <div
            style={{
              marginTop: 6,
              paddingTop: 6,
              borderTop: "1px solid rgba(0,212,255,0.08)",
              fontSize: 11,
              color: "rgba(255,255,255,0.35)",
            }}
          >
            服务 v{activity.server.version || "-"} · 已运行 {fmtDuration(activity.server.uptime)} · 每 3s 自动刷新
          </div>
        )}
      </div>
    </div>
  );

  return (
    <>
      {expanded && detailPanel}
      <Tooltip
        title={
          collapsed
            ? `后台任务：${summaryText} · ${aiText}`
            : `${summaryText} · ${aiText}（点击查看详情）`
        }
        placement="right"
      >
        <div
          className={`bp-task-bar ${busy ? "busy" : ""}`}
          onClick={() => setExpanded((v) => !v)}
          role="button"
        >
          <span className={`bp-status-dot ${busy ? "checking" : "online"}`} />
          {!collapsed ? (
            <>
              <span className="bp-task-bar-text">
                <ThunderboltOutlined
                  style={{ marginRight: 4, color: busy ? "#FAAD14" : "#00D4FF" }}
                />
                {summaryText}
              </span>
              <span className="bp-task-bar-ai">
                {aiBusy && <LoadingOutlined style={{ marginRight: 3 }} />}
                {aiText}
              </span>
            </>
          ) : (
            <span className="bp-task-badge" data-count={running.length}>
              <ThunderboltOutlined style={{ color: busy ? "#FAAD14" : "#00D4FF" }} />
              {running.length > 0 && <i className="bp-task-badge-dot">{running.length}</i>}
            </span>
          )}
        </div>
      </Tooltip>
    </>
  );
}

// ✅ 性能优化：默认导出用 memo 包裹。本组件 props 仅为 {collapsed} 布尔值，
//    而它自身持有一条常驻 SSE 活动流与健康轮询（560 行）。此前 Sidebar 每次因
//    后端健康轮询/路由变化重渲都会连带重渲本组件；memo 后仅 collapsed 真正
//    变化时才重渲，避免轮询周期里的无谓开销。
export default memo(TaskStatusBar);

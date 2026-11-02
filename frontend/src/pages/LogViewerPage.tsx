import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Table, Input, Select, Button, Drawer, Space, Tag, message, Tree, Card, Statistic, Row, Col, Alert, Tabs, Timeline, DatePicker } from "antd";
import { logsApi, systemApi } from "../api";
// ✅ 2026-10-30：状态色统一取 TaskStatusBar 的单一事实源，避免内联终态白名单
//    （contentTerminalParity 的 A2 扫描会把 `v === "failed" ? ... : v === "completed"`
//     这类等值链判为违规——终态枚举必须只有一处定义）。
import { statusMeta } from "../components/TaskStatusBar";

const LEVELS = ["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL"];
const STATUSES = ["", "created", "queued", "started", "running", "paused", "stopping", "stopped", "completed", "failed", "degraded"];

function levelColor(v: string) {
  return v === "ERROR" || v === "FATAL" ? "red" : v === "WARN" || v === "WARNING" ? "orange" : v === "DEBUG" ? "default" : "blue";
}

export default function LogViewerPage() {
  const [sp] = useSearchParams();
  const [data, setData] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState<any>({
    limit: 50, offset: 0,
    trace_id: sp.get("trace_id") || "",
    task_id: sp.get("task_id") || "",
    level: sp.get("level") || "",
    module: sp.get("module") || "",
    error_code: sp.get("error_code") || "",
    keyword: "",
    since: "", until: "",
  });
  const [detail, setDetail] = useState<any>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [traceView, setTraceView] = useState<any>(null);
  const [metrics, setMetrics] = useState<any>(null);
  const [alerts, setAlerts] = useState<any[]>([]);
  const [timelineTaskId, setTimelineTaskId] = useState<string>(sp.get("task_id") || "");
  const [timelineItems, setTimelineItems] = useState<any[]>([]);
  const [taskList, setTaskList] = useState<any[]>([]);
  const [taskStatus, setTaskStatus] = useState<string>("");
  const [taskType, setTaskType] = useState<string>("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await logsApi.list(filters);
      setData(res.data?.items || []);
      setTotal(res.data?.total || 0);
    } catch (e: any) {
      message.error(e?.message || "加载日志失败");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  const loadMetrics = useCallback(async () => {
    try {
      const m = await logsApi.metrics(7);
      setMetrics(m.data);
      const a = await logsApi.alerts(1);
      setAlerts(a.data?.alerts || []);
    } catch (e: any) {
      // ignore
    }
  }, []);

  const loadTimeline = useCallback(async () => {
    if (!timelineTaskId) { setTimelineItems([]); return; }
    try {
      const res = await logsApi.task(timelineTaskId);
      setTimelineItems(res.data?.items || []);
    } catch (e: any) {
      message.error(e?.message || "加载时间线失败");
    }
  }, [timelineTaskId]);

  const loadTasks = useCallback(async () => {
    try {
      const res = await systemApi.activity(50);
      const tasks = res.data?.tasks || res.data || [];
      setTaskList(Array.isArray(tasks) ? tasks : []);
    } catch (e: any) {
      // ignore
    }
  }, []);

  const copyText = useCallback(async (text: string) => {
    try { await navigator.clipboard.writeText(text); message.success("已复制"); }
    catch { message.error("复制失败"); }
  }, []);

  // 注意：doExport 必须先于引用它的 useEffect 声明（React Compiler
  // 不允许「先用后声明」，否则整体 bail-out 并报 preserve-manual-memoization）
  const doExport = useCallback(async (fmt: "csv" | "json") => {
    try {
      const r = await logsApi.export(fmt, filters);
      const blob = new Blob([r.data], { type: fmt === "csv" ? "text/csv" : "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `logs.${fmt}`; a.click();
      URL.revokeObjectURL(url);
    } catch { message.error("导出失败"); }
  }, [filters]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadMetrics(); }, [loadMetrics]);
  useEffect(() => { loadTasks(); }, [loadTasks]);
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r") { e.preventDefault(); load(); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "e") { e.preventDefault(); doExport("csv"); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [load, doExport]);
  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [autoRefresh, load]);

  const columns = useMemo(() => [
    { title: "时间", dataIndex: "timestamp", width: 180, sorter: (a: any, b: any) => String(a.timestamp || "").localeCompare(String(b.timestamp || "")) },
    { title: "级别", dataIndex: "level", width: 80, render: (v: string) => <Tag color={levelColor(v)}>{v}</Tag> },
    { title: "模块", dataIndex: "module", width: 160 },
    { title: "事件", dataIndex: "event", width: 180, ellipsis: true },
    { title: "Trace", dataIndex: "trace_id", width: 120 },
    { title: "状态", dataIndex: "status", width: 90, render: (v: string) => v ? <Tag>{v}</Tag> : null },
    { title: "错误码", dataIndex: "error_code", width: 110, render: (v: string) => v ? <Tag color="red">{v}</Tag> : null },
    { title: "耗时ms", dataIndex: "duration_ms", width: 80, sorter: (a: any, b: any) => (a.duration_ms || 0) - (b.duration_ms || 0) },
    { title: "消息", dataIndex: "message", ellipsis: true },
    { title: "操作", width: 200, render: (_: any, r: any) => (
      <Space>
        <Button size="small" onClick={() => setDetail(r)}>详情</Button>
        <Button size="small" onClick={() => copyText(JSON.stringify(r, null, 2))}>复制</Button>
        {r.trace_id && <Button size="small" onClick={async () => { const res = await logsApi.trace(r.trace_id); setTraceView(res.data); }}>链路</Button>}
      </Space>
    ) },
  ], [copyText]);

  const filteredTasks = useMemo(() => taskList.filter((t: any) =>
    (!taskStatus || (t.status || "") === taskStatus) &&
    (!taskType || (t.task_type || "") === taskType)), [taskList, taskStatus, taskType]);

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input placeholder="关键词" style={{ width: 150 }} value={filters.keyword}
          onChange={(e) => setFilters((f: any) => ({ ...f, keyword: e.target.value, offset: 0 }))} />
        <Select placeholder="级别" style={{ width: 100 }} allowClear value={filters.level || undefined}
          onChange={(v) => setFilters((f: any) => ({ ...f, level: v || "", offset: 0 }))} options={LEVELS.map((l) => ({ value: l, label: l }))} />
        <Input placeholder="模块" style={{ width: 150 }} value={filters.module}
          onChange={(e) => setFilters((f: any) => ({ ...f, module: e.target.value, offset: 0 }))} />
        <Input placeholder="Trace ID" style={{ width: 150 }} value={filters.trace_id}
          onChange={(e) => setFilters((f: any) => ({ ...f, trace_id: e.target.value, offset: 0 }))} />
        <Input placeholder="Task ID" style={{ width: 150 }} value={filters.task_id}
          onChange={(e) => setFilters((f: any) => ({ ...f, task_id: e.target.value, offset: 0 }))} />
        <Input placeholder="错误码" style={{ width: 130 }} value={filters.error_code}
          onChange={(e) => setFilters((f: any) => ({ ...f, error_code: e.target.value, offset: 0 }))} />
        <DatePicker.RangePicker style={{ width: 260 }}
          onChange={(v: any) => setFilters((f: any) => ({
            ...f, offset: 0,
            since: v?.[0] ? v[0].startOf("day").format("YYYY-MM-DD 00:00:00") : "",
            until: v?.[1] ? v[1].endOf("day").format("YYYY-MM-DD 23:59:59") : "",
          }))} />
        <Button type="primary" onClick={load}>查询</Button>
        <Button onClick={() => setAutoRefresh((v) => !v)}>{autoRefresh ? "暂停刷新" : "自动刷新"}</Button>
        <Button onClick={() => doExport("csv")}>导出CSV</Button>
        <Button onClick={() => doExport("json")}>导出JSON</Button>
        <Button onClick={() => setFilters({ limit: 50, offset: 0 })}>重置</Button>
      </Space>
      {alerts.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          {alerts.map((a, i) => <Alert key={i} type="warning" message={`${a.type}: ${a.message} (current ${a.current} / threshold ${a.threshold})`} showIcon style={{ marginBottom: 4 }} />)}
        </div>
      )}
      {metrics && (
        <Row gutter={8} style={{ marginBottom: 12 }}>
          <Col span={6}><Card size="small"><Statistic title="总日志" value={metrics.total} /></Card></Col>
          <Col span={6}><Card size="small"><Statistic title="P99耗时(ms)" value={metrics.latency?.p99} /></Card></Col>
          <Col span={6}><Card size="small"><Statistic title="断链率" value={metrics.broken_trace_rate} /></Card></Col>
          <Col span={6}><Card size="small"><Statistic title="Trace数" value={metrics.trace_count} /></Card></Col>
        </Row>
      )}
      <Tabs
        items={[
          { key: "list", label: "日志列表", children: <Table rowKey="id" columns={columns} dataSource={data} loading={loading} pagination={{ total, pageSize: filters.limit, current: Math.floor(filters.offset / filters.limit) + 1, onChange: (p, ps) => setFilters((f: any) => ({ ...f, limit: ps, offset: (p - 1) * ps })) }} size="small" /> },
          { key: "timeline", label: "运行轨迹", children: (
            <div>
              <Space style={{ marginBottom: 8 }}>
                <Input placeholder="Task ID" style={{ width: 240 }} value={timelineTaskId} onChange={(e) => setTimelineTaskId(e.target.value)} />
                <Button type="primary" onClick={loadTimeline}>加载</Button>
                <Button onClick={() => { setTimelineTaskId(sp.get("task_id") || ""); loadTimeline(); }}>从URL加载</Button>
              </Space>
              <Timeline
                items={timelineItems.map((it: any) => ({
                  color: it.level === "ERROR" || it.level === "FATAL" ? "red" : it.level === "WARN" || it.level === "WARNING" ? "orange" : "blue",
                  children: (
                    <Space size={4} wrap>
                      <span>{it.timestamp}</span>
                      <Tag color={levelColor(it.level)}>{it.level}</Tag>
                      {it.module && <span>{it.module}</span>}
                      {it.event && <Tag>{it.event}</Tag>}
                      {it.status && <Tag>{it.status}</Tag>}
                      <span>{it.message}</span>
                      {it.duration_ms ? <span>({it.duration_ms}ms)</span> : null}
                      <Button size="small" type="link" onClick={() => setDetail(it)}>详情</Button>
                    </Space>
                  ),
                }))}
              />
            </div>
          ) },
          { key: "tasks", label: "任务列表", children: (
            <div>
              <Space style={{ marginBottom: 8 }}>
                <Select placeholder="状态" style={{ width: 140 }} allowClear value={taskStatus || undefined}
                  onChange={(v) => setTaskStatus(v || "")} options={STATUSES.filter(Boolean).map((s) => ({ value: s, label: s }))} />
                <Input placeholder="任务类型" style={{ width: 180 }} value={taskType}
                  onChange={(e) => setTaskType(e.target.value)} />
                <Button onClick={loadTasks}>刷新</Button>
              </Space>
              <Table rowKey="id" size="small" dataSource={filteredTasks}
                columns={[
                  { title: "ID", dataIndex: "id", width: 90, ellipsis: true },
                  { title: "类型", dataIndex: "task_type", width: 120 },
                  { title: "状态", dataIndex: "status", width: 90, render: (v: string) => <Tag color={statusMeta(v).color}>{v}</Tag> },
                  { title: "进度", dataIndex: "progress", width: 80, render: (v: number) => `${Math.round((v || 0) * 100)}%` },
                  { title: "方案", dataIndex: "scheme_name", width: 160, ellipsis: true },
                  { title: "创建", dataIndex: "created_at", width: 160 },
                  { title: "消息", dataIndex: "message", ellipsis: true },
                  { title: "操作", width: 120, render: (_: any, r: any) => (
                    <Button size="small" onClick={() => { setTimelineTaskId(r.id); setTimelineItems([]); loadTimeline(); }}>轨迹</Button>
                  ) },
                ]} />
            </div>
          ) },
        ]}
      />
      <Drawer open={!!detail} onClose={() => setDetail(null)} title="日志详情" width={720}>
        {detail && (
          <div>
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(detail, null, 2)}</pre>
            <Button size="small" onClick={() => copyText(JSON.stringify(detail, null, 2))}>复制</Button>
            {detail.trace_id && <Button size="small" style={{ marginLeft: 8 }} onClick={async () => { const res = await logsApi.trace(detail.trace_id); setTraceView(res.data); }}>查看链路</Button>}
          </div>
        )}
      </Drawer>
      <Drawer open={!!traceView} onClose={() => setTraceView(null)} title="链路视图" width={760}>
        {traceView && (
          <div>
            {traceView.orphan_spans && traceView.orphan_spans.length > 0 && (
              <div style={{ color: "red", marginBottom: 8 }}>断链 span: {traceView.orphan_spans.join(", ")}</div>
            )}
            <Tree
              defaultExpandAll
              treeData={(() => {
                const items = traceView.items || [];
                const byParent: Record<string, any[]> = {};
                items.forEach((it: any) => {
                  const p = it.parent_span_id || "root";
                  (byParent[p] = byParent[p] || []).push(it);
                });
                const build = (pid: string): any[] =>
                  (byParent[pid] || []).map((it: any) => ({
                    key: it.span_id || it.id,
                    title: `${it.module || ""} ${it.event || it.message || ""} ${it.duration_ms ? it.duration_ms + "ms" : ""} ${it.status ? "[" + it.status + "]" : ""}`.trim(),
                    children: build(it.span_id || ""),
                  }));
                return build("root").length ? build("root") : items.map((it: any) => ({ key: it.id, title: it.message }));
              })()}
            />
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 11, marginTop: 12, maxHeight: 240, overflow: "auto" }}>{JSON.stringify(traceView, null, 2)}</pre>
          </div>
        )}
      </Drawer>
    </div>
  );
}

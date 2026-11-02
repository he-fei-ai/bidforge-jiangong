/**
 * 两轮次导出面板（R147 / R149 · 2026-10-30）。
 *
 * 前端展示契约（用户指令 §四）：
 * - 分三段展示进度：初稿 0–40% / 深度检查修复 40–90% / 打包兼容 90–100%；
 * - 实时滚动修复项（SSE finding/action 事件逐条追加，自动滚底）；
 * - 完成后展示修复报告：检出 / 已修复 / 跳过 / 需人工 四卡 +
 *   需人工项按 code 聚合（count + 样本 + 处置建议）+ 回滚提示；
 * - 「下载最终文件」按钮 + 取消按钮。
 *
 * 纯展示组件：状态与副作用全部收敛在 useExportFlow（与 R139 教训一致，
 * hook 与页面字段级对称由测试锁定）。
 */
import { useEffect, useRef } from "react";
import { Progress, Tag, Button, Collapse, Empty, Tooltip } from "antd";
import type { UnresolvedByCode } from "../hooks/useExportFlow";
import {
  CheckCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined,
  DownloadOutlined, StopOutlined,
} from "@ant-design/icons";
import type { ExportFlowState, FlowLogItem } from "../hooks/useExportFlow";

const CATEGORY_LABELS: Record<string, string> = {
  numbering: "目录编号与层级",
  paragraph: "正文排版",
  fonts: "字号字体",
  figures: "图表渲染",
  compatibility: "兼容性安全",
};

function LogRow({ item }: { item: FlowLogItem }) {
  const isAction = item.kind === "action";
  const failed = isAction && item.result && item.result !== "fixed"
    && item.result !== "skipped";
  const icon = item.kind === "finding"
    ? (item.severity === "error"
      ? <CloseCircleOutlined style={{ color: "#cf1322" }} />
      : <ExclamationCircleOutlined style={{ color: "#d48806" }} />)
    : (failed
      ? <CloseCircleOutlined style={{ color: "#cf1322" }} />
      : <CheckCircleOutlined style={{ color: "#389e0d" }} />);
  return (
    <div data-testid="flow-log-row" style={{ display: "flex", gap: 6, alignItems: "baseline" }}>
      {icon}
      <Tag data-testid="flow-log-category" style={{ marginInlineEnd: 4 }}>
        {CATEGORY_LABELS[item.category] || item.category}
      </Tag>
      <span style={{ flex: 1 }}>
        {item.message}
        {item.location ? (
          <Tooltip title={item.location}>
            <code style={{ marginLeft: 6, fontSize: 12, opacity: 0.7 }}>
              {item.location}
            </code>
          </Tooltip>
        ) : null}
      </span>
      {isAction && item.result ? (
        <span data-testid="flow-log-result" style={{ fontSize: 12, color: failed ? "#cf1322" : "#389e0d" }}>
          {item.result === "fixed" ? "已修复" : item.result === "unresolved" ? "未解决" : item.result}
        </span>
      ) : null}
    </div>
  );
}

export function ExportFlowPanel(props: {
  state: ExportFlowState;
  onStart: () => void;
  onCancel: () => void;
  onDownload: () => void;
  disabled?: boolean;
}) {
  const { state, onStart, onCancel, onDownload, disabled } = props;
  const logBoxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // 实时滚动修复项：新日志追加时自动滚底
    if (logBoxRef.current) {
      logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight;
    }
  }, [state.logs.length]);

  const report = state.report;
  const summary = report?.summary;
  const isFallback = report?.mode === "fallback_draft";

  return (
    <div data-testid="export-flow-panel">
      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        <Button
          data-testid="export-flow-start"
          type="primary"
          onClick={onStart}
          loading={state.running}
          disabled={disabled}
        >
          两轮次导出（初稿 + 检查修复）
        </Button>
        {state.running && (
          <Button data-testid="export-flow-cancel" onClick={onCancel} icon={<StopOutlined />}>
            停止
          </Button>
        )}
        {!state.running && state.taskId && (
          <Button
            data-testid="export-flow-download"
            onClick={onDownload}
            icon={<DownloadOutlined />}
          >
            下载最终文件
          </Button>
        )}
      </div>

      {state.running || state.progress > 0 ? (
        <>
          <Progress
            data-testid="export-flow-progress"
            percent={Math.round(state.progress)}
            status={state.running ? "active" : state.error ? "exception" : "success"}
            strokeColor={
              state.stage === "repair" ? { "0%": "#1677ff", "100%": "#389e0d" } : undefined
            }
          />
          <div data-testid="export-flow-stage" style={{ marginBottom: 6 }}>
            <Tag color={state.stage === "draft" ? "blue" : "default"}>
              ① 生成初稿 0–40%
            </Tag>
            <Tag color={state.stage === "repair" ? "green" : "default"}>
              ② 第二轮 · 检查与修复 40–90%
            </Tag>
            <Tag color={state.stage === "packaging" ? "gold" : "default"}>
              ③ 打包兼容 90–100%
            </Tag>
            <span>{state.stageMsg}</span>
          </div>
        </>
      ) : null}

      {state.logs.length > 0 && (
        <div
          ref={logBoxRef}
          data-testid="export-flow-logs"
          style={{
            maxHeight: 220, overflowY: "auto", border: "1px solid #f0f0f0",
            borderRadius: 6, padding: 6, display: "flex",
            flexDirection: "column", gap: 4,
          }}
        >
          {state.logs.map((item, i) => <LogRow key={i} item={item} />)}
        </div>
      )}

      {summary && (
        <Collapse
          data-testid="export-flow-report"
          size="small"
          defaultActiveKey={["report"]}
          style={{ marginTop: 8 }}
          items={[{
            key: "report",
            label: isFallback
              ? "修复报告（已回滚初稿）"
              : `修复报告：检出 ${summary.detected ?? summary.total_findings} 项 · 已修复 ${summary.fixed} 项 · 跳过 ${summary.skipped ?? 0} 项 · 需人工 ${summary.manual ?? summary.unresolved} 项（${Math.round(summary.duration_ms / 100) / 10}s）`,
            children: (
              <div data-testid="export-flow-report-body">
                {isFallback && (
                  <div style={{ color: "#d48806", marginBottom: 6 }}>
                    <ExclamationCircleOutlined /> 第二轮修复失败，本次交付的是「初稿」。
                  </div>
                )}
                <div
                  data-testid="export-flow-report-cards"
                  style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}
                >
                  {[
                    { key: "detected", label: "检出", value: summary.detected ?? summary.total_findings, color: "#1677ff" },
                    { key: "fixed", label: "已修复", value: summary.fixed, color: "#389e0d" },
                    { key: "skipped", label: "跳过", value: summary.skipped ?? 0, color: "#d48806" },
                    { key: "manual", label: "需人工", value: summary.manual ?? summary.unresolved, color: "#cf1322" },
                  ].map((c) => (
                    <div
                      key={c.key}
                      data-testid={`report-card-${c.key}`}
                      style={{
                        minWidth: 86, padding: "6px 12px", border: "1px solid #f0f0f0",
                        borderRadius: 6, textAlign: "center",
                      }}
                    >
                      <div style={{ fontSize: 22, fontWeight: 600, color: c.color }}>
                        {c.value}
                      </div>
                      <div style={{ fontSize: 12, opacity: 0.75 }}>{c.label}</div>
                    </div>
                  ))}
                </div>
                <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                  {Object.entries(summary.by_category || {}).map(([cat, c]) => (
                    <span key={cat} data-testid={`report-cat-${cat}`}>
                      <Tag>{CATEGORY_LABELS[cat] || cat}</Tag>
                      检出 {c.found} · 已修复 {c.fixed}
                      {(c.skipped ?? 0) > 0 ? <span style={{ color: "#d48806" }}> · 跳过 {c.skipped}</span> : null}
                      {(c.manual ?? c.error ?? 0) > 0 ? (
                        <span style={{ color: "#cf1322" }}>
                          {" "}· 需人工 {c.manual ?? c.error}
                        </span>
                      ) : null}
                    </span>
                  ))}
                </div>
                {(summary.unresolved_by_code || []).length > 0 && (
                  <div data-testid="export-flow-unresolved" style={{ marginTop: 8 }}>
                    <b>需人工项（按类型聚合）：</b>
                    <ul style={{ margin: "4px 0 0 16px", padding: 0, listStyle: "none" }}>
                      {(summary.unresolved_by_code as UnresolvedByCode[]).map((g) => (
                        <li key={g.code} style={{ marginBottom: 6 }}>
                          <Tag color="red">{g.code}</Tag>
                          <b>{g.count}</b> 处
                          {g.suggestion ? (
                            <span style={{ opacity: 0.75 }}>{" "}建议：{g.suggestion}</span>
                          ) : null}
                          {g.samples?.length ? (
                            <ul style={{ margin: "2px 0 0 16px", opacity: 0.7, fontSize: 12 }}>
                              {g.samples.map((s, i) => (
                                <li key={i}>{s.location}：{s.message}</li>
                              ))}
                            </ul>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {!(summary.unresolved_by_code || []).length
                  && (report?.unresolved || []).length > 0 && (
                  <div data-testid="export-flow-unresolved" style={{ marginTop: 8 }}>
                    <b>需人工项：</b>
                    <ul style={{ margin: "4px 0 0 16px", padding: 0, listStyle: "none" }}>
                      {(report?.unresolved || []).map((u: any, i: number) => (
                        <li key={i} style={{ marginBottom: 4 }}>
                          {u.location ? `${u.location}：` : ""}{u.message}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            ),
          }]}
        />
      )}

      {state.error && (
        <div data-testid="export-flow-error" style={{ color: "#cf1322", marginTop: 6 }}>
          <CloseCircleOutlined /> {state.error}
        </div>
      )}

      {!state.running && state.progress === 0 && !summary && (
        <Empty
          data-testid="export-flow-empty"
          description="第一轮生成初稿，第二轮对文档进行检查与修复，完成后提供最终下载"
          styles={{ image: { height: 48 } }}
        />
      )}
    </div>
  );
}

export default ExportFlowPanel;

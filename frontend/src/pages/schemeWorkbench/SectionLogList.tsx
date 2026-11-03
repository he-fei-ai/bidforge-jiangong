import { chartDropTexts, type SectionLogItem } from "../../utils/contentEvents";
import { STANDARD_LABELS, issueLabel, reportHasIssues, reportIssueCount } from "../../utils/contentStandard";
import { ElapsedTimer } from "./ElapsedTimer.tsx";
import { GenerationProgressCard, OUTLINE_LOG_RENDER_MAX } from "./GenerationProgressCard.tsx";
import { STAGE_COLOR } from "./outlineTreeModel.ts";
import { CheckCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined, LoadingOutlined, MinusCircleOutlined } from "@ant-design/icons";
import { List, Popover, Progress, Tag, Tooltip } from "antd";
import { memo, useMemo } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

// ✅ 实时日志面板的最大渲染行数：仅限制渲染，数据源保持全量。
// 长方案（120+ 章）逐章多次事件的日志可达上千条，而该滚动区高度固定，
// 全量渲染会把视口外的条目也建成 DOM；与下方 OUTLINE_LOG_RENDER_MAX 同口径。
export const SECTION_LOG_RENDER_MAX = 200;

// ✅ 性能优化：状态图标与行底色与 items 无关，提到模块级只构造一次
// （原实现在组件内每次渲染重建两个对象字面量）
export const SECTION_LOG_ICON: Record<string, React.ReactNode> = {
  running: <LoadingOutlined style={{ color: "#1677ff" }} />,
  success: <CheckCircleOutlined style={{ color: "#52c41a" }} />,
  failed: <CloseCircleOutlined style={{ color: "#ff4d4f" }} />,
  skipped: <MinusCircleOutlined style={{ color: "#faad14" }} />,
};
export const SECTION_LOG_BG: Record<string, string> = {
  running: "rgba(22,119,255,0.06)",
  success: "rgba(82,196,26,0.06)",
  failed: "rgba(255,77,79,0.06)",
  skipped: "rgba(250,173,20,0.06)",
};

export const SectionLogList = memo(function SectionLogList({ items }: { items: SectionLogItem[] }) {
  // ✅ 性能优化：`[...items].reverse()` 原直接写在 JSX 里 —— 长方案日志条目
  // 持续增长，每次条目变化都全量复制 + 反转 + 全量建 DOM。改为按 items 记忆化
  // 并限制渲染行数（数据源 items 本身不动，完成统计等在 GenerationProgressCard
  // 内基于完整历史计算，不受影响）。
  const visibleItems = useMemo(
    () => (items.length > SECTION_LOG_RENDER_MAX
      ? items.slice(-SECTION_LOG_RENDER_MAX).reverse()
      : [...items].reverse()),
    [items],
  );
  return (
    <List
      size="small"
      dataSource={visibleItems}
      renderItem={(item) => (
        <List.Item key={item.section_id || item.title} style={{ padding: "4px 8px", background: SECTION_LOG_BG[item.status], borderRadius: 4, marginBottom: 4 }}>
          <div style={{ width: "100%" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {SECTION_LOG_ICON[item.status]}
              {typeof item.index === "number" && item.total && (
                <Tag style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                  {item.index}/{item.total}
                </Tag>
              )}
              <Text
                strong
                ellipsis
                style={{ flex: 1, fontSize: 13 }}
                title={item.title}
              >
                {item.title}
              </Text>
              {/* ✅ 进度增强：进行中章节显示当前阶段（AI 生成中 / 续写扩充中 …），
                  让用户知道这一章卡在哪一步，而不是只有一个转圈图标 */}
              {item.status === "running" && item.stage_label && (
                <Tag
                  color={STAGE_COLOR[item.stage || "draft"] || "processing"}
                  style={{ margin: 0, fontSize: 11, flexShrink: 0 }}
                >
                  {item.stage_label}
                </Tag>
              )}
              {item.status === "success" && (
                <Tag
                  color={
                    item.word_status === "under"
                      ? "orange"
                      : item.word_status === "over"
                      ? "red"
                      : "success"
                  }
                  style={{ margin: 0, fontSize: 11, flexShrink: 0 }}
                >
                  {item.word_count}字 / {item.word_budget}
                  {item.word_status === "under"
                    ? "（偏少）"
                    : item.word_status === "over"
                    ? "（超字，可压缩）"
                    : ""}
                </Tag>
              )}
              {/* ✅ 续写失败信号：发生过续写调用失败/产出被丢弃且最终仍不达标，
                  单独提示用户「可重试或手工续写」，与「写不满」区分 */}
              {item.status === "success" && item.continue_failed && (
                <Tooltip title="续写调用失败或产出被丢弃，最终字数仍偏少——可对该章「续写本章」或重试补足">
                  <Tag color="warning" style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                    续写未完成
                  </Tag>
                </Tooltip>
              )}
              {/* ✅ 质量告警（后端 section_done.quality_issues）：本章正文存在程序化
                  质检问题（如未闭合代码块、章节重复标题等），即时提示可复查 */}
              {item.status === "success" && Array.isArray(item.quality_issues) && item.quality_issues.length > 0 && (
                <Tooltip title={(item.quality_issues as Array<{ message?: unknown; detail?: unknown }>).map((q) => String(q?.message || q?.detail || q)).join("；")}>
                  <Tag color="gold" style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                    质量告警 {item.quality_issues.length}
                  </Tag>
                </Tooltip>
              )}
              {/* ✅ R49：本章图表产出闭环（后端 section_done 实时下发）。
                  「配图 N」回答本章配了几张图；出现删图时改为「配图 N（已删 M）」并挂
                  Tooltip 列出每张图的删除理由 —— 用户在生成过程中即可解释
                  「为什么正文里少了几张图」，不必等整批结束翻报告或去 grep 后端日志。
                  零图表且无删图的普通章节不占位（那是最常见的正常情况）。 */}
              {item.status === "success" && item.chart_yield &&
                (item.chart_yield.chart_count > 0 || item.chart_yield.charts_dropped_count > 0) &&
                (() => {
                  const cy = item.chart_yield!;
                  const texts = chartDropTexts(cy.charts_dropped);
                  return texts.length > 0 ? (
                    <Tooltip title={texts.join("；")}>
                      <Tag color="warning" style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                        配图 {cy.chart_count}（已删 {cy.charts_dropped_count}）
                      </Tag>
                    </Tooltip>
                  ) : (
                    <Tag color="blue" style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                      配图 {cy.chart_count}
                    </Tag>
                  );
                })()}
              {/* ✅ F-CONTENT-STANDARD(2026-09-26)：模式徽标 + 校验告警。
                  模式徽标回答「这章是按哪种标准写的」，告警 Popover 回答
                  「哪里不符合、怎么改」—— 两问缺一，用户就只能靠通读正文自查。 */}
              {item.generation_standard && (
                <Tag
                  color={item.generation_standard === "precise" ? "blue" : "cyan"}
                  style={{ margin: 0, fontSize: 11, flexShrink: 0 }}
                >
                  {STANDARD_LABELS[item.generation_standard] || item.generation_standard}
                </Tag>
              )}
              {item.status === "success" && reportHasIssues(item.standard_report) && (
                <Popover
                  placement="leftTop"
                  title="生成标准校验（咨询性，不阻断生成）"
                  content={
                    <div style={{ maxWidth: 420, maxHeight: 300, overflowY: "auto" }}>
                      {(item.standard_report?.issues || []).map((it, i) => (
                        <div key={i} style={{ marginBottom: 6, fontSize: 12 }}>
                          <Tag
                            color={it?.severity === "error" ? "red" : "orange"}
                            style={{ marginRight: 4, fontSize: 11 }}
                          >
                            {issueLabel(it)}
                          </Tag>
                          <span>{it?.message || "（无描述）"}</span>
                          {it?.excerpt && (
                            <div style={{ color: "#999", fontSize: 11, marginTop: 2 }}>
                              原文：{it.excerpt}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  }
                >
                  <Tag color="volcano" style={{ margin: 0, fontSize: 11, flexShrink: 0, cursor: "help" }}>
                    标准告警 {reportIssueCount(item.standard_report)}
                  </Tag>
                </Popover>
              )}
              {/* 生成中 → 实时耗时；已完成 → 总耗时 */}
              {item.status === "running" ? (
                <ElapsedTimer startTime={item.time} />
              ) : (
                typeof item.duration === "number" && (
                  <Text type="secondary" style={{ fontSize: 11, marginLeft: 4, flexShrink: 0 }}>
                    {item.duration < 1000
                      ? `${item.duration}ms`
                      : `${(item.duration / 1000).toFixed(1)}s`}
                  </Text>
                )
              )}
              {item.status === "failed" && (
                <ExclamationCircleOutlined style={{ color: "#ff4d4f", fontSize: 13 }} />
              )}
            </div>
            {item.status === "failed" && item.reason && (
              <div
                style={{
                  marginTop: 2,
                  padding: "4px 8px",
                  background: "rgba(255,77,79,0.08)",
                  borderLeft: "2px solid #ff4d4f",
                  borderRadius: 2,
                  fontSize: 12,
                  color: "#cf1322",
                  wordBreak: "break-all",
                }}
              >
                {item.reason}
              </div>
            )}
          </div>
        </List.Item>
      )}
    />
  );
});

// ✅ P0-5 组件拆分：生成进度卡（Progress + 统计 + 双日志）独立 memo 组件——
//    生成期间高频更新的只有本卡与日志，抽离后整页其余部分不再随之重渲

import { buildFactChapterStats } from "./factsShared.ts";
import { BarChartOutlined } from "@ant-design/icons";
import { Card, Progress } from "antd";
import { Typography } from "antd";
import type { FactChapterStatBlock } from "../../types/facts";
const { Title, Text } = Typography;

export function FactsChapterCoveragePanel({ stats, titles }: {
  stats: { by_chapter?: FactChapterStatBlock } | null | undefined; titles?: Record<string, string>;
}) {
  const rows = buildFactChapterStats(stats, titles);
  if (rows.length === 0) {
    return (
      <Card size="small" style={{ marginBottom: 8 }}
        title={<span><BarChartOutlined /> 九章事实分布</span>}>
        <Text type="secondary">暂无章节统计</Text>
      </Card>
    );
  }
  const totalFacts = rows.reduce((s, r) => s + r.count, 0);
  return (
    <Card size="small" style={{ marginBottom: 8 }}
      title={<span><BarChartOutlined /> 九章事实分布（共 {totalFacts} 条）</span>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {rows.map((r) => (
          <div key={r.key || r.order}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
              <span>{r.order}. {r.title || r.key}</span>
              <span style={{ color: r.count === 0 ? "#ff4d4f" : "#888" }}>
                {r.count === 0 ? "无事实" : `${r.count} 条`}
                {r.shared > 0 ? ` · 共性 ${r.shared}` : ""}
              </span>
            </div>
            <Progress
              percent={totalFacts ? Math.round((r.count / totalFacts) * 100) : 0}
              size="small"
              status={r.count === 0 ? "exception" : "normal"}
            />
            {r.missingCount > 0 && (
              <div style={{ fontSize: 11, color: "#999" }}>
                缺字段（{r.missingCount}）：
                {r.missingFields.slice(0, 4).join("、")}
                {r.missingFields.length > 4 ? "…" : ""}
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

/**
 * SSE completed 事件的统一收口：segment_stats / cross_conflicts 缺字段即清空。
 *
 * ✅ BUG 修复（2026-09-21）：增量提取「全部跳过（all_skipped）」的 completed
 * 事件不带 segment_stats，旧实现 `if (evt.segment_stats) setFactsSegmentStats(...)`
 * 只在有字段时才写 → 上一次提取的失败告警会残留，误导用户以为本次也失败；
 * 跨段矛盾横幅同理。现缺字段一律清空，避免跨次 / 跨方案残留。
 */

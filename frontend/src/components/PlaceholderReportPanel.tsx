/**
 * 《待补充清单》面板（2026-09-24，治 F 层：人工补录兜底）
 *
 * 数据来源：导出预检（POST /export/check）返回的 placeholder_report 精简键；
 * 逐条 occurrences 可经 exportApi.placeholderReport 单独拉取（本面板按需展示聚合）。
 * 功能：按字段 / 按章节两个视图聚合展示缺失参数，点击章节可跳转定位
 * （onJumpToSection 由宿主页注入；不注入时仅展示，不渲染跳转按钮）。
 *
 * 设计约束：纯展示组件（无请求、无全局状态），与仓库既有 Tab 组件风格一致。
 */
import { Alert, Button, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import React, { useEffect, useState } from "react";
import { exportApi } from "../api";
import {
  DISPLAY_ENTRY_LIMIT,
  PlaceholderBaselineRow,
  PlaceholderReport,
  derivePlaceholderSeverity,
  deriveTrendDelta,
  limitEntries,
  severityToAlertProps,
  summarizePlaceholderReport,
} from "../utils/placeholderReport";

const { Text } = Typography;

export interface PlaceholderReportPanelProps {
  /** 导出预检返回的 placeholder_report（精简版即可；null = 尚未预检） */
  report: PlaceholderReport | null | undefined;
  /** 点击章节跳转回调（宿主页负责切 Tab + 选中章节；不注入时仅展示，不渲染跳转按钮） */
  onJumpToSection?: (sectionId: string) => void;
  /** ✅ 传入方案 ID 后面板自取历史基线，展示「较上次预检」趋势（监控回归） */
  schemeId?: string;
  /** 可自动重跑的受影响章节数（来自重跑计划；>0 且提供回调时渲染重跑按钮） */
  rerunCount?: number;
  /** 重跑进行中（宿主页逐章重跑期间禁用按钮） */
  rerunning?: boolean;
  /** 「重跑受影响章节」回调（宿主页逐章 mode=section + force_rewrite 重跑） */
  onRerunAffected?: () => void;
}

/** 按字段视图：字段名 | 出现次数 | 所在章节（可跳转） */
function FieldTable({ report, onJump }: { report: PlaceholderReport; onJump?: (sid: string) => void }) {
  const [rows, hidden] = limitEntries(report.by_field);
  const extra = hidden > 0 ? [{ field: `…其余 ${hidden} 个字段略`, count: 0, section_ids: [], section_titles: [] }] : [];
  return (
    <Table
      size="small"
      rowKey="field"
      pagination={false}
      dataSource={[...rows, ...extra]}
      columns={[
        {
          title: "缺失字段",
          dataIndex: "field",
          width: 200,
          render: (v: string) => <Text strong>{v}</Text>,
        },
        { title: "次数", dataIndex: "count", width: 70, render: (c: number) => (c ? <Tag color="orange">{c}</Tag> : "") },
        {
          title: "所在章节（点击跳转）",
          dataIndex: "section_titles",
          render: (_: unknown, rec: { section_ids: string[]; section_titles: string[] }) => (
            <Space size={4} wrap>
              {rec.section_titles.map((t, i) => {
                const sid = rec.section_ids[i];
                return onJump && sid ? (
                  <Button key={sid} size="small" type="link" style={{ padding: 0 }} onClick={() => onJump(sid)}>
                    {t}
                  </Button>
                ) : (
                  <Text key={`${sid || t}-${i}`} type="secondary">{t}</Text>
                );
              })}
            </Space>
          ),
        },
      ]}
    />
  );
}

/** 按章节视图：章节 | 占位数 | 涉及字段（可跳转） */
function SectionTable({ report, onJump }: { report: PlaceholderReport; onJump?: (sid: string) => void }) {
  const [rows, hidden] = limitEntries(report.by_section);
  const extra = hidden > 0
    ? [{ section_id: "__more__", title: `…其余 ${hidden} 个章节略`, count: 0, fields: [] }]
    : [];
  return (
    <Table
      size="small"
      rowKey="section_id"
      pagination={false}
      dataSource={[...rows, ...extra]}
      columns={[
        {
          title: "章节",
          dataIndex: "title",
          width: 240,
          render: (v: string, rec: { section_id: string; count: number }) =>
            rec.count > 0 && onJump ? (
              <Button size="small" type="link" style={{ padding: 0 }} onClick={() => onJump(rec.section_id)}>
                {v}
              </Button>
            ) : (
              <Text strong>{v}</Text>
            ),
        },
        { title: "占位数", dataIndex: "count", width: 80, render: (c: number) => (c ? <Tag color="orange">{c}</Tag> : "") },
        {
          title: "涉及字段",
          dataIndex: "fields",
          render: (fields: string[]) =>
            fields.length ? fields.map((f) => <Tag key={f} style={{ marginBottom: 2 }}>{f}</Tag>) : <Text type="secondary">—</Text>,
        },
      ]}
    />
  );
}

const PlaceholderReportPanel: React.FC<PlaceholderReportPanelProps> = ({
  report, onJumpToSection, schemeId, rerunCount, rerunning, onRerunAffected,
}) => {
  // ✅ 监控基线（六层方案第 6 层）：传入 schemeId 时自取历史，展示趋势。
  //    拉取失败静默降级为无趋势（监控旁路，绝不打断主展示）。
  const [history, setHistory] = useState<PlaceholderBaselineRow[]>([]);
  useEffect(() => {
    let cancelled = false;
    if (!schemeId) return;
    exportApi.placeholderHistory(schemeId)
      .then(({ data }) => { if (!cancelled) setHistory(data?.history || []); })
      .catch(() => { /* 静默：历史趋势属可选增强 */ });
    return () => { cancelled = true; };
  }, [schemeId, report?.total]);
  const trend = deriveTrendDelta(history);

  if (!report) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        待补充清单：先运行「导出预检」生成占位符扫描结果。
      </Text>
    );
  }
  const sev = derivePlaceholderSeverity(report);
  const alert = severityToAlertProps(sev);
  /** 趋势文案：下降绿 / 上升红 / 首次基线灰（负数=改善） */
  const trendNode = trend.delta === null
    ? (trend.latest !== null
      ? <Text type="secondary" style={{ fontSize: 12 }}>已建立监控基线（当前 {trend.latest} 处）</Text>
      : null)
    : (
      <Text style={{ fontSize: 12 }} type={trend.delta <= 0 ? "success" : "danger"}>
        较上次预检 {trend.delta > 0 ? `+${trend.delta}` : trend.delta} 处
        {trend.delta < 0 ? "（下降）" : trend.delta > 0 ? "（上升，建议核查是否引入新占位）" : ""}
      </Text>
    );
  return (
    <div style={{ marginTop: 12 }}>
      <Alert
        type={alert.type}
        showIcon
        message={alert.message}
        description={
          <>
            {summarizePlaceholderReport(report) || undefined}
            {trendNode && <div style={{ marginTop: 2 }}>{trendNode}</div>}
          </>
        }
      />
      {report.total > 0 && onRerunAffected && (rerunCount || 0) > 0 && (
        <div style={{ marginTop: 8 }}>
          <Button
            size="small"
            type="primary"
            ghost
            loading={rerunning}
            onClick={onRerunAffected}
          >
            重跑受影响章节（{rerunCount}）—— 只重跑补录数据已到位的章节
          </Button>
        </div>
      )}
      {report.total > 0 && (
        <Tabs
          size="small"
          style={{ marginTop: 8 }}
          items={[
            { key: "field", label: `按字段（${report.field_count}）`, children: <FieldTable report={report} onJump={onJumpToSection} /> },
            { key: "section", label: `按章节（${report.section_count}）`, children: <SectionTable report={report} onJump={onJumpToSection} /> },
            {
              key: "hint",
              label: "补录指引",
              children: (
                <div style={{ fontSize: 12, color: "rgba(0,0,0,0.65)" }}>
                  <p style={{ margin: "4px 0" }}>
                    ① 优先在「上传解析 → 提取项目 / 全局事实」补齐数据源，再重新生成对应章节（只重跑受影响章节，无需整篇重生成）；
                  </p>
                  <p style={{ margin: "4px 0" }}>
                    ② 确实无法自动获取的参数（业主特殊要求等），手动编辑章节正文替换对应【待补充：字段名】；
                  </p>
                  <p style={{ margin: "4px 0" }}>
                    ③ <Tooltip title="提示词要求统一写作【待补充：字段名】；××/xx 无法被本清单定位">裸标记与 ××/xx 属不规范写法</Tooltip>，请回改为统一格式后再导出。
                  </p>
                  <p style={{ margin: "4px 0" }}>每次补录后重新预检，对比 total 下降即验证生效（上限展示 {DISPLAY_ENTRY_LIMIT} 条）。</p>
                </div>
              ),
            },
          ]}
        />
      )}
    </div>
  );
};

export default PlaceholderReportPanel;

import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Card, Space, Table, Tag, Typography } from "antd";
import { CheckSquareOutlined, ReloadOutlined } from "@ant-design/icons";
import { defectsApi, factsApi } from "../../api";
import type { FactItem } from "../../types/facts";
import type { DefectItem } from "../../types/defect";

const { Text } = Typography;

interface PendingRow {
  key: string;
  kind: "fact" | "defect";
  title: string;
  category: string;
  reasons: string[];
  detail: string;
}

const LOW_CONFIDENCE = 0.8;

/**
 * 与底稿附件三同口径：事实待确认 = 未 resolve 或 confidence < 0.8。
 * 来源为 backend/app/services/dossier.py::collect_facts。
 */
function factReasons(fact: FactItem): string[] {
  const reasons: string[] = [];
  if (!fact.is_resolved) reasons.push("未确认");
  if (typeof fact.confidence === "number" && fact.confidence < LOW_CONFIDENCE) {
    reasons.push(`低置信度 ${fact.confidence.toFixed(2)}`);
  }
  return reasons;
}

function defectReasons(defect: DefectItem): string[] {
  const reasons: string[] = [];
  if (defect.fix_policy === "protected") reasons.push("命中禁改白名单");
  if (defect.status === "suggested") reasons.push("有建议待人工确认");
  return reasons;
}

export interface PendingConfirmPanelProps {
  schemeId: string;
}

/**
 * 待确认清单（R179 Phase H 的前端聚合出口）。
 *
 * 不新造后端聚合接口，复用既有两类数据源：
 * - 事实：`GET /global-facts` 中模拟值 / 未确认 / 矛盾 / 过期 / 低置信度条目；
 * - 缺陷：`GET /schemes/{id}/defects` 中受保护或已有建议的条目。
 *
 * 任一数据源不可用时该半区显示「无法判定」而不是「0 条」——空集与不可达必须区分。
 */
export function PendingConfirmPanel({ schemeId }: PendingConfirmPanelProps) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<PendingRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [factsState, setFactsState] = useState<"ok" | "error">("ok");
  const [defectsState, setDefectsState] = useState<"ok" | "off" | "error">("ok");

  const load = useCallback(async () => {
    if (!schemeId) return;
    setLoading(true);
    const [factsResult, defectsResult] = await Promise.allSettled([
      factsApi.list(schemeId),
      defectsApi.list(schemeId),
    ]);

    const nextRows: PendingRow[] = [];

    if (factsResult.status === "fulfilled") {
      setFactsState("ok");
      const groups = factsResult.value.data?.groups || [];
      for (const group of groups) {
        for (const fact of group.items || []) {
          const reasons = factReasons(fact);
          if (!reasons.length) continue;
          nextRows.push({
            key: `fact-${fact.fact_id}`,
            kind: "fact",
            title: fact.name || "（未命名事实）",
            category: fact.category || group.category || "—",
            reasons,
            detail: `${fact.value}${fact.value_unit || ""}`,
          });
        }
      }
    } else {
      setFactsState("error");
    }

    if (defectsResult.status === "fulfilled") {
      setDefectsState("ok");
      const items = Array.isArray(defectsResult.value.data?.items)
        ? defectsResult.value.data.items
        : [];
      for (const defect of items) {
        const reasons = defectReasons(defect);
        if (!reasons.length) continue;
        nextRows.push({
          key: `defect-${defect.id}`,
          kind: "defect",
          title: defect.title || "（未命名缺陷）",
          category: defect.rule_id || defect.dimension || "—",
          reasons,
          detail: defect.section_title || defect.detail || "—",
        });
      }
    } else {
      const err = defectsResult.reason as { response?: { status?: number } };
      setDefectsState(err?.response?.status === 404 ? "off" : "error");
    }

    setRows(nextRows);
    setLoading(false);
  }, [schemeId]);

  useEffect(() => {
    load();
  }, [load]);

  const refresh = async () => {
    await load();
    message.success("待确认清单已刷新");
  };

  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      title={
        <Space wrap>
          <CheckSquareOutlined />
          <Text strong>待确认清单（人工复核）</Text>
          {factsState === "ok" && defectsState !== "error" && (
            <Tag color={rows.length ? "orange" : "green"} data-testid="pending-count">
              {rows.length ? `待确认 ${rows.length} 项` : "待确认 0 项"}
            </Tag>
          )}
        </Space>
      }
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={refresh}
          data-testid="pending-refresh"
        >
          刷新
        </Button>
      }
    >
      {factsState === "error" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message="事实数据无法判定"
          description="事实清单接口不可用，不能据此断言「没有待确认事实」。"
        />
      )}
      {defectsState === "off" && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message="缺陷账本未启用"
          description="defect_ledger_enabled 关闭，缺陷侧的待确认项无法判定。"
        />
      )}
      {defectsState === "error" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message="缺陷数据无法判定"
          description="缺陷账本接口不可用，不能据此断言「没有待确认缺陷」。"
        />
      )}

      {rows.length === 0 && factsState === "ok" && defectsState !== "error" && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          当前没有未确认或低置信度的事实，也没有受保护或已建议的缺陷。
        </Text>
      )}

      {rows.length > 0 && (
        <Table
          size="small"
          rowKey="key"
          pagination={false}
          scroll={{ x: 720 }}
          dataSource={rows}
          columns={[
            {
              title: "来源",
              dataIndex: "kind",
              width: 80,
              render: (kind: PendingRow["kind"]) =>
                kind === "fact" ? <Tag color="geekblue">事实</Tag> : <Tag color="volcano">缺陷</Tag>,
            },
            {
              title: "条目",
              dataIndex: "title",
              width: 240,
              render: (title: string, row: PendingRow) => (
                <Space direction="vertical" size={0}>
                  <Text strong style={{ fontSize: 12 }}>
                    {title}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {row.detail}
                  </Text>
                </Space>
              ),
            },
            { title: "类别 / 规则", dataIndex: "category", width: 160 },
            {
              title: "待确认原因",
              dataIndex: "reasons",
              render: (reasons: string[]) => (
                <Space size={4} wrap>
                  {reasons.map((reason) => (
                    <Tag key={reason} color="orange">
                      {reason}
                    </Tag>
                  ))}
                </Space>
              ),
            },
          ]}
        />
      )}

      {/* 标准待核实与底稿附件三同口径：没有方案级数据源时显式「无法判定」，
          不输出 0 条冒充「无待核实标准」。 */}
      <Alert
        type="info"
        showIcon
        style={{ marginTop: 8 }}
        data-testid="pending-standards-note"
        message="标准待核实：无法判定"
        description="后端尚未提供方案级 verification_status 查询接口；本清单与底稿附件三同口径，不把「无数据」当成「无待核实标准」。"
      />
    </Card>
  );
}

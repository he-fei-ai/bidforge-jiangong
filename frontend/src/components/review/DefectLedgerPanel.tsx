import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { ProfileOutlined, ReloadOutlined, SyncOutlined } from "@ant-design/icons";
import { defectsApi } from "../../api";
import type { DefectItem, DefectSummary, DefectVerdict } from "../../types/defect";

const { Text } = Typography;

const STATUS_LABEL: Record<string, string> = {
  open: "待处理",
  suggested: "建议（未执行）",
  applied: "已执行（正文已写入）",
  dismissed: "已驳回",
};

const STATUS_COLOR: Record<string, string> = {
  open: "default",
  suggested: "blue",
  applied: "green",
  dismissed: "default",
};

function verdictTag(verdict: DefectVerdict | string) {
  if (verdict === "yes") {
    return (
      <Tag color="green" data-testid="defect-verdict-yes">
        通过
      </Tag>
    );
  }
  if (verdict === "no") {
    return (
      <Tag color="red" data-testid="defect-verdict-no">
        不通过
      </Tag>
    );
  }
  if (verdict === "undetermined") {
    return (
      <Tag color="gold" data-testid="defect-verdict-undetermined">
        无法判定
      </Tag>
    );
  }
  return (
    <Tag color="default" data-testid="defect-verdict-pending">
      待检
    </Tag>
  );
}

function num(value: number | undefined): number {
  return typeof value === "number" ? value : 0;
}

export interface DefectLedgerPanelProps {
  schemeId: string;
}

/**
 * 统一缺陷账本（R179 Phase C 的前端出口）。
 *
 * 关键口径：
 * - 三态（通过 / 不通过 / 无法判定）分列，undetermined 不并入通过；
 * - 建议（status=suggested）与执行（status=applied）分开展示；
 * - fix_policy=protected 的行禁用「自动执行」，只能走人工裁决（后端也会 409）。
 */
export function DefectLedgerPanel({ schemeId }: DefectLedgerPanelProps) {
  const { message } = App.useApp();
  const [items, setItems] = useState<DefectItem[]>([]);
  const [summary, setSummary] = useState<DefectSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!schemeId) return;
    setLoading(true);
    setError("");
    try {
      const [listResp, summaryResp] = await Promise.all([
        defectsApi.list(schemeId),
        defectsApi.summary(schemeId),
      ]);
      setItems(Array.isArray(listResp.data?.items) ? listResp.data.items : []);
      setSummary(summaryResp.data || null);
      setDisabled(false);
    } catch (e: unknown) {
      const err = e as { response?: { status?: number }; message?: string };
      if (err?.response?.status === 404) {
        setDisabled(true);
        setItems([]);
        setSummary(null);
      } else {
        setError(err?.message || "缺陷账本读取失败");
      }
    } finally {
      setLoading(false);
    }
  }, [schemeId]);

  useEffect(() => {
    load();
  }, [load]);

  const sync = async () => {
    if (!schemeId) return;
    setSyncing(true);
    try {
      const { data } = await defectsApi.sync(schemeId);
      const created = (data?.sources || []).reduce((acc, s) => acc + num(s.created), 0);
      const updated = (data?.sources || []).reduce((acc, s) => acc + num(s.updated), 0);
      message.success(`已同步预检 / 一致性结果：新增 ${created} 条，更新 ${updated} 条`);
      await load();
    } catch (e: unknown) {
      const err = e as { response?: { status?: number }; message?: string };
      if (err?.response?.status === 404) {
        setDisabled(true);
      } else {
        message.error(err?.message || "缺陷同步失败");
      }
    } finally {
      setSyncing(false);
    }
  };

  const runAction = async (id: string, action: () => Promise<unknown>, okText: string) => {
    setBusyId(id);
    try {
      await action();
      message.success(okText);
      await load();
    } catch (e: unknown) {
      const err = e as { response?: { status?: number }; message?: string };
      if (err?.response?.status === 409) {
        message.warning("该缺陷命中禁改白名单，自动执行被拒绝，请走人工裁决");
      } else {
        message.error(err?.message || "操作失败");
      }
    } finally {
      setBusyId("");
    }
  };

  if (disabled) {
    return (
      <Card
        size="small"
        style={{ marginBottom: 12 }}
        title={
          <Space>
            <ProfileOutlined />
            <Text strong>统一缺陷账本</Text>
          </Space>
        }
      >
        <Alert
          type="warning"
          showIcon
          message="缺陷账本功能未启用"
          description="后端开关 defect_ledger_enabled 默认关闭；开启后可同步预检 / 一致性审计结果并区分建议与执行。"
        />
      </Card>
    );
  }

  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      title={
        <Space wrap>
          <ProfileOutlined />
          <Text strong>统一缺陷账本（三态）</Text>
          {summary && (
            <Space size={4} wrap data-testid="defect-summary">
              <Tag>总数 {num(summary.total)}</Tag>
              <Tag color="green" data-testid="defect-summary-passed">
                通过 {num(summary.passed)}
              </Tag>
              <Tag color="red" data-testid="defect-summary-failed">
                不通过 {num(summary.failed)}
              </Tag>
              <Tag color="gold" data-testid="defect-summary-undetermined">
                无法判定 {num(summary.undetermined)}
              </Tag>
              <Tag color="default" data-testid="defect-summary-pending">
                待检 {num(summary.pending)}
              </Tag>
            </Space>
          )}
        </Space>
      }
      extra={
        <Space size={4}>
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={load}>
            刷新
          </Button>
          <Button
            size="small"
            type="primary"
            ghost
            icon={<SyncOutlined />}
            loading={syncing}
            onClick={sync}
            data-testid="defect-sync"
          >
            同步预检结果
          </Button>
        </Space>
      }
    >
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 8 }} />}
      {summary?.pass_rule && (
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 8 }}>
          统计口径：{summary.pass_rule}
        </Text>
      )}
      {!error && items.length === 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          账本中暂无缺陷。点击「同步预检结果」从最近一次预检 / 一致性审计拉取（只读，不回写源表）。
        </Text>
      )}
      {items.length > 0 && (
        <Table
          size="small"
          rowKey="id"
          pagination={false}
          scroll={{ x: 860 }}
          dataSource={items}
          columns={[
            {
              title: "缺陷",
              dataIndex: "title",
              width: 220,
              render: (title: string, row: DefectItem) => (
                <Space direction="vertical" size={0}>
                  <Text strong style={{ fontSize: 12 }}>
                    {title || "（未命名缺陷）"}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {row.section_title || row.section_id || "方案级"} · {row.source_kind || "—"}
                  </Text>
                </Space>
              ),
            },
            {
              title: "规则 / 维度",
              dataIndex: "rule_id",
              width: 170,
              render: (ruleId: string, row: DefectItem) => (
                <Space direction="vertical" size={0}>
                  <Text style={{ fontSize: 12 }}>{ruleId || "—"}</Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {row.dimension || "—"}
                    {row.severity ? ` · ${row.severity}` : ""}
                  </Text>
                </Space>
              ),
            },
            {
              title: "三态结论",
              dataIndex: "verdict",
              width: 100,
              render: (verdict: string) => verdictTag(verdict),
            },
            {
              title: "状态 / 策略",
              dataIndex: "status",
              width: 190,
              render: (status: string, row: DefectItem) => (
                <Space size={4} wrap>
                  <Tag color={STATUS_COLOR[status] || "default"}>
                    {STATUS_LABEL[status] || status || "待处理"}
                  </Tag>
                  {row.fix_policy === "protected" ? (
                    <Tag color="red" data-testid={`defect-protected-${row.id}`}>
                      禁改·需人工裁决
                    </Tag>
                  ) : (
                    <Tag color="blue" data-testid={`defect-free-${row.id}`}>
                      建议可执行
                    </Tag>
                  )}
                </Space>
              ),
            },
            {
              title: "操作",
              key: "actions",
              width: 280,
              render: (_: unknown, row: DefectItem) => {
                const hasAfter = Boolean((row.after_text || "").trim());
                const applyDisabled = row.fix_policy === "protected" || !hasAfter || row.status === "applied";
                const applyReason =
                  row.fix_policy === "protected"
                    ? "命中禁改白名单：自动执行已禁用，只能人工裁决"
                    : !hasAfter
                      ? "尚无建议全文，不能执行"
                      : row.status === "applied"
                        ? "该缺陷已执行"
                        : "执行建议并写入正文";
                return (
                  <Space size={4} wrap>
                    <Button
                      size="small"
                      loading={busyId === row.id}
                      onClick={() => runAction(row.id, () => defectsApi.verify(row.id), "检查器已重跑")}
                    >
                      验证
                    </Button>
                    <Tooltip title={applyReason}>
                      <Button
                        size="small"
                        type="primary"
                        disabled={applyDisabled}
                        data-testid={`defect-apply-${row.id}`}
                        loading={busyId === row.id}
                        onClick={() =>
                          runAction(row.id, () => defectsApi.apply(row.id), "建议已执行并写入正文")
                        }
                      >
                        执行
                      </Button>
                    </Tooltip>
                    {row.fix_policy === "protected" && (
                      <Popconfirm
                        title="确认人工接受该建议并写入正文？"
                        okText="确认接受"
                        disabled={!hasAfter}
                        onConfirm={() =>
                          runAction(
                            row.id,
                            () =>
                              defectsApi.decide(row.id, {
                                decision: "accept",
                                reason: "人工复核接受",
                              }),
                            "人工裁决已接受",
                          )
                        }
                      >
                        <Button
                          size="small"
                          disabled={!hasAfter}
                          data-testid={`defect-accept-${row.id}`}
                          loading={busyId === row.id}
                        >
                          人工接受
                        </Button>
                      </Popconfirm>
                    )}
                    <Popconfirm
                      title="确认驳回该缺陷？"
                      onConfirm={() =>
                        runAction(
                          row.id,
                          () =>
                            defectsApi.decide(row.id, {
                              decision: "reject",
                              reason: "人工复核驳回",
                            }),
                          "已驳回",
                        )
                      }
                    >
                      <Button size="small" danger data-testid={`defect-reject-${row.id}`}>
                        驳回
                      </Button>
                    </Popconfirm>
                  </Space>
                );
              },
            },
          ]}
        />
      )}
    </Card>
  );
}

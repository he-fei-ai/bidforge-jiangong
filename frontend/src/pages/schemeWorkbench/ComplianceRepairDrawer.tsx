/**
 * 全文一致性 Agent 修复工作台抽屉（G4d · 2026-11-04 从 SchemeWorkbenchPage
 * 原样抽取，零行为变更）。
 *
 * 纯面板：props 进、回调出。抽屉内的扫描/修复/确认/回滚动作全部由页面注入
 * （既有 hook `useReviewAudit` 提供），本组件不直接调用 api、不读页面 state。
 */
import {
  Button,
  Card,
  Checkbox,
  Collapse,
  Drawer,
  Empty,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import { FileSearchOutlined, ReloadOutlined, SyncOutlined } from "@ant-design/icons";
import type {
  CrConflictItem,
  CrRepairHistoryItem,
  CrRepairResult,
  CrSummary,
} from "./useReviewAudit";
import { CR_ITEM_STATUS, CR_REPAIR_STATUS, CR_STATUS_LABEL, CR_TYPE_LABEL } from "./reviewShared";

const { Text } = Typography;

export interface ComplianceRepairDrawerProps {
  open: boolean;
  onClose: () => void;
  schemeName?: string;
  loading: boolean;
  summary: CrSummary | null;
  scanId: string;
  scanning: boolean;
  repairing: boolean;
  conflicts: CrConflictItem[];
  repair: CrRepairResult | null;
  history: CrRepairHistoryItem[];
  consistencySeverity: string;
  forceFullRepair: boolean;
  /** ✅ R197（F4）：确认请求在飞态（四个确认按钮共用，双击双发防护） */
  confirming: boolean;
  onForceFullRepairChange: (checked: boolean) => void;
  onScan: () => void;
  onCancelScan: () => void;
  onRepair: () => void;
  onCancelRepair: () => void;
  repairItemIds: (status: string) => string[];
  onConfirm: (acceptIds: string[], rejectIds: string[]) => void;
  onReload: () => void;
  onRollback: (row: CrRepairHistoryItem) => void;
}

export function ComplianceRepairDrawer({
  open,
  onClose,
  schemeName,
  loading,
  summary,
  scanId,
  scanning,
  repairing,
  conflicts,
  repair,
  history,
  consistencySeverity,
  forceFullRepair,
  confirming,
  onForceFullRepairChange,
  onScan,
  onCancelScan,
  onRepair,
  onCancelRepair,
  repairItemIds,
  onConfirm,
  onReload,
  onRollback,
}: ComplianceRepairDrawerProps) {
  return (
    <Drawer
      title={
        <Space>
          <SyncOutlined />
          <span>全文一致性 Agent 修复工作台</span>
          {summary && (
            <Tag
              color={
                (summary.high ?? 0) > 0 ? "red" : (summary.total ?? 0) > 0 ? "orange" : "green"
              }
            >
              冲突 {summary.total ?? 0}
            </Tag>
          )}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={1000}
      styles={{ body: { padding: 16 } }}
    >
      <Spin spinning={loading}>
        {/* 概要 + 操作 */}
        <Card size="small" style={{ marginBottom: 12 }}>
          <Space size={16} wrap>
            <Text strong>{schemeName || "当前方案"}</Text>
            {scanId && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                扫描号：{scanId}
              </Text>
            )}
            {summary && (
              <>
                <Tag>冲突总数 {summary.total ?? 0}</Tag>
                <Tag color="red">高 {summary.high ?? 0}</Tag>
                <Tag color="orange">中 {summary.medium ?? 0}</Tag>
                <Tag color="blue">低 {summary.low ?? 0}</Tag>
                <Tag>待处理 {summary.pending ?? 0}</Tag>
                <Tag>待人工 {summary.skipped ?? 0}</Tag>
              </>
            )}
          </Space>
          <div style={{ marginTop: 10 }}>
            <Space wrap>
              <Button
                type="primary"
                icon={<FileSearchOutlined />}
                loading={scanning}
                onClick={onScan}
              >
                开始扫描
              </Button>
              {/* ✅ R168（FIX-10）：扫描/修复是 10 分钟级 AI 长任务，补取消入口
                  （口径与 R106 FIX-5 的三个预检取消按钮一致，运行中才出现） */}
              {scanning && (
                <Button danger onClick={onCancelScan}>
                  取消扫描
                </Button>
              )}
              <Button
                type="primary"
                ghost
                icon={<SyncOutlined />}
                loading={repairing}
                disabled={conflicts.length === 0}
                onClick={onRepair}
              >
                一键修复（
                {consistencySeverity === "high"
                  ? "仅高危"
                  : consistencySeverity === "medium"
                    ? "中及以上"
                    : "全部"}
                ）
              </Button>
              {repairing && (
                <Button danger onClick={onCancelRepair}>
                  取消修复
                </Button>
              )}
              {/* ✅ 强制全量重修（2026-09-22）：默认跳过「已修复且成果仍在」的冲突，
                  勾选后本批全部重新调用 AI 修复 */}
              <Checkbox
                checked={forceFullRepair}
                onChange={(e) => onForceFullRepairChange(e.target.checked)}
              >
                强制全量重修
              </Checkbox>
              <Button
                loading={confirming}
                disabled={!(repair?.items || []).some((i) => i.status === "repaired")}
                onClick={() => onConfirm(repairItemIds("repaired"), [])}
              >
                全部接受
              </Button>
              <Button
                danger
                loading={confirming}
                disabled={!(repair?.items || []).some((i) => i.status === "repaired")}
                onClick={() => onConfirm([], repairItemIds("repaired"))}
              >
                全部拒绝
              </Button>
              <Button icon={<ReloadOutlined />} onClick={onReload}>
                刷新
              </Button>
            </Space>
            <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
              {/* ✅ R197（F5）：文案对齐 R181 冲突级片段回滚语义（旧文案宣称
                  「整体恢复为修复前原文」与后端现行三态行为矛盾） */}
              💡 一次修复是按「章节」成稿的：逐条<Text strong>拒绝</Text>某条冲突会回滚该
              冲突对应的变更片段；无法定位到变更片段时<Text strong>正文不会回滚</Text>
              （仅回写冲突状态，会显式提示）。需要保留部分修改时，请先「全部拒绝」后用更低的修复等级重新「一键修复」。
            </div>
            <div style={{ marginTop: 4, fontSize: 12, color: "#999" }}>
              💡 默认会跳过「已修复且修复成果仍在正文里」的冲突以节省 AI 调用；
              勾选「强制全量重修」后本批对所有命中的冲突重新调用 AI 修复。
            </div>
          </div>
        </Card>

        {/* 冲突清单 */}
        <Card size="small" title="冲突清单" style={{ marginBottom: 12 }}>
          {conflicts.length === 0 ? (
            <Empty
              description="暂无冲突清单，请先点击「开始扫描」"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            <Table
              size="small"
              rowKey="id"
              pagination={{ pageSize: 8, size: "small" }}
              // ✅ 性能优化：虚拟滚动（固定 260px 表体，避免视口外行全量建 DOM）
              scroll={{ y: 260 }}
              virtual
              dataSource={conflicts}
              expandable={{
                expandedRowRender: (r) => (
                  <div style={{ fontSize: 12 }}>
                    {(r.occurrences || []).map((o, i: number) => (
                      <div key={i} style={{ marginBottom: 4 }}>
                        <Tag>{o.section_title || o.section_id}</Tag>
                        <span>{o.text || o.value}</span>
                      </div>
                    ))}
                    {r.repair_instruction && (
                      <div style={{ marginTop: 6, color: "#1677ff" }}>
                        修复指令：{r.repair_instruction}
                      </div>
                    )}
                    {r.reason && (
                      <div style={{ marginTop: 2, color: "#999" }}>仲裁理由：{r.reason}</div>
                    )}
                  </div>
                ),
              }}
              columns={[
                { title: "ID", dataIndex: "id", width: 70 },
                {
                  title: "类型",
                  dataIndex: "conflict_type",
                  width: 100,
                  render: (v: string) => CR_TYPE_LABEL[v] || v || "—",
                },
                {
                  title: "等级",
                  dataIndex: "severity",
                  width: 70,
                  render: (s: string) => (
                    <Tag color={s === "high" ? "red" : s === "medium" ? "orange" : "blue"}>
                      {s || "-"}
                    </Tag>
                  ),
                },
                { title: "主题", dataIndex: "topic", width: 150, ellipsis: true },
                {
                  title: "涉及章节",
                  dataIndex: "occurrences",
                  width: 160,
                  ellipsis: true,
                  render: (occs) =>
                    Array.from(
                      new Set(
                        (occs || []).map(
                          (o: { section_title?: string; section_id?: string }) =>
                            o.section_title || o.section_id,
                        ),
                      ),
                    ).join(" / ") || "—",
                },
                {
                  title: "当前值",
                  dataIndex: "occurrences",
                  width: 150,
                  ellipsis: true,
                  render: (occs) =>
                    Array.from(
                      new Set((occs || []).map((o: { value?: string }) => o.value).filter(Boolean)),
                    ).join(" / ") || "—",
                },
                {
                  title: "权威值",
                  dataIndex: "authoritative_value",
                  width: 130,
                  ellipsis: true,
                  render: (v: string) => v || "—（待人工）",
                },
                {
                  title: "状态",
                  dataIndex: "status",
                  width: 100,
                  render: (s: string) => <Tag>{CR_STATUS_LABEL[s] || s || "—"}</Tag>,
                },
              ]}
            />
          )}
        </Card>

        {/* 修复预览（逐条确认） */}
        <Card
          size="small"
          title={
            <Space>
              <span>修复预览</span>
              {repair?.repair_id && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  批次 {repair.repair_id} · {CR_REPAIR_STATUS[repair.status!] || repair.status}
                </Text>
              )}
            </Space>
          }
          style={{ marginBottom: 12 }}
        >
          {!(repair?.items || []).length ? (
            <Empty
              description="暂无修复结果，点击「一键修复」后在此逐条确认"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            <Collapse
              items={(repair!.items || []).map((it, idx: number) => ({
                key: `${it.conflict_id}-${idx}`,
                label: (
                  <Space wrap size={6}>
                    <Tag>{it.conflict_id || "—"}</Tag>
                    <Text style={{ fontSize: 12 }}>
                      {it.section_title || it.section_id || "（无章节）"}
                    </Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {it.topic}
                    </Text>
                    <Tag
                      color={
                        it.status === "repaired"
                          ? "green"
                          : it.status === "skipped"
                            ? "orange"
                            : "red"
                      }
                    >
                      {CR_ITEM_STATUS[it.status!] || it.status}
                    </Tag>
                  </Space>
                ),
                children: (
                  <div style={{ fontSize: 12 }}>
                    <div style={{ marginBottom: 6 }}>
                      <Text type="secondary">修复前：</Text>
                      <div
                        style={{
                          background: "rgba(255,77,79,0.06)",
                          padding: "4px 8px",
                          borderRadius: 4,
                          whiteSpace: "pre-wrap",
                          maxHeight: 120,
                          overflow: "auto",
                        }}
                      >
                        {it.before || "—"}
                      </div>
                    </div>
                    <div style={{ marginBottom: 6 }}>
                      <Text type="secondary">修复后：</Text>
                      <div
                        style={{
                          background: "rgba(82,196,26,0.06)",
                          padding: "4px 8px",
                          borderRadius: 4,
                          whiteSpace: "pre-wrap",
                          maxHeight: 120,
                          overflow: "auto",
                        }}
                      >
                        {it.after || "—"}
                      </div>
                    </div>
                    {(it.problems || []).length > 0 && (
                      <div style={{ color: "#cf1322", marginBottom: 6 }}>
                        问题：{(it.problems || []).join("；")}
                      </div>
                    )}
                    {it.status === "repaired" && (
                      <Space>
                        <Button
                          size="small"
                          type="primary"
                          disabled={confirming}
                          onClick={() => onConfirm([it.conflict_id!], [])}
                        >
                          接受
                        </Button>
                        <Button
                          size="small"
                          danger
                          disabled={confirming}
                          onClick={() => onConfirm([], [it.conflict_id!])}
                        >
                          拒绝（回滚变更片段）
                        </Button>
                      </Space>
                    )}
                  </div>
                ),
              }))}
            />
          )}
        </Card>

        {/* 修复记录（可回滚） */}
        <Card size="small" title="修复记录（可一键回滚）">
          {history.length === 0 ? (
            <Empty description="暂无修复记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <Table
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={history}
              columns={[
                {
                  title: "时间",
                  dataIndex: "created_at",
                  width: 160,
                  render: (v: string) => (v || "").replace("T", " ").slice(0, 19),
                },
                { title: "批次", dataIndex: "id", width: 160, ellipsis: true },
                { title: "冲突", dataIndex: "total_conflicts", width: 60 },
                { title: "已修复", dataIndex: "repaired", width: 70 },
                { title: "失败", dataIndex: "failed", width: 60 },
                {
                  title: "状态",
                  dataIndex: "status",
                  width: 110,
                  render: (s: string) => <Tag>{CR_REPAIR_STATUS[s] || s}</Tag>,
                },
                {
                  title: "操作",
                  width: 90,
                  render: (_, r) => (
                    <Button size="small" danger onClick={() => onRollback(r)}>
                      回滚
                    </Button>
                  ),
                },
              ]}
            />
          )}
        </Card>
      </Spin>
    </Drawer>
  );
}

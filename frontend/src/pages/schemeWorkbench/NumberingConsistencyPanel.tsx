import { useState } from "react";
import { App, Button, Card, Space, Table, Tag, Typography } from "antd";
import {
  NumberOutlined,
  RollbackOutlined,
  SafetyOutlined,
  HistoryOutlined,
} from "@ant-design/icons";
import { sectionsApi } from "../../api";

const { Text } = Typography;

/**
 * ✅ R174 F1（2026-11-03）：编号一致性检查 / 修复 / 版本 / 回滚 面板。
 *
 * 背景（审计 R173 · D1/F1）：后端 4 个端点早已实现
 * （GET /sections/numbering-consistency 校验、POST .../repair 修复、
 *  GET .../versions 版本列表、POST .../rollback/{snapshot_id} 回滚），
 * 前端 api 封装也早已存在（sectionsApi.numberingConsistency 等 4 个方法），
 * 但生产代码零调用、注释还谎称「✅ 已接线」—— 用户永远发现不了
 * 「落库正文子标题编号与当前目录编号漂移」（D4 类问题），导出前无自救入口。
 *
 * 后端契约（backend/app/services/numbering.py，实读确认，非臆造）：
 *   - 校验：{ scheme_id, checked, mismatched, consistent,
 *             sections: [{ section_id, consistent, skipped, changed, diffs, error? }] }
 *     diffs 为差异行列表 { tag, old, new }；
 *   - 修复：{ fixed, total, report, snapshot_id }（修复前自动建 numbering_repair 快照）；
 *   - 版本：{ scheme_id, versions: [{ snapshot_id, ... }] }（新→旧）；
 *   - 回滚：{ ...res, scheme_id, snapshot_type }（回滚本身可撤销）。
 */

/** 校验报告单章行（仅声明渲染读取的键） */
interface NumberingSectionRow {
  section_id?: string;
  consistent?: boolean;
  skipped?: boolean;
  changed?: boolean;
  diffs?: Array<{ tag?: string; old?: string; new?: string }>;
  error?: string;
}

/** 校验报告（GET numbering-consistency 响应体） */
interface NumberingReport {
  checked?: number;
  mismatched?: number;
  consistent?: boolean;
  sections?: NumberingSectionRow[];
  error?: string;
}

/** 版本快照行（GET numbering-consistency/versions 响应体 versions[]） */
interface NumberingVersionRow {
  snapshot_id?: string;
  [k: string]: unknown;
}

export function NumberingConsistencyPanel({ schemeId }: { schemeId: string }) {
  const { message: antdMsg, modal } = App.useApp();
  const [checking, setChecking] = useState(false);
  const [repairing, setRepairing] = useState(false);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [report, setReport] = useState<NumberingReport | null>(null);
  const [versions, setVersions] = useState<NumberingVersionRow[]>([]);
  const [versionsOpen, setVersionsOpen] = useState(false);

  /** 检查编号一致性（GET，只读不落库） */
  const handleCheck = async () => {
    if (!schemeId) return;
    setChecking(true);
    try {
      const { data } = await sectionsApi.numberingConsistency(schemeId);
      setReport(data || {});
      if (data?.error) {
        antdMsg.warning(`校验接口返回异常：${data.error}`);
      } else if (data?.mismatched > 0) {
        antdMsg.warning(`发现 ${data.mismatched} 个章节的正文编号与目录漂移，可一键修复`);
      } else {
        // ✅ R181（2026-11-06）：三态呈现 —— 通过 / 不通过 / 无法判定。
        //    skipped=true（编号规范化开关关闭 / 无编号可判定）此前被归入
        //    "校验通过"，把"无法判定"错报成"通过"，会误导用户直接导出。
        const skippedCount = ((data?.sections || []) as NumberingSectionRow[]).filter(
          (s) => s.skipped,
        ).length;
        const checked = data?.checked ?? 0;
        if (skippedCount > 0 || checked === 0) {
          antdMsg.info(
            checked === 0
              ? "无法判定：未检查到可判定的章节（可能编号规范化开关关闭）"
              : `部分章节无法判定：${skippedCount} 章被跳过，仅 ${checked} 章完成一致性判定`,
          );
        } else {
          antdMsg.success(`编号一致性校验通过（共检查 ${checked} 章）`);
        }
      }
    } catch (e: unknown) {
      antdMsg.error((e as { message?: string })?.message || "编号一致性校验失败");
    } finally {
      setChecking(false);
    }
  };

  /** 一键修复漂移（POST repair，写库前自动建快照，可回滚）—— 有确认与结果反馈 */
  const handleRepair = () => {
    if (!schemeId) return;
    modal.confirm({
      title: "确认修复正文编号漂移？",
      content: (
        <div>
          <p>
            将按<b>当前目录编号</b>重写所有漂移章节的正文子标题编号（共 {report?.mismatched ?? "?"}{" "}
            章命中）。
          </p>
          <p>修复前会自动创建快照，可在「版本历史」中一键回滚。</p>
        </div>
      ),
      okText: "开始修复",
      cancelText: "取消",
      onOk: async () => {
        setRepairing(true);
        try {
          const { data } = await sectionsApi.repairNumbering(schemeId);
          const fixed = Number(data?.fixed ?? 0);
          const total = Number(data?.total ?? 0);
          if (data?.report) setReport(data.report);
          if (fixed > 0) {
            antdMsg.success(
              `修复完成：已修复 ${fixed} 处（共检查 ${total} 章）` +
                (data?.snapshot_id ? `，快照 ${String(data.snapshot_id).slice(0, 8)} 可回滚` : ""),
            );
          } else {
            antdMsg.info(`无需修复（共检查 ${total} 章，未发现可修复的编号漂移）`);
          }
        } catch (e: unknown) {
          antdMsg.error((e as { message?: string })?.message || "编号修复失败");
        } finally {
          setRepairing(false);
        }
      },
    });
  };

  /** 版本历史（GET versions） */
  const handleLoadVersions = async () => {
    if (!schemeId) return;
    setLoadingVersions(true);
    setVersionsOpen(true);
    try {
      const { data } = await sectionsApi.numberingVersions(schemeId);
      setVersions(data?.versions || []);
    } catch (e: unknown) {
      antdMsg.error((e as { message?: string })?.message || "版本历史加载失败");
    } finally {
      setLoadingVersions(false);
    }
  };

  /** 回滚到指定快照（POST rollback/{snapshot_id}）—— 有确认与结果反馈 */
  const handleRollback = (snapshotId: string) => {
    if (!schemeId) return;
    modal.confirm({
      title: "确认回滚该编号快照？",
      content: "将恢复该快照涉及章节的修复前正文（回滚本身可撤销）。",
      okText: "确认回滚",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await sectionsApi.rollbackNumbering(schemeId, snapshotId);
          antdMsg.success(
            `已回滚快照 ${snapshotId.slice(0, 8)}（类型：${data?.snapshot_type || "numbering"}），正在刷新校验结果`,
          );
          // 回滚后正文已变，旧校验结果过期：自动重查一次给用户新鲜结论
          void handleCheck();
        } catch (e: unknown) {
          antdMsg.error((e as { message?: string })?.message || "回滚失败");
        }
      },
    });
  };

  const driftSections = (report?.sections || []).filter((s) => !s.consistent && !s.skipped);
  // ✅ R181：无法判定 ≠ 通过 —— 有跳过章节或根本没检查到章节时单独呈现
  const skippedCount = (report?.sections || []).filter((s) => s.skipped).length;
  const checkedCount = report?.checked ?? 0;
  const undetermined =
    !report?.error && !((report?.mismatched ?? 0) > 0) && (skippedCount > 0 || checkedCount === 0);

  return (
    <Card
      size="small"
      title={
        <Space>
          <NumberOutlined />
          <Text strong>编号一致性（目录 vs 正文子标题编号）</Text>
          {report && !report.error && (
            <Tag
              data-testid="numbering-summary-tag"
              color={report.mismatched ? "red" : undetermined ? "gold" : "green"}
            >
              {report.mismatched
                ? `${report.mismatched}/${checkedCount} 章漂移`
                : undetermined
                  ? skippedCount > 0
                    ? `已检查 ${checkedCount} 章，${skippedCount} 章无法判定`
                    : "无法判定：未检查到章节"
                  : `已检查 ${checkedCount} 章，全部一致`}
            </Tag>
          )}
        </Space>
      }
      extra={
        <Space wrap>
          <Button
            size="small"
            type="primary"
            icon={<SafetyOutlined />}
            loading={checking}
            data-testid="numbering-check-btn"
            onClick={handleCheck}
          >
            检查编号一致性
          </Button>
          <Button
            size="small"
            icon={<RollbackOutlined />}
            disabled={!report || driftSections.length === 0}
            loading={repairing}
            data-testid="numbering-repair-btn"
            onClick={handleRepair}
          >
            一键修复漂移
          </Button>
          <Button
            size="small"
            icon={<HistoryOutlined />}
            loading={loadingVersions}
            data-testid="numbering-versions-btn"
            onClick={handleLoadVersions}
          >
            版本历史
          </Button>
        </Space>
      }
      style={{ marginBottom: 12 }}
      data-testid="numbering-consistency-panel"
    >
      <Text type="secondary" style={{ fontSize: 12 }}>
        导出前核对「落库正文的子标题编号」与当前目录是否漂移；发现漂移可一键修复（修复前自动建快照，支持回滚）。
      </Text>

      {!report && (
        <div style={{ marginTop: 8 }} data-testid="numbering-empty">
          <Text type="secondary">尚未检查。点击右上角「检查编号一致性」开始。</Text>
        </div>
      )}

      {/* ✅ R181：无法判定单独提示，绝不以"通过"外观呈现 */}
      {report && undetermined && (
        <div style={{ marginTop: 8 }} data-testid="numbering-undetermined-hint">
          <Text type="warning">
            {skippedCount > 0
              ? `${skippedCount} 章因编号规范化开关关闭或缺少可判定编号被跳过，本次结论为「无法判定」，不代表已通过。`
              : "本次未检查到可判定的章节，结论为「无法判定」，不代表已通过。"}
          </Text>
        </div>
      )}

      {report && !report.error && driftSections.length > 0 && (
        /* ✅ 2026-11-11：旧实现用 Math.random() 作兜底 rowKey —— 每次渲染都是新值，
           antd 会把整张表销毁重建（滚动位置、展开态、焦点全丢）。改为确定性 key，
           并启用与项目既有 5 张表同款的 virtual 滚动。 */
        <Table
          size="small"
          style={{ marginTop: 8 }}
          rowKey={(r, i) => r.section_id || `drift-${r.diffs?.[0]?.old || ""}-${i}`}
          data-testid="numbering-drift-table"
          pagination={false}
          scroll={{ y: 240 }}
          virtual
          dataSource={driftSections}
          locale={{ emptyText: "无漂移章节" }}
          columns={[
            {
              title: "章节 ID",
              dataIndex: "section_id",
              width: 220,
              render: (v: string) => (
                <Text code style={{ fontSize: 11 }}>
                  {(v || "").slice(0, 18)}
                </Text>
              ),
            },
            {
              title: "差异明细（旧 → 新）",
              dataIndex: "diffs",
              render: (diffs: NumberingSectionRow["diffs"]) => (
                <div style={{ fontSize: 12 }}>
                  {(diffs || []).slice(0, 5).map((d, i) => (
                    <div key={`${d.tag || ""}-${i}`}>
                      <Text delete type="secondary">
                        {d.old}
                      </Text>
                      {" → "}
                      <Text type="warning">{d.new}</Text>
                    </div>
                  ))}
                  {(diffs?.length || 0) > 5 && <Text type="secondary">…等 {diffs!.length} 处</Text>}
                  {!diffs?.length && <Text type="secondary">（无逐行差异，可能为校验异常）</Text>}
                </div>
              ),
            },
          ]}
        />
      )}

      {report?.error && (
        <div style={{ marginTop: 8 }} data-testid="numbering-error">
          <Text type="danger">校验失败：{report.error}</Text>
        </div>
      )}

      {versionsOpen && (
        <Table
          size="small"
          style={{ marginTop: 8 }}
          loading={loadingVersions}
          rowKey={(r, i) => r.snapshot_id || `version-${i}`}
          data-testid="numbering-versions-table"
          pagination={false}
          scroll={{ y: 200 }}
          virtual
          dataSource={versions}
          locale={{ emptyText: "暂无编号修复 / 回滚快照" }}
          columns={[
            {
              title: "快照 ID",
              dataIndex: "snapshot_id",
              width: 200,
              render: (v: string) => (
                <Text code style={{ fontSize: 11 }}>
                  {(v || "").slice(0, 18)}
                </Text>
              ),
            },
            {
              title: "操作",
              width: 100,
              render: (_, r) => (
                <Button
                  size="small"
                  type="link"
                  danger
                  data-testid={`numbering-rollback-${r.snapshot_id}`}
                  onClick={() => r.snapshot_id && handleRollback(r.snapshot_id)}
                >
                  回滚
                </Button>
              ),
            },
          ]}
        />
      )}
    </Card>
  );
}

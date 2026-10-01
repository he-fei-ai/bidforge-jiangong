/**
 * 审核预检 · 单条问题「一键自动修复」弹窗（2026-09-30）
 *
 * 交互分两步，与后端 `/autofix/plan` + `/autofix/apply` 一一对应：
 *
 * 1. **定位**（plan，只读、不调 AI、不落库）：展示「问题出在哪一章第几行、
 *    原文是什么」。用户先确认位置，再决定要不要花一次 AI 调用 ——
 *    避免「点了就改，改完才发现找错章」。
 * 2. **修复**（apply）：确认后调 AI 做最小必要改写；结果按章展示
 *    「修复前 / 修复后」对比，失败章显式给出原因并保留原文。
 *    成功后提供**一键回滚**（后端按快照还原）。
 *
 * 按钮可用性完全取自后端 `finding.autofix`（能力表唯一事实源），
 * 前端不按 rule_id 自行猜测 —— 否则会出现「按钮可点、后端拒绝」。
 */
import { useCallback, useState } from "react";
import {
  Alert, App, Button, Descriptions, Divider, Empty, Modal, Space, Spin, Tag,
  Typography,
} from "antd";
import {
  CheckCircleOutlined, CloseCircleOutlined, RollbackOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import { reviewAutoFixApi } from "../../api";
import { hookAntdMessage } from "../../utils/activityCenter";
import type { AutoFixPlanResult, AutoFixResult, PreflightFinding } from "../../types/audit";

const { Text, Paragraph } = Typography;

export interface AutoFixModalProps {
  schemeId: string;
  finding: PreflightFinding | null;
  open: boolean;
  onClose: () => void;
  /** 修复成功后通知宿主页刷新（目录/正文已变，总检结论也已过期） */
  onFixed?: (snapshotId: string) => void;
}

function AutoFixModal({ schemeId, finding, open, onClose, onFixed }: AutoFixModalProps) {
  const { message: _antdMsg } = App.useApp();
  const msg = hookAntdMessage(_antdMsg, "审核预检");
  const [plan, setPlan] = useState<AutoFixPlanResult | null>(null);
  const [result, setResult] = useState<AutoFixResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [rolling, setRolling] = useState(false);

  const reset = useCallback(() => {
    setPlan(null);
    setResult(null);
    setLoading(false);
    setApplying(false);
    setRolling(false);
  }, []);

  const close = useCallback(() => {
    reset();
    onClose();
  }, [reset, onClose]);

  // 步骤 1：定位矛盾位置（只读，不调 AI）
  const locate = useCallback(async () => {
    if (!finding) return;
    setLoading(true);
    setResult(null);
    try {
      const { data } = await reviewAutoFixApi.plan(schemeId, {
        rule_id: finding.rule_id,
        section_id: finding.section_id || undefined,
      });
      setPlan(data as AutoFixPlanResult);
    } catch (e: any) {
      msg.error(`定位失败：${e?.message || e}`);
    } finally {
      setLoading(false);
    }
  }, [finding, schemeId, msg]);

  // 步骤 2：执行修复
  const runFix = useCallback(async () => {
    if (!finding) return;
    setApplying(true);
    try {
      const { data } = await reviewAutoFixApi.apply(schemeId, {
        rule_id: finding.rule_id,
        section_id: finding.section_id || undefined,
      });
      const res = data as AutoFixResult;
      setResult(res);
      if (res.ok) {
        const s = res.stats;
        msg.success(
          `修复完成：${s?.repaired ?? 0} 章已更新` +
          (s?.skipped ? `，${s.skipped} 章因单次上限跳过` : "")
        );
        onFixed?.(res.snapshot_id || "");
      } else if (res.status === "not_located") {
        msg.warning(res.reason || "未能定位到问题位置，未改动正文");
      } else if (res.status === "unsupported") {
        msg.info(res.reason || "该问题不支持自动修复");
      } else {
        msg.warning("修复未通过校验，正文已保留原文");
      }
    } catch (e: any) {
      msg.error(`修复失败：${e?.message || e}`);
    } finally {
      setApplying(false);
    }
  }, [finding, schemeId, msg, onFixed]);

  // 回滚：还原修复前正文
  const rollback = useCallback(async (snapshotId: string) => {
    setRolling(true);
    try {
      await reviewAutoFixApi.rollback(schemeId, { snapshot_id: snapshotId });
      msg.success("已回滚到修复前正文");
      setResult(null);
      onFixed?.("");
    } catch (e: any) {
      msg.error(`回滚失败：${e?.message || e}`);
    } finally {
      setRolling(false);
    }
  }, [schemeId, msg, onFixed]);

  if (!finding) return null;
  const cap = finding.autofix;
  const fixable = cap?.fixable ?? false;
  const isAi = cap?.mode === "ai";

  return (
    <Modal
      open={open}
      onCancel={close}
      width={860}
      destroyOnClose
      title={
        <Space>
          <ThunderboltOutlined style={{ color: "#1677ff" }} />
          <Text strong>自动修复：{finding.title}</Text>
          <Tag color="blue">{finding.rule_id || "—"}</Tag>
          {cap?.mode === "auto" && <Tag color="green">程序化修复（不调用 AI）</Tag>}
          {isAi && <Tag color="orange">AI 定向改写</Tag>}
          {cap?.mode === "manual" && <Tag>需人工处理</Tag>}
        </Space>
      }
      footer={[
        <Button key="close" onClick={close}>关闭</Button>,
        result?.snapshot_id ? (
          <Button
            key="rollback"
            icon={<RollbackOutlined />}
            loading={rolling}
            onClick={() => rollback(result.snapshot_id)}
          >
            回滚本次修复
          </Button>
        ) : (
          <Button
            key="apply"
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={applying}
            disabled={!fixable || !plan?.fixable || loading}
            onClick={runFix}
          >
            {isAi ? "调用 AI 修复此处" : "执行修复"}
          </Button>
        ),
      ]}
    >
      <Descriptions size="small" column={1} bordered style={{ marginBottom: 8 }}>
        <Descriptions.Item label="问题">{finding.detail || "—"}</Descriptions.Item>
        <Descriptions.Item label="整改建议">{finding.suggestion || "—"}</Descriptions.Item>
        {finding.basis && (
          <Descriptions.Item label="行业依据">{finding.basis}</Descriptions.Item>
        )}
      </Descriptions>

      {cap?.mode === "manual" && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message="该问题不支持自动修复"
          description={cap.reason || "请按整改建议人工处理。"}
        />
      )}
      {!cap && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message="该问题未提供自动修复"
          description="请先重新执行「一键总检」以获取最新的问题清单与修复能力标注。"
        />
      )}

      {/* ---------- 步骤 1：定位矛盾位置 ---------- */}
      {!plan && fixable && (
        <div style={{ textAlign: "center", padding: "12px 0" }}>
          {loading ? <Spin /> : (
            <Button type="primary" icon={<ThunderboltOutlined />} onClick={locate}>
              定位矛盾位置
            </Button>
          )}
          <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
            先定位再修复：确认问题出在哪一章第几行之后，才会调用 AI 改写。
          </Paragraph>
        </div>
      )}

      {plan && plan.mode !== "manual" && (
        <>
          {!plan.fixable && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message="未能定位到问题位置"
              description={plan.reason}
            />
          )}
          {plan.fixable && (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 8 }}
                message={`已定位到 ${plan.targets.length} 处矛盾位置` +
                  (plan.max_sections ? `（本次最多修复 ${plan.max_sections} 章）` : "")}
                description="确认无误后点击右下角按钮，AI 将在这些位置做最小必要修改，其余内容保持原样。"
              />
              <div style={{ maxHeight: 260, overflowY: "auto", marginBottom: 8 }}>
                {plan.targets.map((t, i) => (
                  <div
                    key={`${t.section_id}-${t.line}-${i}`}
                    style={{
                      border: "1px solid #f0f0f0", borderRadius: 6,
                      padding: "6px 8px", marginBottom: 6, background: "#fafafa",
                    }}
                  >
                    <Space size={4} wrap style={{ marginBottom: 2 }}>
                      <Tag color="blue" style={{ marginRight: 0 }}>第 {i + 1} 处</Tag>
                      <Text strong style={{ fontSize: 12 }}>
                        {t.section_title || "（未命名章节）"}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 11 }}>第 {t.line} 行</Text>
                    </Space>
                    {t.why && (
                      <Text type="secondary" style={{ fontSize: 11 }}>{t.why}</Text>
                    )}
                    {t.context && (
                      <pre style={{
                        margin: "4px 0 0", fontSize: 11, whiteSpace: "pre-wrap",
                        wordBreak: "break-all", color: "#555",
                      }}>{t.context}</pre>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}

      {/* ---------- 步骤 2：修复结果 ---------- */}
      {result && result.items.length > 0 && (
        <>
          <Divider style={{ margin: "8px 0" }}>修复结果</Divider>
          {result.items.map((it) => (
            <div
              key={it.section_id}
              style={{
                border: `1px solid ${it.status === "repaired" ? "#b7eb8f" : "#ffa39e"}`,
                borderRadius: 6, padding: "6px 8px", marginBottom: 6,
              }}
            >
              <Space size={4} wrap style={{ marginBottom: 4 }}>
                {it.status === "repaired"
                  ? <CheckCircleOutlined style={{ color: "#52c41a" }} />
                  : <CloseCircleOutlined style={{ color: "#ff4d4f" }} />}
                <Text strong style={{ fontSize: 12 }}>
                  {it.section_title || "（未命名章节）"}
                </Text>
                <Tag color={it.status === "repaired" ? "green" : "red"} style={{ marginRight: 0 }}>
                  {it.status === "repaired" ? "已修复" : "未修复（保留原文）"}
                </Tag>
              </Space>
              {it.problems.length > 0 && (
                <ul style={{ margin: "4px 0", paddingLeft: 18, fontSize: 11, color: "#cf1322" }}>
                  {it.problems.map((p, i) => <li key={i}>{p}</li>)}
                </ul>
              )}
              {it.status === "repaired" && (
                <div style={{ fontSize: 11 }}>
                  <Text type="secondary">修复前：</Text>
                  <pre style={{
                    margin: "2px 0", fontSize: 11, whiteSpace: "pre-wrap",
                    wordBreak: "break-all", color: "#999",
                    maxHeight: 120, overflowY: "auto",
                  }}>{it.before}</pre>
                  <Text type="secondary">修复后：</Text>
                  <pre style={{
                    margin: "2px 0 0", fontSize: 11, whiteSpace: "pre-wrap",
                    wordBreak: "break-all", color: "#333",
                    maxHeight: 120, overflowY: "auto",
                  }}>{it.after}</pre>
                </div>
              )}
            </div>
          ))}
        </>
      )}

      {result && result.items.length === 0 && result.reason && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={result.reason} />
      )}

      {result?.ok && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
          message="正文已变更：该章节的审核结论已自动退回「待审核」，请复核后重新送审；导出缓存与总检结论亦已失效。"
        />
      )}
    </Modal>
  );
}

export default AutoFixModal;


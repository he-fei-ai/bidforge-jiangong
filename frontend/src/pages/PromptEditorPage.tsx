import { useEffect, useState, useRef, useMemo, useCallback } from "react";
import { App, Button, Typography, Tabs, List, Spin, Modal, Input, Tag, Space, Tooltip, Alert, Drawer,} from "antd";
import {
  EditOutlined, RollbackOutlined, SaveOutlined, CodeOutlined,
  HistoryOutlined,
} from "@ant-design/icons";

import { promptsApi } from "../api";
import { useAntdMessageHub } from "../utils/activityCenter";

import { PageHero, StatCards, StatItem } from '../utils/ui';
import type { PromptAuditLog, PromptItem } from "../types/prompt";
import { diffPromptVariables, extractPromptVariables } from "../utils/promptVariables";


// ✅ 修复：后端 category 实际值域为 outline/content/charts/analysis/配图/共享规则，
// 旧实现键是中文（目录/正文/...）→ 全部 miss 落入 extra 显示 cyan
const CATEGORY_COLORS: Record<string, string> = {
  outline: "green",
  content: "orange",
  charts: "purple",
  analysis: "blue",
  配图: "geekblue",
  共享规则: "default",
};

const CATEGORY_LABELS: Record<string, string> = {
  outline: "目录",
  content: "正文",
  charts: "图表",
  analysis: "分析",
  配图: "配图",
  共享规则: "共享规则",
};

// ✅ 2026-09-27（前端契约修复）：后端 400/409 的 detail 已经被 axios 响应
//   拦截器解析进 error.message（见 api/index.ts:135 注释），但本页 4 处
//   `catch {}` 把它整个丢掉、只显示「保存失败」这类固定文案。后果是
//   「引用了不存在的共享片段 / 被他人修改 / 内容过长」这三类**用户可以自己
//   解决**的问题，全部退化成一句无信息量的提示。现统一透传。
function promptErrorText(err: unknown, fallback: string): string {
  const raw = (err as { response?: { data?: { detail?: unknown } };
                     message?: string } | null);
  const detail = raw?.response?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail) && detail.length) {
    // FastAPI 422 校验错误：[{loc, msg, type}, ...]
    const first = detail[0] as { msg?: string };
    if (first?.msg) return first.msg;
  }
  if (typeof raw?.message === "string" && raw.message.trim()
      && !/^Request failed with status code \d+$/.test(raw.message.trim())) {
    return raw.message.trim();
  }
  return fallback;
}

// 与后端 routers/prompts.py::PROMPT_MAX_CHARS 保持一致（单一值 200000）。
// 前端提前拦一道，避免用户粘贴几十万字后才收到 400（整包上传白跑）。
const PROMPT_MAX_CHARS = 200000;

export default function PromptEditorPage() {
  const { message: _antdMsg, modal } = App.useApp();
  const msg = useAntdMessageHub(_antdMsg, "提示词管理");
  const [prompts, setPrompts] = useState<PromptItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editContent, setEditContent] = useState("");
  // ✅ G2 版本回滚（2026-09-24）：提示词变更历史 + 回滚
  const [historyKey, setHistoryKey] = useState<string | null>(null);
  const [historyLogs, setHistoryLogs] = useState<PromptAuditLog[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const isMountedRef = useRef(true);
  const promptsRef = useRef(prompts);

  const liveEditVariables = useMemo(() => extractPromptVariables(editContent), [editContent]);
  const currentPrompt = prompts.find((p) => p.key === editingKey);
  const liveVariableDiff = useMemo(
    () => diffPromptVariables(editContent, currentPrompt?.default_content || ""),
    [editContent, currentPrompt?.default_content],
  );

  const categories = useMemo(() => {
    const known = Object.keys(CATEGORY_COLORS);
    const seen = new Set<string>();
    prompts.forEach((p) => { if (p.category) seen.add(p.category); });
    const ordered = known.filter((k) => seen.has(k));
    const extra = [...seen].filter((k) => !known.includes(k));
    return [...ordered, ...extra].map((key) => ({
      key,
      label: CATEGORY_LABELS[key] || key,
      color: CATEGORY_COLORS[key] || "cyan",
    }));
  }, [prompts]);

  useEffect(() => {
    isMountedRef.current = true;
    return () => { isMountedRef.current = false; };
  }, []);

  useEffect(() => {
    promptsRef.current = prompts;
  }, [prompts]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    promptsApi.list()
      .then(({ data }) => {
        if (cancelled) return;
        setPrompts(data.items || []);
      })
      .catch(() => {
        if (cancelled) return;
        msg.error("加载提示词失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const handleSave = async (key: string) => {
    if (editContent.length > PROMPT_MAX_CHARS) {
      msg.error(`内容过长（${editContent.length.toLocaleString()} 字符），上限 ${PROMPT_MAX_CHARS.toLocaleString()} 字符`);
      return;
    }
    try {
      const { data } = await promptsApi.update(key, editContent);
      // ✅ 修复：后端把空内容按"恢复默认"处理并返回 {content, reset}，
      // 旧实现忽略响应体用本地空串覆盖 state → 界面显示空提示词，实际生效的是出厂默认
      const finalContent = data?.content ?? editContent;
      const warnings = data?.warnings || [];
      msg.success(
        data?.reset ? "内容为空，已恢复默认提示词"
          : warnings.length ? `已保存，但有 ${warnings.length} 条提醒`
          : "提示词已更新",
      );
      // ✅ 2026-09-27：把后端回传的 warning 原样展示（保存成功 ≠ 一定正确）。
      //   例：删掉了契约变量、引用了未声明的新变量 —— 这些不会阻断保存，
      //   但若不提示，用户会以为一切正常而模板实际已失效。
      warnings.forEach((w: string) => msg.warning(w));
      setPrompts((prev) =>
        prev.map((p) =>
          p.key === key
            ? {
                ...p,
                content: finalContent,
                modified: finalContent !== p.default_content,
                variables: extractPromptVariables(finalContent).map((v) => v.name),
                content_hash: data.content_hash,
                updated_at: data.reset ? "" : new Date().toISOString(),
                audit_count: (p.audit_count || 0) + 1,
              }
            : p,
        ),
      );
      setEditingKey(null);
    } catch (e) {
      msg.error(promptErrorText(e, "保存失败"));
    }
  };

  const handleReset = async (key: string) => {
    try {
      const { data } = await promptsApi.reset(key);
      msg.success("已重置为默认值");
      setPrompts((prev) =>
        prev.map((p) =>
          p.key === key
            ? {
                ...p,
                content: data.content,
                modified: false,
                variables: extractPromptVariables(data.content || "").map((v) => v.name),
                content_hash: data.content_hash,
                updated_at: "",
                audit_count: (p.audit_count || 0) + 1,
              }
            : p,
        ),
      );
      if (editingKey === key) {
        const latestPrompt = promptsRef.current.find((p) => p.key === key);
        setEditContent(data.content || latestPrompt?.content || "");
      }
    } catch (e) {
      msg.error(promptErrorText(e, "重置失败"));
    }
  };

  /** ✅ G2：打开版本历史抽屉并加载该提示词的变更审计 */
  const openHistory = useCallback(async (key: string) => {
    setHistoryKey(key);
    setHistoryOpen(true);
    setHistoryLoading(true);
    try {
      const { data } = await promptsApi.auditLogs(key, 50, 0);
      if (!isMountedRef.current) return;
      setHistoryLogs(data.items || []);
      setHistoryTotal(data.total || 0);
    } catch (e) {
      if (isMountedRef.current) msg.error(promptErrorText(e, "加载变更历史失败"));
    } finally {
      if (isMountedRef.current) setHistoryLoading(false);
    }
  }, [msg]);

  /** ✅ G2：回滚到某条审计记录「变更前」的版本（后端会再写一条 rollback 审计） */
  const handleRollback = useCallback(async (key: string, auditId: string) => {
    modal.confirm({
      title: "回滚此版本？",
      content: "将把该提示词恢复到此条变更之前的内容，并立即对所有生成任务生效。",
      okText: "回滚",
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await promptsApi.rollback(key, auditId);
          msg.success("已回滚到所选版本");
          setPrompts((prev) =>
            prev.map((p) =>
              p.key === key
                ? {
                    ...p,
                    content: data.content,
                    modified: data.content !== p.default_content,
                    variables: extractPromptVariables(data.content || "").map((v) => v.name),
                    content_hash: data.content_hash,
                    updated_at: new Date().toISOString(),
                    audit_count: (p.audit_count || 0) + 1,
                  }
                : p,
            ),
          );
          if (editingKey === key) setEditContent(data.content || "");
          // 刷新历史列表（回滚本身也写了一条审计）
          await openHistory(key);
        } catch (e) {
          msg.error(promptErrorText(e, "回滚失败"));
        }
      },
    });
  }, [modal, msg, editingKey, openHistory]);

  if (loading) return <Spin size="large" style={{ display: "block", marginTop: 80, textAlign: "center" }} />;

  const categoryPrompts = (cat: string) =>
    prompts.filter((p) => p.category === cat);

  const tabItems = categories.map((cat) => ({
    key: cat.key,
    label: (
      <span>
        <Tag color={cat.color} style={{ marginRight: 4 }}>{cat.label}</Tag>
        ({categoryPrompts(cat.key).length})
      </span>
    ),
    children: (
      <List
        dataSource={categoryPrompts(cat.key)}
        renderItem={(item: PromptItem) => (
          <List.Item
            actions={[
              <Tooltip title="编辑" key="edit">
                <Button
                  type="link"
                  icon={<EditOutlined />}
                  onClick={() => {
                    setEditingKey(item.key);
                    setEditContent(item.content);
                  }}
                />
              </Tooltip>,
              <Tooltip title="版本历史与回滚" key="history">
                <Button
                  type="link"
                  icon={<HistoryOutlined />}
                  onClick={() => openHistory(item.key)}
                />
              </Tooltip>,
              <Tooltip title="重置为默认值" key="reset">
                <Button
                  type="link"
                  danger
                  icon={<RollbackOutlined />}
                  onClick={() => handleReset(item.key)}
                />
              </Tooltip>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space>
                  <CodeOutlined />
                  <Typography.Text code style={{ fontSize: 12 }}>{item.key}</Typography.Text>
                  <Typography.Text strong>{item.label}</Typography.Text>
                  {item.modified && (
                    <Tooltip title="当前内容与出厂默认不同，可点击重置按钮恢复">
                      <Tag color="orange" style={{ marginRight: 0 }}>已修改</Tag>
                    </Tooltip>
                  )}
                </Space>
              }
              description={
                <>
                  <Typography.Paragraph
                    ellipsis={{ rows: 2 }}
                    style={{ margin: 0, marginBottom: 4, fontSize: 12, color: "#888" }}
                  >
                      {item.content.slice(0, 200)}
                  </Typography.Paragraph>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    {item.updated_at
                      ? `最近修改：${item.updated_at.replace("T", " ")} · `
                      : "尚未修改 · "}
                    审计 {item.audit_count} 次
                  </Typography.Text>
                  {Array.isArray(item.variables) && item.variables.length > 0 && (
                    <Space wrap size={4}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        变量（{item.variables.length}）：
                      </Typography.Text>
                      {item.variables.slice(0, 6).map((v: string) => (
                        <Tag key={v} style={{ fontFamily: "monospace", fontSize: 11, marginRight: 0 }}>
                          {v}
                        </Tag>
                      ))}
                      {item.variables.length > 6 && (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          +{item.variables.length - 6}
                        </Typography.Text>
                      )}
                    </Space>
                  )}
                </>
              }
            />
          </List.Item>
        )}
      />
    ),
  }));

  return (
    <div className="scroll-area" style={{ maxWidth: 960, margin: "0 auto", overflowY: "auto", overflowX: "hidden" }}>
      <Typography.Title level={4}>
        <CodeOutlined /> 提示词管理
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        自由编辑各模块调用的 AI 提示词内容。修改后保存将立即生效，无需重启服务。
      </Typography.Paragraph>

      <Tabs items={tabItems} />

      <Modal
        title={
          <Space>
            <CodeOutlined />
            <span>编辑提示词</span>
            {currentPrompt && (
              <>
                <Typography.Text code>{currentPrompt.key}</Typography.Text>
                <Typography.Text type="secondary">— {currentPrompt.label}</Typography.Text>
              </>
            )}
          </Space>
        }
        open={!!editingKey}
        onCancel={() => setEditingKey(null)}
        width={960}
        footer={
          <Space>
            <Button
              danger
              icon={<RollbackOutlined />}
              onClick={() => editingKey && handleReset(editingKey)}
            >
              重置为默认
            </Button>
            <Button onClick={() => setEditingKey(null)}>取消</Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              onClick={() => editingKey && handleSave(editingKey)}
            >
              保存
            </Button>
          </Space>
        }
        styles={{ body: { maxHeight: "65vh", overflow: "auto" } }}
      >
        {liveEditVariables.length > 0 && (
          <Space wrap style={{ display: "flex", marginBottom: 10 }}>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              占位符变量（{liveEditVariables.length}，实时提取）：
            </Typography.Text>
            {liveEditVariables.map((v) => (
              <Tag key={v.name} color="blue" style={{ fontFamily: "monospace" }}>
                {v.dunder ? `__${v.name}__` : `{${v.name}}`}
              </Tag>
            ))}
          </Space>
        )}
        {(liveVariableDiff.added.length > 0 || liveVariableDiff.removed.length > 0) && (
          <Alert
            type={liveVariableDiff.removed.length > 0 ? "warning" : "info"}
            showIcon
            style={{ marginBottom: 10 }}
            message={
              <Space wrap>
                <span>相对默认模板的变量变化：</span>
                {liveVariableDiff.added.map((name) => (
                  <Tag key={`add-${name}`} color="green">+{name}</Tag>
                ))}
                {liveVariableDiff.removed.map((name) => (
                  <Tag key={`del-${name}`} color="red">-{name}</Tag>
                ))}
              </Space>
            }
          />
        )}
        <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
          提示词中可使用 <Tag>{"{变量名}"}</Tag> 格式的占位符，运行时会自动替换为实际内容。
        </Typography.Text>
        {/* ✅ 2026-09-27：超长提前拦截（与后端 PROMPT_MAX_CHARS 同值）。
            旧实现无任何长度提示，用户粘贴几十万字后整包上传才收 400。 */}
        {editContent.length > PROMPT_MAX_CHARS && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 8 }}
            message={`当前 ${editContent.length.toLocaleString()} 字符，超过上限 ${PROMPT_MAX_CHARS.toLocaleString()} 字符，无法保存`}
          />
        )}
        <Input.TextArea
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
          rows={20}
          showCount
          maxLength={PROMPT_MAX_CHARS}
          style={{ fontFamily: "monospace", fontSize: 13 }}
        />
      </Modal>

      {/* ✅ G2 版本回滚（2026-09-24）：变更历史与回滚 */}
      <Drawer
        title={
          <Space>
            <HistoryOutlined />
            <span>版本历史</span>
            {historyKey && <Typography.Text code style={{ fontSize: 12 }}>{historyKey}</Typography.Text>}
          </Space>
        }
        placement="right"
        width={520}
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="「回滚」会把提示词恢复到该条变更之前的内容，并立即对所有生成任务生效。回滚本身也会留下一条可再次回滚的记录。"
        />
        {historyLoading ? (
          <Spin style={{ display: "block", marginTop: 40, textAlign: "center" }} />
        ) : historyLogs.length === 0 ? (
          <Typography.Text type="secondary">该提示词暂无变更记录。</Typography.Text>
        ) : (
          <List
            size="small"
            dataSource={historyLogs}
            renderItem={(log) => (
              <List.Item
                actions={[
                  log.rollbackable ? (
                    <Button
                      key="rb"
                      size="small"
                      icon={<RollbackOutlined />}
                      onClick={() => historyKey && handleRollback(historyKey, log.id)}
                    >
                      回滚
                    </Button>
                  ) : (
                    <Tooltip
                      key="rb-na"
                      // ✅ 2026-09-27：直接展示后端给的不可回滚原因，
                      //   不再自己猜「较早于快照功能上线」（截断/超长也属此类）。
                      title={log.rollback_blocked_reason
                        || "此记录没有可用的变更前正文，无法回滚"}
                    >
                      <Button size="small" disabled icon={<RollbackOutlined />}>
                        回滚
                      </Button>
                    </Tooltip>
                  ),
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space wrap size={4}>
                      <Tag color={log.action === "rollback" ? "purple" : log.action === "reset" ? "red" : "blue"}>
                        {log.action_label || log.action}
                      </Tag>
                      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                        {(log.created_at || "").replace("T", " ").slice(0, 19)}
                      </Typography.Text>
                    </Space>
                  }
                  description={
                    <Space wrap size={4} style={{ fontSize: 11 }}>
                      {(log.changes || []).length === 0 ? (
                        <Typography.Text type="secondary">无结构化变更摘要</Typography.Text>
                      ) : (
                        (log.changes || []).map((c) => (
                          <Tag key={c.field} style={{ fontSize: 11, marginRight: 0 }}>
                            {c.label}: {String(c.before ?? "")} → {String(c.after ?? "")}
                          </Tag>
                        ))
                      )}
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        )}
        {historyTotal > historyLogs.length && (
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            共 {historyTotal} 条记录，当前显示最近 {historyLogs.length} 条。
          </Typography.Text>
        )}
      </Drawer>
    </div>
  );
}

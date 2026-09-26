/**
 * 章节审核工作流面板（PRD §3.12.5）
 *
 * 背景
 * ----
 * ``sections.review_status`` 在库表与模型里都存在，但此前全库无写入方 ——
 * 是事实上的死字段：方案写完了，谁审的、审到哪一章、驳回理由是什么，系统一无所知。
 *
 * 工程软件的可追溯性要求评审过程留痕（**谁、何时、什么意见、什么结论**）。
 * 本面板落地 `待审核 → 审核中 → 已通过 / 已驳回` 状态机：
 * - 逐章审核，可附评审意见；
 * - 多选批量通过 / 驳回（长方案逐章点太慢）；
 * - 完整评审轨迹可回溯；
 * - 方案级提交审核时，后端会校验最近一次预检是否存在阻断项（有硬伤不放行评审）。
 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  App, Alert, Button, Card, Checkbox, Col, Drawer, Empty, Input, Modal, Progress, Row,
  Space, Spin, Table, Tag, Timeline, Tooltip, Typography,
} from "antd";
import {
  CheckCircleOutlined, ClockCircleOutlined, CloseCircleOutlined,
  HistoryOutlined, UserOutlined,
} from "@ant-design/icons";
import { reviewApi } from "../../api";
import { hookAntdMessage } from "../../utils/activityCenter";
import {
  REVIEW_STATUS_COLOR,
  type ReviewChecklistItem, type ReviewRecord, type ReviewStatus, type ReviewSummary,
} from "../../types/audit";

const { Text, Paragraph } = Typography;

const REVIEWER_STORAGE_KEY = "scheme_review_reviewer_name";
// ✅ G8：评审轨迹分页大小（与后端 /records 的 limit 上限对齐，前端一次一页）
const RECORDS_PAGE_SIZE = 50;

const NEXT_ACTIONS: Record<string, Array<{ to: ReviewStatus; label: string }>> = {
  "": [{ to: "approved", label: "通过" }, { to: "rejected", label: "驳回" }],
  pending: [{ to: "reviewing", label: "开始审核" }, { to: "approved", label: "通过" },
    { to: "rejected", label: "驳回" }],
  reviewing: [{ to: "approved", label: "通过" }, { to: "rejected", label: "驳回" }],
  approved: [{ to: "rejected", label: "复审驳回" }],
  rejected: [{ to: "approved", label: "整改后通过" }],
};

// ✅ BUG 修复（2026-09-23）：目标状态的完整文案。旧实现的成功消息是
//    `to === "approved" ? "通过" : "驳回"` 二元判定 —— 把「重置为待审核」
//    （pending）与「开始审核」（reviewing）都提示成「已标记为驳回」，
//    与实际操作相反，误导用户。现按状态取全量文案。
const STATUS_DONE_LABEL: Record<string, string> = {
  pending: "待审核", reviewing: "审核中", approved: "通过", rejected: "驳回",
};

export interface ReviewWorkflowPanelProps {
  schemeId: string;
  /** 外部数据（如正文重新生成）变更后递增，触发刷新 */
  refreshKey?: number;
  /** 章节被修改后通知父组件刷新目录树（review_status 变化） */
  onChanged?: () => void;
}

function ReviewWorkflowPanel({
  schemeId,
  refreshKey = 0,
  onChanged,
}: ReviewWorkflowPanelProps) {
  const { message: _antdMsg, modal } = App.useApp();
  const msg = hookAntdMessage(_antdMsg, "审核流程");
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<ReviewChecklistItem[]>([]);
  const [summary, setSummary] = useState<ReviewSummary | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewer, setReviewer] = useState(
    () => localStorage.getItem(REVIEWER_STORAGE_KEY) || "");
  const [recordsOpen, setRecordsOpen] = useState(false);
  const [records, setRecords] = useState<ReviewRecord[]>([]);
  const [recordsLoading, setRecordsLoading] = useState(false);
  // ✅ G8（2026-09-21）：评审轨迹分页加载。此前一次拉 100 条就结束 ——
  // 章节多、反复送审的方案里评审记录很容易超过 100 条，早期轨迹在 UI 上
  // 永久不可见（工程可追溯性要求完整历史可查）。现按 offset 追加加载。
  const [recordsOffset, setRecordsOffset] = useState(0);
  const [recordsHasMore, setRecordsHasMore] = useState(false);
  const [recordsLoadingMore, setRecordsLoadingMore] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const loadRequestId = useRef(0);
  const recordsRequestId = useRef(0);

  const load = useCallback(async () => {
    if (!schemeId) return;
    const requestId = ++loadRequestId.current;
    setLoading(true);
    // ✅ BUG 修复（2026-09-21）：Promise.all → Promise.allSettled。
    //    旧实现任一接口失败，整块 setItems/setSummary 都不执行 → 用户看到
    //    「审核清单加载失败」但界面完全空白；即使只有 summary 失败，checklist
    //    本已成功返回也不显示。现改为独立兜底，任一成功都渲染、失败单独提示。
    const [clRes, smRes] = await Promise.allSettled([
      reviewApi.checklist(schemeId),
      reviewApi.summary(schemeId),
    ]);
    if (requestId !== loadRequestId.current) return;
    if (clRes.status === "fulfilled") {
      const cl = clRes.value.data as { items?: ReviewChecklistItem[] };
      setItems(cl.items || []);
      // ✅ 修复：items 刷新后，把已删除/被过滤掉的 section_id 从 selected 中移除，
      //    否则跨页 rowSelection 会保留"幽灵 id"，批量提交会命中不存在的章节。
      setSelected((prev) => {
        const valid = new Set((cl.items || []).map((it) => it.id));
        const next = prev.filter((id) => valid.has(id));
        return next.length === prev.length ? prev : next;
      });
    } else {
      msg.error(clRes.reason?.message || "审核清单加载失败");
    }
    if (smRes.status === "fulfilled") {
      setSummary(smRes.value.data || null);
    } else {
      msg.error(smRes.reason?.message || "审核进度加载失败");
    }
    if (requestId === loadRequestId.current) setLoading(false);
  }, [schemeId, msg]);

  // 切换方案时立即清空旧清单/汇总/选择，避免新方案请求完成前展示错误数据。
  useEffect(() => {
    loadRequestId.current += 1;
    recordsRequestId.current += 1;
    setItems([]);
    setSummary(null);
    setSelected([]);
    setRecords([]);
    setRecordsOffset(0);
    setRecordsHasMore(false);
  }, [schemeId]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const setReview = useCallback(
    async (sectionId: string, to: ReviewStatus, comment = "", title = "") => {
      // ✅ BUG 修复（2026-09-21）：评审人必填校验。
      //    后端接受空 reviewer，落库会出现「空评审人」的评审记录——工程软件
      //    的评审轨迹必须能追溯到人。前端在发出请求前拦截，用户明确拒绝留痕。
      if (!reviewer || !reviewer.trim()) {
        msg.warning("请先填写评审人姓名，评审记录需可追溯");
        return;
      }
      try {
        await reviewApi.reviewSection(schemeId, sectionId, {
          to_status: to,
          reviewer,
          comment,
        });
        msg.success(`「${title || "章节"}」已标记为「${STATUS_DONE_LABEL[to] ?? to}」`);
        await load();
        onChanged?.();
      } catch (e: any) {
        msg.error(e?.message || "审核操作失败");
      }
    },
    [schemeId, reviewer, msg, load, onChanged]
  );

  const askAndReview = useCallback(
    (row: ReviewChecklistItem, to: ReviewStatus, label: string) => {
      // ✅ BUG 修复（2026-09-21）：评审人未填时提前拦截，避免走完弹窗再报错。
      if (!reviewer || !reviewer.trim()) {
        msg.warning("请先填写评审人姓名，评审记录需可追溯");
        return;
      }
      const required = to === "rejected"; // 驳回理由必填
      // ✅ BUG 修复（2026-09-21）：弹窗意见输入框改从 React ref 读取。
      //    旧实现用 `document.getElementById("__review_comment__")`——多个弹窗
      //    并存（连点两次「驳回」/「通过」/「批量」）时会命中**第一个**元素，
      //    把上一弹窗的意见当成当前弹窗的提交，甚至命中已被销毁的节点。
      //    现用 useRef 绑到 modal.confirm 的 content，闭包捕获局部引用，天然隔离。
      const commentRef = { current: "" };
      // 主拦截用「受控的 okButtonProps.disabled」：未填理由时按钮禁用，
      // 正常操作下 onOk 根本不会触发。之所以不靠 onOk 抛 reject 来保持弹窗：
      // antd 非 await（命令式）模式下，confirm.onOk 的 rejected 会被其内部
      // 再 Promise.reject 一次（ant-design/ant-design#6183），调用方未 await
      // 该派生 Promise → 落成 unhandled rejection（生产控制台/测试都会报）。
      let inst: { update?: (cfg: Record<string, unknown>) => void } | undefined;
      inst = modal.confirm({
        title: `确认${label}：${row.title}`,
        width: 520,
        content: (
          <div style={{ marginTop: 8 }}>
            <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
              评审人：{reviewer}　—　评审意见将留痕，可在「评审轨迹」中回溯。
            </Paragraph>
            <Input.TextArea
              rows={3}
              placeholder={required ? "请填写驳回理由（必填，便于编制人整改）" : "评审意见（可选）"}
              onChange={(e) => {
                commentRef.current = e.target.value;
                if (!required) return;
                inst?.update?.({ okButtonProps: { disabled: !commentRef.current.trim() } });
              }}
            />
          </div>
        ),
        okText: label,
        okButtonProps: required ? { disabled: true } : undefined,
        cancelText: "取消",
        onOk: async () => {
          // ✅ 从 ref 读取，跨弹窗不冲突
          const comment = commentRef.current.trim();
          if (required && !comment) {
            // 兜底（按钮已禁用，正常路径不会走到）：保持弹窗打开不提交
            msg.warning("驳回时必须填写驳回理由");
            return Promise.reject(new Error("缺少驳回理由"));
          }
          await setReview(row.id, to, comment, row.title);
        },
      });
    },
    [modal, reviewer, msg, setReview]
  );

  const batchReview = useCallback(
    (to: ReviewStatus, label: string) => {
      if (selected.length === 0) {
        msg.warning("请先勾选章节");
        return;
      }
      // ✅ BUG 修复（2026-09-21）：批量操作同样前置评审人校验。
      if (!reviewer || !reviewer.trim()) {
        msg.warning("请先填写评审人姓名，评审记录需可追溯");
        return;
      }
      // ✅ BUG 修复（2026-09-21）：与 askAndReview 同理，改用 ref 隔离。
      const commentRef = { current: "" };
      modal.confirm({
        title: `批量${label} ${selected.length} 个章节？`,
        content: (
          <Input.TextArea
            rows={3}
            style={{ marginTop: 8 }}
            placeholder="统一评审意见（可选）"
            onChange={(e) => { commentRef.current = e.target.value; }}
          />
        ),
        okText: label,
        cancelText: "取消",
        onOk: async () => {
          try {
            const { data } = await reviewApi.batch(schemeId, {
              section_ids: selected,
              to_status: to,
              reviewer,
              comment: commentRef.current.trim(),
            });
            // ✅ BUG 修复（2026-09-23）：旧实现只取 data.changed，后端返回的
            //    skipped（状态机拦截）/ not_found（章节不存在）/ nochange（已是
            //    目标态）/ truncated（超 500 截断）全部静默吞掉 —— 部分失败时
            //    用户以为全部生效。现把明细拼进反馈，有偏差时降级为 warning。
            const changed: number = data.changed ?? 0;
            const skippedN: number = (data.skipped ?? []).length;
            const notFoundN: number = (data.not_found ?? []).length;
            const nochangeN: number = (data.nochange ?? []).length;
            const parts: string[] = [];
            if (skippedN) parts.push(`${skippedN} 个被状态机拦截`);
            if (notFoundN) parts.push(`${notFoundN} 个章节不存在`);
            if (nochangeN) parts.push(`${nochangeN} 个已是目标状态`);
            if (data.truncated) {
              const dropped = Math.max(0, (data.total_ids ?? 0) - changed - skippedN - notFoundN - nochangeN);
              parts.push(dropped > 0 ? `超出单次上限未处理 ${dropped} 个` : "超出单次上限部分未处理");
            }
            const detail = parts.length ? `，${parts.join("、")}` : "";
            if (changed > 0 && parts.length) {
              msg.warning(`已${label} ${changed} 个章节${detail}`);
            } else if (changed > 0) {
              msg.success(`已${label} ${changed} 个章节${detail}`);
            } else {
              msg.warning(`没有章节发生状态变更${parts.length ? `（${parts.join("、")}）` : ""}`);
            }
            setSelected([]);
            await load();
            onChanged?.();
          } catch (e: any) {
            msg.error(e?.message || "批量审核失败");
          }
        },
      });
    },
    [selected, schemeId, reviewer, modal, msg, load, onChanged]
  );

  // ✅ G8：按 offset 加载一页评审轨迹（首页替换、后续页追加）
  const loadRecordsPage = useCallback(async (offset: number) => {
    const requestId = ++recordsRequestId.current;
    try {
      const { data } = await reviewApi.records(schemeId, "", RECORDS_PAGE_SIZE, offset);
      if (requestId !== recordsRequestId.current) return;
      setRecords((prev) =>
        offset === 0 ? data.items || [] : [...prev, ...(data.items || [])]);
      setRecordsHasMore(!!data.has_more);
    } catch (e: any) {
      if (requestId === recordsRequestId.current) msg.error(e?.message || "评审轨迹加载失败");
    }
  }, [schemeId, msg]);

  const openRecords = useCallback(async () => {
    setRecordsOpen(true);
    setRecords([]);
    setRecordsOffset(0);
    setRecordsHasMore(false);
    setRecordsLoading(true);
    try {
      await loadRecordsPage(0);
      setRecordsOffset(RECORDS_PAGE_SIZE);
    } finally {
      setRecordsLoading(false);
    }
  }, [loadRecordsPage]);

  const loadMoreRecords = useCallback(async () => {
    if (recordsLoadingMore || !recordsHasMore) return;
    setRecordsLoadingMore(true);
    try {
      await loadRecordsPage(recordsOffset);
      setRecordsOffset((o) => o + RECORDS_PAGE_SIZE);
    } finally {
      setRecordsLoadingMore(false);
    }
  }, [recordsLoadingMore, recordsHasMore, recordsOffset, loadRecordsPage]);

  const submitScheme = useCallback(
    (to: ReviewStatus, label: string) => {
      // ✅ BUG 修复（2026-09-21）：方案级提交同样要求评审人必填。
      if (!reviewer || !reviewer.trim()) {
        msg.warning("请先填写评审人姓名，评审记录需可追溯");
        return;
      }
      // ✅ BUG 修复（2026-09-21）：前置提示——后端在 /submit 会校验：
      //   1) 未执行过总检 → 422
      //   2) 最近一次总检存在交付阻断项 → 422
      // 前端无法拿到"是否有预检记录"这一后端状态（不能新增端点调用），
      // 但可以用**已有数据**（items/summary）判断：只要有章节未审核完成，
      // 说明流程走得不彻底。这里在弹窗里给出明确警告，让用户知情而非被动 422。
      // 注：真正的"是否有阻断项"判断仍需依赖后端，本组件只提示已知的可推断情况。
      const incompleteCount = items.filter(
        (it) => it.review_status !== "approved"
      ).length;
      const allPending = items.length > 0 &&
        items.every((it) => !it.review_status || it.review_status === "pending");
      // ✅ 复用 ref 隔离；多个弹窗并存时不会互相命中
      const commentRef = { current: "" };
      // ✅ G6：审核完整性开关（默认开启，仅提交「通过」时生效）
      const requireAllRef = { current: to === "approved" };
      modal.confirm({
        title: `确认将方案整体标记为「${label}」？`,
        content: (
          <div style={{ marginTop: 8 }}>
            <Paragraph type="secondary" style={{ fontSize: 12 }}>
              后端会校验最近一次预检结论：存在交付阻断项或未执行总检时，提交将被拒绝。
            </Paragraph>
            {to === "approved" && incompleteCount > 0 && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 8 }}
                message={
                  allPending
                    ? "尚无任何章节完成审核"
                    : `仍有 ${incompleteCount} 个章节未通过审核`
                }
                description="建议先完成逐章评审，再提交方案级通过，以便评审轨迹完整。"
              />
            )}
            <Input.TextArea
              rows={3}
              placeholder="评审意见（可选）"
              onChange={(e) => { commentRef.current = e.target.value; }}
            />
            {to === "approved" && (
              // ✅ G6（2026-09-21）：require_all_sections_reviewed 此前后端字段
              // 存在但前端从不传，"必须所有章节过审才能提交"这一开关形同虚设。
              // 提交「通过」时默认勾选（更严格的审核闭环语义）；用户可手动取消，
              // 后端即放行但会在返回体里附上未审章节明细。
              <Checkbox
                defaultChecked
                style={{ marginTop: 8 }}
                onChange={(e) => { requireAllRef.current = e.target.checked; }}
              >
                要求所有章节已通过审核后才允许提交（推荐）
              </Checkbox>
            )}
          </div>
        ),
        okText: label,
        cancelText: "取消",
        onOk: async () => {
          setSubmitting(true);
          try {
            await reviewApi.submit(schemeId, {
              to_status: to,
              reviewer,
              comment: commentRef.current.trim(),
              // ✅ G6：开启时后端会硬校验「所有章节已纳入审核」，不满足则 422
              require_all_sections_reviewed: requireAllRef.current,
            });
            msg.success(`方案已标记为「${label}」`);
            await load();
            onChanged?.();
          } catch (e: any) {
            msg.error(e?.message || "提交失败");
          } finally {
            setSubmitting(false);
          }
        },
      });
    },
    [schemeId, reviewer, items, modal, msg, load, onChanged]
  );

  const columns = useMemo(
    () => [
      {
        title: "章节", dataIndex: "title", ellipsis: true,
        render: (v: string, r: ReviewChecklistItem) => (
          <Tooltip title={r.last_comment ? `最近意见：${r.last_comment}` : undefined}>
            <span style={{ paddingLeft: Math.max(0, (r.level - 1) * 12) }}>{v}</span>
          </Tooltip>
        ),
      },
      {
        title: "字数", dataIndex: "word_count", width: 80,
        render: (v: number) => (v || 0).toLocaleString(),
      },
      {
        title: "审核状态", dataIndex: "review_status", width: 100,
        render: (s: ReviewStatus, r: ReviewChecklistItem) => (
          <Tag color={REVIEW_STATUS_COLOR[s] ?? "default"} style={{ marginRight: 0 }}>
            {r.review_status_label || "未纳入审核"}
          </Tag>
        ),
      },
      {
        title: "最近评审", dataIndex: "last_reviewer", width: 150, ellipsis: true,
        render: (v: string, r: ReviewChecklistItem) =>
          v ? (
            <Tooltip title={r.last_comment || "无意见"}>
              <Space size={4}>
                <UserOutlined style={{ color: "#bfbfbf" }} />
                <Text style={{ fontSize: 12 }}>{v}</Text>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {r.last_reviewed_at?.replace("T", " ").slice(5, 16)}
                </Text>
              </Space>
            </Tooltip>
          ) : "—",
      },
      {
        title: "操作", key: "op", width: 190,
        render: (_: unknown, r: ReviewChecklistItem) => (
          <Space size={2} wrap>
            {(NEXT_ACTIONS[r.review_status ?? ""] || []).map((a) => (
              <Button
                key={a.to}
                size="small"
                type={a.to === "approved" ? "primary" : "default"}
                danger={a.to === "rejected"}
                onClick={() => askAndReview(r, a.to, a.label)}
              >
                {a.label}
              </Button>
            ))}
            {r.review_status && (
              <Button
                size="small"
                type="link"
                onClick={() => setReview(r.id, "pending", "重置为待审核", r.title)}
              >
                重置
              </Button>
            )}
          </Space>
        ),
      },
    ],
    [askAndReview, setReview]
  );

  return (
    <Card
      size="small"
      title={
        <Space>
          <CheckCircleOutlined style={{ color: "#52c41a" }} />
          <Text strong>章节审核工作流</Text>
          {summary && (
            <Tag color={summary.reviewed_all ? "green" : "blue"}>
              {summary.approved_sections}/{summary.total_sections} 已通过
            </Tag>
          )}
        </Space>
      }
      extra={
        <Space size={4} wrap>
          <Button size="small" icon={<HistoryOutlined />} onClick={openRecords}>
            评审轨迹
          </Button>
          <Button
            size="small"
            onClick={() => submitScheme("approved", "方案审核通过")}
            loading={submitting}
          >
            提交审核通过
          </Button>
          <Button size="small" onClick={load} icon={<ClockCircleOutlined />}>
            刷新
          </Button>
        </Space>
      }
      style={{ marginBottom: 12 }}
    >
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        <Space wrap>
          <Text type="secondary" style={{ fontSize: 12 }}>评审人：</Text>
          <Input
            size="small"
            style={{ width: 160 }}
            value={reviewer}
            placeholder="填写评审人姓名"
            onChange={(e) => {
              setReviewer(e.target.value);
              localStorage.setItem(REVIEWER_STORAGE_KEY, e.target.value);
            }}
          />
          <Text type="secondary" style={{ fontSize: 11 }}>
            评审人将随每次审核留痕
          </Text>
        </Space>

        {summary && summary.total_sections > 0 && (
          <Row gutter={[12, 8]} align="middle">
            <Col span={10}>
              <Progress
                percent={summary.progress}
                size="small"
                status={summary.reviewed_all ? "success" : "active"}
              />
            </Col>
            <Col span={14}>
              <Space size={4} wrap>
                {/* ✅ 2026-09-17：方案级审核状态（独立列 review_status，与编译状态解耦） */}
                {summary.review_status && (
                  <Tag color={REVIEW_STATUS_COLOR[summary.review_status as ReviewStatus] ?? "default"}>
                    方案{summary.review_status_label || "未纳入审核"}
                  </Tag>
                )}
                {Object.entries(summary.counts).map(([k, v]) => (
                  <Tag key={k} color={REVIEW_STATUS_COLOR[k as ReviewStatus] ?? "default"}>
                    {summary.labels?.[k] || "未纳入审核"} {v}
                  </Tag>
                ))}
              </Space>
            </Col>
          </Row>
        )}

        {selected.length > 0 && (
          <Alert
            type="info"
            showIcon
            message={
              <Space wrap>
                <Text style={{ fontSize: 12 }}>已选 {selected.length} 个章节</Text>
                <Button size="small" type="primary" onClick={() => batchReview("approved", "通过")}>
                  批量通过
                </Button>
                <Button size="small" danger onClick={() => batchReview("rejected", "驳回")}>
                  批量驳回
                </Button>
                <Button size="small" type="link" onClick={() => setSelected([])}>
                  取消选择
                </Button>
              </Space>
            }
          />
        )}

        {loading ? (
          <div style={{ textAlign: "center", padding: 24 }}><Spin /></div>
        ) : items.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span style={{ fontSize: 12, color: "#999" }}>方案暂无章节</span>}
          />
        ) : (
          <Table<ReviewChecklistItem>
            size="small"
            rowKey="id"
            pagination={items.length > 15 ? { pageSize: 15, size: "small" } : false}
            // ✅ 性能优化：虚拟滚动（同 ReadinessDashboard：固定 360px 表体，
            // 避免视口外行全量建 DOM）
            scroll={{ y: 360 }}
            virtual
            dataSource={items}
            columns={columns}
            rowSelection={{
              selectedRowKeys: selected,
              // ✅ BUG 修复（2026-09-21）：pagination + scroll.y 下翻页会重置
              //    selection，跨页勾选丢失。加 preserveSelectedRowKeys 让选中
              //    集合跨页保留（批量操作的核心体验）。
              preserveSelectedRowKeys: true,
              onChange: (keys) => setSelected(keys as string[]),
            }}
          />
        )}
      </Space>

      <Drawer
        title="评审轨迹"
        width={560}
        open={recordsOpen}
        onClose={() => setRecordsOpen(false)}
      >
        {recordsLoading ? (
          <Spin />
        ) : records.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无评审记录" />
        ) : (
          <>
          <Timeline
            items={records.map((r) => ({
              color: r.to_status === "approved" ? "green"
                : r.to_status === "rejected" ? "red" : "blue",
              children: (
                <div style={{ fontSize: 12 }}>
                  <Space size={4} wrap>
                    {r.section_title
                      ? <Text strong>{r.section_title}</Text>
                      : <Text strong>方案整体</Text>}
                    <Text type="secondary">{r.from_label}</Text>
                    <Text type="secondary">→</Text>
                    <Tag
                      color={REVIEW_STATUS_COLOR[r.to_status as ReviewStatus] ?? "default"}
                      style={{ marginRight: 0 }}
                    >
                      {r.to_label}
                    </Tag>
                  </Space>
                  <div style={{ color: "#999", marginTop: 2 }}>
                    {r.reviewer ? `${r.reviewer} · ` : ""}
                    {r.created_at?.replace("T", " ").slice(0, 19)}
                  </div>
                  {r.comment && (
                    <div style={{ marginTop: 2 }}>意见：{r.comment}</div>
                  )}
                </div>
              ),
            }))}
          />
          {/* ✅ G8：还有更多历史时提供「加载更多」，避免超过一页的早期轨迹不可见 */}
          <div style={{ textAlign: "center", margin: "12px 0" }}>
            <Button
              size="small"
              loading={recordsLoadingMore}
              disabled={!recordsHasMore}
              onClick={loadMoreRecords}
            >
              {recordsHasMore
                ? `已显示 ${records.length} 条，加载更多`
                : `共 ${records.length} 条，已全部显示`}
            </Button>
          </div>
          </>
        )}
      </Drawer>
    </Card>
  );
}

// ✅ 性能优化：默认导出用 memo 包裹。props 为 {schemeId, refreshKey, onChanged}，
//    其中 schemeId/refreshKey 为原始值、onChanged 复用宿主页已稳定的
//    useCallback(load) —— 三者引用均恒定，SSE progress 每帧变化时本组件
//    （650 行、含章节评审清单表格）可完整跳过渲染。
export default memo(ReviewWorkflowPanel);

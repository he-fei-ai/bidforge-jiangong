import { formatOutlineTitle } from "./outlineNumbering.ts";
import { type TreeNode } from "./outlineTreeModel.ts";
import { InputNumber, Space, Tag } from "antd";
import { memo, useEffect, useState } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

export const WORD_BUDGET_MIN = 50;
export const WORD_BUDGET_MAX = 50000;

/**
 * 选中章节的「字数预算」编辑面板（模块级导出，行为由 outlineTab.test 钉住）。
 *
 * ✅ 功能缺口补齐（2026-09-21）：字数预算此前在整条前端链路中**只有读取、没有写入** ——
 *   · 目录树行内只显示只读的 `(L1 · 1500字)`；
 *   · 正文页只显示 `x字 / y字`，预算超了只能点「字数压缩」；
 *   · 压缩器「只缩不扩」，正文偏少时没有任何入口调低预算。
 * 用户无法在正文生成**前**按需规划每章篇幅，只能等生成完再补救。
 * 现把预算编辑收敛到「目录生成」Tab 的选中章节面板：失焦 / 回车即回抛，
 * 落库由父组件统一处理（临时节点 local_/upload_ 仅改本地，等「保存目录」）。
 *
 * 校验口径（防脏数据写库）：非正数 → 回退原值不提交；越界 → 收敛到
 * [WORD_BUDGET_MIN, WORD_BUDGET_MAX]；与原值相同 → 不触发回调（避免无谓写库）。
 */
export const OutlineNodeBudgetPanel = memo(function OutlineNodeBudgetPanel({
  node,
  onBudgetChange,
}: {
  node: TreeNode;
  onBudgetChange: (key: string, budget: number) => void;
}) {
  const current = node.word_budget || 1200;
  const [draft, setDraft] = useState<number>(current);
  // 选中章节切换 / 预算被外部改动（如 load 刷新）时同步草稿
  useEffect(() => {
    setDraft(current);
  }, [node.key, current]);

  const commit = () => {
    const n = Math.round(Number(draft));
    // 空值 / 非数字 / 非正数：回退原值，不静默提交脏数据
    if (!Number.isFinite(n) || n <= 0) {
      setDraft(current);
      return;
    }
    const clamped = Math.min(WORD_BUDGET_MAX, Math.max(WORD_BUDGET_MIN, n));
    setDraft(clamped);
    if (clamped !== current) onBudgetChange(node.key, clamped);
  };

  const overBudget = (node.word_count || 0) > current;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        padding: "6px 10px",
        marginBottom: 8,
        background: "#fafafa",
        border: "1px solid #f0f0f0",
        borderRadius: 4,
      }}
    >
      <Text strong style={{ fontSize: 12 }}>
        📏 字数预算
      </Text>
      <Text ellipsis style={{ fontSize: 12, fontWeight: 600, maxWidth: 240 }}>
        {formatOutlineTitle(node.outlineId || node.key, node.level, node.title)}
      </Text>
      <Text type="secondary" style={{ fontSize: 11 }}>
        L{node.level}
      </Text>
      {/* addonAfter 已废弃，改用 Space.Compact + Space.Addon（antd v5.29+） */}
      <Space.Compact size="small" style={{ width: 116 }}>
        <InputNumber
          // ⚠️ 不设 min/max：antd 会在 blur 时自行把越界值收敛到 min/max 并触发
          // 一轮额外的 onChange —— 与下方 commit 的受控夹取互相打架（测试里出现
          // 「输入 10 → 期望 50 却 0 次回调 / 输入 0 → 被夹成 50」的竞态）。
          // 夹取口径统一收口在 commit（[WORD_BUDGET_MIN, WORD_BUDGET_MAX]），
          // 秒回弹、幂等、可单测。
          step={100}
          value={draft}
          onChange={(v) => setDraft(v as number)}
          onBlur={commit}
          onPressEnter={commit}
          style={{ width: "100%" }}
          aria-label="字数预算"
        />
        <Space.Addon>字</Space.Addon>
      </Space.Compact>
      <Text type="secondary" style={{ fontSize: 11 }}>
        已写 {(node.word_count || 0).toLocaleString()} 字
      </Text>
      {overBudget ? (
        <Tag color="warning" style={{ margin: 0, fontSize: 11 }}>
          超预算，可在正文页压缩
        </Tag>
      ) : (
        <Tag style={{ margin: 0, fontSize: 11 }}>生成正文时按此目标控制篇幅</Tag>
      )}
    </div>
  );
});

/**
 * 后端「生成后自动校验」报告（`review.quality`，由 services/outline_quality 产出）
 * → 面向用户的一句话提示（纯函数，可单测）。
 *
 * 三类信号（对应需求要求三 / 要求四）：
 * - `name_coverage.missing`：方案名称里解析出的施工内容/工序/工艺/对象，
 *   在目录中**无任何落点**（全面性缺口，后端已尝试外科式补齐）；
 * - `continuity.ok === false`：层级跳级 / 有父无子 / 同名章节 / 编号错位 /
 *   破折号长尾标题 / 空标题节点 / 占位泛化标题（七类，与后端标签表同步）；
 * - `redundant_titles`：与方案名称零关联、且不属于通用必要章节的标题候选。
 *
 * ⚠️ 刻意**不自动删除**任何章节：删章是不可逆的用户数据变更，
 * 只提示 + 定位，由用户决定（与后端 `find_redundant_titles` 的口径一致）。
 */

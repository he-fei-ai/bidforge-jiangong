/**
 * 全局事实分组列表（G2 · 2026-11-04 从 SchemeWorkbenchPage 原样抽取，零行为变更）。
 *
 * 纯展示：props 进、回调出，不读页面级 state。页面仍以
 * `export { FactsGroupList } from "./schemeWorkbench/FactsGroupList"` 保持对外导出名，
 * 既有消费方（页面 + 多个组件级测试）无需改动。
 */
import { memo, useMemo } from "react";
import { Button, Collapse, Space, Tag, Tooltip, Typography } from "antd";
import { CheckCircleOutlined, DeleteOutlined, EditOutlined } from "@ant-design/icons";
import MarkdownRenderer from "../../components/MarkdownRenderer";
import type { FactGroupLike, FactItemLike } from "../../types/facts";
import {
  applyFactsFilter,
  factAttrTitle,
  factChapterTitle,
  factSourceKindTitle,
  factValueKindLabel,
} from "./factsShared";

const { Text } = Typography;

export const FactsGroupList = memo(function FactsGroupList({
  groups,
  filter,
  onEditGroup,
  onDeleteGroup,
  onEditItem,
  onResolveItem,
  onResolveConflict,
  onAckStaleOne,
  // ✅ 2026-10-06：三套枚举的可选注入（来自 /global-facts/category-map）。
  //    设为可选以兼容既有渲染点与测试替身；缺省时各标题函数用本地兜底表。
  chapterTitles,
  factAttrTitles,
  sourceKindTitles,
}: {
  // ✅ R89（2026-10-17）：接入真实契约（此前 groups / 各回调全是 any）。
  //   本组件是 facts 交互覆盖最充分的组件（既有 13+ 例），却对「后端字段改名」
  //   完全失明 —— BUG-1（by_chapter 形状错位）正属此类。
  //   ⚠️ 刻意**不**把 items/回调参数收到 FactItem：既有测试夹具是**局部对象**
  //   （只列被断言的字段），强行收紧会让 20+ 处夹具与断言一起改写 ——
  //   「判据过严 → 后人改松」比判据缺失更糟（§5.14）。形状正确性改由
  //   `types/facts.ts ↔ 后端真实响应` 的跨语言 parity 护栏担守（pytest 侧）。
  groups: FactGroupLike[];
  filter: string;
  onEditGroup: (group: FactGroupLike) => void;
  onDeleteGroup: (group: FactGroupLike) => void;
  onEditItem: (group: FactGroupLike, item: FactItemLike) => void;
  onResolveItem: (item: FactItemLike) => void;
  onResolveConflict: (item: FactItemLike, value: string) => void;
  onAckStaleOne?: (itemId: string, title?: string) => void;
  chapterTitles?: Record<string, string>;
  factAttrTitles?: Record<string, string>;
  sourceKindTitles?: Record<string, string>;
}) {
  // ✅ 性能优化：按输入记忆化过滤结果（原实现每次渲染无条件重算）
  const visible = useMemo(() => applyFactsFilter(groups, filter), [groups, filter]);
  return (
    <Collapse
      items={visible.map((g) => {
        const allItems: FactItemLike[] = g.items || [];
        const items: FactItemLike[] = g.filteredItems || allItems;
        // ✅ 性能优化：单次遍历同时统计三项计数（原实现 3 次独立 filter，
        // 事实条目数百条时每组重复扫描 3 遍）
        let simCount = 0;
        let conflictCount = 0;
        let unresolvedCount = 0;
        for (const it of allItems) {
          if (it.is_simulated) simCount += 1;
          if (it.has_conflict) conflictCount += 1;
          if (!it.is_resolved) unresolvedCount += 1;
        }

        return {
          key: g.id || g.title,
          label: (
            <Space>
              <span>{g.title}</span>
              <Tag>{allItems.length} 项</Tag>
              {simCount > 0 && <Tag color="orange">⚠️{simCount}</Tag>}
              {conflictCount > 0 && <Tag color="red">🔸{conflictCount}</Tag>}
              {unresolvedCount > 0 && <Tag color="blue">📝{unresolvedCount}</Tag>}
            </Space>
          ),
          // ✅ 断链修复：分组级「编辑」（旧前端 openFactEdit 为死代码，
          //    分组重建弹窗/接口能力不可见）与「删除」（factsApi.delete 无调用点）
          extra: (
            <Space size={4} onClick={(e) => e.stopPropagation()}>
              <Button
                size="small"
                type="link"
                icon={<EditOutlined />}
                onClick={() => onEditGroup(g)}
              >
                编辑分组
              </Button>
              <Button
                size="small"
                type="link"
                danger
                icon={<DeleteOutlined />}
                onClick={() => onDeleteGroup(g)}
              >
                删除
              </Button>
            </Space>
          ),
          children: (
            <div>
              {/* 增强版：展示结构化 items（无结构化条目时回退整段 Markdown） */}
              {items.length > 0 && items.some((it) => it.name || it.value) ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {items.map((it, idx: number) => (
                    // ✅ 性能优化：稳定 key。旧实现用数组下标，切换筛选条件
                    //（全部/模拟值/矛盾/待审核）导致条目顺序变化时整块 DOM 被销毁
                    // 重建（展开态、输入焦点全部丢失）
                    <div
                      key={it.id || it.fact_id || `${g.id || g.title}#${idx}`}
                      style={{
                        padding: "8px 12px",
                        borderRadius: 6,
                        border: it.has_conflict
                          ? "1px solid #ff4d4f"
                          : it.is_simulated
                            ? "1px solid #faad14"
                            : it.is_resolved
                              ? "1px solid #d9d9d9"
                              : "1px solid #91caff",
                        background: it.is_simulated
                          ? "#fffbe6"
                          : it.has_conflict
                            ? "#fff2f0"
                            : it.is_resolved
                              ? "#fafafa"
                              : "#e6f4ff",
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          {/* 事实名称 + 值 */}
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 6,
                              flexWrap: "wrap",
                            }}
                          >
                            <Text strong style={{ color: it.has_conflict ? "#ff4d4f" : undefined }}>
                              {it.name || `事实 ${idx + 1}`}
                            </Text>
                            <span style={{ color: "#666" }}>:</span>
                            <Text
                              style={{
                                color: it.is_simulated
                                  ? "#fa8c16"
                                  : it.has_conflict
                                    ? "#ff4d4f"
                                    : "#1677ff",
                                fontWeight: 500,
                              }}
                            >
                              {it.value}
                            </Text>
                          </div>

                          {/* 标记行：章节 / 事实属性 / 模拟值 / 矛盾 / 置信度 / 来源 */}
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                              marginTop: 4,
                              flexWrap: "wrap",
                            }}
                          >
                            {/* ✅ 2026-09-24：九大章节徽标（建办质〔2018〕31号）。
                                chapter 由后端在提取/读路径惰性派生；历史行也能展示。 */}
                            {it.scope === "project" && (
                              <Tooltip title="项目共享事实：当前项目下各方案均会读取，编辑或删除会影响其它方案">
                                <Tag color="cyan" style={{ margin: 0 }}>
                                  项目共享
                                </Tag>
                              </Tooltip>
                            )}
                            {factChapterTitle(it.chapter, chapterTitles) && (
                              <Tooltip
                                title={`九大章节归属：${factChapterTitle(it.chapter, chapterTitles)}`}
                              >
                                <Tag color="geekblue" style={{ margin: 0 }}>
                                  📖 {factChapterTitle(it.chapter, chapterTitles)}
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_shared && (
                              <Tooltip title="跨章节共性事实：多个章节都需要引用，避免重复提取">
                                <Tag color="purple" style={{ margin: 0 }}>
                                  🔗 共性事实
                                </Tag>
                              </Tooltip>
                            )}
                            {factAttrTitle(it.fact_attr, factAttrTitles) && (
                              <span style={{ fontSize: 11, color: "#888" }}>
                                {factAttrTitle(it.fact_attr, factAttrTitles)}
                              </span>
                            )}
                            {/* ✅ 2026-10-06 新增展示：数据来源类型。
                                此前后端逐条下发 source_kind、统计里也有
                                by_source_kind，但界面零展示 —— 用户无法分辨
                                一条事实来自项目资料 / 图纸 / 现场勘察 / 总体方案 /
                                人工手册，而这正是判断「资料原文 vs AI 编造」的关键依据。 */}
                            {factSourceKindTitle(it.source_kind, sourceKindTitles) && (
                              <Tooltip
                                title={`数据来源：${factSourceKindTitle(it.source_kind, sourceKindTitles)}`}
                              >
                                <Tag color="default" style={{ margin: 0, fontSize: 11 }}>
                                  🗂 {factSourceKindTitle(it.source_kind, sourceKindTitles)}
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_stale && (
                              <Tooltip title="来源资料已重新解析或删除，本条旧事实已停止注入正文与导出；点击可确认「该值仍有效」并解除标记">
                                <Tag
                                  color="volcano"
                                  style={{ margin: 0, cursor: "pointer" }}
                                  onClick={() =>
                                    onAckStaleOne?.(
                                      (it.fact_id || it.id) as string,
                                      it.name || (it as { title?: string }).title,
                                    )
                                  }
                                >
                                  来源已变化 ✓
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_simulated && (
                              <Tooltip title="AI 模拟生成，需人工确认">
                                <Tag color="orange" style={{ margin: 0 }}>
                                  ⚠️ 模拟值
                                </Tag>
                              </Tooltip>
                            )}
                            {/* ✅ R80（2026-10-15）：要求口径事实 —— 该值是招标/规范
                                要求原文，正文引用时后端会提示 AI 按「满足/响应要求」
                                口径书写。空值（未区分/本方案口径）不渲染徽标。 */}
                            {it.value_kind === "requirement" && (
                              <Tooltip title="该条目是招标/规范要求原文口径：正文引用时将写为「满足/响应招标要求」，不会当作本方案既定值">
                                <Tag color="geekblue" style={{ margin: 0 }}>
                                  {/* ✅ R81：徽标文案走单一导出（与测试同源） */}
                                  📋 {factValueKindLabel(it.value_kind)}
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_safety_critical && (
                              <Tooltip title="安全关键参数：禁止使用模拟值，正文引用前必须核对原始资料">
                                <Tag color="volcano" style={{ margin: 0 }}>
                                  🛡 安全关键
                                </Tag>
                              </Tooltip>
                            )}
                            {it.has_conflict && (
                              <Tooltip
                                title={
                                  <div>
                                    <div>
                                      <strong>矛盾选项：</strong>
                                    </div>
                                    {(it.conflict_values || []).map((cv, ci: number) => (
                                      // ✅ R174 F6：key 由 index 改为「值+来源」内容组合键，
                                      //    候选值增删/裁决后重排不再发生邻行 DOM 复用错位
                                      <div key={`${String(cv.value)}::${String(cv.source)}::${ci}`}>
                                        {cv.value as string} ({cv.source}, 置信度{" "}
                                        {(cv.confidence! * 100).toFixed(0)}%)
                                      </div>
                                    ))}
                                  </div>
                                }
                              >
                                <Tag color="red" style={{ margin: 0 }}>
                                  🔸 存在矛盾
                                </Tag>
                              </Tooltip>
                            )}
                            {!it.is_resolved && (
                              <Tag color="blue" style={{ margin: 0 }}>
                                📝 待审核
                              </Tag>
                            )}
                            {/* 置信度小条 */}
                            <Tooltip title={`置信度 ${(it.confidence! * 100).toFixed(0)}%`}>
                              <div
                                style={{
                                  width: 50,
                                  height: 6,
                                  borderRadius: 3,
                                  background:
                                    it.confidence! >= 0.9
                                      ? "#52c41a"
                                      : it.confidence! >= 0.7
                                        ? "#1677ff"
                                        : it.confidence! >= 0.4
                                          ? "#fa8c16"
                                          : "#ff4d4f",
                                  opacity: 0.8,
                                }}
                              />
                            </Tooltip>
                            {/* 来源 */}
                            {it.source && (
                              <span style={{ fontSize: 11, color: "#999" }}>
                                📎 {it.source}
                                {(it as { source_ref?: string }).source_ref &&
                                  ` · ${(it as { source_ref?: string }).source_ref!.slice(0, 30)}`}
                              </span>
                            )}
                          </div>

                          {/* 矛盾候选值列表 + 一键选值 */}
                          {it.has_conflict && (it.conflict_values?.length ?? 0) > 0 && (
                            <div
                              style={{
                                marginTop: 4,
                                padding: "4px 8px",
                                background: "#fff7e6",
                                borderRadius: 4,
                              }}
                            >
                              <Text type="secondary" style={{ fontSize: 12 }}>
                                候选值（点击「选此值」裁决）：
                              </Text>
                              <div
                                style={{
                                  display: "flex",
                                  flexDirection: "column",
                                  gap: 4,
                                  marginTop: 4,
                                }}
                              >
                                {/* ✅ 缺口修复：候选值列表只含「其它取值」，事实自身
                                    的当前值不在其中。这里补「保留当前值」入口，
                                    走同一裁决接口。 */}
                                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                  <Tag color="green" style={{ margin: 0 }}>
                                    {it.value}（当前）
                                  </Tag>
                                  <Button
                                    size="small"
                                    type="link"
                                    style={{ padding: 0, height: "auto" }}
                                    onClick={() => onResolveConflict(it, it.value!)}
                                  >
                                    保留当前值
                                  </Button>
                                </div>
                                {it.conflict_values!.map((cv, ci: number) => (
                                  // ✅ R174 F6：同上，内容组合键防止裁决后 DOM 复用错位
                                  <div
                                    key={`${String(cv.value)}::${String(cv.source)}::${ci}`}
                                    style={{ display: "flex", alignItems: "center", gap: 8 }}
                                  >
                                    <Tag color="orange" style={{ margin: 0 }}>
                                      {cv.value as string}
                                    </Tag>
                                    <span style={{ fontSize: 11, color: "#999" }}>
                                      来自 {cv.source}
                                    </span>
                                    <Button
                                      size="small"
                                      type="link"
                                      style={{ padding: 0, height: "auto" }}
                                      onClick={() => onResolveConflict(it, cv.value as string)}
                                    >
                                      选此值
                                    </Button>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>

                        {/* 操作按钮 */}
                        <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                          {/* 单条编辑：走 item_updates，不整组重建 */}
                          {it.fact_id && (
                            <Button
                              size="small"
                              type="link"
                              icon={<EditOutlined />}
                              onClick={() => onEditItem(g, it)}
                            >
                              编辑
                            </Button>
                          )}
                          {!it.is_resolved && (
                            <Button
                              size="small"
                              type="link"
                              icon={<CheckCircleOutlined />}
                              onClick={() => onResolveItem(it)}
                            >
                              确认
                            </Button>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <MarkdownRenderer content={g.content || ""} />
              )}
            </div>
          ),
        };
      })}
    />
  );
});

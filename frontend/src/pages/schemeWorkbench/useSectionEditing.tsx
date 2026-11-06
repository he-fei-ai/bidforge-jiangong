import { useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { Modal, Checkbox, Descriptions, Typography, Button } from "antd";
import type { ModalFuncProps } from "antd";
import { sectionsApi, schemesApi } from "../../api";
import { STANDARD_LABELS, normalizeStandard } from "../../utils/contentStandard";
import type { GenerationStandardChoice } from "../../utils/contentStandard";
import { sectionSaveFenceNotice } from "../../utils/diagnosticsText";
import { findSectionById, updateNodeFields, type TreeNode } from "./outlineTreeModel.ts";
import type { SchemeRecord, WorkbenchSection } from "./workbenchTypes";

const { Text } = Typography;

/**
 * ✅ 2026-11-04 拆分（G7 姊妹组）：正文编辑动作自 SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：
 *   - 预估/保存/标准切换/重写/压缩/重置/启动确认，函数体逐字搬运；
 *   - `shrinking` state 随之迁入并以同名返回，页面引用零改动；
 *   - `handleGenerateContent`（红线生成链）仍留在页面，经 deps 传入；
 *   - 字数预算解析（resolveWordBudgetTier/Override）仍留在页面（生成链共用），
 *     经 deps 传入。
 */
export interface SectionEditingDeps {
  id: string | undefined;
  msg: {
    success: (s: string, d?: number) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    info: (s: string) => void;
  };
  modal: {
    confirm: (cfg: ModalFuncProps) => void;
    info: (cfg: ModalFuncProps) => void;
  };
  selectedSection: WorkbenchSection | null;
  setSelectedSection: Dispatch<SetStateAction<WorkbenchSection | null>>;
  tree: TreeNode[];
  setTree: Dispatch<SetStateAction<TreeNode[]>>;
  generating: boolean;
  scheme: SchemeRecord | null;
  setScheme: Dispatch<SetStateAction<SchemeRecord | null>>;
  generationStandard: GenerationStandardChoice;
  concurrencyOption: string;
  wordBudgetOption: string;
  customWordBudget: number;
  forceFullRepair: boolean;
  setForceFullRepair: (v: boolean) => void;
  draftKey: string | null;
  setSavingSection: (v: boolean) => void;
  setIsEditingSection: (v: boolean) => void;
  setEditDirty: (v: boolean) => void;
  setReviewTick: Dispatch<SetStateAction<number>>;
  setSavingStandardDefault: (v: boolean) => void;
  load: () => Promise<unknown>;
  resolveWordBudgetTier: () => string | undefined;
  resolveWordBudgetOverride: () => number | undefined;
  wordBudgetTierValues: Record<string, number>;
  handleGenerateContent: (extra?: {
    section_id?: string;
    mode?: string;
    force_rewrite?: boolean;
    task_standard?: "precise" | "fuzzy";
    override_section_standard?: boolean;
  }) => Promise<void>;
}

export function useSectionEditing(deps: SectionEditingDeps) {
  const {
    id,
    msg,
    modal,
    selectedSection,
    setSelectedSection,
    tree,
    setTree,
    generating,
    scheme,
    setScheme,
    generationStandard,
    concurrencyOption,
    wordBudgetOption,
    customWordBudget,
    forceFullRepair,
    setForceFullRepair,
    draftKey,
    setSavingSection,
    setIsEditingSection,
    setEditDirty,
    setReviewTick,
    setSavingStandardDefault,
    load,
    resolveWordBudgetTier,
    resolveWordBudgetOverride,
    wordBudgetTierValues,
    handleGenerateContent,
  } = deps;

  /** 收集目标章节的叶子节点信息（用于预估弹窗） */
  function collectLeavesInfo(
    scope: "all" | "missing" | "section",
    sectionId?: string,
    forceRewrite = false,
  ): {
    leaves: TreeNode[];
    totalBudget: number;
    willOverwrite: number;
    missingCount: number;
  } {
    // 先截取子树
    let nodes = tree;
    if (scope === "section" && sectionId) {
      const sec = findSectionById(tree, sectionId);
      if (!sec) return { leaves: [], totalBudget: 0, willOverwrite: 0, missingCount: 0 };
      nodes = [sec];
    }
    // DFS 取所有叶子
    const leaves: TreeNode[] = [];
    const dfs = (arr: TreeNode[]) => {
      arr.forEach((n) => {
        if (!n.children || n.children.length === 0) leaves.push(n);
        else dfs(n.children);
      });
    };
    dfs(nodes);

    const wbOverride = resolveWordBudgetOverride();
    // ✅ R66：档位模式下每个叶子按档位字数预估（与后端 tier 分支同口径）
    const wbTierValue = resolveWordBudgetTier();
    const wbTier = wbTierValue ? wordBudgetTierValues[wbTierValue] : 0;
    let totalBudget = 0;
    let willOverwrite = 0;
    let missingCount = 0;

    let filtered = leaves;
    // ✅ 与后端 select_target_leaves 对齐：统一以"是否已有非空正文"为判据。
    //    旧实现只看 status==='generated'，导致 reviewed/expanded 等已有正文的
    //    章节被前端算作"将生成"，预估数量/字数与实际生成范围不符。
    const hasContent = (l: TreeNode) => !!(l.content || "").trim() && l.word_count > 0;
    if (scope === "missing") {
      filtered = leaves.filter(
        (l) => ["empty", "failed", "pending"].includes(l.status) && !hasContent(l),
      );
    } else if (!forceRewrite) {
      filtered = leaves.filter((l) => !hasContent(l));
    }
    // forceRewrite=true 时不过滤

    filtered.forEach((l) => {
      const budget = wbTier || wbOverride || l.word_budget || 1200;
      totalBudget += budget;
      if (hasContent(l)) willOverwrite++;
      missingCount++;
    });
    return { leaves: filtered, totalBudget, willOverwrite, missingCount };
  }

  /** 保存章节手动编辑内容（content 由 SectionContentCard 的自持草稿上抛） */
  async function saveSectionEdit(content: string) {
    if (!selectedSection || !id) return;
    setSavingSection(true);
    try {
      const { data } = await sectionsApi.update(id, selectedSection.key!, {
        content: content,
        status: content.trim() ? "generated" : "empty",
      });
      msg.success(`已保存，当前 ${data.word_count || 0} 字`);
      // ✅ R181（N-06）：围栏自动补齐必须让用户看见（尤其「疑似吞入正文」时）。
      const fenceNotice = sectionSaveFenceNotice(data);
      if (fenceNotice) msg.warning(fenceNotice);
      setIsEditingSection(false);
      setEditDirty(false);
      // ✅ G10（2026-09-21）：正文已变 → 审核结论随之失效。
      // 旧实现 reviewTick 只在切 Tab 时递增：用户在正文生成 Tab 改完正文，
      // 不切走再回来，审核工作台的「已通过」徽标与就绪度总分都还是旧的。
      setReviewTick((t) => t + 1);
      if (data.review_reset) {
        // ✅ G9：后端把该章节从 approved/reviewing/rejected 退回了「待审核」
        msg.warning("正文已变更，该章节审核结论已失效，需重新送审");
      }
      // 保存成功后清除草稿
      if (draftKey) {
        try {
          localStorage.removeItem(draftKey);
        } catch {
          /* ignore */
        }
      }
      // ✅ 优化：局部更新当前章节而非全量 load()，避免重置目录展开/选中状态
      const updatedSection = {
        ...selectedSection,
        content: content,
        word_count: data.word_count ?? 0,
        status: content.trim() ? "generated" : "empty",
      };
      setSelectedSection(updatedSection);
      // 同步更新 tree 中对应节点
      const updateTreeNode = (nodes: TreeNode[]): TreeNode[] =>
        nodes.map((n) =>
          n.key === selectedSection.key
            ? {
                ...n,
                content: content,
                word_count: data.word_count ?? 0,
                status: content.trim() ? "generated" : "empty",
              }
            : { ...n, children: n.children ? updateTreeNode(n.children) : undefined },
        );
      setTree((prev) => updateTreeNode(prev));
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "保存失败");
    } finally {
      setSavingSection(false);
    }
  }

  /**
   * ✅ F-CONTENT-STANDARD(2026-09-26 · F3）：章节级生成标准变更 → PATCH 落库。
   *
   * 语义：传 `""` = 清除章节覆盖、回落方案默认（后端 `SectionUpdate` 接受空串，
   * 路由内归一为「沿用」）。失败时**不更新本地 state** —— Select 是受控的，
   * 保持旧值即视觉回滚，无需额外 revert（避免出现"界面显示已改、库里没改"）。
   *
   * 该字段不触发审核重置（后端未把 generation_standard 计入正文变更判定），
   * 因此**不**递增 reviewTick。
   */
  async function handleSectionStandardChange(v: string) {
    if (!selectedSection || !id) return;
    if (generating) {
      msg.warning("生成进行中，请等待本轮结束后再修改生成标准");
      return;
    }
    const prev = selectedSection.generation_standard || "";
    if (prev === v) return;
    const sectionKey = selectedSection.key!;
    try {
      await sectionsApi.update(id, sectionKey, { generation_standard: v });
      const patchNode = (nodes: TreeNode[]): TreeNode[] =>
        nodes.map((n) =>
          n.key === sectionKey
            ? { ...n, generation_standard: v }
            : { ...n, children: n.children ? patchNode(n.children) : undefined },
        );
      setTree((prevTree) => patchNode(prevTree));
      setSelectedSection((s) => (s && s.key === sectionKey ? { ...s, generation_standard: v } : s));
      msg.success(
        v
          ? `本章生成标准已设为「${STANDARD_LABELS[v as "precise" | "fuzzy"]}」`
          : "本章已改为沿用方案默认",
      );
    } catch (e: unknown) {
      // 受控 Select 未改 state → 自动回滚到旧值
      const err = e as { message?: string };
      msg.error(err.message || "生成标准保存失败");
    }
  }

  /**
   * ✅ F-CONTENT-STANDARD(2026-09-26 · F5）：把当前任务级选择存为**方案默认**。
   * 仅 precise/fuzzy 可存（「按章节设置」不是一个方案级取值）。
   */
  async function handleSaveSchemeDefaultStandard() {
    if (!id || !scheme) return;
    const std = generationStandard === "inherit" ? "" : generationStandard;
    if (!std) {
      msg.info("「按章节设置」不能存为方案默认，请先选择精准内容或模糊内容");
      return;
    }
    setSavingStandardDefault(true);
    try {
      const { data } = await schemesApi.update(scheme.project_id!, id, {
        generation_standard: std,
      });
      setScheme((s) => (s ? { ...s, generation_standard: data?.generation_standard || std } : s));
      msg.success(`已存为方案默认：${STANDARD_LABELS[std as "precise" | "fuzzy"]}`);
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "保存方案默认失败");
    } finally {
      setSavingStandardDefault(false);
    }
  }

  /** 重写当前章节（带二次确认） */
  function regenerateCurrentSection() {
    if (!selectedSection) return;
    const hasContent = selectedSection.word_count! > 0 || selectedSection.content;
    modal.confirm({
      title: "⚠️ 确认重写本章？",
      content: (
        <div>
          将重新生成章节 <b>「{selectedSection.title}」</b> 的全部叶子子章节，
          {hasContent
            ? "已有的正文内容将被<b style={{ color: '#ff4d4f' }}>覆盖</b>。"
            : "章节目前为空。"}
        </div>
      ),
      okText: "确认重写",
      cancelText: "取消",
      onOk: () => {
        // ✅ BUG 修复：这里必须显式 force_rewrite。
        //    后端 select_target_leaves 对"已有非空正文"的章节默认跳过（智能跳过），
        //    故不带 force_rewrite 的 mode="section" 会一章都不生成 ——
        //    用户点了「确认重写」「覆盖」却毫无反应。
        handleGenerateContent({
          section_id: selectedSection.key,
          mode: "section",
          force_rewrite: true,
        });
      },
    });
  }

  /** 字数压缩当前章节（对齐 OpenBidKit 的 shrink 修复器：AI 只输出局部
   *  replace/delete 操作，表格/图表/代码块/图片与技术参数受程序级保护） */
  const [shrinking, setShrinking] = useState(false);
  function handleShrinkCurrentSection() {
    if (!id || !selectedSection) return;
    const wc = selectedSection.word_count || 0;
    const budget = selectedSection.word_budget || 1200;
    if (wc <= budget) {
      msg.info(`当前字数 ${wc} 未超出目标 ${budget}，无需压缩`);
      return;
    }
    modal.confirm({
      title: "确认压缩本章？",
      content: (
        <div>
          「{selectedSection.title}」当前 <b>{wc}</b> 字，目标 <b>{budget}</b> 字。 AI
          将删除重复、空泛内容进行压缩（最多 3 轮）；
          <b>技术参数、表格、图表、代码块与图片受保护不会被修改</b>。
        </div>
      ),
      okText: "开始压缩",
      cancelText: "取消",
      onOk: async () => {
        setShrinking(true);
        try {
          const { data } = await sectionsApi.shrink(id, selectedSection.key!);
          // 就地更新选中章节与树（避免整页 load 闪烁）
          setSelectedSection((prev) =>
            prev && prev.key === selectedSection.key
              ? {
                  ...prev,
                  content: data.content,
                  word_count: data.word_count,
                  word_status: data.word_status,
                }
              : prev,
          );
          setTree((prev) =>
            updateNodeFields(prev, selectedSection.key!, {
              content: data.content,
              word_count: data.word_count,
              word_status: data.word_status,
            }),
          );
          msg.success(
            `压缩完成：${data.before} → ${data.word_count} 字（${data.rounds_used} 轮，${data.stop_reason}）`,
          );
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "压缩失败");
        } finally {
          setShrinking(false);
        }
      },
    });
  }

  /** 重置正文：一键清空目录树中所有章节已生成的正文（目录结构保留，不可恢复） */
  function handleResetContent() {
    if (!id) return;
    modal.confirm({
      title: "确认重置正文？",
      content: (
        <div>
          将清空本方案目录树中<b style={{ color: "#ff4d4f" }}>所有章节已生成的正文</b>
          （含随正文生成的内联图表与章节审核状态），目录结构与字数预算保留。
          <div style={{ marginTop: 6 }}>
            该操作<b style={{ color: "#ff4d4f" }}>不可恢复</b>，重置后可重新生成。
          </div>
        </div>
      ),
      okText: "确认重置",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await sectionsApi.resetContent(id);
          await load();
          msg.success(`已重置：清除 ${data.cleared ?? 0} 个章节的正文`);
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "重置失败");
        }
      },
    });
  }

  /** 启动生成前的预估确认（统一入口，默认智能跳过已生成章节） */
  function confirmAndGenerate(scope: "all" | "missing" | "section", sectionId?: string) {
    const smartInfo = collectLeavesInfo(scope, sectionId, false);
    if (smartInfo.leaves.length === 0) {
      if (scope === "missing") {
        modal.info({
          title: "无需补全",
          content: (
            <div>
              范围内所有章节都已有正文内容。
              <div style={{ marginTop: 4 }}>
                如果需要重新生成已有的章节，请使用「🔄 重写本章」或
                在预估弹窗中勾选「覆盖已生成章节」。
              </div>
            </div>
          ),
        });
      } else {
        msg.info("没有找到可生成的章节");
      }
      return;
    }

    const concurrencyMap: Record<string, { label: string; eta: string }> = {
      slow: { label: "精细模式（并发 2）", eta: "约 2-3 分钟 / 10 章" },
      balanced: { label: "平衡模式（并发 3）", eta: "约 1-2 分钟 / 10 章" },
      fast: { label: "快速模式（并发 5）", eta: "约 30-60 秒 / 10 章" },
    };
    const cm = concurrencyMap[concurrencyOption];

    const totalAll = collectLeavesInfo(scope, sectionId, true).leaves.length;
    const smartSkipped = totalAll - smartInfo.leaves.length;

    const buildContent = (forceRewrite: boolean) => {
      const info = collectLeavesInfo(scope, sectionId, forceRewrite);
      return (
        <div style={{ lineHeight: 1.8 }}>
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label="生成范围">
              {scope === "all"
                ? "本方案全部叶子章节"
                : scope === "missing"
                  ? "仅未生成 / 失败章节"
                  : `章节「${selectedSection?.title}」子树`}
            </Descriptions.Item>
            <Descriptions.Item label="本次将生成">
              <Text strong>{info.leaves.length}</Text> 个叶子章节
              {smartSkipped > 0 && !forceRewrite && scope !== "missing" && (
                <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                  （已智能跳过 {smartSkipped} 个已生成章节 ✅）
                </Text>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="预估总字数">
              <Text strong>{info.totalBudget.toLocaleString()}</Text> 字
            </Descriptions.Item>
            {info.willOverwrite > 0 && (
              <Descriptions.Item label="⚠️ 风险提示">
                <Text type="danger">将覆盖 {info.willOverwrite} 章已有正文</Text>
              </Descriptions.Item>
            )}
            <Descriptions.Item label="并发 / 模式">{cm.label}</Descriptions.Item>
            {/* ✅ F-CONTENT-STANDARD(2026-09-26 · F6)：弹窗必须显示本次实际生效的
                生成标准。缺这一行，用户点「开始生成」时无法确认要的是精准还是模糊 ——
                而这正是两种模式的唯一区别。 */}
            <Descriptions.Item label="生成标准">
              {generationStandard === "precise"
                ? "精准内容（数值与全局事实严格一致，缺失数据标注待补充）"
                : generationStandard === "fuzzy"
                  ? "模糊内容（以全局事实为基础，允许概括归纳，不得与事实冲突）"
                  : `按章节设置（章节级 → 方案默认「${STANDARD_LABELS[normalizeStandard(scheme?.generation_standard) || "precise"]}」）`}
            </Descriptions.Item>
            <Descriptions.Item label="预计耗时">{cm.eta}</Descriptions.Item>
          </Descriptions>
          <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
            💡 生成过程中可随时「暂停」/「恢复」/「停止」，失败章节可单独重试
          </div>
          {/* ✅ 强制全量重修：非受控（弹窗内容是快照，受控 checked 不会回显） */}
          <div style={{ marginTop: 8 }}>
            <Checkbox
              defaultChecked={forceFullRepair}
              onChange={(e) => setForceFullRepair(e.target.checked)}
            >
              强制全量重修（不跳过已修复且正文未变的冲突）
            </Checkbox>
            <div style={{ marginTop: 4, fontSize: 12, color: "#999" }}>
              💡 默认关闭：收尾的一致性修复会跳过「已修复且修复成果仍在正文里」的冲突以节省 AI
              调用；勾选后本次对所有命中的冲突重新调用 AI 修复（费用随冲突章节数增加）。
            </div>
          </div>
        </div>
      );
    };

    // 先弹默认（智能跳过）预估
    modal.confirm({
      title: "🚀 正文生成预估",
      width: 540,
      content: buildContent(false),
      okText: "开始生成（智能跳过）",
      okButtonProps: { type: "primary" },
      cancelText: "取消",
      onOk: () => {
        handleGenerateContent({
          section_id: scope === "section" ? sectionId : undefined,
          mode: scope === "section" ? "section" : scope,
          force_rewrite: false,
        });
      },
      footer: (_, { OkBtn, CancelBtn }) => (
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <Button
            danger
            onClick={() => {
              Modal.destroyAll();
              // 弹 force rewrite 确认
              modal.confirm({
                title: "⚠️ 强制覆盖所有章节",
                width: 540,
                content: buildContent(true),
                okText: "确认覆盖并重写",
                okButtonProps: { danger: true, type: "primary" },
                cancelText: "返回智能跳过",
                onOk: () => {
                  handleGenerateContent({
                    section_id: scope === "section" ? sectionId : undefined,
                    mode: scope === "section" ? "section" : scope,
                    force_rewrite: true,
                  });
                },
              });
            }}
          >
            强制覆盖（重新生成所有）
          </Button>
          <div>
            <CancelBtn />
            <OkBtn />
          </div>
        </div>
      ),
    });
  }

  return {
    saveSectionEdit,
    handleSectionStandardChange,
    handleSaveSchemeDefaultStandard,
    regenerateCurrentSection,
    handleShrinkCurrentSection,
    handleResetContent,
    confirmAndGenerate,
    shrinking,
  };
}

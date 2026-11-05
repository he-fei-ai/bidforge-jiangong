import { useCallback, useMemo } from "react";
import type { Dispatch, Key, SetStateAction } from "react";
import type { TreeDataNode } from "antd";
import { Tag, Tooltip } from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  FileAddOutlined,
  LoadingOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import type { ExportRerunPlan } from "./useExportPresets";
import { formatOutlineTitle } from "./outlineNumbering.ts";
import {
  collectAllKeys,
  findFirstContentDescendant,
  findSectionById,
  type TreeNode,
} from "./outlineTreeModel.ts";
import type { WorkbenchSection } from "./workbenchTypes";

/**
 * ✅ 2026-11-04 拆分（G6 续）：目录「展示树」与树交互回调自 SchemeWorkbenchPage
 * 原位搬迁。纯结构重构，零行为变更：
 *   - convertToAntTree / treeData 的 useCallback / useMemo 形态与依赖原样保留；
 *   - 树选择/展开/占位符跳转/受影响重跑/全展开切换/前往目录，函数体逐字搬运；
 *   - 返回值同名，页面 JSX 引用零改动。
 */
export interface OutlineViewTreeDeps {
  id: string | undefined;
  msg: {
    success: (s: string) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    info: (s: string) => void;
  };
  tree: TreeNode[];
  failedOutlineChapters: string[];
  expandedKeys: Key[];
  setExpandedKeys: Dispatch<SetStateAction<Key[]>>;
  setSelectedSection: Dispatch<SetStateAction<WorkbenchSection | null>>;
  setActiveTab: (v: string) => void;
  findParentKey: (nodes: TreeNode[], key: string) => string | null;
  generating: boolean;
  rerunPlan: ExportRerunPlan | null;
  setRerunning: (v: boolean) => void;
  handleGenerateContent: (extra?: {
    section_id?: string;
    mode?: string;
    force_rewrite?: boolean;
    task_standard?: "precise" | "fuzzy";
    override_section_standard?: boolean;
  }) => Promise<void>;
  handleExportCheck: () => Promise<void>;
}

export function useOutlineViewTree(deps: OutlineViewTreeDeps) {
  const {
    id,
    msg,
    tree,
    failedOutlineChapters,
    expandedKeys,
    setExpandedKeys,
    setSelectedSection,
    setActiveTab,
    findParentKey,
    generating,
    rerunPlan,
    setRerunning,
    handleGenerateContent,
    handleExportCheck,
  } = deps;

  // ✅ 性能优化：与 convertToEditTree 同款 useCallback 化，并修正下方 treeData 的
  //    陈旧闭包问题 —— 旧 deps 只有 [tree]，但闭包内读了 failedOutlineChapters，
  //    导致「失败章高亮」在只改 failedOutlineChapters 时不刷新（当前被「setTree 与
  //    setFailedOutlineChapters 总同批次提交」掩盖，新增单点改动路径就会暴露）。
  const convertToAntTree = useCallback(
    (node: TreeNode, depth = 0): TreeDataNode => {
      // ✅ 2026-11-05 目录占位章节：status=placeholder 表示按规则只登记
      //    「另行补充内容」，不生成正文；与「生成失败 / 未生成」严格区分。
      const isPlaceholder = node.status === "placeholder";
      const isGenerated = !isPlaceholder && (node.status === "generated" || node.word_count > 0);
      const isGenerating = node.status === "running" || node.status === "pending";
      const isFailed = node.status === "failed";
      // ✅ 统一左右两树状态：目录生成失败（长方案分步生成）在左侧树同样橙色高亮
      const isOutlineFailed = failedOutlineChapters.some(
        (t) => node.title.includes(t) || t.includes(node.title),
      );

      // 状态样式
      const statusIcon = isPlaceholder ? (
        <FileAddOutlined style={{ color: "#722ed1" }} />
      ) : isGenerated ? (
        <CheckCircleOutlined style={{ color: "#52c41a" }} />
      ) : isFailed ? (
        <CloseCircleOutlined style={{ color: "#ff4d4f" }} />
      ) : isOutlineFailed ? (
        <WarningOutlined style={{ color: "#fa8c16" }} />
      ) : isGenerating ? (
        <LoadingOutlined style={{ color: "#1677ff" }} />
      ) : (
        <span style={{ color: "#d9d9d9" }}>○</span>
      );

      const rowBg = isPlaceholder
        ? "rgba(114,46,209,0.06)"
        : isGenerated
          ? "rgba(82,196,26,0.06)"
          : isFailed
            ? "rgba(255,77,79,0.06)"
            : isOutlineFailed
              ? "rgba(250,140,22,0.06)"
              : isGenerating
                ? "rgba(22,119,255,0.06)"
                : "transparent";

      const wordTagColor = isPlaceholder
        ? "purple"
        : isGenerated
          ? "success"
          : isFailed
            ? "error"
            : isGenerating
              ? "processing"
              : "default";

      return {
        key: node.key,
        title: (
          <Tooltip
            title={
              <div style={{ fontSize: 12 }}>
                <div>层级：L{node.level}</div>
                <div>
                  状态：
                  {isPlaceholder
                    ? "另行补充（按规则跳过正文生成）"
                    : isGenerated
                      ? "已生成"
                      : isFailed
                        ? "生成失败"
                        : isGenerating
                          ? "生成中"
                          : "未生成"}
                </div>
                <div>
                  字数：
                  {isPlaceholder ? "—（登记占位文本）" : `${node.word_count}/${node.word_budget}`}
                </div>
              </div>
            }
            placement="right"
          >
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                width: "100%",
                padding: "2px 6px",
                margin: "0 -6px",
                borderRadius: 4,
                background: rowBg,
              }}
            >
              {statusIcon}
              {node.level > 1 && <span style={{ color: "#bfbfbf" }}>·</span>}
              <span
                style={{
                  fontWeight: node.level === 1 ? 600 : 400,
                  color: isPlaceholder
                    ? "#531dab"
                    : isGenerated
                      ? "#389e0d"
                      : isFailed
                        ? "#cf1322"
                        : isOutlineFailed
                          ? "#d46b08"
                          : isGenerating
                            ? "#0958d9"
                            : undefined,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  maxWidth: 180,
                }}
              >
                {formatOutlineTitle(node.outlineId || node.key, node.level, node.title)}
              </span>
              {isPlaceholder && (
                <Tag
                  color={wordTagColor}
                  style={{ margin: "0 0 0 auto", fontSize: 11, lineHeight: "16px" }}
                >
                  另行补充
                </Tag>
              )}
              {!isPlaceholder && node.word_count > 0 && !node.children?.length && (
                <Tag
                  color={wordTagColor}
                  style={{ margin: "0 0 0 auto", fontSize: 11, lineHeight: "16px" }}
                >
                  {node.word_count}字
                </Tag>
              )}
            </span>
          </Tooltip>
        ),
        children: node.children?.map((c) => convertToAntTree(c, depth + 1)),
      };
    },
    [failedOutlineChapters],
  );

  const treeData = useMemo(() => tree.map((n) => convertToAntTree(n)), [tree, convertToAntTree]);

  // ✅ P0-5：树选择/展开逻辑提取为稳定回调（配合 SchemeTreePanel memo 隔离重渲）
  // ✅ 树选择：仅选中章节，不再强制跳转「正文生成」Tab。
  //    旧实现 setActiveTab("content") 会让用户在「全局事实 / 目录生成」等页操作时
  //    误点左侧树就被拽回正文页，打断当前工作流——这是导航混乱的核心来源之一。
  //    选中后若用户当前就在正文页，内容区自然跟随更新；在其他页则保持当前步骤。
  const handleTreeSelect = useCallback(
    (key: Key) => {
      let sec = findSectionById(tree, key as string);
      if (sec) {
        // 父节点（无 content）→ 自动跳第一个有内容的后代
        if (!sec.content && sec.children && sec.children.length > 0) {
          const first = findFirstContentDescendant(sec);
          if (first) {
            sec = first;
            setExpandedKeys((prev) => Array.from(new Set([...prev, first.key])));
          }
        }
        setSelectedSection(sec);
      }
    },
    [tree],
  );

  const handleTreeExpand = useCallback((keys: Key[]) => setExpandedKeys(keys), []);

  /** ✅ 《待补充清单》跳转（2026-09-24）：切到「正文生成」并选中目标章节。
   *  展开其祖先链保证树中可见；章节已被删除时如实提示（避免「点了没反应」）。
   *  必须定义在 handleTreeSelect 之后（useCallback deps 数组是立即求值，前向引用会 TS2448）。 */
  const handlePlaceholderJump = useCallback(
    (sectionId: string) => {
      const sec = findSectionById(tree, sectionId);
      if (!sec) {
        msg.warning("章节不存在或已被删除，请重新预检刷新清单");
        return;
      }
      const keysToExpand: string[] = [sectionId];
      let cur = sectionId;
      for (let i = 0; i < 20; i++) {
        // 祖先链有界回溯（防御环状脏数据）
        const p = findParentKey(tree, cur);
        if (!p) break;
        keysToExpand.push(p);
        cur = p;
      }
      setExpandedKeys((prev) => Array.from(new Set([...prev, ...keysToExpand])));
      handleTreeSelect(sectionId);
      setActiveTab("content");
      msg.info(`已定位到「${sec.title}」`);
    },
    [tree, handleTreeSelect],
  );

  /** ✅ 重跑受影响章节（2026-09-24，治 F 层第 3 条）：补录后只重跑受影响章节。
   *  逐章 mode=section + force_rewrite（智能跳过会跳过已有正文的章节，见 4023
   *  行同款修复），串行 await 保证任务不并发打架；完成后自动刷新预检，
   *  占位符清单与基线趋势随之更新。 */
  const handleRerunAffected = useCallback(async () => {
    if (!id) return;
    if (generating) {
      msg.warning("当前有生成任务进行中，请等待完成后再重跑");
      return;
    }
    const targets: string[] = rerunPlan?.rerunnable_sections || [];
    if (!targets.length) {
      msg.info(
        "没有可自动重跑的章节（占位字段均无补录数据，请先补录全局事实/解析提取，或手动回改正文）",
      );
      return;
    }
    setRerunning(true);
    try {
      for (const sid of targets) {
        await handleGenerateContent({
          section_id: sid,
          mode: "section",
          force_rewrite: true,
        });
      }
      msg.success(`已重跑 ${targets.length} 个受影响章节，正在刷新清单…`);
      await handleExportCheck();
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "重跑中断，可稍后重新预检查看进度");
    } finally {
      setRerunning(false);
    }
  }, [id, generating, rerunPlan, handleGenerateContent, handleExportCheck]);

  const handleToggleExpand = useCallback(() => {
    if (expandedKeys.length > 0) {
      setExpandedKeys([]);
    } else {
      setExpandedKeys(collectAllKeys(tree));
    }
  }, [tree, expandedKeys]);

  // ✅ 左侧面板的「前往目录生成」：稳定引用，避免 memo 面板重渲
  const handleGoToOutline = useCallback(() => setActiveTab("outline"), []);

  return {
    treeData,
    handleTreeSelect,
    handleTreeExpand,
    handlePlaceholderJump,
    handleRerunAffected,
    handleToggleExpand,
    handleGoToOutline,
  };
}

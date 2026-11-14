import { useCallback, useMemo } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { TreeDataNode } from "antd";
import { Tag, Tooltip, Button } from "antd";
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  FileAddOutlined,
  LoadingOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { formatOutlineTitle } from "./outlineNumbering.ts";
import type { TreeNode } from "./outlineTreeModel.ts";
import { OutlineRenameInput } from "./OutlineRenameInput.tsx";
import { setRenameDraft } from "../../utils/perf/renameDraftStore.ts";

/**
 * ✅ 2026-11-04 拆分（G6）：目录「编辑树」渲染派生自 SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：
 *   - `moveFlags`（可上移/下移预计算）与 `convertToEditTree`、`outlineEditTreeData`
 *     的 useMemo/useCallback 形态与依赖数组逐字保留，仅搬迁宿主；
 *   - 返回值同名（moveFlags / convertToEditTree / outlineEditTreeData），
 *     页面 JSX 引用零改动。
 */
export interface OutlineEditTreeRenderDeps {
  tree: TreeNode[];
  moveNode: (key: string, dir: -1 | 1) => void;
  renameNode: (key: string, newTitle: string) => void;
  failedOutlineChapters: string[];
  renamingKey: string | null;
  renamingValue: string;
  setRenamingKey: Dispatch<SetStateAction<string | null>>;
  setRenamingValue: Dispatch<SetStateAction<string>>;
  /** ✅ R231（2026-11-13）：omp 目录生成期间禁用双击改名——改名落库
   *  （update_section title）与 omp 结束后的整表重建互踩（改后的标题
   *  匹配不到旧路径 ⇒ 正文丢失面），后端双 409 感知不到 omp 旁路
   *  （可行性报告 §11.4 局限 B）；缺省 false，既有调用行为逐字不变。 */
  ompBusy?: boolean;
}

export function useOutlineEditTreeRender(deps: OutlineEditTreeRenderDeps) {
  const {
    tree,
    moveNode,
    renameNode,
    failedOutlineChapters,
    renamingKey,
    setRenamingKey,
    setRenamingValue,
    ompBusy = false,
  } = deps;

  // 预计算每个节点是否可上移/下移（同级首/尾禁用），供目录树行内按钮使用
  const moveFlags = useMemo(() => {
    const flags: Record<string, { up: boolean; down: boolean }> = {};
    const scan = (ns: TreeNode[], _parent: TreeNode[] | null) => {
      ns.forEach((n, i) => {
        flags[n.key] = { up: i > 0, down: i < ns.length - 1 };
        if (n.children?.length) scan(n.children, ns);
      });
    };
    scan(tree, null);
    return flags;
  }, [tree]);

  // ✅ 性能优化：改为 useCallback —— 目录树 300 个节点时，每次整页渲染都会
  //    重建 ~1200 个 JSX 元素（每节点 Tooltip/Input/图标 + 3 个内联闭包）+
  //    每节点 O(失败章数) 子串匹配。配合下方 outlineEditTreeData 的 useMemo，
  //    无关 state（progress / sectionLogs / genStats …）变化时整棵编辑树不再重算。
  // ⚠️ 必须定义在 renameNode 与 moveFlags 之后：deps 数组是立即求值，前向引用会 TDZ。
  const convertToEditTree = useCallback(
    (node: TreeNode): TreeDataNode => {
      // ✅ 2026-11-05 目录占位章节：按规则只登记「另行补充内容」，不生成正文。
      const isPlaceholder = node.status === "placeholder";
      const isGenerated = !isPlaceholder && (node.status === "generated" || node.word_count > 0);
      const isGenerating = node.status === "running" || node.status === "pending";
      const isFailed = node.status === "failed";
      // 长方案分步目录生成时子目录生成失败的章节（橙色警告，区别于正文生成失败的红色）
      const isOutlineFailed = failedOutlineChapters.some(
        (t) => node.title.includes(t) || t.includes(node.title),
      );

      const statusDot = isPlaceholder ? (
        <FileAddOutlined style={{ color: "#722ed1", fontSize: 12 }} />
      ) : isGenerated ? (
        <CheckCircleOutlined style={{ color: "#52c41a", fontSize: 12 }} />
      ) : isFailed ? (
        <CloseCircleOutlined style={{ color: "#ff4d4f", fontSize: 12 }} />
      ) : isOutlineFailed ? (
        <WarningOutlined style={{ color: "#fa8c16", fontSize: 12 }} />
      ) : isGenerating ? (
        <LoadingOutlined style={{ color: "#1677ff", fontSize: 12 }} />
      ) : (
        <span
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: "#d9d9d9",
          }}
        />
      );

      const rowBg = isPlaceholder
        ? "rgba(114,46,209,0.04)"
        : isGenerated
          ? "rgba(82,196,26,0.04)"
          : isFailed
            ? "rgba(255,77,79,0.04)"
            : isOutlineFailed
              ? "rgba(250,140,22,0.06)"
              : isGenerating
                ? "rgba(22,119,255,0.04)"
                : "transparent";

      return {
        key: node.key,
        title: (
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              width: "100%",
              padding: "2px 8px",
              margin: "0 -8px",
              borderRadius: 4,
              background: rowBg,
            }}
            onDoubleClick={() => {
              // ✅ R231（2026-11-13）：omp 生成期禁用改名入口（见 deps.ompBusy 注释）
              if (ompBusy) return;
              setRenameDraft(node.title);
              setRenamingKey(node.key);
              setRenamingValue(node.title);
            }}
          >
            {renamingKey === node.key ? (
              <OutlineRenameInput
                initialValue={node.title}
                onCommit={(value) => {
                  setRenamingValue(value);
                  renameNode(node.key, value);
                  setRenamingKey(null);
                }}
              />
            ) : (
              <>
                {statusDot}
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
                  }}
                >
                  {formatOutlineTitle(node.outlineId || node.key, node.level, node.title)}
                </span>
                <span style={{ fontSize: 11, color: "#999" }}>
                  (L{node.level} · {isPlaceholder ? "另行补充" : `${node.word_budget}字`})
                </span>
                {isPlaceholder && (
                  <Tag
                    color="purple"
                    style={{ margin: "0 0 0 8px", fontSize: 11, lineHeight: "16px" }}
                  >
                    另行补充
                  </Tag>
                )}
                {!isPlaceholder && node.word_count > 0 && !node.children?.length && (
                  <Tag
                    color={isGenerated ? "success" : isFailed ? "error" : "processing"}
                    style={{ margin: "0 0 0 8px", fontSize: 11, lineHeight: "16px" }}
                  >
                    {node.word_count}字
                  </Tag>
                )}
                {/* ✅ 2026-09-29（事实变更失效标记）：正文写于全局事实变更之前。
                  只提示不阻断 —— 后端不会自动重写用户已编辑的章节。 */}
                {node.facts_stale && (
                  <Tooltip title="全局事实已变更，本章正文引用的参数可能已过期，建议重新生成">
                    <Tag
                      color="warning"
                      icon={<ExclamationCircleOutlined />}
                      style={{
                        margin: "0 0 0 8px",
                        fontSize: 11,
                        lineHeight: "16px",
                        cursor: "help",
                      }}
                    >
                      事实已变更
                    </Tag>
                  </Tooltip>
                )}
                <span
                  style={{
                    marginLeft: "auto",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 2,
                    flexShrink: 0,
                  }}
                >
                  <Button
                    type="text"
                    size="small"
                    tabIndex={-1}
                    disabled={ompBusy || !moveFlags[node.key]?.up}
                    icon={<ArrowUpOutlined />}
                    title="上移"
                    onClick={(e) => {
                      e.stopPropagation();
                      moveNode(node.key, -1);
                    }}
                  />
                  <Button
                    type="text"
                    size="small"
                    tabIndex={-1}
                    disabled={ompBusy || !moveFlags[node.key]?.down}
                    icon={<ArrowDownOutlined />}
                    title="下移"
                    onClick={(e) => {
                      e.stopPropagation();
                      moveNode(node.key, 1);
                    }}
                  />
                </span>
              </>
            )}
          </div>
        ),
        children: node.children?.map((c) => convertToEditTree(c)),
      };
    },
    // ✅ 2026-11-11 性能：renamingValue 已移出依赖。逐字符输入只更新
    //    renameDraftStore（订阅者仅重命名 Input），不再让 300 节点编辑树
    //    随每次按键全量重建；renamingKey 变化（低频，切换重命名节点）仍会重建。
    // ✅ R231（2026-11-13）：ompBusy 进 deps —— 它在 onDoubleClick 与行内
    //    上/下移按钮的 disabled 判据里被闭包引用，漏进 deps 会捕获初值
    //    （stale closure：omp 结束后门禁不解除）。
    [moveFlags, moveNode, renameNode, failedOutlineChapters, renamingKey, ompBusy],
  );

  // ✅ 性能优化：编辑树数据 useMemo 化。旧实现在组件顶层无条件 `tree.map(...)` ——
  //    300 节点 = 每次整页渲染都重建全量树元素，且与 outline Tab 是否激活无关
  //    （该语句在函数体中，会拖慢正文/审核/导出等所有 Tab 的渲染）。
  //    现在仅在树结构或影响节点渲染的状态（可移动性 / 失败章 / 重命名节点 /
  //    重命名输入值）变化时才重算。deps 用 convertToEditTree 而非逐项展开，
  //    其自身的 useCallback deps 已精确覆盖全部输入。
  const outlineEditTreeData = useMemo(
    () => tree.map((n) => convertToEditTree(n)),
    [tree, convertToEditTree],
  );

  return { moveFlags, convertToEditTree, outlineEditTreeData };
}

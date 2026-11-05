import { useCallback } from "react";
import type { Dispatch, Key, MutableRefObject, SetStateAction } from "react";
import type { ModalFuncProps } from "antd";
import { sectionsApi } from "../../api";
import { MAX_OUTLINE_DEPTH } from "../../utils/outlineConstants";
import {
  findSectionById,
  renumberTreeLocally,
  isUnsavedLocalKey,
  moveSiblingInTree,
  flattenTreeKeys,
  scrollToNodeInContainer,
  hasUnsavedLocalNodes,
  type TreeNode,
} from "./outlineTreeModel.ts";
// ✅ any 清理专项（2026-11-02）：选中章节形状与 useReviewAudit 共用同一契约（仅类型引用，运行期零依赖）
import type { SelectedSectionLike } from "./useReviewAudit";

/**
 * R-P1 拆分（纯结构重构）：目录树编辑域回调，自 SchemeWorkbenchPage 原位搬迁。
 * 新增 / 同级新增 / 删除 / 重命名 / 上下移全部保持 useCallback 形态与依赖数组
 * 原样（含原依赖说明注释），仅搬迁宿主。flashReorderHint 为页面内提升的
 * 函数声明，经 deps 传入（引用语义等价）。
 */
export interface OutlineTreeEditDeps {
  id: string | undefined;
  msg: {
    success: (s: string) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
  };
  modal: { confirm: (cfg: ModalFuncProps) => void };
  load: () => Promise<unknown>;
  treeRef: MutableRefObject<TreeNode[]>;
  expandedKeysRef: MutableRefObject<Key[]>;
  editTreeScrollRef: MutableRefObject<HTMLDivElement | null>;
  leftTreeScrollRef: MutableRefObject<HTMLDivElement | null>;
  setTree: Dispatch<SetStateAction<TreeNode[]>>;
  setSelectedSection: Dispatch<SetStateAction<SelectedSectionLike | null>>;
  setRenamingKey: (v: string | null) => void;
  setRenamingValue: (v: string) => void;
  setExpandedKeys: Dispatch<SetStateAction<Key[]>>;
  flashReorderHint: (msg?: string) => void;
}

export function useOutlineTreeEdit(deps: OutlineTreeEditDeps) {
  const {
    id,
    msg,
    modal,
    load,
    treeRef,
    expandedKeysRef,
    editTreeScrollRef,
    leftTreeScrollRef,
    setTree,
    setSelectedSection,
    setRenamingKey,
    setRenamingValue,
    setExpandedKeys,
    flashReorderHint,
  } = deps;

  /** 目录树：在指定节点下新增一个子节点 */
  const addChildNode = useCallback(
    (parentKey?: string) => {
      const curTree = treeRef.current;
      const curExpanded = expandedKeysRef.current;
      const newId = `local_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
      const newLevel = parentKey ? (findSectionById(curTree, parentKey)?.level || 0) + 1 : 1;
      // ✅ BUG 修复：目录系统硬性限三级（后端 MAX_OUTLINE_DEPTH=3），在三级章节下
      // 添加的四级节点保存时会被静默裁剪并入父章节描述——用户以为加成功了，
      // 保存后节点"消失"。这里在添加入口直接拦截并说明。
      if (newLevel > MAX_OUTLINE_DEPTH) {
        msg.warning(`目录最多支持 ${MAX_OUTLINE_DEPTH} 级，无法在三级章节下继续添加子章节`);
        return;
      }
      const newNode: TreeNode = {
        key: newId,
        title: "新章节",
        level: newLevel,
        status: "empty",
        word_count: 0,
        word_budget: 1200,
        children: [],
      };
      // ✅ BUG 修复（2026-10-07 · 新增节点编号不同步）：新增节点本身不带 outlineId
      //    （渲染时无编号），追加为子节点还会使同父后续兄弟的相对位置变化。旧实现
      //    直接 setTree 不重算，与 moveNode / onOutlineTreeDrop「结构变更即本地
      //    renumberTreeLocally」的口径不一致，用户在点「保存目录」前看到的编号是
      //    缺失/陈旧的。现统一按位置即时重算（仅展示字段，不动 key/正文关联）。
      if (!parentKey) {
        setTree(renumberTreeLocally([...curTree, newNode]));
      } else {
        setTree(renumberTreeLocally(curTree.map((n) => insertIntoNode(n, parentKey, newNode))));
      }
      // 自动选中新节点
      setSelectedSection(newNode);
      // ✅ 工作流增强（2026-10-07）：新增节点默认标题「新章节」必然要改，
      //    直接进入内联重命名态（Input autoFocus，回车/失焦即提交），省去
      //    「新增 → 再双击/点编辑」的一次往返；直接失焦则保留默认名，无害。
      setRenamingKey(newId);
      setRenamingValue("新章节");
      // 自动展开父节点
      if (parentKey && !curExpanded.includes(parentKey)) {
        setExpandedKeys([...curExpanded, parentKey]);
      }
    },
    [msg, id],
  );

  /** 目录树：在指定节点同级（其后）新增一个节点；未指定 key 时追加为根级章节 */
  const addSiblingNode = useCallback(
    (key?: string) => {
      const curTree = treeRef.current;
      const curExpanded = expandedKeysRef.current;
      if (!key) {
        addChildNode(); // 无参照节点：复用"追加到根"
        return;
      }
      const ref = findSectionById(curTree, key);
      if (!ref) return;
      const newId = `local_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
      const newLevel = ref.level || 1;
      const newNode: TreeNode = {
        key: newId,
        title: "新章节",
        level: newLevel,
        status: "empty",
        word_count: 0,
        word_budget: 1200,
        children: [],
      };
      // 插入到参照节点之后（同父层级），保留其余结构
      const insertAfter = (nodes: TreeNode[]): TreeNode[] => {
        const out: TreeNode[] = [];
        for (const n of nodes) {
          out.push(n);
          if (n.key === key) {
            out.push(newNode);
          } else if (n.children && n.children.length) {
            out[out.length - 1] = { ...n, children: insertAfter(n.children) };
          }
        }
        return out;
      };
      // ✅ BUG 修复（2026-10-07 · 中间插入兄弟编号错位）：新节点插在参照节点之后，
      //    其后所有兄弟的相对序号 +1。旧实现不本地重算，界面短暂呈现
      //    「第一章 / 新章节(无编号) / 第二章 / 第三章」的错位编号，直到保存后
      //    load 才纠正。与 moveNode / 拖拽 / 新增子章同口径即时重算。
      setTree(renumberTreeLocally(insertAfter(curTree)));
      setSelectedSection(newNode);
      // ✅ 工作流增强（2026-10-07）：与 addChildNode 同口径，新增后立即内联重命名
      setRenamingKey(newId);
      setRenamingValue("新章节");
      // 自动展开父节点（确保新同级节点可见）
      const parentKey = findParentKey(curTree, key);
      if (parentKey && !curExpanded.includes(parentKey)) {
        setExpandedKeys([...curExpanded, parentKey]);
      }
    },
    [addChildNode, id],
  );

  /** 查找某节点的父节点 key（用于新增同级后确保父级展开） */
  function findParentKey(nodes: TreeNode[], key: string, parent = ""): string {
    for (const n of nodes) {
      if (n.key === key) return parent;
      if (n.children && n.children.length) {
        const r = findParentKey(n.children, key, n.key);
        if (r !== "") return r;
      }
    }
    return "";
  }

  function insertIntoNode(node: TreeNode, parentKey: string, newNode: TreeNode): TreeNode {
    if (node.key === parentKey) {
      return { ...node, children: [...(node.children || []), newNode] };
    }
    if (node.children && node.children.length) {
      return {
        ...node,
        children: node.children.map((c) => insertIntoNode(c, parentKey, newNode)),
      };
    }
    return node;
  }

  /** 目录树：删除一个节点 */
  const deleteNode = useCallback(
    (key: string) => {
      const target = findSectionById(treeRef.current, key);
      if (!target) return;
      // 确认删除
      modal.confirm({
        title: "确认删除章节",
        content: `「${target.title}」及其所有子章节将被删除，确定继续？`,
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            const cur = findSectionById(treeRef.current, key);
            // ✅ BUG 修复：判据改用 isUnsavedLocalKey（覆盖 local_ + upload_）。
            // 旧实现只认 local_，「导入目录」替换后的 upload_ 节点会被当成已落库
            // 章节，误调 DELETE /sections/{upload_...} → 后端 404，异常抛出后
            // setTree(removeNode(...)) 执行不到 → **本地节点永远删不掉**。
            let persisted = false;
            if (cur && !isUnsavedLocalKey(cur.key) && id) {
              await sectionsApi.delete(id, cur.key);
              persisted = true;
            }
            // ✅ BUG 修复（2026-10-07 · 删除章节后编号未重算）：删除中间章节后，
            //    其后所有兄弟的相对序号 -1。旧实现只 removeNode 不重算，且已落库
            //    删除成功后不 load() —— 后端 renumber_sections_after_reorder 已把
            //    DB 的 outline_json.id 与正文子标题全部重写，前端却仍显示旧编号
            //    （如删掉第2章后原第3章仍显示「第三章」），前后端短暂不一致。
            //    现本地即时重算（仅改展示字段），已落库场景再 load() 拉回权威树，
            //    与 moveNode 的「本地重算 + 服务端同步」口径完全一致。
            setTree(renumberTreeLocally(removeNode(treeRef.current, key)));
            setSelectedSection(null);
            msg.success("已删除");
            if (persisted) load();
          } catch (e: unknown) {
            // 结构化窄断言：仅读取错误对象上真实存在的 message
            const err = e as { message?: string };
            msg.error(err.message || "删除失败");
          }
        },
      });
    },
    [modal, id, msg, load],
  );

  function removeNode(nodes: TreeNode[], key: string): TreeNode[] {
    return nodes
      .filter((n) => n.key !== key)
      .map((n) => ({
        ...n,
        children: n.children ? removeNode(n.children, key) : [],
      }));
  }

  /** 目录树：重命名 */
  const renameNode = useCallback(
    (key: string, newTitle: string) => {
      const trimmed = newTitle.trim();
      if (!trimmed) {
        msg.warning("章节标题不能为空");
        return;
      }
      const node = findSectionById(treeRef.current, key);
      // ✅ 等值短路：用户把旧标题清空/回车即触发（失焦/回车两条路径都调本函数），
      //    标题未变就直接退出，既避免冗余本地重渲，也不再 PATCH /sections/{id}
      //    发送「未改即提交」的空变更。node.title 是后端原始文本（buildTree 直取
      //    n.title，formatOutlineTitle 仅用于渲染展示），这里 .trim() 对齐用户输入。
      if (node && node.title.trim() === trimmed) return;
      setTree(treeRef.current.map((n) => renameInNode(n, key, trimmed)));
      // ✅ 判据改用 isUnsavedLocalKey：upload_（导入识别）节点同样尚未落库，
      // 误调 PATCH /sections/{upload_...} 会 404。
      // ✅ BUG 修复（B12 真正落地）：旧实现 `.catch(() => {})` 完全吞掉写库失败，
      // 用户看到本地标题已改就以为已保存；实际服务端仍是旧标题 —— 若此后不点
      // 「保存目录」直接生成正文，正文里会出现旧章节名。现失败即明示并给出补救动作。
      if (node && !isUnsavedLocalKey(key) && id) {
        sectionsApi.update(id, key, { title: trimmed }).catch((rej: unknown) => {
          // 结构化窄断言：仅读取错误对象上真实存在的 message
          const err = rej as { message?: string };
          msg.warning(
            `本地已改名为「${trimmed}」，但服务端保存失败（${err?.message || "未知错误"}）——` +
              `请点击「保存目录」持久化，否则刷新后仍是旧标题`,
          );
          flashReorderHint("⚠️ 改名未同步到服务端，请点击「保存目录」持久化");
        });
      }
    },
    [msg, id],
  );

  function renameInNode(node: TreeNode, key: string, title: string): TreeNode {
    if (node.key === key) return { ...node, title };
    if (node.children) {
      return {
        ...node,
        children: node.children.map((c) => renameInNode(c, key, title)),
      };
    }
    return node;
  }

  /**
   * 目录树：上移/下移章节（dir=-1 上 / +1 下）。
   * 本地树立即交换顺序给出视觉反馈；若当前没有「未保存的本地新增节点(local_)」，
   * 则立即调用 /reorder 持久化（重排 sort_order 并回写章节编号），随后刷新树，
   * 保证正文生成读取的「当前章节编号」与展示一致。
   * 若存在未保存新增节点，则仅本地交换、依赖「保存目录」统一落库（与拖拽一致，
   * 避免 /reorder 只重排 DB 节点而丢失未落库的新增章节）。
   */
  const moveNode = useCallback(
    (key: string, dir: -1 | 1) => {
      const newTree = moveSiblingInTree(treeRef.current, key, dir);
      if (!newTree) return;
      const hasUnsavedLocal = hasUnsavedLocalNodes(treeRef.current);
      // ✅ 编辑增强：移动后滚动定位到目标节点（展开状态天然保留——load 不清空
      //    expandedKeys，且被移动节点原本可见、其祖先必然已展开）。
      const scrollToTarget = () => {
        // ✅ 编辑增强：编辑树与左侧导航树同步滚动定位到目标节点
        scrollToNodeInContainer(editTreeScrollRef.current, key);
        scrollToNodeInContainer(leftTreeScrollRef.current, key);
      };
      if (!hasUnsavedLocal && id) {
        // 无未保存新增节点：立即持久化（/reorder 重排 + 回写编号），load 后编号自然正确
        const order = flattenTreeKeys(newTree);
        setTree(renumberTreeLocally(newTree)); // 即时刷新编号展示
        flashReorderHint("章节已上/下移，编号已实时更新（已同步到服务端）");
        sectionsApi
          .reorder(id, { order })
          .then(() => load().then(() => setTimeout(scrollToTarget, 60)))
          .catch(() => {
            // ✅ 后端 /reorder 失败降级为"仅本地调整"：当前章节顺序只在前端可见，
            // 刷新后会回到 DB 原有顺序；明确告知用户需要手动点「保存目录」才能持久化。
            msg.warning("移动已在本地生效，服务端同步失败，请点击「保存目录」完成持久化");
            flashReorderHint("⚠️ 服务端同步失败，本地顺序尚未持久化，请点击「保存目录」");
          });
      } else {
        // 存在未保存新增节点：仅本地交换 + 本地重算编号，依赖「保存目录」统一落库
        setTree(renumberTreeLocally(newTree));
        flashReorderHint();
        setTimeout(scrollToTarget, 60);
      }
      // ✅ 依赖说明：flashReorderHint 为函数声明（每渲染新建但仅用 setState+ref，
      //    捕获旧引用语义等价），故不列入依赖，以保证本回调引用稳定。
    },
    [msg, id, load, scrollToNodeInContainer],
  );

  return {
    addChildNode,
    addSiblingNode,
    deleteNode,
    renameNode,
    moveNode,
    // 页面拖拽等其余区域仍在使用的纯辅助函数（R-P1：随域迁入后回导）
    findParentKey,
  };
}

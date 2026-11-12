import { useCallback, useRef } from "react";
import type { Dispatch, Key, MutableRefObject, SetStateAction } from "react";
import type { FormInstance, ModalFuncProps, TreeProps } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { sectionsApi, outlineLibraryApi, uploadOutlineApi } from "../../api";
import { MAX_OUTLINE_DEPTH } from "../../utils/outlineConstants";
import { outlineClampNotice, outlineUnmountedNotice } from "./outlineNotices";
import {
  collectAllKeys,
  countOutline,
  findSectionById,
  hasDeepOutlineNodes,
  isUnsavedLocalKey,
  outlineToTreeNode,
  outlineTreeDepth,
  renumberOutline,
  renumberTreeLocally,
  summarizeUploadOutlineResult,
  treeNodesWithContent,
  treeToOutline,
  updateNodeFields,
  type TreeNode,
} from "./outlineTreeModel.ts";
import type {
  MultiSectionHint,
  OutlineLibraryItem,
  OutlineNodeInput,
  SchemeRecord,
  WorkbenchSection,
} from "./workbenchTypes";

/**
 * R-P1 拆分（纯结构重构）：目录操作回调簇，自 SchemeWorkbenchPage 原位搬迁。
 * 保存目录 / 字数预算 / 导出目录树 / 目录库套用与存库 / 清空目录 /
 * 上传识别替换目录 / 拖拽排序 / 编号提示，全部保持原有实现与依赖数组原样，
 * 仅把外部标识符改为经 deps 解构。
 */
export interface OutlineActionsDeps {
  id: string | undefined;
  msg: MessageInstance;
  modal: { confirm: (cfg: ModalFuncProps) => void };
  load: () => Promise<void>;
  scheme: SchemeRecord | null;
  tree: TreeNode[];
  treeRef: MutableRefObject<TreeNode[]>;
  setTree: Dispatch<SetStateAction<TreeNode[]>>;
  setSelectedSection: Dispatch<SetStateAction<WorkbenchSection | null>>;
  setExpandedKeys: Dispatch<SetStateAction<Key[]>>;
  setActiveTab: Dispatch<SetStateAction<string>>;
  setReviewTick: Dispatch<SetStateAction<number>>;
  setMultiSectionHint: Dispatch<SetStateAction<MultiSectionHint | null>>;
  outlineImportReorganize: boolean;
  lastUpload: { id: string; name: string; outline: OutlineNodeInput[] } | null;
  setLastUpload: Dispatch<
    SetStateAction<{ id: string; name: string; outline: OutlineNodeInput[] } | null>
  >;
  exportingRef: MutableRefObject<boolean>;
  exportingTree: boolean;
  setExportingTree: Dispatch<SetStateAction<boolean>>;
  setLibOpen: Dispatch<SetStateAction<boolean>>;
  setLibLoading: Dispatch<SetStateAction<boolean>>;
  setLibItems: Dispatch<SetStateAction<OutlineLibraryItem[]>>;
  setLibError: Dispatch<SetStateAction<string>>;
  setLibApplying: Dispatch<SetStateAction<boolean>>;
  setSaveLibOpen: Dispatch<SetStateAction<boolean>>;
  saveLibForm: FormInstance<{ name: string }>;
  setReorderHint: Dispatch<SetStateAction<string | null>>;
  reorderHintTimer: MutableRefObject<number | null>;
  notifyClearedContent: (
    data: { cleared_content_sections?: number } | null | undefined,
    prefix?: string,
  ) => void;
  notifyGenericResidual: (data: unknown) => void;
}

export interface OutlineActions {
  saveOutlineTree: () => Promise<void>;
  doSaveOutlineTree: (opts?: { allowEmptyChapters?: boolean }) => Promise<void>;
  handleBudgetChange: (key: string, budget: number) => void;
  handleExportTree: () => Promise<void>;
  openLibraryPicker: () => Promise<void>;
  handleApplyLibrary: (lib: OutlineLibraryItem) => void;
  handleSaveAsLibrary: () => Promise<void>;
  submitSaveAsLibrary: () => Promise<void>;
  clearAllOutline: () => void;
  handleUploadParseOutline: (file: File) => Promise<void>;
  onOutlineTreeDrop: (info: Parameters<NonNullable<TreeProps["onDrop"]>>[0]) => void;
  flashReorderHint: (msg?: string) => void;
}

export function useOutlineActions(deps: OutlineActionsDeps): OutlineActions {
  const {
    id,
    msg,
    modal,
    load,
    scheme,
    tree,
    treeRef,
    setTree,
    setSelectedSection,
    setExpandedKeys,
    setActiveTab,
    setReviewTick,
    setMultiSectionHint,
    outlineImportReorganize,
    lastUpload,
    setLastUpload,
    exportingRef,
    exportingTree,
    setExportingTree,
    setLibOpen,
    setLibLoading,
    setLibItems,
    setLibError,
    setLibApplying,
    setSaveLibOpen,
    saveLibForm,
    setReorderHint,
    reorderHintTimer,
    notifyClearedContent,
    notifyGenericResidual,
  } = deps;

  /** 保存当前目录树到后端（含自动编号） */
  const saveOutlineTree = useCallback(async () => {
    if (!id) return;
    // ✅ BUG 修复：手动编辑/拖拽可能产生 4 级及更深节点，后端 normalize_outline
    // 会静默裁剪并把深层标题并入父章节描述——用户"保存成功"后节点消失却无提示。
    // 现保存前检测并二次确认，让用户知情。
    if (hasDeepOutlineNodes(treeRef.current)) {
      modal.confirm({
        title: "目录包含超过三级的章节",
        content: (
          <div>
            系统目录最多支持 <b>三级</b>。保存时，三级以下的深层章节标题将
            <b>自动并入其父章节的章节说明</b>，不会作为独立章节保留。
            <br />
            如需保留这些内容，请先取消并手动将其调整为二级/三级章节。
          </div>
        ),
        okText: "仍然保存",
        cancelText: "返回调整",
        onOk: () => doSaveOutlineTree(),
      });
      return;
    }
    await doSaveOutlineTree();
  }, [id, modal, msg, load]);

  // ✅ R216（2026-11-11 · P3 · D-7）：同步在飞锁（ref 判据，非异步 state）——
  //    同帧双击「保存目录」此前双发 save-outline（双 success、双 cleared toast、
  //    双 reviewTick bump、双快照归档）。与 R212 专家论证同帧双击修法同型。
  const saveInFlightRef = useRef(false);
  async function doSaveOutlineTree(opts?: { allowEmptyChapters?: boolean }) {
    if (!id) return;
    if (saveInFlightRef.current) {
      msg.info("目录保存正在进行中，请稍候");
      return;
    }
    saveInFlightRef.current = true;
    try {
      await doSaveOutlineTreeInner(opts);
    } finally {
      saveInFlightRef.current = false;
    }
  }

  async function doSaveOutlineTreeInner(opts?: { allowEmptyChapters?: boolean }) {
    if (!id) return;
    const outline = renumberOutline(treeToOutline(treeRef.current));
    try {
      const { data } = await sectionsApi.saveOutline(id, {
        outline,
        source: "手动编辑",
        // ✅ R175 G-3 逃生舱：仅在用户于空章确认框点「仍要保存」后才显式传该键；
        //    默认请求不带此键 ⇒ 请求体与引入前逐字一致（后端 400 闸门零豁免）。
        ...(opts?.allowEmptyChapters ? { allow_empty_chapters: true } : {}),
      });
      msg.success(`目录已保存，共 ${data.count || collectAllKeys(treeRef.current).length} 章节`);
      // ✅ R213（FIX-13）：后端 save-outline 回传「超三级被折叠」的信号（R209），
      //    前端此前零消费 ⇒ 用户以为四级目录已完整保存。加法式，未折叠零提示。
      const _clamp = outlineClampNotice(data);
      if (_clamp) msg.warning(_clamp);
      notifyClearedContent(data);
      notifyGenericResidual(data);
      // ✅ G10：目录结构变更会改变预检的章节集合 → 刷新审核 / 就绪度工作台
      setReviewTick((t) => t + 1);
      await load();
    } catch (e: unknown) {
      const err = e as { message?: string };
      const message = err.message || "";
      // ✅ R175 G-3：后端空壳一级章闸门（400，detail 含稳定标记「无子章节的一级章」
      //    —— axios 拦截器已把 detail 写入 error.message）→ 逃生舱确认框。
      //    这是前端唯一会闸门 400 的提交路径（AI/手动清除链路后端放行或不触发）。
      if (message.includes("无子章节的一级章")) {
        modal.confirm({
          title: "检出无子章节的一级章节",
          width: 560,
          content: (
            <div>
              {message}
              <br />
              <br />
              「仍要保存」将按当前结构入库（保存后会持续提示补充空章）；
              「返回调整」不入库——可就地为空章补充二级章节，或删除空章。
            </div>
          ),
          okText: "仍要保存（保留空章）",
          okButtonProps: { danger: true },
          cancelText: "返回调整",
          onOk: () => doSaveOutlineTree({ allowEmptyChapters: true }),
        });
        return;
      }
      msg.error(message || "保存目录失败");
    }
  }

  /**
   * ✅ 字数预算编辑回抛（OutlineNodeBudgetPanel → 本处统一落库）。
   *
   * 口径与「改名」完全对齐：
   *   · 已落库章节 → 立即 PATCH /sections/{id}，失败即明示（不静默吞掉）；
   *   · 临时节点（local_/upload_）→ 仅改本地，等「保存目录」随树一起落库。
   * 旧实现整条链路无预算写入入口（见 OutlineNodeBudgetPanel 说明）。
   */
  function handleBudgetChange(key: string, budget: number) {
    const node = findSectionById(tree, key);
    if (!node) return;
    setTree(updateNodeFields(tree, key, { word_budget: budget }));
    setSelectedSection((prev) =>
      prev && prev.key === key ? { ...prev, word_budget: budget } : prev,
    );
    if (isUnsavedLocalKey(key)) {
      flashReorderHint("字数预算已本地调整，点击「保存目录」后生效");
      return;
    }
    if (!id) return;
    sectionsApi
      .update(id, key, { word_budget: budget })
      .then(() => {
        flashReorderHint(`「${node.title}」字数预算已更新为 ${budget} 字`);
      })
      .catch((err: unknown) => {
        const saveFail = err as { message?: string };
        msg.warning(
          `本地已把字数预算改为 ${budget} 字，但服务端保存失败（${saveFail?.message || "未知错误"}）——` +
            `请点击「保存目录」持久化`,
        );
        flashReorderHint("⚠️ 字数预算未同步到服务端，请点击「保存目录」持久化");
      });
  }

  /**
   * ✅ 接线 sectionsApi.exportTree：
   * 从后端取目录树（程序化重排编号后）并导出为 JSON 文件。
   * 与「保存目录」的区别：这里导出的是后端权威编号（level 已归一），
   * 可直接给外部工具/存档使用，不改动数据库。
   */
  // ✅ R223（2026-11-12 · P3 · FE-7）：handleExportTree 专属在飞锁 —— 旧守卫读的
  //    `exportingRef` 是 DOCX/PDF 导出链的 ref（本函数从不置位），同帧双击时
  //    异步 state `exportingTree` 仍为旧值 ⇒ 双 GET /export-tree、双下载文件。
  //    与本 hook 既有 saveInFlightRef / importInFlightRef 同型（ref 判据）。
  const exportTreeInFlightRef = useRef(false);
  async function handleExportTree() {
    if (!id || exportTreeInFlightRef.current) return;
    exportTreeInFlightRef.current = true;
    setExportingTree(true);
    try {
      const { data } = await sectionsApi.exportTree(id);
      // ✅ R218（2026-11-11 · P3 · D-D）：/export-tree 回传 `unmounted_count`
      //    （R216），前端此前零消费 ⇒ 导出目录 JSON 对成环/悬挂章节静默缺章却
      //    无任何提示。与 list_sections（R213 FIX-12）同口径消费（不阻断导出）。
      const _unmounted = outlineUnmountedNotice(data);
      if (_unmounted) msg.warning(_unmounted);
      const outline = data.outline || [];
      const total = countOutline(outline);
      if (total === 0) {
        msg.info("当前目录为空，无可导出内容");
        return;
      }
      // ✅ 编码修复：JSON 下载前置 UTF-8 BOM（\uFEFF），避免 Windows 记事本打开乱码；
      //    重新导入时前端会剥离 BOM，故不影响解析。
      const blob = new Blob(["\uFEFF" + JSON.stringify(outline, null, 2)], {
        type: "application/json;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${scheme?.name || "方案"}目录树.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      msg.success(`目录树已导出，共 ${total} 个章节`);
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "导出目录树失败");
    } finally {
      exportTreeInFlightRef.current = false;
      setExportingTree(false);
    }
  }

  // ===== ✅ 目录库：套用到当前方案 =====
  /** 打开目录库选择器（只列审核通过的库） */
  async function openLibraryPicker() {
    setLibOpen(true);
    setLibLoading(true);
    setLibError("");
    try {
      const { data } = await outlineLibraryApi.list({ review_status: "已通过" });
      setLibItems(data.items || []);
      if ((data.items || []).length === 0) {
        setLibError("目录库中暂无「已通过」审核的目录。请先到「专项方案目录库」页新建并审核通过。");
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      setLibError(err.message || "加载目录库失败");
    } finally {
      setLibLoading(false);
    }
  }

  /** 套用某个目录库到当前方案：apply 取 outline_json → 写入 sections */
  function handleApplyLibrary(lib: OutlineLibraryItem) {
    if (!id) return;
    modal.confirm({
      title: "⚠️ 确认套用该目录库？",
      content: (
        <div>
          将用「<b>{lib.name}</b>」的目录（{lib.version || "-"}）
          <b style={{ color: "#ff4d4f" }}>覆盖</b>当前方案目录，
          当前已生成的正文（含其中内嵌的图表代码块）将<b style={{ color: "#ff4d4f" }}>一并丢失</b>
          ，不可恢复。
        </div>
      ),
      okText: "覆盖套用",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setLibApplying(true);
        try {
          // ✅ 优化：改用 /apply-and-save 一步完成（后端取库内容 + 裁剪三级
          //    + 按目录库结构重建 sections + 累加引用计数），
          //    替代原来"apply 取 JSON → 前端解析 → save-outline 再提交"的两次往返
          //    （旧链路在第二步失败时引用计数已被累加，且大目录 JSON 多传一次）。
          //    ⚠️ 注意：库节点 id 是编号而非章节主键，无法与已有章节匹配 →
          //    套用是**整表重建**，已有正文不保留（上方确认弹窗文案与此口径一致）。
          const { data } = await outlineLibraryApi.applyAndSave(lib.id!, { scheme_id: id });
          msg.success(`已套用「${lib.name}」，共 ${data.count || 0} 个章节`);
          // ✅ R88 FIX-9：本消费点此前**两个加法式键都不读** ⇒ 双重静默：
          //   ① `cleared_content_sections` —— 套用是整表重建、**已有正文被级联
          //      删除**，后端为这件事专门量化回传（2026-09-30 P1-7），此处不读
          //      ⇒ **用户静默丢正文**；
          //   ② `generic_residual` —— 套用命中占位/泛化标题时的明细清单，
          //      后端 R79 起就回传，经 R88 FIX-8 才真正透传到此处。
          notifyClearedContent(data);
          notifyGenericResidual(data);
          // ✅ R216（2026-11-11 · P3 · D-8）：套用消费 clamped_* 信号 ——
          //    库内存量深树（R209 normalize 入库之前的历史行）套用时后端会
          //    折叠并经 passthrough_additive 透传 clamped_depth/clamped_nodes，
          //    此处此前零消费 ⇒ 静默折叠（与保存主路径 :165 同一文案出口）。
          const _applyClamp = outlineClampNotice(data);
          if (_applyClamp) msg.warning(_applyClamp);
          setLibOpen(false);
          setSelectedSection(null);
          await load();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "套用目录库失败");
        } finally {
          setLibApplying(false);
        }
      },
    });
  }

  /** 把「导入目录（智能识别）」的结果存为目录库（待审核） */
  async function handleSaveAsLibrary() {
    if (!lastUpload) {
      msg.warning("请先用「导入目录（智能识别）」上传文件");
      return;
    }
    setSaveLibOpen(true);
    saveLibForm.setFieldsValue({ name: lastUpload.name.replace(/\.[^.]+$/, "") });
  }

  // ✅ R223（2026-11-12 · P3 · FE-8）：入库在飞锁 —— 弹窗无 confirmLoading、
  //    函数无任何判据，同帧双击「入库（待审核）」会双 POST save-as-library，
  //    同一识别结果入库两条。与 saveInFlightRef 同型（ref 判据）。
  const saveLibInFlightRef = useRef(false);
  async function submitSaveAsLibrary() {
    if (!lastUpload || saveLibInFlightRef.current) return;
    saveLibInFlightRef.current = true;
    try {
      const values = await saveLibForm.validateFields();
      const { data } = await uploadOutlineApi.saveAsLibrary(lastUpload.id, {
        name: values.name,
        outline: lastUpload.outline,
      });
      msg.success(`已存入目录库（${data.id?.slice(0, 8) || "ok"}），状态为「待审核」`);
      setSaveLibOpen(false);
    } catch (e: unknown) {
      const err = e as { errorFields?: unknown; message?: string };
      if (err.errorFields) return;
      msg.error(err.message || "存入目录库失败");
    } finally {
      saveLibInFlightRef.current = false;
    }
  }

  /** 清除所有目录（调 saveOutline 传空数组 → 后端先 DELETE 再 INSERT，自然清空） */
  function clearAllOutline() {
    if (!id) return;
    const total = collectAllKeys(tree).length;
    if (total === 0) {
      msg.info("当前已经没有目录");
      return;
    }
    modal.confirm({
      title: "⚠️ 确认清除所有目录？",
      content: (
        <div>
          本方案下的 <b style={{ color: "#ff4d4f" }}>{total}</b> 个目录章节将被
          <b style={{ color: "#ff4d4f" }}>全部删除</b>， 包括已生成的正文内容。此操作不可恢复！
        </div>
      ),
      okText: "确认清除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await sectionsApi.saveOutline(id, { outline: [], source: "手动清除" });
          msg.success("所有目录已清除");
          notifyClearedContent(data, "清空目录同时删除了：");
          notifyGenericResidual(data);
          setTree([]);
          setSelectedSection(null);
          setExpandedKeys([]);
          await load();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "清除失败");
        }
      },
    });
  }

  /** 从上传文件识别目录 */
  // ✅ R216（2026-11-11 · P3 · D-7）：同步在飞锁 —— 导入目录此前无任何在飞
  //    防护（按钮连 importing 态都没有），同帧双选文件可双份上传 + 双 AI
  //    识别 + 双替换确认弹窗。
  const importInFlightRef = useRef(false);
  const handleUploadParseOutline = async (file: File) => {
    if (importInFlightRef.current) {
      msg.info("目录导入正在进行中，请稍候");
      return;
    }
    importInFlightRef.current = true;
    try {
      await handleUploadParseOutlineInner(file);
    } finally {
      importInFlightRef.current = false;
    }
  };

  const handleUploadParseOutlineInner = async (file: File) => {
    // ✅ BUG 修复：上传识别结果会直接替换本地目录树（尚未保存也会丢）。
    // 若当前树中已有生成正文的章节，误操作会让用户在调整后保存时整组重建、
    // 溯源/正文归属被重置。这里先要求显式确认。
    const chaptersWithContent = treeNodesWithContent(tree);
    if (tree.length > 0) {
      const confirmed = await new Promise<boolean>((resolve) => {
        modal.confirm({
          title: "用识别结果替换当前目录？",
          content:
            chaptersWithContent > 0 ? (
              <div>
                当前目录中有 <b style={{ color: "#ff4d4f" }}>{chaptersWithContent}</b>{" "}
                个章节已生成正文。 替换后保存目录可能影响这些章节的结构归属，正文本身不会立即删除，
                但建议先确认已保存或导出。是否继续？
              </div>
            ) : (
              "当前未保存的目录调整将被识别结果替换，是否继续？"
            ),
          okText: "继续替换",
          cancelText: "取消",
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!confirmed) return;
    }
    const hide = msg.loading("正在解析文件并识别目录...", 0);
    try {
      // ✅ 补齐调用链路：把「整理为标准结构」开关与方案名透传后端
      //    （后端 match_template / reorganize_to_standard 依赖 scheme_name）
      // ✅ 链路修复（2026-10-03）：补传 project_id，使 uploaded_outlines 记录
      //    建立项目归属链（删项目时才能级联清理；无项目上下文时为空、行为不变）
      const { data } = await uploadOutlineApi.parse(file, {
        scheme_name: scheme?.name || "",
        reorganize: outlineImportReorganize,
        project_id: scheme?.project_id || "",
      });
      // ✅ 多标段提示（对齐 OpenBidKit bidSectionDetector）
      if (data.multi_section_hint?.has_multiple) {
        setMultiSectionHint(data.multi_section_hint);
      } else {
        setMultiSectionHint(null);
      }
      // ✅ 结果诊断收口（纯函数）：空结果 / 截断 / 归位统计一律如实告知
      const summary = summarizeUploadOutlineResult(data);
      if (!summary.ok) {
        // 识别失败/空目录：绝不清空用户当前目录树，也不覆盖「存为目录库」来源
        msg.warning(summary.message);
        summary.notices.forEach((n) => msg.info(n));
        return;
      }
      summary.notices.forEach((n) => msg.info(n));
      msg.success(summary.message);
      const outline = summary.outline;
      // 记录 upload 记录，供「存为目录库」使用
      setLastUpload({ id: data.id, name: data.file_name, outline });
      // 构造本地临时节点替换树
      const newTree = outlineToTreeNode(outline);
      setTree(newTree);
      setSelectedSection(null);
      setExpandedKeys(collectAllKeys(newTree));
      setActiveTab("outline");
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "文件识别失败");
    } finally {
      hide();
    }
  };

  /** 目录树拖拽排序 */
  function onOutlineTreeDrop(info: Parameters<NonNullable<TreeProps["onDrop"]>>[0]) {
    // 本页树节点 key 一律为章节主键 string（antd Tree 的 Key 联合在此收窄）
    const dropKey = info.node.key as string;
    const dragKey = info.dragNode.key as string;
    const dropPos = info.node.pos.split("-");
    const dropPosition = info.dropPosition - Number(dropPos[dropPos.length - 1]); // -1 到 +1

    // ✅ BUG 修复：禁止拖入自身或自身子孙（数据成环后递归遍历/保存栈溢出）。
    const findByKey = (nodes: TreeNode[], key: string): TreeNode | null => {
      for (const n of nodes) {
        if (n.key === key) return n;
        if (n.children?.length) {
          const hit = findByKey(n.children, key);
          if (hit) return hit;
        }
      }
      return null;
    };
    const containsKey = (root: TreeNode | null, key: string): boolean => {
      if (!root) return false;
      if (root.key === key) return true;
      return (root.children || []).some((c) => containsKey(c, key));
    };
    const dragNode = findByKey(tree, dragKey);
    if (!dragNode || dragKey === dropKey || containsKey(dragNode, dropKey)) {
      msg.warning("不能将章节移动到自身或其子章节内");
      return;
    }

    const loop = (
      data: TreeNode[],
      key: string,
      callback: (node: TreeNode, i: number, arr: TreeNode[]) => void,
    ) => {
      for (let i = 0; i < data.length; i++) {
        if (data[i].key === key) {
          return callback(data[i], i, data);
        }
        if (data[i].children) {
          loop(data[i].children!, key, callback);
        }
      }
    };

    // ✅ BUG 修复：旧实现仅浅拷贝根数组，splice/unshift 直接突变 state 的
    //    嵌套数组与节点对象。深拷贝后再操作，保证 React 状态不可变。
    const data: TreeNode[] = JSON.parse(JSON.stringify(tree));
    let dragObj: TreeNode | undefined;
    loop(data, dragKey, (item, index, arr) => {
      arr.splice(index, 1);
      dragObj = item;
    });
    if (!dragObj) return;

    if (!info.dropToGap && dropPosition === 0) {
      // 作为子节点
      loop(data, dropKey, (item) => {
        item.children = item.children || [];
        item.children!.unshift(dragObj!);
      });
    } else {
      // 兄弟节点位置
      let ar: TreeNode[] = [];
      let i = 0;
      loop(data, dropKey, (_item, index, arr) => {
        ar = arr;
        i = index;
      });
      if (dropPosition === -1) {
        ar.splice(i, 0, dragObj!);
      } else {
        ar.splice(i + 1, 0, dragObj!);
      }
    }
    // ✅ BUG 修复：拖拽可能把节点拖成四级及更深（如拖到三级章节之下）。旧实现
    //    仅在本地接受，点「保存目录」时后端 normalize_outline 会静默裁剪并把深层
    //    标题并入父章节描述——用户"保存成功"后节点却消失。这里与「新增子章节」
    //    入口保持同一口径，在拖拽落地前拦截。
    if (outlineTreeDepth(data) > MAX_OUTLINE_DEPTH) {
      msg.warning(`目录最多支持 ${MAX_OUTLINE_DEPTH} 级，已取消本次拖拽（请调整目标层级后重试）`);
      return;
    }
    // ✅ 编辑增强：拖拽后本地按位置重算编号，让"编号已更新"实时可见，
    //    并弹出高亮提示（落库仍由「保存目录」统一执行 normalize_outline）。
    setTree(renumberTreeLocally(data));
    flashReorderHint();
  }

  // ✅ 编辑增强：拖拽/上移下移后实时高亮"编号已更新"提示
  function flashReorderHint(
    msg = "目录顺序已调整，编号已实时更新（记得点击「保存目录」同步到服务端）",
  ) {
    setReorderHint(msg);
    if (reorderHintTimer.current) window.clearTimeout(reorderHintTimer.current);
    reorderHintTimer.current = window.setTimeout(() => setReorderHint(null), 3200);
  }

  return {
    saveOutlineTree,
    doSaveOutlineTree,
    handleBudgetChange,
    handleExportTree,
    openLibraryPicker,
    handleApplyLibrary,
    handleSaveAsLibrary,
    submitSaveAsLibrary,
    clearAllOutline,
    handleUploadParseOutline,
    onOutlineTreeDrop,
    flashReorderHint,
  };
}

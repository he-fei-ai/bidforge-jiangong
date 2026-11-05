import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { ModalFuncProps } from "antd";
import { factsApi, docPipelineApi } from "../../api";
import { computeDocStats, buildUploadNotice } from "../../utils/workflowDerived";
import type { DocRecord } from "../../utils/workflowDerived";
import type { FactsSummary } from "../../types/facts";
import {
  MAX_UPLOAD_BYTES,
  splitByUploadTotalQuota,
  splitByUploadFileCount,
  partitionUploadFiles,
  MAX_UPLOAD_FILES_FALLBACK,
} from "../../utils/uploadAccept";
import { buildBatchResolveCopy, buildBatchResolveResultCopy } from "./factsShared.ts";
import type {
  DocumentPipelineStatusLike,
  PipelineActionKey,
} from "../../components/DocumentPipelineSummary";

// =========================================================================
// ✅ any 清理专项（2026-11-02）：本域消费的后端 REST 响应契约。
//    字段按 hook 内实际读取推导，未知字段一律 ? 修饰，不虚构。
// =========================================================================

/** 文档预览响应（GET /global-facts/documents/{id}/preview；本域读 is_parsed/message/preview） */
export interface DocPreviewData {
  is_parsed?: boolean;
  preview?: string;
  message?: string;
}

/** 批量解析失败单条（POST /global-facts/documents/parse-all 响应 failed[]） */
export interface ParseFailureItem {
  file_name?: string;
  reason?: string;
}

/** 批量解析截断单条（parse-all 响应 truncated[]） */
export interface ParseTruncatedItem {
  file_name?: string;
}

/**
 * R-P1 拆分（纯结构重构）：文档上传 / 解析 / 四层管线 / 分类 / 批量确认 /
 * 过期解除 / 删除文档域处理器，自 SchemeWorkbenchPage 原位搬迁。
 * 行为零变更：所有状态仍由页面持有，处理器保持普通函数（非 useCallback，
 * 与原实现一致每次 render 重建）；hook 调用点保持在第一段原位置。
 */
export interface DocumentWorkflowDeps {
  id: string | undefined;
  schemeProjectId: string | undefined;
  msg: {
    success: (s: string) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    info: (s: string) => void;
  };
  modal: { confirm: (cfg: ModalFuncProps) => void };
  // 上传 / 解析
  uploadingFacts: boolean;
  setUploadingFacts: (v: boolean) => void;
  setUploadedFiles: (v: string[]) => void;
  maxUploadBytes: number | undefined;
  maxUploadTotalBytes: number | undefined;
  maxUploadFiles: number | undefined;
  uploadAbortRef: MutableRefObject<AbortController | null>;
  parseAbortRef: MutableRefObject<AbortController | null>;
  parsingDocs: boolean;
  setParsingDocs: (v: boolean) => void;
  parsingDocId: string | null;
  setParsingDocId: (v: string | null) => void;
  docList: DocRecord[];
  loadDocuments: () => void | Promise<void>;
  // 预览 / 四层管线
  setPreviewDoc: (v: DocRecord) => void;
  setPreviewText: (v: string) => void;
  setPreviewData: (v: DocPreviewData | null) => void;
  setPreviewLoading: (v: boolean) => void;
  previewDoc: DocRecord | null;
  setPipelineStatus: Dispatch<SetStateAction<DocumentPipelineStatusLike | null>>;
  setPipelineError: (v: string | null) => void;
  setPipelineLoading: (v: boolean) => void;
  pipelineAction: PipelineActionKey | null;
  setPipelineAction: (v: PipelineActionKey | null) => void;
  // 分类 / 事实批量
  setDocList: Dispatch<SetStateAction<DocRecord[]>>;
  loadFacts: (pageOverride?: number) => void | Promise<void>;
  factsSummary: FactsSummary | null;
  factsListAbortRef: MutableRefObject<AbortController | null>;
  currentSchemeIdRef: MutableRefObject<string>;
}

export function useDocumentWorkflow(deps: DocumentWorkflowDeps) {
  const {
    id,
    schemeProjectId,
    msg,
    modal,
    uploadingFacts,
    setUploadingFacts,
    setUploadedFiles,
    maxUploadBytes,
    maxUploadTotalBytes,
    maxUploadFiles,
    uploadAbortRef,
    parseAbortRef,
    parsingDocs,
    setParsingDocs,
    parsingDocId,
    setParsingDocId,
    docList,
    loadDocuments,
    setPreviewDoc,
    setPreviewText,
    setPreviewData,
    setPreviewLoading,
    previewDoc,
    setPipelineStatus,
    setPipelineError,
    setPipelineLoading,
    pipelineAction,
    setPipelineAction,
    setDocList,
    loadFacts,
    factsSummary,
    factsListAbortRef,
    currentSchemeIdRef,
  } = deps;

  // ✅ 分步工作流 ①：上传保存（仅存文件，不解析不提取）
  const handleUploadDocuments = async (files: File[]) => {
    if (!id || files.length === 0) return;
    if (uploadingFacts) return;
    // ✅ 单文件硬校验（2026-10-05）：`accept` 属性只过滤文件选择框，拖拽与
    //    「① 上传文件保存」按钮这条入口可绕过。不做这一步的话，超限 / 非法
    //    扩展名的文件会被整批发给后端再逐个拒绝 —— 用户白等一轮上传，
    //    且与 import Tab 的提示口径分叉（那边是先拦、根本不发）。
    //    收敛在此处 = 两个入口一次覆盖（import Tab 已先过滤，进来 rejected 为空）。
    const perFileMax =
      typeof maxUploadBytes === "number" && maxUploadBytes > 0 ? maxUploadBytes : MAX_UPLOAD_BYTES;
    let toUpload = files;
    const partition = partitionUploadFiles(files, perFileMax);
    if (partition.rejected.length > 0) {
      const typeRejected = partition.rejected.filter((r) => r.reason === "type");
      const sizeRejected = partition.rejected.filter((r) => r.reason === "size");
      if (typeRejected.length > 0) {
        msg.error(`不支持的文件类型，已忽略：${typeRejected.map((r) => r.file.name).join("、")}`);
      }
      if (sizeRejected.length > 0) {
        msg.error(
          `单个文件不能超过 ${Math.round(perFileMax / (1024 * 1024))}MB，已忽略：${sizeRejected
            .map((r) => r.file.name)
            .join("、")}`,
        );
      }
      if (partition.accepted.length === 0) return;
      toUpload = partition.accepted;
    }
    // ✅ 上传前累计体积配额预检（与后端 upload_max_total_bytes 同口径）。
    //    不做这一步的话，超出 200MB 的那批文件要等整批上传完成才被后端扣下，
    //    用户白等一轮、且事前对"哪些不会被保存"毫无预期。
    //    本页面有两条上传入口（UploadParseTab + 「① 上传文件保存」按钮），
    //    收敛在此处 = 两个入口一次覆盖。
    const quota = splitByUploadTotalQuota(toUpload, maxUploadTotalBytes);
    if (quota.held.length > 0) {
      const mb = Math.round(
        (Number(maxUploadTotalBytes) > 0 ? Number(maxUploadTotalBytes) : 200 * 1024 * 1024) /
          (1024 * 1024),
      );
      const names = quota.held
        .map((f) => f.name)
        .slice(0, 10)
        .join("、");
      const more = quota.held.length > 10 ? ` 等 ${quota.held.length} 个` : "";
      msg.warning(`单次上传累计不能超过 ${mb}MB，以下文件未上传：${names}${more}`);
      if (quota.accepted.length === 0) return;
      toUpload = quota.accepted;
    }
    // ✅ 上传前「单次文件数」预检（与后端 upload_max_files_per_request 同口径，2026-10-05 补齐）。
    //    与上面两项预检构成完整三段：单文件体积 / 累计体积 / 文件数。缺了这一段时，
    //    超量文件要等整批传完才从响应里得知"未保存"，与另两项体验割裂。
    //    后端语义是「保留前 N 个、其余记入 too_many」，本预检逐字复刻，故不会有
    //    文件被"前端悄悄丢弃" —— 只是提前告知并少传多余部分。
    const byCount = splitByUploadFileCount(toUpload, maxUploadFiles);
    if (byCount.held.length > 0) {
      const cap = Math.floor(
        Number(maxUploadFiles) > 0 ? Number(maxUploadFiles) : MAX_UPLOAD_FILES_FALLBACK,
      );
      const names = byCount.held
        .map((f) => f.name)
        .slice(0, 10)
        .join("、");
      const more = byCount.held.length > 10 ? ` 等 ${byCount.held.length} 个` : "";
      msg.warning(`单次最多上传 ${cap} 个文件，以下文件未上传：${names}${more}`);
      if (byCount.accepted.length === 0) return;
      toUpload = byCount.accepted;
    }
    uploadAbortRef.current?.abort();
    const ac = new AbortController();
    uploadAbortRef.current = ac;

    setUploadingFacts(true);
    setUploadedFiles(toUpload.map((f) => f.name));
    try {
      const { data } = await factsApi.uploadDocuments(toUpload, id, { signal: ac.signal });
      if (ac.signal.aborted) return;
      const savedCount = data.saved_count ?? 0;
      // ✅ 统一口径（2026-09-21）：提示文案唯一入口收敛到
      //    workflowDerived.buildUploadNotice（六类拒绝原因全覆盖：
      //    unsupported/oversize/empty/signature_invalid/too_many/quota_exceeded）。
      //    旧实现在此手写拼装，漏掉累计体积超限（quota_exceeded/quota_files）——
      //    「已保存 3 个文件，其余为什么没了」在界面上完全无解释。
      const notice = buildUploadNotice(data, {
        maxBytes: maxUploadBytes,
        maxTotalBytes: maxUploadTotalBytes,
        maxFiles: maxUploadFiles,
      });
      if (savedCount === 0 && notice.details.length > 0) {
        console.warn("上传被拒明细:", notice.details);
      }
      if (notice.tone === "error") msg.error(notice.text);
      else msg.success(notice.text);
      loadDocuments();
    } catch (e: unknown) {
      // 结构化窄断言：仅读取 axios 错误对象上真实存在的 name / message
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      msg.error(err.message || "上传保存失败");
    } finally {
      if (uploadAbortRef.current === ac) {
        uploadAbortRef.current = null;
        setUploadingFacts(false);
        setUploadedFiles([]);
      }
    }
  };

  // ✅ 分步工作流 ②：批量解析所有待解析文档
  //    force=true 时为「全部重解析」：连同已解析文档一起重新解析，用于
  //    补齐旧版截断内容 / 启用 OCR 后重扫此前失败的扫描件。
  const handleParseDocuments = async (force = false) => {
    if (!id) return;
    // ✅ 防重入：单份解析（OCR 可能数分钟）进行中时不得叠加批量解析，
    //    否则同一文档被两路并发解析，四层产物与版本列互相覆盖。
    //    （组件侧已禁用入口，此处是页面层防御，避免其他调用点绕过）
    if (parsingDocs || parsingDocId) return;
    // ✅ 2026-09-24 B4：与后端 parse-all 选取口径（parsed_markdown 为空，含 failed）
    //    及 UploadParseTab 按钮统一到 computeDocStats.actionableCount；
    //    旧实现用 !text_len 自行计数，与组件按钮的 pendingCount（排除 failed）口径不一致。
    const docStats = computeDocStats(docList);
    if (!force && docStats.actionableCount === 0) {
      // ✅ BUG 修复（2026-09-25，前端早退阻断后端自修复）：actionableCount 为 0
      // 只说明"没有待解析/失败的文档"，**不等于状态列都正确**——存量文档可能
      // parse_status 为 null/pending 而正文早已存在，此时统计条与列表都把它显示
      // 成"已解析"、按钮本应禁用，但后端 parse_status 列永远停在错误值。
      // 旧实现在此直接 return，后端的 parse_status 自修复永远不可达。
      // 现仅当全部状态都确认为 success 时才早退，否则放行请求触发后端修正。
      const hasStaleStatus = docList.some((d) => d.parse_status !== "success");
      if (!hasStaleStatus) {
        msg.info("所有文档均已解析");
        return;
      }
    }
    if (force && docList.length === 0) {
      msg.info("还没有上传资料文件");
      return;
    }
    setParsingDocs(true);
    // ✅ 与上传/SSE 同口径：解析挂 AbortController，卸载/切方案时中断
    parseAbortRef.current?.abort();
    const ac = new AbortController();
    parseAbortRef.current = ac;
    try {
      const { data } = await factsApi.parseAllDocuments(id, schemeProjectId, force, {
        signal: ac.signal,
      });
      if (ac.signal.aborted) return;
      const truncatedCount = data.truncated_count || 0;
      const reconciledCount = data.reconciled_count || 0;
      if (data.failed_count > 0) {
        const details = (data.failed || [])
          .map((f: ParseFailureItem) => `${f.file_name}：${f.reason}`)
          .join("；");
        msg.warning(`解析完成：成功 ${data.parsed} 个，失败 ${data.failed_count} 个。${details}`);
      } else if (truncatedCount > 0) {
        const names = (data.truncated || []).map((f: ParseTruncatedItem) => f.file_name).join("、");
        msg.warning(
          `解析完成：成功 ${data.parsed} 个文档；其中 ${truncatedCount} 个内容超长被截断（${names}），建议拆分后重新上传`,
        );
      } else if (data.parsed === 0 && reconciledCount > 0) {
        // ✅ 2026-09-25：本轮只修正了陈旧的 parse_status（存量文档正文早已存在，
        // 但状态列停在 pending/null）→ 给用户明确解释，否则"什么都没发生"。
        msg.success(`已修正 ${reconciledCount} 份文档的解析状态`);
      } else {
        msg.success(
          `${force ? "重新" : ""}解析完成：成功 ${data.parsed} 个文档` +
            (reconciledCount > 0 ? `，并修正 ${reconciledCount} 份陈旧状态` : ""),
        );
      }
      loadDocuments();
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      msg.error(err.message || "解析失败");
    } finally {
      if (parseAbortRef.current === ac) {
        parseAbortRef.current = null;
        setParsingDocs(false);
      }
    }
  };

  // ✅ 分步工作流 ②：解析单份文档（列表行内按钮）
  // force=true 时强制重解析（用于补齐早期被截断的文档内容）
  // ✅ 单文档解析防重入：parsingDocId 非空时忽略新的单份解析请求
  const handleParseDocument = async (docId: string, fileName: string, force = false) => {
    if (parsingDocId || parsingDocs) return;
    setParsingDocId(docId);
    parseAbortRef.current?.abort();
    const ac = new AbortController();
    parseAbortRef.current = ac;
    try {
      const { data } = await factsApi.parseDocument(docId, force, { signal: ac.signal });
      if (ac.signal.aborted) return;
      if (data.already_parsed) {
        // ✅ 2026-09-25：后端在短路返回时自修复了陈旧 parse_status
        //    （正文已存在但状态仍是 pending）→ 明确告知，避免用户误以为
        //    「点了解析却没反应」。
        msg.info(
          data.reconciled
            ? `「${fileName}」已有解析结果，已修正其解析状态`
            : `「${fileName}」已解析过`,
        );
      } else {
        // ✅ F6（2026-09-26）：与批量解析提示口径一致，即时告知截断/告警，
        //    避免用户在点「解析」那一刻拿不到内容可能不完整的提示。
        const cut = data.truncated ? "（内容可能被截断，请查看解析提示）" : "";
        const warn =
          data.warnings && data.warnings.length ? `（${data.warnings.length} 条解析提示）` : "";
        msg.success(
          `「${fileName}」${force ? "重新" : ""}解析完成（${data.text_len} 字）${cut}${warn}`,
        );
      }
      loadDocuments();
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      msg.error(err.message || "解析失败");
      // ✅ 失败/超时后补一次列表刷新：后端可能已落库，避免 UI 与后端不同步
      loadDocuments();
    } finally {
      if (parseAbortRef.current === ac) {
        parseAbortRef.current = null;
        setParsingDocId(null);
      }
    }
  };

  /** 查看解析内容预览（前 N 字，不下载整份文件） */
  const handlePreviewDocument = async (doc: DocRecord) => {
    if (!doc?.id) return;
    setPreviewDoc(doc);
    setPreviewText("");
    setPreviewData(null);
    setPreviewLoading(true);
    // 重置四层质量概览，随后异步拉取（不阻断正文预览）
    setPipelineStatus(null);
    setPipelineError(null);
    setPipelineLoading(true);
    try {
      const { data } = await factsApi.previewDocument(doc.id);
      if (!data.is_parsed) {
        msg.warning(data.message || "该文档尚未解析");
      }
      setPreviewText(data.preview || "");
      setPreviewData(data);
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "获取预览失败");
    } finally {
      setPreviewLoading(false);
    }
    loadPipelineStatus(doc.id);
  };

  /** 拉取四层解析质量概览（status + completeness 合并；失败非阻断，仅降级为告警） */
  const loadPipelineStatus = async (docId: string) => {
    if (!docId) return;
    setPipelineLoading(true);
    setPipelineError(null);
    try {
      const { data } = await docPipelineApi.status(docId);
      // status 已含 completeness 快照；quality_score 缺省时再取一次报告补全
      setPipelineStatus(data);
    } catch (e: unknown) {
      const err = e as { message?: string };
      setPipelineError(err?.message || "解析质量信息读取失败");
    } finally {
      setPipelineLoading(false);
    }
  };

  /** 刷新完整性校验并回填质量分；成功返回 true，失败返回 false，由调用方决定提示 */
  const refreshPipelineQuality = async (docId: string): Promise<boolean> => {
    try {
      const { data } = await docPipelineApi.completeness(docId, true);
      const q = typeof data?.quality_score === "number" ? data.quality_score : undefined;
      setPipelineStatus((prev) =>
        prev
          ? { ...prev, quality_score: q, completeness: data?.completeness ?? prev.completeness }
          : prev,
      );
      return true;
    } catch {
      /* 质量刷新失败：正文/状态仍在，提示交由调用方处理，避免"失败却提示成功" */
      return false;
    }
  };

  /** 预览弹窗内的四层管线动作：调用对应 API → 反馈 → 重新拉取状态 */
  const handlePipelineAction = async (action: PipelineActionKey) => {
    const docId = previewDoc?.id;
    if (!docId || pipelineAction) return;
    setPipelineAction(action);
    try {
      if (action === "reparse") {
        const { data } = await docPipelineApi.reparse(docId, "force");
        msg.success(data?.skipped ? "文件未变更，无需重解析" : "已重新解析并刷新四层产物");
      } else if (action === "syncExtractions") {
        const { data } = await docPipelineApi.syncExtractions(docId);
        msg.success(`提取层已物化（${(data?.written_types || []).length} 类）`);
      } else if (action === "crossCheck") {
        const { data } = await docPipelineApi.crossCheck(docId);
        msg.success(`交叉校验完成（冲突 ${data?.stored_conflicts ?? 0}）`);
      } else if (action === "refreshQuality") {
        const ok = await refreshPipelineQuality(docId);
        if (ok) {
          msg.success("质量分已刷新");
        } else {
          msg.error("质量分刷新失败，请稍后重试");
        }
      }
      // 动作可能改变四层产物 → 重新拉取权威状态
      if (action !== "refreshQuality") await loadPipelineStatus(docId);
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err?.message || "操作失败");
    } finally {
      setPipelineAction(null);
    }
  };

  /** 修改文档分类（招标文件 / 合同文件 / ...，影响提取优先级） */
  const handleCategoryChange = async (docId: string, category: string) => {
    try {
      await factsApi.updateDocumentCategory(docId, category);
      setDocList((prev) =>
        prev.map((d) => (d.id === docId ? { ...d, doc_category: category } : d)),
      );
      msg.success("分类已更新");
    } catch (e: unknown) {
      // ✅ 2026-09-24 T3：更新失败（超时/网络中断但服务端可能已落库等）时，
      //    重新拉取服务端真值，避免受控 Select 与后端分类发生漂移。
      const err = e as { message?: string };
      msg.error(err.message || "分类更新失败");
      loadDocuments();
    }
  };

  /** 批量确认所有未确认的事实 */
  const handleBatchResolve = async () => {
    if (!id) return;
    const unresolved = factsSummary?.unresolved || 0;
    const simulated = factsSummary?.simulated || 0;
    if (unresolved === 0) {
      msg.info("没有待确认的事实");
      return;
    }
    // ✅ 文案统一走 buildBatchResolveCopy：修复旧文案把「模拟值 ∩ 未确认」
    //    交集重复计数（unresolved + simulated），且空态判定误伤已确认的模拟值。
    const copy = buildBatchResolveCopy({
      unresolved,
      simulated,
      conflicts: factsSummary?.conflicts || 0,
    });
    modal.confirm({
      title: "批量确认所有事实",
      content: (
        <div>
          <div>{copy.headline}</div>
          {copy.simulatedNote && <div style={{ marginTop: 6 }}>{copy.simulatedNote}</div>}
          {copy.conflictNote && (
            <div style={{ marginTop: 6, color: "#d46b08" }}>{copy.conflictNote}</div>
          )}
        </div>
      ),
      okText: "全部确认",
      cancelText: "取消",
      type: copy.confirmType,
      onOk: async () => {
        try {
          const { data } = await factsApi.batchResolve(id);
          const copy = buildBatchResolveResultCopy(data);
          if (copy.tone === "warning") msg.warning(copy.text);
          else msg.success(copy.text);
          await loadFacts();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "确认失败");
        }
      },
    });
  };

  /**
   * ✅ G3（2026-10-04）：批量解除「来源已变化」过期标记。
   *
   * 背景：后端 `_mark_project_facts_stale` 在项目资料重新解析/删除时会把**整个
   * 项目**的事实一次性置 is_stale=1，而解除入口此前只有「改值」和「裁决矛盾」
   * 两条（都要求值必须先变），`resolve` / `batch-resolve` 对 stale 行还直接跳过。
   * 结果是：用户核对过、取值并未变化，却被永久排除在正文注入与导出之外，红色
   * 「来源过期 N」Tag 也消不掉 —— 只能逐条把值改成别的再改回来，或重跑一次
   * AI 提取（真实计费）。
   *
   * 语义只表达「我已核对，这些值仍然有效」；模拟值/矛盾值由后端闸门继续拦住
   * （只清 stale、不置已确认），故响应里的 `gated` 需要显式提示。
   */
  const handleAckStaleAll = async () => {
    if (!id) return;
    const stale = factsSummary?.stale || 0;
    if (stale === 0) {
      msg.info("没有来源过期的事实");
      return;
    }
    modal.confirm({
      title: "确认这些事实仍然有效",
      content: (
        <div>
          <div>
            将把当前方案可见范围内 <strong>{stale}</strong> 条「来源已变化」的事实 标记为
            <strong>已人工核对、仍然有效</strong>，恢复其注入正文与导出。
          </div>
          <div style={{ marginTop: 6, color: "#d46b08" }}>
            仅在你已核对原始资料、确认取值未变时使用；若资料确已更新，请改为重新提取。
          </div>
          <div style={{ marginTop: 6, color: "#888" }}>
            模拟值与存在矛盾的事实仍会保持未确认，需逐条处理。
          </div>
        </div>
      ),
      okText: "确认仍有效",
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await factsApi.batchAckStale(id);
          const changed = Number(data?.changed) || 0;
          const gated = Number(data?.gated) || 0;
          if (changed === 0) {
            msg.info("没有需要解除的过期标记");
          } else if (gated > 0) {
            msg.warning(`已解除 ${changed} 条；其中 ${gated} 条为模拟值/矛盾值，仍需逐条处理`);
          } else {
            msg.success(`已解除 ${changed} 条过期标记`);
          }
          await loadFacts();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "解除失败");
        }
      },
    });
  };

  /**
   * ✅ G3（2026-10-04）：单条解除「来源已变化」。供用户逐条核对后确认，
   * 避免只能「一次性全确认」而跳过复核。后端对模拟值/矛盾值仍保留闸门
   * （只清 stale、不置已确认），返回的 blocked_reason 原样提示。
   */
  const handleAckStaleOne = async (factId: string, title?: string) => {
    if (!id || !factId) return;
    const sid = id;
    // 复用列表加载的中止控制器：切方案时一并取消「补丁请求」和随后的「列表刷新」，
    // 避免旧方案的 stale 回执在切走后触发对已离开方案的刷新。
    factsListAbortRef.current?.abort();
    const ac = new AbortController();
    factsListAbortRef.current = ac;
    try {
      const { data } = await factsApi.ackStale(factId, sid, { signal: ac.signal });
      // 切走方案后不弹提示、不刷新（与 loadFacts 一致）
      if (sid !== currentSchemeIdRef.current || ac.signal.aborted) return;
      if (data?.blocked_reason) {
        msg.warning(data.blocked_reason as string);
      } else if (data?.idempotent) {
        // ✅ 2026-10-06：后端把 changed 由 bool 统一为影响行数 int（与
        //    batch_ack_stale / batch_resolve 同形），并加法式补 idempotent。
        //    旧写法 `data?.changed === false` 是严格布尔比较，后端一改形状就
        //    静默失效（tsc 在接入 types/facts.ts 后当场报出）。
        msg.info("该事实未被标记为过期");
      } else {
        msg.success(`已确认「${title || "该事实"}」仍有效`);
      }
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      if (sid === currentSchemeIdRef.current) msg.error(err.message || "解除失败");
    }
  };

  /** 删除已上传的项目资料文档 */
  const handleDeleteDocument = async (docId: string, fileName: string) => {
    modal.confirm({
      title: "确认删除文件",
      content: `删除后，从「${fileName}」中提取的全局事实不会自动清除，但后续 AI 生成将不再引用该文件。确定删除？`,
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await factsApi.deleteDocument(docId);
          msg.success("文件已删除");
          loadDocuments();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "删除失败");
        }
      },
    });
  };

  return {
    handleUploadDocuments,
    handleParseDocuments,
    handleParseDocument,
    handlePreviewDocument,
    handlePipelineAction,
    handleCategoryChange,
    handleBatchResolve,
    handleAckStaleAll,
    handleAckStaleOne,
    handleDeleteDocument,
  };
}

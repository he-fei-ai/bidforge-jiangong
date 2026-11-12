import { useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import type { ModalFuncProps } from "antd";
import {
  factsApi,
  docPipelineApi,
  PREVIEW_TIMEOUT_MS,
  API_GLOBAL_TIMEOUT_MS,
  REPARSE_TIMEOUT_MS,
  SYNC_EXTRACTIONS_TIMEOUT_MS,
  CROSS_CHECK_TIMEOUT_MS,
} from "../../api";
import { computeDocStats, buildUploadNotice } from "../../utils/workflowDerived";
import type { DocRecord, PreviewError } from "../../utils/workflowDerived";
import type { FactsSummary } from "../../types/facts";
import {
  splitByUploadTotalQuota,
  splitByUploadFileCount,
  partitionUploadFiles,
  resolveMaxUploadBytes,
  MAX_UPLOAD_FILES_FALLBACK,
} from "../../utils/uploadAccept";
import {
  buildBatchAckStaleResultCopy,
  buildBatchResolveCopy,
  buildBatchResolveResultCopy,
} from "./factsShared.ts";
import type {
  DocumentPipelineStatusLike,
  PipelineActionKey,
} from "../../components/DocumentPipelineSummary";

// =========================================================================
// ✅ any 清理专项（2026-11-02）：本域消费的后端 REST 响应契约。
//    字段按 hook 内实际读取推导，未知字段一律 ? 修饰，不虚构。
// =========================================================================

/** 窄断言 axios 错误对象上真实存在的字段（不虚构属性） */
function errFields(e: unknown) {
  return e as { code?: string; name?: string; message?: string };
}

/** 用户主动取消 / 组件卸载中断：不当作失败提示 */
function isCanceledError(e: unknown): boolean {
  const err = errFields(e);
  return err?.code === "ERR_CANCELED" || err?.name === "CanceledError" || err?.name === "AbortError";
}

/** axios 超时（ECONNABORTED）或同类 timeout 文案 */
function isTimeoutError(e: unknown): boolean {
  const err = errFields(e);
  return err?.code === "ECONNABORTED" || /timeout of \d+ms exceeded/i.test(err?.message || "");
}

/**
 * 请求错误文案：超时统一中文化并写明「哪一层 + 多少秒」（秒数取各 API 定义处实际值），
 * 其余错误保留后端 detail 原文，不透传 axios 英文兜底。
 */
function requestErrorText(e: unknown, label: string, timeoutSeconds: number, fallback: string) {
  if (isTimeoutError(e)) return `${label}请求超时（${timeoutSeconds} 秒），请稍后重试`;
  return errFields(e)?.message || fallback;
}

/**
 * 四层管线动作失败文案的元信息：label + 秒数。
 *
 * 秒数一律由 `api/index.ts` 的请求超时常量 /1000 推导，不写字面量 ——
 * 与预览超时同一纪律：两处写死的数字，改一处不会有任何用例变红，
 * 用户看到的就是假话。
 * 对应关系：reparse→REPARSE_TIMEOUT_MS、syncExtractions→SYNC_EXTRACTIONS_TIMEOUT_MS、
 * crossCheck→CROSS_CHECK_TIMEOUT_MS、refreshQuality→completeness 的 API_GLOBAL_TIMEOUT_MS。
 */
export const PIPELINE_ACTION_META: Record<
  PipelineActionKey,
  { label: string; seconds: number }
> = {
  reparse: { label: "重新解析", seconds: Math.round(REPARSE_TIMEOUT_MS / 1000) },
  syncExtractions: { label: "物化提取层", seconds: Math.round(SYNC_EXTRACTIONS_TIMEOUT_MS / 1000) },
  crossCheck: { label: "交叉校验", seconds: Math.round(CROSS_CHECK_TIMEOUT_MS / 1000) },
  refreshQuality: { label: "刷新质量", seconds: Math.round(API_GLOBAL_TIMEOUT_MS / 1000) },
};

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
  /** ✅ R191（C-F6）：截断原因（「字数上限」/「PDF 页数」/「表格行数」），
   *  归因文案不再一律写成「内容超长」。 */
  reason?: string;
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
  /** 预览请求的中止通道：ref 由页面持有并登记进卸载 cleanup（与上传/解析同口径） */
  previewAbortRef: MutableRefObject<AbortController | null>;
  parsingDocs: boolean;
  setParsingDocs: (v: boolean) => void;
  parsingDocId: string | null;
  setParsingDocId: (v: string | null) => void;
  docList: DocRecord[];
  /**
   * ✅ R210（2026-11-10）：补可选 ``skipAbortPrev`` 参数（**轮询专用**）。
   * 默认（用户手动刷新 / 常规加载）仍为「后发者胜」：abort 上一次在飞请求；
   * 轮询入口传 ``true`` ⇒ 不打断用户自己点的刷新（R208 注释承诺、本轮落地）。
   * 传参类型对 `() => void` 的消费方**向后兼容**（可选参数可省略）。
   */
  loadDocuments: (skipAbortPrev?: boolean) => void | Promise<void>;
  // 预览 / 四层管线
  setPreviewDoc: (v: DocRecord | null) => void;
  setPreviewText: (v: string) => void;
  setPreviewData: (v: DocPreviewData | null) => void;
  setPreviewLoading: (v: boolean) => void;
  /** 预览失败状态位（超时 / 请求失败）；null 表示未失败 */
  setPreviewError: (v: PreviewError | null) => void;
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
    previewAbortRef,
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
    setPreviewError,
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

  // ✅ 2026-11-07 竞态守卫：预览 / 四层状态是两次独立异步请求，
  //    用户快速切换文档时先发请求的慢响应会覆盖后发结果（标题是 B、正文是 A）。
  //    以「递增请求序号 + 当前预览文档 id」双条件判定，只允许最新请求写回状态。
  const previewReqIdRef = useRef(0);
  const pipelineReqIdRef = useRef(0);
  const pipelineDocIdRef = useRef<string | null>(null);
  // ✅ R191（C-F9）：上传/解析链的防重入此前全读闭包 state——React 18 批处理下
  //    同一 tick 内二次调用时 state 仍为 false，理论上可双发。补 ref 一行防线
  //    （同步读写，同 tick 立即可见），与生成链 genSeqRef 的世代化防护对齐。
  const uploadInFlightRef = useRef(false);
  const parseInFlightRef = useRef(false);
  // ✅ R203（R191 遗留 G6-SSE 最小承接）：批量解析进行中轻量轮询文档列表
  //    （3s）——后端 parse-all 逐份落库、逐条 commit，列表行 parse_status 随
  //    轮询逐份翻转（用户可见逐份进度）；超时/断线后 UI 也不再与后端脱节
  //    （与 catch 补偿刷新同向）。事件级实时进度（SSE 化）维持 R176 排期登记。
  //    ref 存最新引用：loadDocuments 每次 render 重建，effect 只挂载一次，
  //    经 ref 间接调用避免过期闭包。
  const loadDocumentsRef = useRef(loadDocuments);
  loadDocumentsRef.current = loadDocuments;
  // ✅ R208 修复（轮询与列表自中止互锁）：``loadDocuments``（useWorkbenchDataLoaders）
  //    每次调用**先 abort 上一次在飞的列表请求**（:57）——这与「刷新」按钮的
  //    语义一致（后发者胜）。但 R203 FE-9 的 3s 轮询也走同一个入口，于是：
  //    列表请求耗时 >3s 时，每个轮询 tick 都会把上一个 tick 的请求 abort 掉，
  //    请求永远走不完 → 异常被 ``ac.signal.aborted`` 静默吞掉（:70）
  //    → **列表永不更新**，FE-9 的「逐份进度可见」在慢网/大项目下完全失效，
  //    用户看到的仍是「待解析」；用户自己点的「刷新」也会被自家轮询打断。
  //    修法：轮询走**独立**的加载入口，不与交互刷新共用中止通道——
  //    在轮询窗口内只保留「上一次轮询结果」，不打断正在飞行的轮询请求。
  //    实现：给 loadDocuments 增加可选 ``skipAbortPrev`` 参数。
  //    ✅ R210（2026-11-10）：该参数本轮**真正落地**（此前只有注释、代码未实现）
  //    —— 轮询入口传 ``skipAbortPrev=true`` ⇒ 轮询**不再 abort 用户自己点的
  //    「刷新」**；用户手动刷新仍保持「后发者胜」的默认语义。
  const loadDocumentsPollRef = useRef(loadDocuments);
  loadDocumentsPollRef.current = loadDocuments;
  const pollInFlightRef = useRef(false);
  useEffect(() => {
    if (!parsingDocs) return;
    const t = setInterval(() => {
      // 上一轮轮询仍在飞 → 本轮跳过（不再叠加请求，也不 abort 上一轮）
      if (pollInFlightRef.current) return;
      pollInFlightRef.current = true;
      // skipAbortPrev=true：轮询不得打断在飞的用户刷新请求（R210）
      void Promise.resolve(loadDocumentsPollRef.current(true))
        .catch(() => undefined)
        .finally(() => {
          pollInFlightRef.current = false;
        });
    }, 3000);
    return () => {
      clearInterval(t);
      pollInFlightRef.current = false;
    };
  }, [parsingDocs]);
  // ✅ 真正的取消通道（ref 由页面持有，见 DocumentWorkflowDeps.previewAbortRef）：
  //    关闭弹窗 / 切换文档时中断在途预览（不只是丢弃结果）；页面卸载时由
  //    SchemeWorkbenchPage 的卸载 cleanup 统一 abort。

  // ✅ 分步工作流 ①：上传保存（仅存文件，不解析不提取）
  const handleUploadDocuments = async (files: File[]) => {
    if (!id || files.length === 0) return;
    if (uploadingFacts || uploadInFlightRef.current) {
      // ✅ R208 修复（假动作）：此前静默 return —— 用户点击上传后
      //    「完全没反应」（无 toast / 无 loading）。R203 FE-1 的门禁口径
      //    修好了一部分入口（按钮 disabled），但仍有可达路径绕过组件层
      //    （FactsTab「① 上传文件保存」整批直传本函数、并发双击同 tick）。
      //    现给出明确反馈，行为不变（仍不发起请求）。
      msg.info(uploadingFacts ? "文件正在上传，请稍候" : "上传正在进行中，请勿重复提交");
      return;
    }
    // ✅ R203 修复（P1）：置位从函数头迁到三段预检全部早退之后（与下方
    //    parse 侧 C-F9 形态对齐）——旧实现置位后有三段预检各含一个裸 return
    //    早退（全被拒/超配额/超数量且无一通过），全部位于 try 块之外，
    //    finally 的复位不覆盖 → ref 永久为 true，此后两个上传入口的所有
    //    请求被防重入检查静默吞掉（无提示、无 loading），上传入口永久卡死。
    //    FactsTab 的「① 上传文件保存」按钮不做组件级预检、整批直传本函数，
    //    正是三段预检存在的设计前提（选 1 个超限文件即可触发）。
    // ✅ 单文件硬校验（2026-10-05）：`accept` 属性只过滤文件选择框，拖拽与
    //    「① 上传文件保存」按钮这条入口可绕过。不做这一步的话，超限 / 非法
    //    扩展名的文件会被整批发给后端再逐个拒绝 —— 用户白等一轮上传，
    //    且与 import Tab 的提示口径分叉（那边是先拦、根本不发）。
    //    收敛在此处 = 两个入口一次覆盖（import Tab 已先过滤，进来 rejected 为空）。
    // ✅ R191（C-F7）：收敛到唯一口径 resolveMaxUploadBytes（挡 Infinity/NaN）。
    const perFileMax = resolveMaxUploadBytes(
      typeof maxUploadBytes === "number" ? maxUploadBytes : undefined,
    );
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
    // ✅ R203（FE-1）：置位放在全部预检早退之后（早退路径不经 finally）。
    uploadInFlightRef.current = true;
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
      // ✅ R203 修复（R191 F1 后半程）：同名替换时后端回传 stale_facts/note
      //    （「请重新执行『解析』与『AI 提取事实』」重提指引）——此前只有
      //      parse/parse-all 分支消费 note，上传分支漏网，指引在 toast 里
      //      丢失，旧事实注入生成链路的窗口期用户零感知。
      const uploadNote = typeof data.note === "string" && data.note ? `；${data.note}` : "";
      if (notice.tone === "error") msg.error(notice.text + uploadNote);
      else msg.success(notice.text + uploadNote);
      loadDocuments();
      // ✅ R225（2026-11-12 · D2）：同名替换已把项目事实批量置 stale
      //    （后端回传 stale_facts）—— 事实列表/概览不回读则「来源过期」
      //    徽标与「全部就绪」入口继续显示旧态，正文生成的注入门控实际
      //    已把这些事实排除（用户零感知的静默缺料）。
      void loadFacts();
    } catch (e: unknown) {
      // 结构化窄断言：仅读取 axios 错误对象上真实存在的 name / message
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      // API 超时 120s（api/index.ts uploadDocuments 配置）
      msg.error(requestErrorText(e, "上传", 120, "上传保存失败"));
    } finally {
      if (uploadAbortRef.current === ac) {
        uploadAbortRef.current = null;
        setUploadingFacts(false);
        setUploadedFiles([]);
      }
      // ✅ R191（C-F9）：无论 abort 归属如何都要解除在飞标记（abort 抢占时
      //    state 不复位但请求确实已结束，漏清会永久卡死上传入口）。
      uploadInFlightRef.current = false;
    }
  };

  // ✅ 分步工作流 ②：批量解析所有待解析文档
  //    force=true 时为「全部重解析」：连同已解析文档一起重新解析，用于
  //    补齐旧版截断内容 / 启用 OCR 后重扫此前失败的扫描件。
  const handleParseDocuments = async (force = false) => {
    // ✅ R191（C-F9）：见 uploadInFlightRef 注释（同 tick 双发防线）。
    if (!id) return;
    // ✅ 防重入：单份解析（OCR 可能数分钟）进行中时不得叠加批量解析，
    //    否则同一文档被两路并发解析，四层产物与版本列互相覆盖。
    //    （组件侧已禁用入口，此处是页面层防御，避免其他调用点绕过）
    if (parsingDocs || parsingDocId || parseInFlightRef.current) {
      // ✅ R208 修复（假动作）：见上传侧同类注释——静默 return 让用户
      //    点击「解析全部」毫无反馈。给出明确提示，行为不变。
      msg.info(
        parsingDocId ? "该文档正在解析中，请稍候" : "解析正在进行中，请勿重复提交",
      );
      return;
    }
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
    // ✅ R191（C-F9）：置位放在全部早退之后（早退路径不经 finally，
    //    提前置位会在「所有文档均已解析」后永久卡死批量解析入口）。
    parseInFlightRef.current = true;
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
      // ✅ 2026-11-08：后端在「本轮新解析 / 重解析改变了资料全集」时回传 note
      //    （提示「请重新执行 AI 提取事实」）与 stale_facts 计数，此前前端完全
      //    忽略这两个字段 → 用户拿到「解析成功」却不知道旧事实已失效、必须重提。
      //    现随结果一并提示，补齐前后端接口一致性。
      const note: string | undefined =
        typeof data.note === "string" && data.note ? data.note : undefined;
      let detail = "";
      let isWarning = false;
      if (data.failed_count > 0) {
        const details = (data.failed || [])
          .map((f: ParseFailureItem) => `${f.file_name}：${f.reason}`)
          .join("；");
        detail = `解析完成：成功 ${data.parsed} 个，失败 ${data.failed_count} 个。${details}`;
        isWarning = true;
      } else if (truncatedCount > 0) {
        const names = (data.truncated || []).map((f: ParseTruncatedItem) => f.file_name).join("、");
        // ✅ R191（C-F6）：reason 有值时按真实原因归因（PDF 截页类「拆分重传」
        //    指引无效，应按 reason 提示），无值回落旧文案（向后兼容）。
        const reasons = [
          ...new Set(
            (data.truncated || []).map((f: ParseTruncatedItem) => (f.reason || "").trim()).filter(Boolean),
          ),
        ].join("、");
        detail =
          `解析完成：成功 ${data.parsed} 个文档；其中 ${truncatedCount} 个被截断（${names}）` +
          (reasons ? `，原因：${reasons}` : "，内容超长被截断，建议拆分后重新上传");
        isWarning = true;
      } else if (data.parsed === 0 && reconciledCount > 0) {
        // ✅ 2026-09-25：本轮只修正了陈旧的 parse_status（存量文档正文早已存在，
        // 但状态列停在 pending/null）→ 给用户明确解释，否则"什么都没发生"。
        detail = `已修正 ${reconciledCount} 份文档的解析状态`;
      } else if (data.parsed === 0 && truncatedCount === 0 && typeof data.message === "string" && data.message) {
        // ✅ R191（C-F5）：列表陈旧（前端 actionable>0）而服务端实际无待解析
        //    （多端操作/另一设备已解析完）时，后端早退分支的 message 才是
        //    真相——旧实现落进 else 分支误导性显示「成功 0 个文档」。
        detail = data.message;
      } else {
        detail =
          `${force ? "重新" : ""}解析完成：成功 ${data.parsed} 个文档` +
          (reconciledCount > 0 ? `，并修正 ${reconciledCount} 份陈旧状态` : "");
      }
      if (note) detail += `；${note}`;
      if (isWarning) msg.warning(detail);
      else msg.success(detail);
      loadDocuments();
      // ✅ R225（D2）：parse-all 完成后旧事实全体 stale —— 同步回读事实
      //    列表/概览，消除「徽标仍旧态 vs 注入门控已排除」的口径分叉。
      void loadFacts();
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      // 批量解析 API 超时 600s（api/index.ts parseAllDocuments 配置）
      msg.error(requestErrorText(e, "批量解析", 600, "解析失败"));
      // ✅ R203 修复（与单份解析口径对齐）：parse-all 是逐份落库、逐条 commit，
      //    失败/超时时后端往往已成功落库多份——批量路径恰恰最需要补偿刷新，
      //    否则列表仍显「待解析」，用户改点「全部重解析」会让已解析文档重复
      //    OCR（分钟级空转）。
      loadDocuments();
      // ✅ R225（D2）：失败补偿同理覆盖已落库份的 stale 回读。
      void loadFacts();
    } finally {
      if (parseAbortRef.current === ac) {
        parseAbortRef.current = null;
        setParsingDocs(false);
      }
      parseInFlightRef.current = false;
    }
  };

  // ✅ 分步工作流 ②：解析单份文档（列表行内按钮）
  // force=true 时强制重解析（用于补齐早期被截断的文档内容）
  // ✅ 单文档解析防重入：parsingDocId 非空时忽略新的单份解析请求
  const handleParseDocument = async (docId: string, fileName: string, force = false) => {
    if (parsingDocId || parsingDocs || parseInFlightRef.current) {
      // ✅ R208 修复（假动作）：单份解析 guard 此前同样静默 return。
      msg.info(
        parsingDocId ? "该文档正在解析中，请稍候" : "解析正在进行中，请勿重复提交",
      );
      return;
    }
    parseInFlightRef.current = true;
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
        // ✅ R203 修复（failed 重试链）：后端短路分支对「上次解析失败」的
        //    文档回传 parse_status='failed' + note 重试指引（R193）——旧实现
        //    一律显示「已解析过」，指引在最后一米丢失，用户反复点普通解析
        //    永远无法重试（正文非空的 failed 文档不在 parse-all 选取口径内，
        //    行内按钮是唯一重试入口）。
        const shortNote = typeof data.note === "string" && data.note ? data.note : undefined;
        if (data.parse_status === "failed") {
          msg.warning(`「${fileName}」上次解析失败：${shortNote || "请使用「强制重新解析」重试"}`);
        } else if (data.reconciled) {
          msg.info(`「${fileName}」已有解析结果，已修正其解析状态`);
        } else {
          msg.info(`「${fileName}」已解析过`);
        }
      } else {
        // ✅ F6（2026-09-26）：与批量解析提示口径一致，即时告知截断/告警，
        //    避免用户在点「解析」那一刻拿不到内容可能不完整的提示。
        const cut = data.truncated ? "（内容可能被截断，请查看解析提示）" : "";
        const warn =
          data.warnings && data.warnings.length ? `（${data.warnings.length} 条解析提示）` : "";
        // ✅ R191（C-F2）：单份解析响应的 note/stale_facts 此前零消费
        //    （R181 只收口了 parse-all 批量分支）——重解析/首次解析成功后
        //    后端已把旧事实置 stale，用户必须回「AI 提取事实」重提，否则
        //    下游目录/正文继续用残缺事实。现与批量口径一致拼入 note。
        const note = typeof data.note === "string" && data.note ? `；${data.note}` : "";
        msg.success(
          `「${fileName}」${force ? "重新" : ""}解析完成（${data.text_len} 字）${cut}${warn}${note}`,
        );
      }
      loadDocuments();
      // ✅ R225（D2）：单份解析完成同样把旧事实置 stale —— 回读事实列表/
      //    概览（R191 C-F2 只消费了 note 文案，列表回读一直缺位）。
      void loadFacts();
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      // 单份解析 API 超时 300s（api/index.ts parseDocument 配置）
      msg.error(requestErrorText(e, "解析", 300, "解析失败"));
      // ✅ 失败/超时后补一次列表刷新：后端可能已落库，避免 UI 与后端不同步
      loadDocuments();
    } finally {
      if (parseAbortRef.current === ac) {
        parseAbortRef.current = null;
        setParsingDocId(null);
      }
      parseInFlightRef.current = false;
    }
  };

  /** 关闭预览弹窗：中断在途请求并清空预览/管线状态（取消 = 不再等，而非仅丢弃结果） */
  const handleCancelPreview = () => {
    // 先让序号失效，再 abort：即使中断异常同步抛回 catch，也已不会写状态
    previewReqIdRef.current += 1;
    pipelineReqIdRef.current += 1;
    previewAbortRef.current?.abort();
    previewAbortRef.current = null;
    // ✅ R191（C-F4）：清掉管线动作归属——弹窗已关闭，之后的 reparse/质量
    //    刷新回执不再写状态、不再多发 status/preview 请求（取消语义
    //    「取消 = 不再等」在管线动作路径上同步成立）。
    pipelineDocIdRef.current = null;
    setPreviewDoc(null);
    setPreviewText("");
    setPreviewData(null);
    setPreviewError(null);
    setPreviewLoading(false);
    setPipelineStatus(null);
    setPipelineError(null);
    setPipelineLoading(false);
  };

  /** 查看解析内容预览（前 N 字，不下载整份文件） */
  const handlePreviewDocument = async (doc: DocRecord) => {
    if (!doc?.id) return;
    // ✅ 竞态守卫：本次请求的序号 + 当前预览文档，晚到的旧响应一律丢弃
    const reqId = ++previewReqIdRef.current;
    // ✅ 取消通道：切换文档时中断上一份仍在途的预览请求
    previewAbortRef.current?.abort();
    const ac = new AbortController();
    previewAbortRef.current = ac;
    pipelineDocIdRef.current = doc.id;
    setPreviewDoc(doc);
    setPreviewText("");
    setPreviewData(null);
    setPreviewError(null);
    setPreviewLoading(true);
    // 重置四层质量概览，随后异步拉取（不阻断正文预览）
    setPipelineStatus(null);
    setPipelineError(null);
    setPipelineLoading(true);
    try {
      const { data } = await factsApi.previewDocument(doc.id, 5000, { signal: ac.signal });
      if (reqId !== previewReqIdRef.current) return;
      if (!data.is_parsed) {
        msg.warning(data.message || "该文档尚未解析");
      }
      setPreviewText(data.preview || "");
      setPreviewData(data);
    } catch (e: unknown) {
      // 已切到别的文档 / 用户取消：不弹错误、不写状态（避免「标题是 B、提示是 A」）
      if (reqId !== previewReqIdRef.current || isCanceledError(e)) return;
      // ✅ 秒数与请求实际 timeout 共用同一常量（api/index.ts PREVIEW_TIMEOUT_MS），
      //    避免「改接口超时而文案不动」的两处真相源
      const text = requestErrorText(
        e,
        "预览",
        Math.round(PREVIEW_TIMEOUT_MS / 1000),
        "获取预览失败",
      );
      // ✅ 失败状态位：让空态显示「失败/超时」，而不是复用「尚未解析」
      setPreviewError({ kind: isTimeoutError(e) ? "timeout" : "request", message: text });
      msg.error(text);
    } finally {
      if (previewAbortRef.current === ac) previewAbortRef.current = null;
      if (reqId === previewReqIdRef.current) setPreviewLoading(false);
    }
    // ✅ 页面卸载等「只 abort、不递增序号」的取消路径：不再追拉管线状态，
    //    否则会在已卸载组件上发起第二次请求并 setState。
    if (ac.signal.aborted) return;
    if (reqId !== previewReqIdRef.current) return;
    loadPipelineStatus(doc.id);
  };

  /** 拉取四层解析质量概览（status + completeness 合并；失败非阻断，仅降级为告警） */
  const loadPipelineStatus = async (docId: string) => {
    if (!docId) return;
    // 只允许「当前预览文档」的状态写回；同文档重入时以最新序号为准
    if (pipelineDocIdRef.current !== docId) return;
    const reqId = ++pipelineReqIdRef.current;
    setPipelineLoading(true);
    setPipelineError(null);
    try {
      const { data } = await docPipelineApi.status(docId);
      if (reqId !== pipelineReqIdRef.current || pipelineDocIdRef.current !== docId) return;
      // status 已含 completeness 快照；quality_score 缺省时再取一次报告补全
      setPipelineStatus(data);
    } catch (e: unknown) {
      if (reqId !== pipelineReqIdRef.current || pipelineDocIdRef.current !== docId) return;
      setPipelineError(
        requestErrorText(
          e,
          "解析质量信息",
          Math.round(API_GLOBAL_TIMEOUT_MS / 1000),
          "解析质量信息读取失败",
        ),
      );
    } finally {
      if (reqId === pipelineReqIdRef.current) setPipelineLoading(false);
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
        // ✅ 2026-11-07 缓存失效修复：重解析会重建 parsed_markdown，
        //    旧实现只刷新四层状态、不刷新正文 → 弹窗继续显示重解析前的旧内容。
        try {
          const { data: fresh } = await factsApi.previewDocument(docId);
          if (pipelineDocIdRef.current === docId) {
            setPreviewText(fresh.preview || "");
            setPreviewData(fresh);
          }
        } catch {
          if (pipelineDocIdRef.current === docId) {
            msg.warning("重解析已完成，但预览正文刷新失败，请关闭后重新打开");
          }
        }
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
      const meta = PIPELINE_ACTION_META[action];
      msg.error(requestErrorText(e, meta.label, meta.seconds, "操作失败"));
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
      msg.error(
        requestErrorText(
          e,
          "分类更新",
          Math.round(API_GLOBAL_TIMEOUT_MS / 1000),
          "分类更新失败",
        ),
      );
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
          msg.error(
            requestErrorText(
              e,
              "批量确认",
              Math.round(API_GLOBAL_TIMEOUT_MS / 1000),
              "确认失败",
            ),
          );
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
          const copy = buildBatchAckStaleResultCopy(data);
          if (copy.tone === "success") msg.success(copy.text);
          else if (copy.tone === "warning") msg.warning(copy.text);
          else msg.info(copy.text);
          await loadFacts();
        } catch (e: unknown) {
          msg.error(
            requestErrorText(
              e,
              "批量解除",
              Math.round(API_GLOBAL_TIMEOUT_MS / 1000),
              "解除失败",
            ),
          );
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
    // ✅ R225（2026-11-12 · D6）：补用户反馈 —— R210 已给同形态的
    //    resolve/resolveConflict 守卫补 msg.info（历史/复制方案残留缺 fact_id
    //    的条目是真实形态），stale 解除路径漏改：脏行上点「来源已变化 ✓」
    //    Tag 零反应（假动作家族）。
    if (!id || !factId) {
      msg.info("该条目缺少事实标识（历史/复制方案残留），请重新提取事实后再试");
      return;
    }
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
      if (sid === currentSchemeIdRef.current) {
        msg.error(
          requestErrorText(
            e,
            "解除过期标记",
            Math.round(API_GLOBAL_TIMEOUT_MS / 1000),
            "解除失败",
          ),
        );
      }
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
          // ✅ R225（D2）：删除文档后端已把关联事实置 stale（回传 stale_facts）
          //    —— 回读事实列表/概览让「来源过期」徽标即时可见。
          void loadFacts();
        } catch (e: unknown) {
          msg.error(
            requestErrorText(
              e,
              "删除文件",
              Math.round(API_GLOBAL_TIMEOUT_MS / 1000),
              "删除失败",
            ),
          );
        }
      },
    });
  };

  return {
    handleUploadDocuments,
    handleParseDocuments,
    handleParseDocument,
    handlePreviewDocument,
    handleCancelPreview,
    handlePipelineAction,
    handleCategoryChange,
    handleBatchResolve,
    handleAckStaleAll,
    handleAckStaleOne,
    handleDeleteDocument,
  };
}

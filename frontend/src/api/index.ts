import axios from "axios";

// ✅ D7（2026-09-23）：文本模型配置模块的显式类型契约（替代此前的 any）
import type {
  AIActiveEnvResponse, AIAuditCleanupResponse, AIAuditLogsResponse,
  AIConfigAuditLogsResponse, AIConfigClearKeyResponse, AIConfigDeleteResponse,
  AIConfigExport,
  AIConfigExportItem, AIConfigImportResponse, AIConfigListResponse,
  AIConfigPayload, AIConfigPrecheckResponse, AIConfigRollbackResponse,
  AIConfigSaveResponse, AIConfigTestRequest, AIConfigTestResponse,
  AIConfigToggleResponse, AIFallbackChainResponse, AIFetchModelsResponse,
  AIHealth, AIModelsResponse, AIPrecheckAllResponse, AIRuntimeResponse,
  AISceneRouteUpdateResponse, AISceneRoutesResponse,
  AISetDisabledProvidersResponse, AISetEnvResponse,
  AIUsageStats,
} from "../types/aiConfig";
import type {
  PromptAuditLogsResponse, PromptListResponse, PromptMutationResponse,
  PromptRollbackResponse,
} from "../types/prompt";

/**
 * ✅ R14 修复（2026-09-22）：全局 401 短路保护。
 *
 * 背景：当后端启用 API_AUTH_TOKEN 而前端未携带/凭据错误时，所有请求都会返回 401。
 * 各业务组件仍按原逻辑继续轮询（Layout 的 projectsApi.list 加载、
 * useSchemeLiveTask 的 3s 轮询、TaskStatusBar 的多轮询），
 * 后端日志里会出现大量「拒绝未授权请求」告警（logs/backend.log 曾见 120 次连续同一签名）。
 *
 * 做法：interceptor 收到 401 时置 authGuard.blocked=true；
 * 后续每个请求先检查该标志，若已短路则**直接不发请求**，返回一个已标记
 * isAuthBlocked=true 的 Promise，让各业务 catch 能识别并停止轮询。
 * 用户切换凭据后调用 `clearAuthShortCircuit()` 重置。
 */
export const authGuard = {
  blocked: false,
  reason: "",
};
export function clearAuthShortCircuit(): void {
  authGuard.blocked = false;
  authGuard.reason = "";
}

/**
 * 全局 axios 超时（默认 60s）。
 *
 * 本地开发环境下，后端 uvicorn --reload 热重载、SQLite 首次连接、
 * 或短时间内大量并发请求排队时，可能出现 30s 内无响应的情况。
 * 此前默认 30s 会导致页面加载时偶发 "timeout of 30000ms exceeded"（2026-09-25 观察到）。
 * 提至 60s 作为宽容兜底；对明确需要更短或更长的端点，在各 API 定义处单独覆盖。
 */
const api = axios.create({
  baseURL: "/api/v1",
  timeout: 60000,
});

/**
 * 带重试的只读请求包装器。
 *
 * 适用场景：启动时元数据加载（如 /bid-analysis/items），这类 GET 请求幂等、
 * 无副作用，偶发超时后自动重试 1-2 次即可大幅降低"页面首屏加载失败"的概率。
 *
 * @param fn  实际请求函数（通常是 axios 实例上的 get）
 * @param opts 可选参数：retries（重试次数，默认 2）、backoff（首次退避 ms，默认 500）
 */
async function withRetry<T>(
  fn: () => Promise<T>,
  opts: { retries?: number; backoff?: number } = {},
): Promise<T> {
  const { retries = 2, backoff = 500 } = opts;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return await fn();
    } catch (e: unknown) {
      lastErr = e;
      // 只有网络错误 / 超时才重试；401（authGuard）、4xx（参数错误）等确定性错误直接抛出
      const err = e as any;
      const isRetryable =
        err?.code === "ECONNABORTED" ||     // axios timeout
        err?.code === "ERR_CANCELED" ||
        err?.response?.status === undefined ||  // 无响应 = 网络层问题
        err?.response?.status >= 500;       // 5xx 服务端临时错误
      if (!isRetryable || attempt === retries) break;
      // 退避：500ms → 1000ms → 2000ms
      const delay = backoff * Math.pow(2, attempt);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  throw lastErr;
}

// ✅ 可选 API Token（后端 API_AUTH_TOKEN 非空时启用；为空则完全不发送该头，行为不变）
//    取值优先构建期注入 VITE_API_TOKEN，其次 localStorage.api_token（便于内网部署临时开启）。
function getApiToken(): string {
  try {
    const env = (import.meta as any)?.env?.VITE_API_TOKEN;
    if (typeof env === "string" && env) return env;
  } catch {
    // import.meta 不可用时忽略
  }
  try {
    return localStorage.getItem("api_token") || "";
  } catch {
    return "";
  }
}

function authHeaders(): Record<string, string> {
  const t = getApiToken();
  return t ? { "X-API-Key": t } : {};
}

api.interceptors.request.use((config) => {
  // ✅ R14 修复（2026-09-22）：401 短路 —— 一旦后端返回过 401，
  //    除非用户显式 clearAuthShortCircuit()，否则后续请求**不再发出**，
  //    从源头切断"120 次连续拒绝未授权请求"的日志噪音与无效网络开销。
  if (authGuard.blocked) {
    const err: any = new Error(authGuard.reason || "未授权访问，请检查 API Token");
    err.name = "AuthBlockedError";
    err.isAuthBlocked = true;
    err.status = 401;
    return Promise.reject(err);
  }
  const h = authHeaders();
  for (const key of Object.keys(h)) {
    // 不覆盖调用方显式设置的同名头
    if ((config.headers as any)?.[key] === undefined) {
      (config.headers as any)[key] = h[key];
    }
  }
  return config;
});

// ✅ 统一错误提示：响应拦截器把后端 detail 附加到 error.message，
// 各页面 catch 里直接 msg.error(e.message) 即可显示可读信息
api.interceptors.response.use(
  (resp) => resp,
  async (error) => {
    // AbortController 主动取消（组件卸载 / 停止按钮）：静默，不弹错误提示
    // axios 内部 abort 后浏览器会抛 net::ERR_ABORTED，我们只把它标记为 AbortError
    if (error?.code === "ERR_CANCELED" || error?.name === "AbortError") {
      error.name = "AbortError";
      return Promise.reject(error);
    }
    let data: any = error?.response?.data;
    // ✅ R14 修复（2026-09-22）：401 后置 authGuard.blocked，
    //    后续所有请求由请求拦截器短路，避免持续轮询产生日志噪音。
    if (error?.response?.status === 401) {
      authGuard.blocked = true;
      authGuard.reason =
        (typeof data === "object" && data?.detail) ||
        (typeof data === "object" && data?.message) ||
        "未授权访问，请检查 API Token";
      (error as any).isAuthBlocked = true;
    }
    // ✅ 修复：导出接口用 responseType:"blob"，失败时后端返回的 JSON 错误体
    // 会被 axios 包成 Blob，此时 data.detail 恒为 undefined，用户只能看到
    // "Request failed with status code 500"（例如 PDF 转换工具缺失的真实原因被吞掉）。
    // 这里把 Blob 解包成文本再尝试 JSON 解析。
    if (typeof Blob !== "undefined" && data instanceof Blob) {
      try {
        const text = await data.text();
        try {
          data = JSON.parse(text);
        } catch {
          data = { detail: text };
        }
      } catch {
        data = undefined;
      }
    }
    const detail = data?.detail || data?.message || error?.message;
    if (detail && typeof detail === "string") {
      error.message = detail;
    }
    return Promise.reject(error);
  }
);

// 项目
export const projectsApi = {
  list: (params?: Record<string, string>) => api.get("/projects", { params }),
  get: (id: string) => api.get(`/projects/${id}`),
  create: (data: any) => api.post("/projects", data),
  update: (id: string, data: any) => api.patch(`/projects/${id}`, data),
  delete: (id: string) => api.delete(`/projects/${id}`),
};

// 方案
export const schemesApi = {
  list: (projectId: string) => api.get(`/projects/${projectId}/schemes`),
  get: (projectId: string, schemeId: string) =>
    api.get(`/projects/${projectId}/schemes/${schemeId}`),
  create: (projectId: string, data: any) =>
    api.post(`/projects/${projectId}/schemes`, data),
  update: (projectId: string, schemeId: string, data: any) =>
    api.patch(`/projects/${projectId}/schemes/${schemeId}`, data),
  delete: (projectId: string, schemeId: string) =>
    api.delete(`/projects/${projectId}/schemes/${schemeId}`),
  duplicate: (projectId: string, schemeId: string) =>
    api.post(`/projects/${projectId}/schemes/${schemeId}/duplicate`),
  archive: (projectId: string, schemeId: string) =>
    api.post(`/projects/${projectId}/schemes/${schemeId}/archive`),
};

// 章节
export const sectionsApi = {
  list: (schemeId: string, params?: { include_content?: boolean }) =>
    api.get(`/schemes/${schemeId}/sections`, { params }),
  create: (schemeId: string, data: any) =>
    api.post(`/schemes/${schemeId}/sections`, data),
  update: (schemeId: string, sectionId: string, data: any) =>
    api.patch(`/schemes/${schemeId}/sections/${sectionId}`, data),
  delete: (schemeId: string, sectionId: string) =>
    api.delete(`/schemes/${schemeId}/sections/${sectionId}`),
  saveOutline: (schemeId: string, data: any) =>
    api.post(`/schemes/${schemeId}/sections/save-outline`, data),
  reorder: (schemeId: string, data: any) =>
    api.post(`/schemes/${schemeId}/sections/reorder`, data),
  exportTree: (schemeId: string) =>
    api.get(`/schemes/${schemeId}/sections/export-tree`),
  /** 正文质量审计（交付前自检）：口语化/AI 腔残留 + 引用已废止标准 */
  quality: (schemeId: string) =>
    api.get(`/schemes/${schemeId}/sections/quality`),
  /** 字数压缩（对齐 OpenBidKit 的 shrink）：超字数章节 AI 局部 replace/delete 压缩 */
  shrink: (schemeId: string, sectionId: string) =>
    api.post(`/schemes/${schemeId}/sections/${sectionId}/shrink`, null, { timeout: 600000 }),
  /** 重置正文：清空目录树中所有章节已生成正文（目录结构保留，不可恢复） */
  resetContent: (schemeId: string) =>
    api.post(`/schemes/${schemeId}/sections/reset-content`),
  /** F-CONTENT-STANDARD(2026-09-26)：单个章节最近一次生成标准校验报告 */
  report: (schemeId: string, sectionId: string) =>
    api.get(`/schemes/${schemeId}/sections/report/${sectionId}`),
  /** F-CONTENT-STANDARD(2026-09-26)：方案所有章节生成标准校验汇总 */
  reportSummary: (schemeId: string) =>
    api.get(`/schemes/${schemeId}/sections/report-summary`),
};

// 目录库
export const outlineLibraryApi = {
  list: (params?: Record<string, string>) =>
    api.get("/outline-library", { params }),
  get: (id: string) => api.get(`/outline-library/${id}`),
  create: (data: any) => api.post("/outline-library", data),
  update: (id: string, data: any) => api.patch(`/outline-library/${id}`, data),
  delete: (id: string) => api.delete(`/outline-library/${id}`),
  review: (id: string, data: any) => api.post(`/outline-library/${id}/review`, data),
  newVersion: (id: string, data: any) => api.post(`/outline-library/${id}/new-version`, data),
  // ✅ 已移除废弃的 apply（旧链路只取 JSON 不落库、却累加引用计数，易产生幽灵请求）；
  //    套用目录库统一走 applyAndSave（一步写库）。
  // 一步完成：目录库 → 方案 sections（后端复用 _save_outline_to_db 写入 sections）
  applyAndSave: (id: string, data: any) =>
    api.post(`/outline-library/${id}/apply-and-save`, data),
  // ---- 增强：统计 / 筛选项 / 标准模板 ----
  stats: () => api.get("/outline-library/stats"),
  filters: () => api.get("/outline-library/filters"),
  templates: () => api.get("/outline-library/templates"),
  template: (key: string) => api.get(`/outline-library/templates/${key}`),
  // ---- 增强：复制 / 版本回滚 / 批量审核 / 导出 ----
  duplicate: (id: string, data?: any) => api.post(`/outline-library/${id}/duplicate`, data || {}),
  /** 把方案当前目录树反向沉淀为目录库 */
  fromScheme: (data: any) => api.post("/outline-library/from-scheme", data),
  restoreVersion: (id: string, data: any) =>
    api.post(`/outline-library/${id}/restore-version`, data),
  batchReview: (data: any) => api.post("/outline-library/batch-review", data),
  export: (id: string, format: string = "text") =>
    api.get(`/outline-library/${id}/export`, { params: { format } }),
};

// 上传目录识别
export const uploadOutlineApi = {
  parse: (file: File, params?: { scheme_name?: string; reorganize?: boolean }) => {
    const form = new FormData();
    form.append("file", file);
    const qs: string[] = [];
    if (params?.scheme_name) qs.push(`scheme_name=${encodeURIComponent(params.scheme_name)}`);
    // ✅ BUG 修复：旧实现只在 `reorganize === false` 时才拼这个参数 ——
    //    传 true 时被整条丢弃、落到后端默认值 false，于是「整理为标准结构」
    //    这个能力（后端已实现 + 有单测）在真实前端链路上**永不生效**。
    //    现按「是否显式传入」透传，true/false 均如实发送。
    if (params?.reorganize !== undefined) qs.push(`reorganize=${params.reorganize}`);
    const url = "/upload-outline/parse" + (qs.length ? `?${qs.join("&")}` : "");
    return api.post(url, form, {
      headers: { "Content-Type": "multipart/form-data" },
      timeout: 120000,
    });
  },
  // ✅ 契约清理（2026-09-20）：移除无人调用的 saveAsOutline（/upload-outline/{id}/save-as-outline）。
  //    该接口按"归一化标题匹配"保留正文，与工作台实际保存链路
  //    （识别→本地树→保存目录 → /sections/save-outline 按 id 匹配保留正文）语义并存冲突，
  //    前端从不调用（识别结果只替换本地树、由用户编辑后统一落库），属事实死契约。
  //    后端路由保留（有单测、可作程序化 API 使用），前端不再声明以免契约漂移。
  saveAsLibrary: (id: string, data: any) =>
    api.post(`/upload-outline/${id}/save-as-library`, data),
};

// 全局事实（增强版：支持模拟值标记、矛盾检测、批量确认、缓存失效）
// ✅ 优化：移除 summary / uploadAndExtract / extractFromProject 三个重复方法
//   - list 已返回 stats（含 has_warnings 由前端计算），无需单独 summary 请求
//   - uploadFilesAndExtract 天然兼容单文件，无需单文件专用接口
//   - 「从项目资料提取」统一走 SSE /sse/generate-facts（带进度与断线重挂接）
export const factsApi = {
  list: (schemeId: string, options?: { signal?: AbortSignal }) =>
    api.get("/global-facts", { params: { scheme_id: schemeId }, signal: options?.signal }),
  create: (data: any, schemeId: string) =>
    api.post("/global-facts", data, { params: { scheme_id: schemeId } }),
  update: (id: string, data: any, schemeId?: string) =>
    api.patch(`/global-facts/${id}`, data, { params: { scheme_id: schemeId || "" } }),
  resolve: (factId: string, schemeId?: string) =>
    api.patch(`/global-facts/${factId}/resolve`, {}, { params: { scheme_id: schemeId || "" } }),
  // 选择一个候选值解决矛盾（前端候选值列表「选此值」）
  resolveConflict: (factId: string, value: string, schemeId?: string) =>
    api.patch(`/global-facts/${factId}/resolve-conflict`, { value },
      { params: { scheme_id: schemeId || "" } }),
  batchResolve: (schemeId: string, factIds?: string[]) =>
    api.post("/global-facts/batch-resolve", { scheme_id: schemeId, fact_ids: factIds }),
  delete: (id: string, schemeId?: string) =>
    api.delete(`/global-facts/${id}`, { params: { scheme_id: schemeId || "" } }),
  // ✅ 一键清除全部已提取的项目信息（含增量提取进度重置，2026-09-17）
  clearAll: (schemeId: string) =>
    api.post("/global-facts/clear", { scheme_id: schemeId }, { timeout: 60000 }),
  // ✅ 事实分类白名单（后端 CATEGORY_TITLES 单一事实源，消三侧口径债 2026-09-20）
  //    注意与 categoryOptions（文档分类 /documents/category-options）是两套不同分类
  categories: () => api.get("/global-facts/categories"),
  categoryMap: () => api.get("/global-facts/category-map"),
  chapters: (schemeId: string, options?: { signal?: AbortSignal }) =>
    api.get("/global-facts/chapters", {
      params: { scheme_id: schemeId },
      signal: options?.signal,
    }),
  dangerCheck: (
    data: { scheme_name?: string; extra_text?: string },
    schemeId: string,
    options?: { signal?: AbortSignal },
  ) => api.post("/global-facts/danger-check", data, {
    params: { scheme_id: schemeId },
    signal: options?.signal,
  }),
  // ✅ 分步工作流：① 上传保存（仅存文件，不解析不提取）
  uploadDocuments: (files: File[], schemeId: string, options?: { signal?: AbortSignal }) => {
    const form = new FormData();
    files.forEach(f => form.append("files", f));
    return api.post("/global-facts/upload-documents", form, {
      params: { scheme_id: schemeId },
      headers: { "Content-Type": "multipart/form-data" },
      timeout: 120000,
      signal: options?.signal,
    });
  },
  // ② 解析单份文档（force=true 强制重解析，用于补齐被截断/需重新 OCR 的内容）
  //    options.signal：解析（OCR）耗时可达数分钟，组件卸载/切方案时中断请求
  parseDocument: (docId: string, force = false, options?: { signal?: AbortSignal }) =>
    api.post(`/global-facts/documents/${docId}/parse`, null, {
      params: force ? { force: true } : undefined,
      timeout: 300000, // 扫描件 OCR 较慢，放宽超时
      signal: options?.signal,
    }),
  // ② 批量解析该项目下的文档（force=true 时连同已解析文档一起重解析，
  //    用于补齐旧版截断内容 / 启用 OCR 后重扫扫描件）
  parseAllDocuments: (
    schemeId: string, projectId?: string, force = false,
    options?: { signal?: AbortSignal },
  ) =>
    api.post("/global-facts/documents/parse-all", null, {
      params: { scheme_id: schemeId, project_id: projectId, ...(force ? { force: true } : {}) },
      timeout: 600000,
      signal: options?.signal,
    }),
  // 已上传的项目资料文档
  // ✅ 加固：以 schemeId 为主键查询（后端内部反查 project_id），
  //    避免依赖 scheme.project_id 字段而出现「列表静默为空」
  listDocuments: (options: { schemeId: string; projectId?: string; signal?: AbortSignal }) =>
    api.get("/global-facts/documents", {
      params: { scheme_id: options.schemeId, project_id: options.projectId },
      signal: options.signal,
    }),
  deleteDocument: (docId: string) =>
    api.delete(`/global-facts/documents/${docId}`),
  // ✅ 文件导入/解析增强：文件预览（前 N 字）和分类管理
  previewDocument: (docId: string, maxChars = 5000) =>
    api.get(`/global-facts/documents/${docId}/preview`, { params: { max_chars: maxChars } }),
  updateDocumentCategory: (docId: string, docCategory: string) =>
    api.patch(`/global-facts/documents/${docId}/category`, { doc_category: docCategory }),
  categoryOptions: () => api.get("/global-facts/documents/category-options"),
  // ✅ 缺口修复（2026-09-24）：后端 POST /global-facts/adjust（自然语言批量调整
  //    事实，返回最小操作计划，apply=false 默认不落库）早已实现，前端 api 层
  //    **从未封装** —— 「AI 批量调整事实」能力对 UI 完全不可见（调用链断裂）。
  //    现补齐封装；默认 apply=false，与后端「零数据风险」默认一致。
  adjust: (payload: { instruction: string; scheme_id?: string; project_id?: string; apply?: boolean; operations?: Array<Record<string, unknown>> }) =>
    api.post("/global-facts/adjust", payload, { timeout: 120000 }),
};

// =========================================================================
// ✅ 资料四层存储查询 API（对应后端 routers/doc_pipeline.py）
//    —— 此前这些端点在后端已实现（status/extractions/chunks/completeness/
//       freshness/sync-extractions/cross-check/index），但前端 api/index.ts
//       完全没有封装，「四层存储」能力对用户不可见，属调用链断裂。现补齐
//       统一封装，复用同一套鉴权头 / 错误处理 / AbortSignal 通道。
//    数据流：上传解析（factsApi）→ 解析落库（parsed_markdown + 四层产物）
//            → 下游（目录生成/事实提取/标前分析）经本组查询溯源与校验。
// =========================================================================
export const docPipelineApi = {
  /** 解析状态：DB 时效列 + 磁盘 meta + 各层产物落盘概览 */
  status: (docId: string) => api.get(`/documents/${docId}/status`),
  /** 提取结果查询（按类别：project_info / engineering / geology / ...；后端参数名固定为 type） */
  extractions: (docId: string, extractType?: string) =>
    api.get(`/documents/${docId}/extractions`, { params: extractType ? { type: extractType } : {} }),
  /** 分块查询（含 source_ref 溯源，供目录/事实提取定位原文） */
  chunks: (docId: string, chunkType?: string) =>
    api.get(`/documents/${docId}/chunks`, { params: chunkType ? { chunk_type: chunkType } : {} }),
  /** 增量重解析：指纹未变直接跳过（mode=incremental）；force=true 强制重解析并刷新四层产物 */
  reparse: (docId: string, mode: "incremental" | "force" = "incremental") =>
    api.post(`/documents/${docId}/reparse`, { mode }, { timeout: 300000 }),
  /** 完整性校验报告（覆盖率 / 质量分 / 冲突汇总） */
  completeness: (docId: string, refresh = false) =>
    api.get(`/documents/${docId}/completeness`, { params: refresh ? { refresh: true } : {} }),
  /** 时效性：指纹一致性 / 解析有效期 / 版本信息（file_changed/expired 提示重跑） */
  freshness: (docId: string) => api.get(`/documents/${docId}/freshness`),
  /** 提取层物化：把 AI 提取结果按标准格式落盘到 extracted/*.json + doc_extractions */
  syncExtractions: (docId: string) =>
    api.post(`/documents/${docId}/sync-extractions`, null, { timeout: 60000 }),
  /** 交叉校验：多文档事实冲突 + 一致性规则跑批 */
  crossCheck: (docId: string) =>
    api.post(`/documents/${docId}/cross-check`, null, { timeout: 120000 }),
  /** 项目文档索引：磁盘 documents_index.json 与 DB 档案合并视图 */
  projectIndex: (projectId: string) =>
    api.get(`/projects/${projectId}/documents/index`),
};

// =========================================================================
// ✅ 项目资料结构化提取（18 项 AI 并发提取：12 项方案基本信息 + 6 项施工组织设计，
//    17 必选 + 1 可选，13 分组，对齐《专项方案生成与编制 — 基本信息需求清单》）
//
// ⚠️ 口径唯一来源是后端 /bid-analysis/items（total / required_count /
//    optional_count / markdown_count / json_count / group_count）。本节注释里的
//    数字仅作导读；此前这里残留「20 项 / 14 项 / 15 分组」，与实际 18/12+6/13
//    长期不一致，排查时极易被误导，故不再在此声明任何可能失真的数字。
// =========================================================================
export const bidAnalysisApi = {
  /**
   * 解析项定义 + 口径明细（total/required_count/optional_count/markdown_count/json_count/group_count）。
   *
   * ✅ 重试加固（2026-09-25）：首次进入工作台时若后端刚热重载 / SQLite 首次连接慢，
   * 偶发超时 → 自动重试 2 次，退避 500ms → 1000ms → 2000ms。
   * 只读 API，无副作用，重试安全。
   */
  items: () => withRetry(() => api.get("/bid-analysis/items")),
  /** 单个解析项定义 */
  item: (itemId: string) => api.get(`/bid-analysis/items/${itemId}`),
  /** 多标段检测（对已解析文档运行规则检测） */
  checkSections: (schemeId: string, projectId?: string) =>
    api.post("/bid-analysis/check-sections", null, {
      params: { scheme_id: schemeId, project_id: projectId },
    }),
  /**
   * 查询多标段检测结果与当前选中的投标范围。
   *
   * 返回 needs_selection（多标段但未选择 → 前端应提示用户选择）与
   * context_hint（该选择会被注入 AI 的原文，便于向用户解释影响）。
   */
  bidSections: (schemeId: string, projectId?: string) =>
    api.get("/bid-analysis/bid-sections", {
      params: { scheme_id: schemeId, project_id: projectId },
    }),
  /**
   * 选择 / 清除本次投标范围（多标段项目）。
   *
   * section_id 传空串表示清除选择、恢复「不注入标段上下文」的旧行为；
   * 选择只影响**后续** AI 调用，不会自动重跑已完成的解析项。
   */
  selectSection: (
    schemeId: string,
    section: {
      section_id: string;
      section_title?: string;
      head_line?: string;
      description?: string;
      evidence?: string[];
    },
    projectId?: string,
  ) =>
    api.post("/bid-analysis/select-section", section, {
      params: { scheme_id: schemeId, project_id: projectId },
    }),
  /** 查询已存储的提取结果（含汇总；summary 含 success_valid / manual_count） */
  /** ✅ 重试加固（同 items，见上方注释） */
  results: (schemeId: string, projectId?: string) =>
    withRetry(() => api.get("/bid-analysis/results", { params: { scheme_id: schemeId, project_id: projectId } })),
  /** 单个解析项结果 */
  singleResult: (itemId: string, schemeId: string, projectId?: string) =>
    api.get(`/bid-analysis/results/${itemId}`, {
      params: { scheme_id: schemeId, project_id: projectId },
    }),
  /**
   * 人工校正单个解析项的提取结果（覆盖 AI 输出，后端标记 source='manual'）。
   *
   * 为什么需要：AI 抽错关键参数（基坑深度、支护形式等）时，唯一出路此前只有
   * 整项重跑（贵且不稳定），而抽错的值会继续被目录/正文生成消费。
   * json 项提交非法 JSON → 422；空内容 → 422（请用 clearResult）。
   */
  updateResult: (
    itemId: string,
    schemeId: string,
    content: string,
    projectId?: string,
  ) =>
    api.put(`/bid-analysis/results/${itemId}`, { content }, {
      params: { scheme_id: schemeId, project_id: projectId },
    }),
  /** 清空单个解析项结果（撤销人工校正 / 放弃该项，回到 idle + source='ai'） */
  clearResult: (itemId: string, schemeId: string, projectId?: string) =>
    api.delete(`/bid-analysis/results/${itemId}`, {
      params: { scheme_id: schemeId, project_id: projectId },
    }),
  /** 启动结构化解析（非 SSE，简单同步版） */
  start: (data: {
    scheme_id: string;
    project_id?: string;
    /** item = 单项/局部重跑（不强制补全必选项） */
    mode?: "key" | "full" | "custom" | "item";
    selected_item_ids?: string[];
    force_rerun?: boolean;
  }) => api.post("/bid-analysis/start", data, { timeout: 600000 }),
  /**
   * ✅ SSE 实时推送版（正式使用）
   *   返回 { path, params } 供调用方传入 sseGetStream，
   *   从而复用统一的鉴权头 / AbortController / SSE 分帧逻辑，
   *   消除此前裸 fetch 在开启 VITE_API_TOKEN 后 401 断链的问题。
   */
  startSse: (schemeId: string, projectId?: string, opts?: {
    mode?: "key" | "full" | "custom" | "item";
    selectedItemIds?: string[];
    forceRerun?: boolean;
  }) => {
    const params: Record<string, string> = { scheme_id: schemeId };
    if (projectId) params.project_id = projectId;
    if (opts?.mode) params.mode = opts.mode;
    if (opts?.selectedItemIds?.length) {
      params.selected_item_ids = JSON.stringify(opts.selectedItemIds);
    }
    if (opts?.forceRerun) params.force_rerun = "true";
    return { path: "/bid-analysis/start-sse", params };
  },
  /**
   * 危大工程方案自动分类 + 九大章节字段完整性校验（2026-09-24 新增）。
   * 返回 { ok, classification, primary_category, chapter_completeness }。
   * body 可携带 { scheme_name?, params?: {depth,height,span,...}, extra_text? }。
   */
  classify: (
    schemeId: string,
    projectId?: string,
    body?: { scheme_name?: string; params?: Record<string, any>; extra_text?: string },
  ) =>
    api.post(
      "/bid-analysis/classify",
      body ?? {},
      { params: { scheme_id: schemeId, project_id: projectId } },
    ),
};

// 合规检查（AI 逐条判定 10+ 项清单，耗时较长，120s 超时）
export const complianceApi = {
  expertItems: () => api.get("/compliance/expert-review/items"),
  check: (data: any) => api.post("/compliance/check", data, { timeout: 120000 }),
  expertReview: (data: any) => api.post("/compliance/expert-review", data, { timeout: 120000 }),
  // ✅ G7（2026-09-21）：透传 limit / check_type —— 此前 limit 完全不传，
  // 后端 /compliance/results 每次都回全量历史（大方案动辄数百条），
  // 列表渲染与内存都被拖慢；现在调用方可显式限定条数。
  results: (schemeId: string, checkType?: string, limit?: number) =>
    api.get(`/compliance/results/${schemeId}`, {
      params: {
        check_type: checkType || "",
        ...(limit ? { limit } : {}),
      },
    }),
  /** 全文一致性审计（事实 vs 正文比对，AI 耗时较长） */
  consistencyAudit: (schemeId: string) =>
    api.post(`/compliance/consistency-audit/${schemeId}`, null, { timeout: 300000 }),
  consistencyLatest: (schemeId: string) =>
    api.get(`/compliance/consistency-audit/${schemeId}/latest`),
  /**
   * 一致性审计历史趋势。
   * @deprecated 孤儿 API：本前端无调用方（后端保留兼容）；大版本评估清理。
   */
  consistencyHistory: (schemeId: string) =>
    api.get(`/compliance/consistency-audit/${schemeId}/history`),
  // ===== 商业级增强：规则目录 / 程序化预检 / 就绪度总览 / 整改报告 =====
  /** 全量审核规则目录（含行业依据），用于「规则说明」抽屉与用户自查 */
  rules: () => api.get("/compliance/rules"),
  /**
   * 维度目录。
   * @deprecated 孤儿 API：本前端无调用方（UI 已改用 /overview 的 dimensions）；大版本评估清理。
   */
  dimensions: () => api.get("/compliance/dimensions"),
  /**
   * 程序化预检：确定性规则，无需 AI，秒级返回。
   * @deprecated 孤儿 API：本前端走 /overview（内部已含程序化预检且带落库+缓存）；
   * 端点保留供脚本/手工诊断，后端已补幂等锁+缓存；大版本评估清理。
   */
  preflight: (schemeId: string) =>
    api.post(`/compliance/preflight/${schemeId}`, null, { timeout: 120000 }),
  /** 就绪度总览：聚合程序化预检 + 导出预检 + AI 检查 + 一致性审计 + 专家预检，给出总分与放行结论。
   *  ✅ G2：force=true 时后端强制重算（否则同内容指纹下会命中服务端缓存） */
  overview: (schemeId: string, force = false) =>
    api.post(`/compliance/overview/${schemeId}`, null, {
      timeout: 120000,
      params: force ? { force: true } : undefined,
    }),
  /** 预检历史（分数趋势：整改后分数涨了没） */
  runs: (schemeId: string, limit = 10) =>
    api.get(`/compliance/runs/${schemeId}`, { params: { limit } }),
  /** 整改清单报告（Markdown，可复制进评审意见 / 整改通知单） */
  report: (schemeId: string) => api.get(`/compliance/report/${schemeId}`),
};

/** 方案 / 章节审核工作流（PRD §3.12.5） */
export const reviewApi = {
  /**
   * 审核状态值域与合法流转表。
   * @deprecated 孤儿 API：本前端无调用方（状态机常量已镜像在 types/audit.ts）；大版本评估清理。
   */
  statuses: (schemeId: string) => api.get(`/schemes/${schemeId}/review/statuses`),
  summary: (schemeId: string) => api.get(`/schemes/${schemeId}/review/summary`),
  checklist: (schemeId: string) => api.get(`/schemes/${schemeId}/review/checklist`),
  reviewSection: (
    schemeId: string,
    sectionId: string,
    data: { to_status: string; reviewer?: string; comment?: string }
  ) => api.post(`/schemes/${schemeId}/review/sections/${sectionId}`, data),
  batch: (
    schemeId: string,
    data: {
      section_ids: string[];
      to_status: string;
      reviewer?: string;
      comment?: string;
      force?: boolean;
    }
  ) => api.post(`/schemes/${schemeId}/review/sections/batch`, data),
  /** 提交方案级审核。
   *  ✅ G6：require_all_sections_reviewed 此前后端字段存在但前端从不传，
   *  于是"必须所有章节过审才能提交"这一开关形同虚设，只能靠人工盯。 */
  submit: (
    schemeId: string,
    data: {
      to_status: string;
      reviewer?: string;
      comment?: string;
      require_all_sections_reviewed?: boolean;
    }
  ) => api.post(`/schemes/${schemeId}/review/submit`, data),
  // ✅ G8：offset + has_more —— 后端评审轨迹超过 200 条时早期记录此前永久不可见
  records: (schemeId: string, sectionId?: string, limit = 50, offset = 0) =>
    api.get(`/schemes/${schemeId}/review/records`, {
      params: { section_id: sectionId || "", limit, offset },
    }),
};

// 全文一致性 Agent 修复（扫描 → 仲裁 → 定向修复 → 确认/回滚）
// 扫描/修复为多章串行 AI 长任务，超时放宽到 10 分钟
export const consistencyRepairApi = {
  scan: (schemeId: string, data?: any) =>
    api.post(`/schemes/${schemeId}/consistency/scan`, data || {}, { timeout: 600000 }),
  conflicts: (schemeId: string, scanId?: string) =>
    api.get(`/schemes/${schemeId}/consistency/conflicts`, { params: scanId ? { scan_id: scanId } : {} }),
  repair: (schemeId: string, data?: any) =>
    api.post(`/schemes/${schemeId}/consistency/repair`, data || {}, { timeout: 600000 }),
  repairDetail: (schemeId: string, repairId: string) =>
    api.get(`/schemes/${schemeId}/consistency/repair/${repairId}`),
  confirm: (schemeId: string, data: { repair_id: string; accepted?: string[]; rejected?: string[] }) =>
    api.post(`/schemes/${schemeId}/consistency/confirm`, data),
  rollback: (schemeId: string, data: { snapshot_id?: string; repair_id?: string }) =>
    api.post(`/schemes/${schemeId}/consistency/rollback`, data),
  repairs: (schemeId: string, limit = 20) =>
    api.get(`/schemes/${schemeId}/consistency/repairs`, { params: { limit } }),
  snapshots: (schemeId: string, limit = 20) =>
    api.get(`/schemes/${schemeId}/consistency/snapshots`, { params: { limit } }),
};

// 知识库 / 素材库（§3.9；生成链路自动注入项目级条目）
export const knowledgeApi = {
  list: (params: { project_id?: string; scheme_id?: string }) =>
    api.get("/knowledge", { params }),
  create: (data: { project_id?: string; scheme_id?: string; name: string; usage_hint?: string; content?: string }) =>
    api.post("/knowledge", data),
  update: (id: string, data: any) => api.put(`/knowledge/${id}`, data),
  delete: (id: string) => api.delete(`/knowledge/${id}`),
  asText: (params: { project_id?: string; scheme_id?: string }) =>
    api.get("/knowledge/as-text", { params }),
};

// 导出
export const exportApi = {
  check: (schemeId: string, signal?: AbortSignal) =>
    api.post(`/schemes/${schemeId}/export/check`, null, { signal }),
  // ✅ 《待补充清单》（2026-09-24）：逐条占位符扫描 + 按字段/按章节聚合，
  //    供「导出预检」卡片下钻定位缺失字段（人工补录兜底层）
  placeholderReport: (schemeId: string, signal?: AbortSignal) =>
    api.get(`/schemes/${schemeId}/export/placeholder-report`, { signal }),
  // ✅ 重跑计划（治 F 层第 3 条）：对照当前数据源判定哪些章节可自动重跑
  placeholderRerunPlan: (schemeId: string, signal?: AbortSignal) =>
    api.get(`/schemes/${schemeId}/export/placeholder-rerun-plan`, { signal }),
  // ✅ 监控基线（六层方案第 6 层）：占位符统计历史趋势
  placeholderHistory: (schemeId: string, signal?: AbortSignal) =>
    api.get(`/schemes/${schemeId}/export/placeholder-history`, { signal }),
  // ✅ 修复：导出需等待图表预渲染（每图 HTTP 预算 20s + PIL），全局 30s 超时极易误报失败
  // chartImages：前端 mermaid.js（与预览一致）渲染的 PNG，后端优先采用实现所见即所得
  docx: (schemeId: string, config: any, chartImages?: { chart_type: string; mermaid_code: string; png: string }[], signal?: AbortSignal) =>
    api.post(
      `/schemes/${schemeId}/export/docx`,
      { config, chart_images: chartImages || [] },
      { responseType: "blob", timeout: 600000, signal }
    ),
  pdf: (schemeId: string, config: any, chartImages?: { chart_type: string; mermaid_code: string; png: string }[], signal?: AbortSignal) =>
    api.post(
      `/schemes/${schemeId}/export/pdf`,
      { config, chart_images: chartImages || [] },
      { responseType: "blob", timeout: 600000, signal }
    ),
  cacheStatus: (schemeId: string, signal?: AbortSignal) =>
    api.get(`/schemes/${schemeId}/export/cache-status`, { signal }),
  // ✅ 导出格式预设库（对标 OpenBidKit exportFormatPresets）：同项目排版格式一键复用
  presets: {
    list: (schemeId: string, signal?: AbortSignal) =>
      api.get(`/schemes/${schemeId}/export/presets`, { signal }),
    create: (schemeId: string, name: string, config: any) =>
      api.post(`/schemes/${schemeId}/export/presets`, { name, config }),
    update: (schemeId: string, presetId: string, payload: { name?: string; config?: any }) =>
      api.put(`/schemes/${schemeId}/export/presets/${presetId}`, payload),
    setDefault: (schemeId: string, presetId: string) =>
      api.post(`/schemes/${schemeId}/export/presets/${presetId}/default`),
    remove: (schemeId: string, presetId: string) =>
      api.delete(`/schemes/${schemeId}/export/presets/${presetId}`),
  },
};

// 专项方案清单
export const schemeCatalogApi = {
  list: (params?: Record<string, string>) =>
    api.get("/scheme-catalog", { params }),
  categories: () => api.get("/scheme-catalog/categories"),
  get: (id: string) => api.get(`/scheme-catalog/${id}`),
};

// 图表（正文同步内嵌 + 导出渲染）
export const chartsApi = {
  types: () => api.get("/charts/types"),
  render: (data: { chart_type: string; code: string; skip_http?: boolean }) =>
    api.post("/charts/render", data, { responseType: "blob", timeout: 60000 }),
  fixMermaid: (data: { code: string; error: string; chart_type?: string; section_id?: string; prediction_id?: string }) =>
    api.post("/charts/fix-mermaid", data),
  /** 图表清单（按方案；导出预渲染用） */
  list: (schemeId: string, signal?: AbortSignal) =>
    api.get(`/charts/list/${schemeId}`, { signal }),
  // ✅ 接口移除（2026-09-23，AGENTS.md 显式迁移决策）：chartsApi.generateAiImage
  //    已删除 —— v17 起配图在导出 DOCX 时由后端全自动生成（全仓 0 调用方已核实）。
  //    后端 /charts/generate-ai-image 端点按「禁止删除既有 API 端点不提供迁移路径」
  //    规则保留：默认 409 拒绝并提示 AI_IMAGE_MANUAL_ENABLED 逃生门（即迁移路径），
  //    回归守卫见 backend/tests/test_charts_ai_image_gate.py。
};

// 文本模型配置（菜单「文本模型配置」，路由 /settings/ai）
// ✅ D7（2026-09-23）：全部出入参改用 `types/aiConfig.ts` 的显式契约，
//    取代此前的 `any` —— 请求体字段拼错、读取不存在的响应字段从此在 tsc 阶段暴露。
export const aiApi = {
  getConfig: () => api.get<AIConfigListResponse>("/ai/config"),
  saveConfig: (data: AIConfigPayload) => api.post<AIConfigSaveResponse>("/ai/config", data),
  deleteConfig: (id: string) => api.delete<AIConfigDeleteResponse>(`/ai/config/${id}`),
  toggleConfig: (id: string) => api.patch<AIConfigToggleResponse>(`/ai/config/${id}/toggle`),
  testConfig: (data: AIConfigTestRequest) =>
    api.post<AIConfigTestResponse>("/ai/config/test", data, { timeout: 120000 }),
  getModels: () => api.get<AIModelsResponse>("/ai/models"),
  health: () => api.get<AIHealth>("/ai/health"),
  stats: (days = 30) => api.get<AIUsageStats | null>("/ai/stats", { params: { days } }),
  /** 调用审计明细：支持按供应商 / 操作 / 场景 / 结果 / 天数筛选 + 分页 */
  auditLogs: (params?: {
    limit?: number;
    offset?: number;
    provider_name?: string;
    action?: string;
    /** ✅ 2026-09-25：按业务场景下钻（与 /ai/stats 的 by_scene 同一口径） */
    scene?: string;
    success?: string;
    days?: number;
  }) => api.get<AIAuditLogsResponse>("/ai/audit-logs", { params }),
  /** 清理历史审计日志（默认保留最近 N 天） */
  cleanupAuditLogs: (keep_days = 30, only_failed = false) =>
    api.delete<AIAuditCleanupResponse>("/ai/audit-logs", { data: { keep_days, only_failed } }),
  updateFallbackChain: (chain: { id: string }[]) =>
    api.put<AIFallbackChainResponse>("/ai/fallback-chain", { chain }),
  fetchCustomModels: (base_url: string, api_key: string, config_id?: string) =>
    api.post<AIFetchModelsResponse>("/ai/config/fetch-models", { base_url, api_key, config_id }),
  precheck: (data: { base_url: string; config_id: string }) =>
    api.post<AIConfigPrecheckResponse>("/ai/config/precheck", data, { timeout: 60000 }),
  /** 批量连通性预检（全部配置，仅 DNS/TCP，不消耗额度） */
  precheckAll: () =>
    api.post<AIPrecheckAllResponse>("/ai/config/precheck-all", null, { timeout: 120000 }),
  /** 导出全部配置（不含 API Key，用于备份/迁移） */
  exportConfig: () => api.get<AIConfigExport>("/ai/config/export"),
  /** 导入配置（API Key 需重新填写） */
  importConfig: (items: AIConfigExportItem[], overwrite = false, set_first_active = false) =>
    api.post<AIConfigImportResponse>("/ai/config/import", { items, overwrite, set_first_active }),
  /**
   * ✅ 2026-09-23 新增：清除某条配置已保存的 API Key（收回密钥）。
   * 背景：此前只能整体删除配置才能移除 Key，而「当前使用」的配置**不允许删除**，
   * 用户实际上无法收回已保存的密钥。
   */
  clearConfigKey: (id: string) => api.delete<AIConfigClearKeyResponse>(`/ai/config/${id}/key`),
  /**
   * ✅ 2026-09-23 新增：配置变更审计（谁在什么时候改了哪条配置）。
   * 与 /ai/audit-logs（AI 调用审计）是两套记录：这里记的是**配置本身**的改动。
   */
  configAuditLogs: (params?: { limit?: number; offset?: number; days?: number; action?: string }) =>
    api.get<AIConfigAuditLogsResponse>("/ai/config/audit-logs", { params }),
  /**
   * ✅ 2026-09-23 新增：场景模型路由（多模型）。
   * 默认（表为空）所有场景共用「当前使用」配置，行为与旧版一致。
   */
  getSceneRoutes: () => api.get<AISceneRoutesResponse>("/ai/scene-routes"),
  updateSceneRoute: (scene: string, config_id: string) =>
    api.put<AISceneRouteUpdateResponse>("/ai/scene-routes", { scene, config_id }),
  /** ✅ 多环境：当前生效环境 + 已用环境标签 */
  getEnv: () => api.get<AIActiveEnvResponse>("/ai/env"),
  /** ✅ 多环境：切换当前生效环境（空串 = 通用，不做过滤） */
  setEnv: (env: string) => api.put<AISetEnvResponse>("/ai/env", { env }),
  /** ✅ G6：把配置回滚到某条变更记录**变更前**的状态（密钥不参与回滚） */
  rollbackConfig: (config_id: string, audit_id: string, include_active = false) =>
    api.post<AIConfigRollbackResponse>(
      `/ai/config/${config_id}/rollback`, { audit_id, include_active }),
  /** ✅ 运行时开关：当前生效环境 + 被禁用厂商 + 可选厂商清单 */
  getRuntime: () => api.get<AIRuntimeResponse>("/ai/runtime"),
  /** ✅ 运行时开关：整体覆盖被禁用厂商（空数组 = 全部恢复），即时生效无需重启 */
  setDisabledProviders: (providers: string[]) =>
    api.put<AISetDisabledProvidersResponse>("/ai/runtime/disabled-providers", { providers }),
};

// 提示词
export const promptsApi = {
  list: (category?: string) =>
    api.get<PromptListResponse>("/prompts", { params: category ? { category } : {} }),
  update: (key: string, content: string) =>
    api.patch<PromptMutationResponse>(`/prompts/${key}`, { content }),
  reset: (key: string) =>
    api.post<PromptMutationResponse>(`/prompts/${key}/reset`),
  auditLogs: (key: string, limit = 50, offset = 0) =>
    api.get<PromptAuditLogsResponse>("/prompts/audit-logs", {
      params: { key, limit, offset },
    }),
  /** ✅ G2 版本回滚：把提示词恢复到某条审计记录「变更前」的版本 */
  rollback: (key: string, auditId: string) =>
    api.post<PromptRollbackResponse>(`/prompts/${key}/rollback`, { audit_id: auditId }),
};

// 任务
export const tasksApi = {
  control: (taskId: string, action: string) =>
    api.post(`/sse/task/${taskId}/control`, { action }),
  /** 查询单个任务状态（SSE 断线后重新挂接） */
  status: (taskId: string) => api.get(`/sse/task/${taskId}`),
  /** 列出最近任务（可按方案过滤），刷新页面后据此检测进行中任务 */
  list: (schemeId?: string, limit = 20) =>
    api.get("/sse/tasks", { params: { scheme_id: schemeId, limit } }),
};

// 系统活动聚合（侧边栏「后台任务运行状态栏」轮询：任务 + AI 调用 + 服务态）
export const systemApi = {
  activity: (limit = 8) => api.get("/system/activity", { params: { limit } }),
  /** SSE 实时流：任务 / AI 状态变更时立即推送快照 */
  activityStream: (
    limit = 8,
    signal?: AbortSignal,
    softStop?: Promise<void>
  ) => sseGetStream("/system/activity/stream", { params: { limit }, signal, softStop }),
};

// SSE 流式请求辅助

type SseFetchOptions = {
  signal?: AbortSignal;
  onHeartbeat?: () => void;
  connectTimeoutMs?: number;
  /** 响应头已到达后，连续无任何字节的最大等待；0 表示禁用。 */
  idleTimeoutMs?: number;
};

/** 从 SSE 文本中解析完整事件块（支持 LF / CRLF / CR 与多行 data）。 */
export function parseSseEventBlock(block: string): { data?: string; heartbeat: boolean } {
  let heartbeat = false;
  const data: string[] = [];
  for (const rawLine of block.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n")) {
    if (rawLine.startsWith(":")) {
      if (rawLine.startsWith(": heartbeat")) heartbeat = true;
      continue;
    }
    if (rawLine.startsWith("data:")) {
      let value = rawLine.slice(5);
      if (value.startsWith(" ")) value = value.slice(1);
      data.push(value);
    }
  }
  return { data: data.length ? data.join("\n") : undefined, heartbeat };
}

export async function* sseFetch(
  url: string,
  body?: any,
  options?: SseFetchOptions
): AsyncGenerator<any> {
  // AbortController 主动取消时静默返回，避免 net::ERR_ABORTED 在控制台产红线
  if (options?.signal?.aborted) return;
  // ✅ 连接超时防御（2026-09-23 事故）：后端进程挂死时 TCP 可连上但响应头永不到达，
  //    fetch 会无限悬挂 —— 此前用户点「生成目录」只会停在「正在连接...」，无任何报错。
  //    现对「响应头到达前」阶段加超时（默认 30s；正常链路路由体只做 DB 装配，
  //    StreamingResponse 毫秒级返回，不会误伤）。仅约束建连阶段，不限制流式读取时长。
  const connectTimeoutMs = options?.connectTimeoutMs ?? 30_000;
  const inner = new AbortController();
  let timedOut = false;
  const timeoutMsg = `后端连接超时（${Math.round(connectTimeoutMs / 1000)}s 无响应）：请确认后端服务是否正常运行（可查看 logs/backend.log）`;
  const onOuterAbort = () => inner.abort();
  options?.signal?.addEventListener("abort", onOuterAbort, { once: true });
  // 建连阶段中断源：inner 被 abort（超时或外层取消）时立即决出，不依赖
  // fetch 实现对 signal 的响应（部分 polyfill / 挂死场景 fetch 永不 reject）
  const abortPromise = new Promise<never>((_, reject) => {
    inner.signal.addEventListener("abort", () => {
      if (timedOut) {
        reject(new Error(timeoutMsg));
      } else {
        const e: any = new Error("Aborted");
        e.name = "AbortError";
        reject(e);
      }
    }, { once: true });
  });
  abortPromise.catch(() => {}); // race 结束后再 reject 时避免 unhandled rejection
  const connectTimer =
    connectTimeoutMs > 0 ? setTimeout(() => { timedOut = true; inner.abort(); }, connectTimeoutMs) : null;
  let resp: Response;
  try {
    resp = await Promise.race([
      fetch(`/api/v1${url}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: body ? JSON.stringify(body) : undefined,
        signal: inner.signal,
      }),
      abortPromise,
    ]);
  } catch (err: any) {
    if (timedOut) throw new Error(timeoutMsg);
    if (err?.name === "AbortError" || options?.signal?.aborted) return;
    throw err;
  } finally {
    if (connectTimer !== null) clearTimeout(connectTimer);
    options?.signal?.removeEventListener("abort", onOuterAbort);
  }
  if (!resp.ok) {
    let detail = `SSE 请求失败: ${resp.status}`;
    try {
      const data = await resp.json();
      if (data?.detail && typeof data.detail === "string") detail = data.detail;
    } catch {
      // 响应体非 JSON，保持默认信息
    }
    throw new Error(detail);
  }
  if (!resp.body) {
    throw new Error("SSE 响应体为空");
  }
  const idleTimeoutMs = options?.idleTimeoutMs ?? 45_000;
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatchBlocks = (text: string, flushRemainder = false): Array<{ data?: string; heartbeat: boolean }> => {
    const out: Array<{ data?: string; heartbeat: boolean }> = [];
    const separator = /\r\n\r\n|\n\n|\r\r/;
    let match: RegExpExecArray | null;
    while ((match = separator.exec(text)) !== null) {
      out.push(parseSseEventBlock(text.slice(0, match.index)));
      text = text.slice(match.index + match[0].length);
    }
    if (flushRemainder && text.trim()) out.push(parseSseEventBlock(text));
    buffer = flushRemainder ? "" : text;
    return out;
  };
  try {
    while (true) {
      if (options?.signal?.aborted) break;
      let readResult: ReadableStreamReadResult<Uint8Array>;
      if (idleTimeoutMs > 0) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          readResult = await Promise.race([
            reader.read(),
            new Promise<never>((_, reject) => {
              timer = setTimeout(() => reject(new Error(
                `SSE 数据流空闲超时（${Math.round(idleTimeoutMs / 1000)}s 无心跳或事件）：后台任务可能已失联`
              )), idleTimeoutMs);
            }),
          ]);
        } finally {
          if (timer !== undefined) clearTimeout(timer);
        }
      } else {
        readResult = await reader.read();
      }
      if (readResult.done) {
        buffer += decoder.decode();
        for (const block of dispatchBlocks(buffer, true)) {
          if (block.heartbeat) options?.onHeartbeat?.();
          if (block.data !== undefined) {
            try { yield JSON.parse(block.data); } catch { /* 跳过非 JSON 数据 */ }
          }
        }
        break;
      }
      buffer += decoder.decode(readResult.value, { stream: true });
      for (const block of dispatchBlocks(buffer)) {
        if (block.heartbeat) options?.onHeartbeat?.();
        if (block.data !== undefined) {
          try { yield JSON.parse(block.data); } catch { /* 跳过非 JSON 数据 */ }
        }
      }
    }
  } catch (err: any) {
    if (err?.name === "AbortError" || options?.signal?.aborted) return;
    throw err;
  } finally {
    reader.cancel().catch(() => {});
  }
}

/** SSE GET 流式请求辅助（用于 /system/activity/stream 等只读 SSE 端点） */
export async function* sseGetStream(
  url: string,
  options?: {
    signal?: AbortSignal;
    params?: Record<string, any>;
    onHeartbeat?: () => void;
    /**
     * 优雅停止信号：resolve 时立即 break 循环，走 reader.cancel() 优雅收尾，
     * 不触发浏览器 net::ERR_ABORTED（AbortController.abort() 会触发）。
     * 典型场景：React.StrictMode 开发态 mount→cleanup→mount 时关闭首次连接。
     */
    softStop?: Promise<void>;
  }
): AsyncGenerator<any> {
  if (options?.signal?.aborted) return;
  const query = options?.params
    ? "?" + new URLSearchParams(options.params as Record<string, string>).toString()
    : "";
  // ✅ 性能/健壮性：补齐「建连超时」防御（对齐 sseFetch）。此前 sseGetStream 仅依赖
  //    TaskStatusBar 的 30s 看门狗兜底，若后端在「建连阶段」挂死，fetch 会无限悬挂、
  //    卸载/看门狗都关不掉，造成连接泄漏。现对「响应头到达前」阶段加 30s 超时。
  const connectTimeoutMs = 30_000;
  const inner = new AbortController();
  const onOuterAbort = () => inner.abort();
  if (options?.signal) options.signal.addEventListener("abort", onOuterAbort, { once: true });
  const connectTimer = setTimeout(() => inner.abort(), connectTimeoutMs);
  const timeoutMsg = `后端连接超时（${Math.round(connectTimeoutMs / 1000)}s 无响应）：请确认后端服务是否正常运行（可查看 logs/backend.log）`;
  let resp: Response;
  try {
    resp = await Promise.race([
      fetch(`/api/v1${url}${query}`, {
        method: "GET",
        headers: { ...authHeaders() },
        signal: inner.signal,
      }),
      new Promise<never>((_, reject) => {
        inner.signal.addEventListener("abort", () => {
          // 仅当不是外层主动取消时才报超时（外层取消走下方 catch 的 return 分支）
          if (!options?.signal?.aborted) {
            reject(new Error(timeoutMsg));
          }
        }, { once: true });
      }),
    ]);
  } catch (err: any) {
    if (err?.name === "AbortError" || options?.signal?.aborted) return;
    throw err;
  } finally {
    clearTimeout(connectTimer);
    if (options?.signal) options.signal.removeEventListener("abort", onOuterAbort);
  }
  if (!resp.ok) {
    throw new Error(`SSE 请求失败: ${resp.status}`);
  }
  if (!resp.body) {
    throw new Error("SSE 响应体为空");
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let stoppedBySoftStop = false;
  try {
    while (true) {
      if (options?.signal?.aborted) break;
      let done = false;
      let value: Uint8Array | undefined;
      if (options?.softStop) {
        // softStop 先 resolve 则当作 "流结束" 处理（不抛 AbortError、不记 ERR_ABORTED）
        const race = await Promise.race([
          reader.read(),
          options.softStop.then(() => ({ _soft: true } as const)),
        ]);
        if ((race as any)._soft) {
          stoppedBySoftStop = true;
          break;
        }
        done = (race as ReadableStreamReadResult<Uint8Array>).done;
        value = (race as ReadableStreamReadResult<Uint8Array>).value;
      } else {
        const r = await reader.read();
        done = r.done;
        value = r.value;
      }
      if (done) break;
      buffer += decoder.decode(value!, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (line.startsWith(": heartbeat")) {
          options?.onHeartbeat?.();
          continue;
        }
        if (line.startsWith("data: ")) {
          try {
            yield JSON.parse(line.slice(6));
          } catch {
            // skip 非 JSON 数据行
          }
        }
      }
    }
  } catch (err: any) {
    if (err?.name === "AbortError" || options?.signal?.aborted) return;
    throw err;
  } finally {
    if (!stoppedBySoftStop) {
      // 异常退出路径：需要 reader.cancel() 来关闭流，避免浏览器资源泄漏
      reader.cancel().catch(() => {});
    } else {
      // ✅ BUG 修复（性能/泄漏）：softStop 优雅停止后**必须最终释放连接**。
      // 旧实现完全不调用 reader.cancel()，会让 fetch/Response 一直挂在 TCP 上：
      //   前端 → 浏览器连接句柄不释放；
      //   后端 → request.is_disconnected() 长期为 False，activity_broadcaster 的
      //          订阅队列残留（无上限 Set），每 10s 继续执行 2 条 SQL 构建快照
      //          并 yield 到无人读取的缓冲区（StrictMode 双挂载会持续累积）。
      // 这里「延迟一小段」再 cancel：等当前微任务与生成器收尾完成，既真正释放
      // 连接，又把 Chrome 119+ 记录的 net::ERR_ABORTED 降到最低（该警告只在
      // cancel 与浏览器判定窗口重叠时出现）。以「资源不泄漏」为优先。
      setTimeout(() => reader.cancel().catch(() => {}), 100);
    }
  }
  if (stoppedBySoftStop) return; // 明确标记：这是主动软关，不是异常
}

export default api;
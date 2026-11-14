/**
 * API 层通用契约类型（错误体 / SSE 流 / 动态 JSON 载荷）。
 *
 * ✅ any 清理专项（2026-11-02）：此前 api/index.ts 的拦截器、SSE 辅助与
 * 写路径载荷大量使用 `any`，本文件为这些位置提供显式类型出口。
 * 字段名与后端逐字对应：FastAPI 默认错误体（routers 通用异常处理）与
 * SSE 端点（routers/sse_handlers.py、system.py 等）。
 */

// =========================================================================
// 错误响应体（REST 响应拦截器 + SSE HTTP 错误共用）
// =========================================================================

/** 后端错误响应体。FastAPI 惯例：
 *  - 业务错误：`{"detail": "中文消息"}`
 *  - 422 校验失败：`{"detail": [{loc, msg, type}, ...]}`
 *  - 部分端点用 `{"message": ...}`。
 *  detail/message 均按 unknown 下发，消费侧先 typeof 收窄再使用。 */
export interface ApiErrorBody {
  detail?: unknown;
  message?: unknown;
}

/** FastAPI 422 校验明细单条（`{loc, msg, type}`）。
 *  结构由 FastAPI/Pydantic 生成（外部规范、前端不可控），前端仅消费
 *  `loc`（路径段数组）与 `msg`（消息文本）两个字段，其余字段放行。 */
export interface FastAPIValidationErrorItem {
  loc?: unknown;
  msg?: unknown;
}

/** Blob 错误响应解包后的可读错误体（导出接口 responseType:"blob" 专用）。 */
export type BlobUnpackedErrorBody = ApiErrorBody | { detail: string };

// =========================================================================
// 401 短路（请求拦截器构造的合成错误）
// =========================================================================

/** 401 短路错误：由请求拦截器在 authGuard.blocked 时构造并 reject，
 *  各轮询方通过 `isAuthBlocked` 识别并停止轮询。 */
export type AuthBlockedError = Error & {
  name: "AuthBlockedError";
  isAuthBlocked: true;
  status: 401;
};

// =========================================================================
// SSE 流事件
// =========================================================================

/**
 * 后端 SSE 事件载荷：服务端动态 JSON，字段随 `type`/`event` 变化。
 *
 * ⚠️ 消费约定：SSE 事件是**本项目后端**下发的自有契约（非第三方黑盒），
 * 消费侧应在 for-await 循环体内按已知事件形态**一次性断言到真实事件类型**
 * （`const evt = raw as SectionStartEvent;`），禁止把 unknown 一路透传；
 * 事件类型定义逐步收敛进 types/ 或对应 hook 的局部 interface。
 * 该断言为编译期行为，不改变运行期取值，等价于原先的 any 直读。
 */
export type SseEvent = Record<string, unknown>;

/** sseGetStream 的 softStop 竞态分支标记（内部实现细节，导出仅为测试用）。 */
export interface SseSoftStopMarker {
  _soft: true;
}

// =========================================================================
// 动态 JSON 写路径载荷
// =========================================================================

/** 客户端提交体：字段随 UI 演进、后端接受宽松 JSON 的写路径载荷。
 *  有稳定形状的载荷应定义专用类型（如 OutlineSavePayload），本别名仅兜底。 */
export type JsonPayload = Record<string, unknown>;

/** 导出排版配置：导出表单（含折叠面板，deepMergeExportConfig 深合并后）提交给
 *  /export/docx|pdf 与导出预设库。键集合由后端 `_normalize_export_config`
 *  消费（margins/cover_info/scheme_forms/body/headings 等动态键），前端
 *  无法穷举（随排版能力演进），故声明为动态 JSON 对象。 */
export type ExportFormatConfig = Record<string, unknown>;

// ---- 有稳定形状的写路径载荷（与后端 Pydantic 模型字段逐字对应） ----

/** POST /schemes/{id}/sections/save-outline 请求体 */
export interface OutlineSavePayload {
  outline: unknown[];
  source?: string;
  /** AI 生成目录确认保存时锁定一级章节（后端 save-outline 可选开关） */
  lock_roots?: boolean;
  /** ✅ R175 G-3 逃生舱：显式保留「无子章节的一级章」（空壳章，多为分步生成
   *  失败章或手工误留）。默认不传 ⇒ 后端 400 闸门行为与旧版本完全一致；
   *  仅在用户于空章确认框点「仍要保存」后置 true，此时后端响应会加法式
   *  回传 empty_top_chapters 明细（由 notifyGenericResidual 统一提示）。 */
  allow_empty_chapters?: boolean;
  /** ✅ R179 N1（2026-11-05）：保存前在同一事务内先建目录快照（结构 + 正文全量）。
   *  取值即快照 type：`outline_auto_confirm`（自动确认前，可一键回退）。
   *  不传 ⇒ 后端零行为变化（不建快照、响应不含 snapshot_id）。 */
  snapshot_before?: string;
}

/** ✅ 阶段 2 旁路：POST /schemes/{id}/sections/generate-outline-omp 请求体（全部可选）。
 *  brief 缺省由后端从方案名/类型/专业派生；project_type/industry 缺省也从方案取。 */
export interface GenerateOutlineOmpPayload {
  brief?: string;
  project_type?: string;
  industry?: string;
  extra_instructions?: string;
  allow_empty_chapters?: boolean;
}

/** ✅ 阶段 2：GET /system/feature-flags 响应（受控功能开关；后续加法式扩展） */
export interface FeatureFlags {
  omp_task_enabled: boolean;
}

/** POST /outline-library/{id}/review 请求体 */
export interface OutlineReviewPayload {
  status: string;
}

/** POST /outline-library/batch-review 请求体 */
export interface OutlineBatchReviewPayload {
  ids: string[];
  status: string;
}

/** POST /outline-library/{id}/new-version 请求体 */
export interface OutlineNewVersionPayload {
  version: string;
  outline_json: string;
}

/** POST /outline-library/{id}/restore-version 请求体 */
export interface OutlineRestoreVersionPayload {
  version_id: string;
}

/** POST /outline-library/{id}/apply-and-save 请求体 */
export interface OutlineApplyAndSavePayload {
  scheme_id: string;
}

/** POST /global-facts（分组新增）与 PATCH /global-facts/{id}（分组更新）请求体 */
export interface FactGroupUpsertPayload {
  /** 后端 FactGroupUpdate 要求 id 必填（更新时一并提交） */
  id?: string;
  title?: string;
  content?: string;
  category?: string;
  /** 单条修正模式：PATCH item_updates，不重建整个分组 */
  item_updates?: Array<Record<string, unknown>>;
}

/** POST /compliance/check 请求体（清单未被手工改动时带 rule_ids，否则带 checklist） */
export interface ComplianceCheckPayload {
  scheme_id: string;
  rule_ids?: string[];
  checklist?: string[];
}

/** POST /compliance/expert-review 请求体 */
export interface ComplianceExpertReviewPayload {
  scheme_id: string;
}

/** POST /schemes/{id}/consistency/scan 请求体 */
export interface ConsistencyScanPayload {
  scope?: string;
  include_global_facts?: boolean;
  include_project_docs?: boolean;
  include_design_docs?: boolean;
}

/** POST /schemes/{id}/consistency/repair 请求体 */
export interface ConsistencyRepairPayload {
  scan_id?: string;
  mode?: string;
  severity_threshold?: string;
  conflict_ids?: string[];
  force_full_repair?: boolean;
}

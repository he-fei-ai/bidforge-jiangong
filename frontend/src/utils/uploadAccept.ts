/**
 * 上传文件类型白名单（前端 Upload 组件 accept 单一数据源）。
 *
 * ✅ 2026-09-23 抽离：此前「上传解析」Tab 与「全局事实」Tab 两处各硬编码一份
 *    accept 字符串，后端支持列表（app/services/file_parser.py 的
 *    SUPPORTED_EXTENSIONS）一旦增减格式，前端两处易漏改、与后端口径漂移
 *    （用户看到能选的格式后端却 400 拒绝 / 反之）。现统一在此收口，两处复用；
 *    改动格式只需改这一处 + 同步后端 SUPPORTED_EXTENSIONS。
 *
 * ⚠️ 必须与后端 `file_parser.SUPPORTED_EXTENSIONS` 保持一致：
 *    docx / pdf / md / txt / xlsx / xls / csv / png / jpg / jpeg / bmp / tiff / doc / wps
 */
export const UPLOAD_FILE_EXTENSIONS: string[] = [
  "docx", "pdf", "md", "txt", "xlsx", "xls", "csv",
  "png", "jpg", "jpeg", "bmp", "tiff",
  "doc", "wps", // 旧版 Word（需本地 Office 组件转换，见后端 legacy_office.py）
];

/** 供 antd Upload 的 accept 属性使用（".ext" 逗号分隔） */
export const UPLOAD_FILE_ACCEPT: string = UPLOAD_FILE_EXTENSIONS
  .map((ext) => `.${ext}`)
  .join(",");

/**
 * 单文件大小上限（30MB）。
 *
 * ⚠️ 必须与后端 `global_facts.MAX_UPLOAD_BYTES`（经 file_parser
 *    `_resolve_upload_max_bytes()` 生效）保持一致：accept 只过滤文件选择框，
 *    拖拽或切换选择器过滤器即可绕过，因此前端必须再做一次硬校验，避免用户
 *    白等一整个大文件上传后才被后端 413/400 拒绝。
 */
// 兜底默认：后端下发值尚未到达 / 请求失败时使用。正常口径以后端
// GET /api/v1/system/upload-limits 的 max_upload_bytes 为准（单一事实源），
// 此常量不再作为"权威上限"，仅保证后端不可用时前端仍有保守拦截。
export const MAX_UPLOAD_BYTES: number = 30 * 1024 * 1024;

/**
 * 解析「单文件体积上限」的生效值：优先后端下发（configured），缺失 / 非法回落
 * `MAX_UPLOAD_BYTES`（30MB）。
 *
 * ✅ 2026-10-05 收敛：此前方案工作台、目录库编辑弹窗等多处入口各写一份
 *    `x > 0 ? x : 30MB` 判定，一旦有的入口用了动态值、有的仍写死，就出现
 *    "同一次上传两处提示上限不同"的口径漂移。现统一走本函数（唯一口径）。
 */
export function resolveMaxUploadBytes(configured?: number): number {
  const v = Number(configured);
  return Number.isFinite(v) && v > 0 ? v : MAX_UPLOAD_BYTES;
}

/**
 * 单文件体积上限（字节）→ 整 MB 文案，用于「请上传 N MB 以内」类提示。
 * 入参缺失 / 非法时随 `resolveMaxUploadBytes` 回落 30MB（与历史文案逐字一致）。
 */
export function formatUploadLimitMb(configured?: number): string {
  return `${Math.round(resolveMaxUploadBytes(configured) / (1024 * 1024))}MB`;
}

export type RejectedUploadReason = "type" | "size";
export type RejectedUpload = { file: File; reason: RejectedUploadReason };
export type PartitionedUploadFiles = {
  accepted: File[];
  rejected: RejectedUpload[];
};

function fileExtension(name: string): string {
  const dot = (name || "").lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/**
 * 上传前硬校验：把一批文件按「扩展名白名单 + 30MB 上限」拆成
 * accepted / rejected。扩展名大小写不敏感（A.PDF 合法）；无扩展名按
 * 类型非法处理。纯函数，便于组件测试直接钉住边界。
 */
export function partitionUploadFiles(
  files: File[],
  maxBytes: number = MAX_UPLOAD_BYTES,
): PartitionedUploadFiles {
  const allowed = new Set(UPLOAD_FILE_EXTENSIONS);
  const accepted: File[] = [];
  const rejected: RejectedUpload[] = [];
  // 非法（非正数）入参等价于"无上限"，比保守默认更危险，故回落兜底值。
  const limit = resolveMaxUploadBytes(maxBytes);
  for (const file of files || []) {
    if (!allowed.has(fileExtension(file.name))) {
      rejected.push({ file, reason: "type" });
    } else if (file.size > limit) {
      rejected.push({ file, reason: "size" });
    } else {
      accepted.push(file);
    }
  }
  return { accepted, rejected };
}

export type SplitByTotalQuota = {
  /** 本次可以安全上传的文件（累计体积 ≤ 上限） */
  accepted: File[];
  /** 因累计体积触顶而被扣下的文件（含触顶那一份及其后全部） */
  held: File[];
};

/**
 * 单次请求文件数上限兜底（后端 config.upload_max_files_per_request 默认值）。
 *
 * ⚠️ 与 `upload_max_total_bytes` 同源：正常口径以
 *    `GET /api/v1/system/upload-limits` 的 `max_files_per_request` 为准；
 *    该值缺失 / 请求失败时用此兜底，避免前端退回「无数量上限」。
 */
export const MAX_UPLOAD_FILES_FALLBACK: number = 20;

/**
 * 上传前的**单次文件数**预检：与后端 `global_facts.upload_documents` 的
 * `MAX_UPLOAD_FILES_PER_REQUEST`（`upload_max_files_per_request`）同口径。
 *
 * ✅ 2026-10-05 补齐功能缺口：此前前端只预检了「单文件体积」与「累计体积」，
 *    唯独漏了「文件数」—— 用户一次性选 25 个文件（上限 20）时，前面 20 个照常
 *    上传、后 5 个要等整批传完才从响应里得知"没保存"，与另两项预检的体验割裂。
 *
 * 后端判据（global_facts.py）：按请求顺序取**前 N 个**，超出的记入 `too_many`
 * 且不回滚前 N 个。本函数逐字复刻该顺序语义（保前 N、其余 held），使前端能在
 * 发起请求前就告知用户哪些文件不会被保存。
 *
 * ⚠️ 非法（非正数 / NaN）入参等价于"无上限"比保守默认更危险，故回落兜底值。
 */
export function splitByUploadFileCount(
  files: File[],
  maxFiles: number = MAX_UPLOAD_FILES_FALLBACK,
): SplitByTotalQuota {
  const parsed = Number(maxFiles);
  const limit = Number.isFinite(parsed) && parsed > 0
    ? Math.floor(parsed)
    : MAX_UPLOAD_FILES_FALLBACK;
  const accepted: File[] = [];
  const held: File[] = [];
  (files || []).forEach((file, index) => {
    if (index < limit) accepted.push(file);
    else held.push(file);
  });
  return { accepted, held };
}

/**
 * 上传前的**累计体积配额**预检：与后端 `global_facts.upload_documents` 的
 * 「单次请求累计体积上限」（`upload_max_total_bytes`，默认 200MB）同口径。
 *
 * 后端判据（global_facts.py）：按请求顺序逐份落盘，`累计已保存 + 本份 > 上限`
 * 时**立即停止**处理，并把触顶文件与排在其后的全部剩余文件一并记入
 * `quota_files`（2026-10-05 修复「静默丢文件名」）。本函数逐字复刻该顺序语义，
 * 使前端能在**发起请求之前**就告知用户哪些文件不会被保存 —— 否则用户要白等
 * 整批上传完成，才从响应里得知"后面几个其实没存"。
 *
 * ⚠️ 非法（非正数）入参等价于"无上限"会比保守默认更危险，故回落内置兜底值
 *    `MAX_UPLOAD_TOTAL_FALLBACK`（与后端 config.upload_max_total_bytes 默认值一致）。
 *    正常口径以 `GET /api/v1/system/upload-limits` 的 `max_total_bytes` 为准。
 */
export const MAX_UPLOAD_TOTAL_FALLBACK: number = 200 * 1024 * 1024;

export function splitByUploadTotalQuota(
  files: File[],
  maxTotalBytes: number = MAX_UPLOAD_TOTAL_FALLBACK,
): SplitByTotalQuota {
  const limit =
    Number(maxTotalBytes) > 0 ? Number(maxTotalBytes) : MAX_UPLOAD_TOTAL_FALLBACK;
  const accepted: File[] = [];
  const held: File[] = [];
  let used = 0;
  let tripped = false;
  for (const file of files || []) {
    const size = Number(file?.size) || 0;
    // 触顶后剩余文件与触顶文件同因（配额耗尽）不会被保存，全部记为 held。
    if (tripped) {
      held.push(file);
      continue;
    }
    if (used + size > limit) {
      held.push(file);
      tripped = true;
      continue;
    }
    used += size;
    accepted.push(file);
  }
  return { accepted, held };
}

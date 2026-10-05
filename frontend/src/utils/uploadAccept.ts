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
  const limit = Number(maxBytes) > 0 ? Number(maxBytes) : MAX_UPLOAD_BYTES;
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

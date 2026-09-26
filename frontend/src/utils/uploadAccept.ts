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

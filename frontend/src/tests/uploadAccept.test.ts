/**
 * 上传格式白名单单一数据源测试。
 *
 * ✅ 2026-09-23：UPLOAD_FILE_ACCEPT 抽离为单一数据源，供「上传解析」Tab、
 * 「全局事实」Tab、目录库编辑弹窗共用。本测试钉住其**集合**与后端
 * app/services/file_parser.py 的 SUPPORTED_EXTENSIONS 一致 —— 任一侧增减格式
 * 都会让前端 accept 与后端口径漂移（用户能选的格式后端却 400 / 反之）。
 *
 * 后端集合在此硬编码镜像（与 file_parser.SUPPORTED_EXTENSIONS 同步维护），
 * 用「集合相等」断言防止漏改一侧。新增/删除格式须同时改：
 *   - backend: app/services/file_parser.py :: SUPPORTED_EXTENSIONS
 *   - frontend: src/utils/uploadAccept.ts :: UPLOAD_FILE_EXTENSIONS
 */
import { describe, it, expect } from "vitest";
import { UPLOAD_FILE_ACCEPT, UPLOAD_FILE_EXTENSIONS } from "../utils/uploadAccept";

/** 镜像后端 file_parser.SUPPORTED_EXTENSIONS（2026-09-23） */
const BACKEND_SUPPORTED_EXTENSIONS = [
  "docx", "pdf", "md", "txt", "xlsx", "xls", "csv",
  "png", "jpg", "jpeg", "bmp", "tiff",
  "doc", "wps",
];

describe("UPLOAD_FILE_ACCEPT", () => {
  it("扩展名集合与后端 SUPPORTED_EXTENSIONS 完全一致", () => {
    const front = new Set(UPLOAD_FILE_EXTENSIONS.map((e) => e.toLowerCase()));
    const back = new Set(BACKEND_SUPPORTED_EXTENSIONS.map((e) => e.toLowerCase()));
    expect(front).toEqual(back);
  });

  it("accept 字符串为 '.ext' 逗号分隔，且覆盖全部扩展名", () => {
    // 以逗号或结尾都能切出每个扩展名
    const parts = UPLOAD_FILE_ACCEPT.split(",").map((s) => s.replace(/^\./, ""));
    const parsed = new Set(parts);
    for (const ext of UPLOAD_FILE_EXTENSIONS) {
      expect(parsed.has(ext)).toBe(true);
    }
    // 不含空格、不以逗号开头/结尾
    expect(UPLOAD_FILE_ACCEPT.startsWith(",")).toBe(false);
    expect(UPLOAD_FILE_ACCEPT.endsWith(",")).toBe(false);
    expect(UPLOAD_FILE_ACCEPT.includes(" ")).toBe(false);
  });

  it("含旧版 Word 格式（.doc/.wps），对齐招投标场景", () => {
    expect(UPLOAD_FILE_EXTENSIONS).toContain("doc");
    expect(UPLOAD_FILE_EXTENSIONS).toContain("wps");
  });
});

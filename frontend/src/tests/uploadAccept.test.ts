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
import {
  UPLOAD_FILE_ACCEPT,
  UPLOAD_FILE_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_TOTAL_FALLBACK,
  MAX_UPLOAD_FILES_FALLBACK,
  splitByUploadTotalQuota,
  splitByUploadFileCount,
  resolveMaxUploadBytes,
  formatUploadLimitMb,
} from "../utils/uploadAccept";

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

/** 仅用到 name/size 的桩对象（本函数不触碰 File 的其它成员） */
function fakeFile(name: string, size: number) {
  return { name, size } as unknown as File;
}

describe("splitByUploadTotalQuota（累计体积配额预检）", () => {
  it("全部文件累计未超上限：原样放行、顺序不变", () => {
    const files = [fakeFile("a.pdf", 30), fakeFile("b.docx", 40), fakeFile("c.txt", 30)];
    const r = splitByUploadTotalQuota(files, 100);
    expect(r.accepted.map((f) => f.name)).toEqual(["a.pdf", "b.docx", "c.txt"]);
    expect(r.held).toEqual([]);
  });

  it("恰好等于上限仍放行（后端判据是 `累计 + 本份 > 上限`，闭区间）", () => {
    const r = splitByUploadTotalQuota([fakeFile("a", 60), fakeFile("b", 40)], 100);
    expect(r.accepted).toHaveLength(2);
    expect(r.held).toEqual([]);
  });

  it("触顶文件与其后的**全部**剩余文件一并记为 held（对齐后端 quota_files）", () => {
    // 后端 global_facts.upload_documents：触顶即 break，并把 files[_idx+1:]
    // 一并 extend 进 quota_files —— 修复「静默丢文件名」（2026-10-05）。
    // 旧的前端缺口：这批文件要等整批上传完成才被扣下，用户事前毫无预期。
    const files = [
      fakeFile("a", 50),
      fakeFile("b", 40),
      fakeFile("c", 30), // 50+40+30 = 120 > 100 → 触顶
      fakeFile("d", 1),
      fakeFile("e", 1),
    ];
    const r = splitByUploadTotalQuota(files, 100);
    expect(r.accepted.map((f) => f.name)).toEqual(["a", "b"]);
    expect(r.held.map((f) => f.name)).toEqual(["c", "d", "e"]);
  });

  it("单份即超上限：整批全部 held、accepted 为空（调用方据此直接早退）", () => {
    const r = splitByUploadTotalQuota([fakeFile("big", 200)], 100);
    expect(r.accepted).toEqual([]);
    expect(r.held.map((f) => f.name)).toEqual(["big"]);
  });

  it("非正数/非法上限回落内置兜底 200MB（不得等价于「无上限」）", () => {
    expect(MAX_UPLOAD_TOTAL_FALLBACK).toBe(200 * 1024 * 1024);
    const r = splitByUploadTotalQuota([fakeFile("a", 200 * 1024 * 1024 + 1)], 0);
    expect(r.accepted).toEqual([]);
    expect(r.held).toHaveLength(1);
    // 200MB 以内仍放行
    const ok = splitByUploadTotalQuota([fakeFile("a", 200 * 1024 * 1024)], -5);
    expect(ok.accepted).toHaveLength(1);
    expect(ok.held).toEqual([]);
  });

  it("空批次 / 缺 size 的入参不抛异常（fail-soft，纯函数）", () => {
    expect(splitByUploadTotalQuota([], 100)).toEqual({ accepted: [], held: [] });
    const r = splitByUploadTotalQuota([fakeFile("a", NaN)], 100);
    expect(r.accepted).toHaveLength(1);
  });
});

describe("splitByUploadFileCount（单次文件数预检，2026-10-05 补齐）", () => {
  it("兜底常量与后端 upload_max_files_per_request 默认值一致（20）", () => {
    expect(MAX_UPLOAD_FILES_FALLBACK).toBe(20);
  });

  it("未超上限：原样放行、顺序不变", () => {
    const files = [fakeFile("a", 1), fakeFile("b", 1), fakeFile("c", 1)];
    const r = splitByUploadFileCount(files, 3);
    expect(r.accepted.map((f) => f.name)).toEqual(["a", "b", "c"]);
    expect(r.held).toEqual([]);
  });

  it("恰好等于上限仍全部放行（闭区间）", () => {
    const files = Array.from({ length: 5 }, (_, i) => fakeFile(`f${i}`, 1));
    const r = splitByUploadFileCount(files, 5);
    expect(r.accepted).toHaveLength(5);
    expect(r.held).toEqual([]);
  });

  it("超上限：保留**前 N 个**（对齐后端 files[:N]），其余 held 顺序不变", () => {
    // 后端 global_facts.upload_documents：len(files) > N 时取前 N 个，
    // 其余记入 too_many（不回滚前 N 个）。前端预检须同序，否则提示的文件
    // 与实际未保存的不是同一批。
    const files = Array.from({ length: 7 }, (_, i) => fakeFile(`f${i}`, 1));
    const r = splitByUploadFileCount(files, 5);
    expect(r.accepted.map((f) => f.name)).toEqual(["f0", "f1", "f2", "f3", "f4"]);
    expect(r.held.map((f) => f.name)).toEqual(["f5", "f6"]);
  });

  it("上限为 1：仅首份放行", () => {
    const r = splitByUploadFileCount([fakeFile("a", 1), fakeFile("b", 1)], 1);
    expect(r.accepted.map((f) => f.name)).toEqual(["a"]);
    expect(r.held.map((f) => f.name)).toEqual(["b"]);
  });

  it("非正数 / NaN / 非法上限 → 回落兜底 20（不得等价于「无上限」）", () => {
    const files = Array.from({ length: 25 }, (_, i) => fakeFile(`f${i}`, 1));
    for (const bad of [0, -3, Number.NaN, Number.POSITIVE_INFINITY]) {
      const r = splitByUploadFileCount(files, bad as number);
      expect(r.accepted).toHaveLength(20);
      expect(r.held).toHaveLength(5);
    }
  });

  it("空批次 / undefined 入参不抛异常（fail-soft，纯函数）", () => {
    expect(splitByUploadFileCount([], 5)).toEqual({ accepted: [], held: [] });
    expect(splitByUploadFileCount(undefined as unknown as File[], 5)).toEqual({
      accepted: [],
      held: [],
    });
  });
});

describe("resolveMaxUploadBytes / formatUploadLimitMb（单文件上限唯一口径，2026-10-05 D3）", () => {
  it("下发有效值：原样返回，文案为整 MB", () => {
    expect(resolveMaxUploadBytes(50 * 1024 * 1024)).toBe(50 * 1024 * 1024);
    expect(formatUploadLimitMb(50 * 1024 * 1024)).toBe("50MB");
  });

  it("缺失 / 非法（0 / 负数 / NaN / Infinity）：回落 30MB 兜底，文案 30MB", () => {
    for (const bad of [undefined, 0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(resolveMaxUploadBytes(bad as number)).toBe(MAX_UPLOAD_BYTES);
      expect(formatUploadLimitMb(bad as number)).toBe("30MB");
    }
  });

  it("非整 MB：四舍五入为整数 MB（提示不出现小数）", () => {
    expect(formatUploadLimitMb(10.4 * 1024 * 1024)).toBe("10MB");
    expect(formatUploadLimitMb(10.6 * 1024 * 1024)).toBe("11MB");
  });
});

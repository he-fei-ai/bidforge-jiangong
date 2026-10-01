/**
 * 解析可信度提示派生测试（parseSourceNotice）。
 *
 * 钉住的不变量：
 *  1. sourceNotice —— 报告为空/未截断 → null（不得误报）；被截断 → warning 且
 *     带文件名；预算耗尽 → warning；截断优先于跳过（更严重的先说）。
 *  2. previewNotices —— **解析级截断与预览级截取互不覆盖**（旧实现的核心缺口：
 *     50 页后被截断的 PDF 正文很短，preview_truncated=false，只有解析级信号
 *     能暴露问题，若被预览级信号覆盖就等于用户永远看不到）。
 *  3. 两种提示的 kind 语义固定：warning = 内容真的可能缺；info = 仅展示层截取。
 */
import { describe, expect, it } from "vitest";
import {
  previewNotices,
  sourceNotice,
  type ParseNotice,
  type SourceReportLike,
} from "../utils/parseSourceNotice";

describe("sourceNotice（提取依据完整性）", () => {
  it("空报告 / 未截断 → null（不得误报）", () => {
    expect(sourceNotice(null)).toBeNull();
    expect(sourceNotice(undefined)).toBeNull();
    expect(sourceNotice({})).toBeNull();
    expect(sourceNotice({ source_truncated: false })).toBeNull();
    expect(sourceNotice({
      source_truncated: false,
      used_doc_count: 3,
      input_doc_count: 3,
      dropped_doc_count: 0,
      truncated_docs: [],
    })).toBeNull();
  });

  it("被截断的文档 → warning，且给出文件名", () => {
    const report: SourceReportLike = {
      source_truncated: true,
      truncated_docs: [{ name: "招标文件.pdf", category: "招标文件", chars: 30000 }],
      used_doc_count: 2,
      input_doc_count: 2,
      dropped_doc_count: 0,
    };
    const n = sourceNotice(report);
    expect(n).not.toBeNull();
    expect(n!.kind).toBe("warning");
    expect(n!.title).toContain("招标文件.pdf");
    expect(n!.title).toContain("截断");
    // 无跳过时不追加 detail
    expect(n!.detail).toBeUndefined();
  });

  it("仅预算耗尽（无被截断文档）→ warning 且提示拆分", () => {
    const n = sourceNotice({ source_truncated: true, dropped_doc_count: 2 });
    expect(n).not.toBeNull();
    expect(n!.kind).toBe("warning");
    expect(n!.title).toContain("2 份");
    expect(n!.detail || "").toContain("拆分");
  });

  it("截断与跳过并存 → 先说截断（更严重），跳过数放进 detail", () => {
    const n = sourceNotice({
      source_truncated: true,
      truncated_docs: [{ name: "招标文件.pdf" }, { name: "地勘报告.pdf" }],
      dropped_doc_count: 1,
    });
    expect(n!.title).toContain("招标文件.pdf");
    expect(n!.detail || "").toContain("1 份");
  });

  it("多文件名超过 3 个时折叠（避免标题过长挤爆告警条）", () => {
    const n = sourceNotice({
      source_truncated: true,
      truncated_docs: [{ name: "a.pdf" }, { name: "b.pdf" },
                       { name: "c.pdf" }, { name: "d.pdf" }],
    });
    expect(n!.title).toContain("a.pdf、b.pdf、c.pdf");
    expect(n!.title).toContain("等 4 份");
    expect(n!.title).not.toContain("d.pdf");
  });

  it("空 name 的条目被过滤（后端契约缺失时不得渲染空白）", () => {
    expect(sourceNotice({ source_truncated: true, truncated_docs: [{}, { name: "" }] }))
      .not.toBeNull();
    // 无有效文件名、也无跳过数 → 退化为通用文案
    expect(sourceNotice({ source_truncated: true, truncated_docs: [{}] }))
      .toMatchObject({ kind: "warning", title: "提取依据不完整：部分源文档内容缺失" });
  });
});

describe("previewNotices（预览弹窗提示）", () => {
  it("完全正常的文档 → 空数组（不干扰阅读）", () => {
    expect(previewNotices(null)).toEqual([]);
    expect(previewNotices(undefined)).toEqual([]);
    expect(previewNotices({ is_parsed: true, text_len: 500 })).toEqual([]);
  });

  it("解析级截断 → warning，且不会被预览级截取覆盖", () => {
    const notices: ParseNotice[] = previewNotices({
      is_parsed: true,
      text_len: 500,
      truncated: true,
      preview_truncated: false,
    });
    expect(notices).toHaveLength(1);
    expect(notices[0].kind).toBe("warning");
    expect(notices[0].title).toContain("不完整");
    expect(notices[0].detail || "").toContain("拆分");
  });

  it("解析级截断 + 预览也被截取 → 只报 warning（更严重的优先）", () => {
    const notices = previewNotices({
      text_len: 120000, truncated: true, preview_truncated: true,
    });
    expect(notices).toHaveLength(1);
    expect(notices[0].kind).toBe("warning");
    expect(notices[0].title).not.toContain("预览仅显示");
  });

  it("仅预览级截取 → info，并说明正文总长", () => {
    const notices = previewNotices({
      text_len: 20000, truncated: false, preview_truncated: true,
    });
    expect(notices).toHaveLength(1);
    expect(notices[0].kind).toBe("info");
    expect(notices[0].title).toContain("预览仅显示");
    expect(notices[0].detail || "").toContain("20,000");
  });

  it("解析告警 → info，逐条拼接（编码降级 / OCR 兜底等诊断必须知情）", () => {
    const notices = previewNotices({
      is_parsed: true, text_len: 800,
      parse_warnings: ["第 50 页之后已截断", "源文件编码无法识别，已按 UTF-8 容错解码"],
    });
    // 无 truncated / preview_truncated 时只有告警一条
    expect(notices).toHaveLength(1);
    expect(notices[0].kind).toBe("info");
    expect(notices[0].title).toContain("2 条");
    expect(notices[0].detail || "").toContain("第 50 页之后已截断");
    expect(notices[0].detail || "").toContain("编码无法识别");
  });

  it("解析告警为非数组/含空串 → 降级为空提示（不得抛错或渲染空白）", () => {
    expect(previewNotices({ is_parsed: true, text_len: 100,
                            parse_warnings: undefined })).toEqual([]);
    expect(previewNotices({ is_parsed: true, text_len: 100,
                            parse_warnings: [] })).toEqual([]);
    // @ts-expect-error 故意传非数组，验证防御性
    expect(previewNotices({ parse_warnings: "不是数组" })).toEqual([]);
    // 后端 JSON 里可能夹带 null（历史脏数据）：过滤掉后仍保留有效告警
    const noisy = ["", null, "有效告警"] as unknown as string[];
    const notices = previewNotices({ is_parsed: true, parse_warnings: noisy });
    expect(notices).toHaveLength(1);
    expect(notices[0].detail || "").toContain("有效告警");
  });

  it("三种信号同时存在 → warning 与 info 各一条，顺序稳定", () => {
    const notices = previewNotices({
      is_parsed: true, text_len: 500,
      truncated: true, preview_truncated: false,
      parse_warnings: ["第 50 页之后已截断"],
    });
    expect(notices.map((n) => n.kind)).toEqual(["warning", "info"]);
  });

  it("未解析文档 → 空数组（由调用方用 message 提示，不重复）", () => {
    expect(previewNotices({ is_parsed: false, message: "该文档尚未解析，请先执行解析" }))
      .toEqual([]);
  });
});

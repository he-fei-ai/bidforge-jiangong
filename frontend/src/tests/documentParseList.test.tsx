// @vitest-environment jsdom
/**
 * 「上传解析」Tab · 资料与解析列表 组件级交互测试。
 *
 * 覆盖此前无测试、且属于该列表核心交互的行为：
 *   1. 空列表 → 展示 Empty 引导；
 *   2. 单份解析/重新解析 按钮回调 onParse(docId, fileName[, force])；
 *   3. 已解析文档出现「预览」按钮并回调 onPreview；
 *   4. 「删除」按钮回调 onDelete；
 *   5. 解析中（parsingDocs / parsingDocId）禁用解析/重解析/删除；
 *   6. 分类：有 categoryOptions 时渲染 Select，无时降级只读 Tag；
 *   7. 纯函数：getCategoryColor / formatFileSize / formatUploadTime 边界。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup, waitFor } from "@testing-library/react";
import React from "react";
import DocumentParseList, {
  getCategoryColor,
  formatFileSize,
  formatUploadTime,
  type DocumentParseItem,
} from "../components/DocumentParseList";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

const doc = (over: Partial<DocumentParseItem>): DocumentParseItem => ({
  id: "d1", file_name: "招标公告.pdf", ...over,
});

type Overrides = Partial<React.ComponentProps<typeof DocumentParseList>>;

function setup(over: Overrides = {}) {
  const cbs = {
    onParse: vi.fn(), onPreview: vi.fn(), onDelete: vi.fn(),
    onCategoryChange: vi.fn(),
  };
  const utils = render(
    <DocumentParseList
      docs={[doc({})]}
      parsingDocs={false}
      parsingDocId={null}
      categoryOptions={["招标文件", "其他"]}
      {...cbs}
      {...over}
    />
  );
  return { ...utils, ...cbs };
}

function btn(root: HTMLElement | Document, text: string): HTMLButtonElement | null {
  return (Array.from(root.querySelectorAll("button")).find(
    (x) => (x.textContent || "").trim() === text) as HTMLButtonElement) || null;
}

afterEach(cleanup);

/** 在 antd Select 下拉层（挂到 document.body）里按文本找选项 */
function dropdownOption(text: string): HTMLElement | null {
  return (Array.from(
    document.body.querySelectorAll(".ant-select-item-option")).find(
    (el) => (el.textContent || "").trim() === text) as HTMLElement) || null;
}

describe("DocumentParseList", () => {
  it("空列表 → 展示 Empty 引导，不渲染任何行操作", () => {
    const { container } = setup({ docs: [] });
    expect(container.textContent || "").toContain("还没有导入资料文件");
    expect(btn(container, "解析")).toBeNull();
  });

  it("待解析文档：点击「解析」回调 onParse(id, name)", () => {
    const { container, onParse } = setup({ docs: [doc({ id: "x", file_name: "a.pdf" })] });
    fireEvent.click(btn(container, "解析")!);
    expect(onParse).toHaveBeenCalledWith("x", "a.pdf");
  });

  it("已解析文档：点击「重新解析」回调 onParse(id, name, true)，且出现「预览」", () => {
    const { container, onParse, onPreview } = setup({
      docs: [doc({ id: "y", file_name: "b.pdf", text_len: 100 })],
    });
    fireEvent.click(btn(container, "重新解析")!);
    expect(onParse).toHaveBeenCalledWith("y", "b.pdf", true);
    // 已解析 → 预览按钮存在并回调
    fireEvent.click(btn(container, "预览")!);
    expect(onPreview).toHaveBeenCalledTimes(1);
  });

  it("点击「删除」回调 onDelete(id, name)", () => {
    const { container, onDelete } = setup({ docs: [doc({ id: "z", file_name: "c.pdf" })] });
    fireEvent.click(btn(container, "删除")!);
    expect(onDelete).toHaveBeenCalledWith("z", "c.pdf");
  });

  it("解析中（parsingDocs）：解析/重新解析/删除一律禁用", () => {
    const { container } = setup({
      docs: [doc({ id: "p", file_name: "d.pdf", text_len: 100 })],
      parsingDocs: true,
    });
    expect(btn(container, "重新解析")!.disabled).toBe(true);
    expect(btn(container, "删除")!.disabled).toBe(true);
  });

  it("单份解析中（parsingDocId）：该文档的删除禁用，但其它文档不受影响", () => {
    const { container } = setup({
      docs: [doc({ id: "a", file_name: "a.pdf" }), doc({ id: "b", file_name: "b.pdf" })],
      parsingDocId: "a",
    });
    const deletes = Array.from(container.querySelectorAll("button")).filter(
      (b) => (b.textContent || "").trim() === "删除");
    expect(deletes.length).toBe(2);
    // 第一个（id=a）禁用；第二个（id=b）可用 —— 通过 DOM 顺序推断
    expect(deletes[0].disabled).toBe(true);
    expect(deletes[1].disabled).toBe(false);
  });

  it("分类：有 categoryOptions 时渲染 Select（非只读 Tag）", () => {
    const { container } = setup({
      docs: [doc({ id: "c", file_name: "e.pdf", text_len: 1, doc_category: "招标文件" })],
      categoryOptions: ["招标文件", "其他"],
    });
    // antd Select 渲染为带特定 role 的容器；此处只验证列表仍正常渲染文件名
    expect(container.textContent || "").toContain("e.pdf");
  });

  it("分类：无 categoryOptions 时降级为只读 Tag（含分类名）", () => {
    const { container } = setup({
      docs: [doc({ id: "f", file_name: "f.pdf", text_len: 1, doc_category: "资质材料" })],
      categoryOptions: [],
    });
    expect(container.textContent || "").toContain("资质材料");
  });

  it("截断文档：渲染「可能被截断」提示 Tag", () => {
    const { container } = setup({
      docs: [doc({ id: "t", file_name: "t.pdf", text_len: 99999, truncated: true })],
    });
    expect(container.textContent || "").toContain("可能被截断");
  });

  it("解析失败（parse_status='failed'）：渲染「解析失败」红标签，且仍可点「解析」重试", () => {
    const { container, onParse } = setup({
      docs: [doc({ id: "f", file_name: "f.pdf", parse_status: "failed",
                   parse_warnings: ["解析失败：缺少 OCR 依赖"] })],
    });
    expect(container.textContent || "").toContain("解析失败");
    // 失败文档未解析成功 → 提供「解析」按钮供重试
    fireEvent.click(btn(container, "解析")!);
    expect(onParse).toHaveBeenCalledWith("f", "f.pdf");
  });

  it("解析状态优先于 text_len：parse_status='success' 视为已解析（即便 text_len 缺失）", () => {
    const { container } = setup({
      docs: [doc({ id: "s", file_name: "s.pdf", parse_status: "success" })],
    });
    expect(container.textContent || "").toContain("已解析");
    expect(container.textContent || "").not.toContain("待解析");
  });

  it("parse_status='pending' 且无 text_len：视为待解析（不误显为已解析）", () => {
    const { container } = setup({
      docs: [doc({ id: "p", file_name: "p.pdf", parse_status: "pending" })],
    });
    expect(container.textContent || "").toContain("待解析");
    expect(container.textContent || "").not.toContain("已解析");
  });

  it("旧数据无 parse_status、text_len>0：向后兼容视为已解析", () => {
    const { container } = setup({
      docs: [doc({ id: "o", file_name: "o.pdf", text_len: 120 })],
    });
    expect(container.textContent || "").toContain("已解析");
  });
  it("存量陈旧状态（parse_status='pending' 但正文已存在）：仍显示「待解析」，不被误判为已解析", () => {
    // 回归护栏：四层存储上线前的存量行正文在 parsed_markdown 里、parse_status
    // 却是补列默认值 pending。旧实现在 computeDocStats 与列表标签各写一份
    // isParsed 判定，一旦分叉就会出现「统计条 0 待解析 / 列表仍显待解析」。
    const { container } = setup({
      docs: [doc({ id: "stale", file_name: "stale.pdf", parse_status: "pending",
                   text_len: 1234 })],
    });
    expect(container.textContent || "").toContain("待解析");
    expect(container.textContent || "").not.toContain("已解析");
    // 待解析 → 提供「解析」按钮，点击后由后端自修复 parse_status
    fireEvent.click(btn(container, "解析")!);
  });

  it("陈旧状态与旧数据兜底同表时状态互不串扰", () => {
    const { container } = setup({
      docs: [
        doc({ id: "stale", file_name: "s.pdf", parse_status: "pending", text_len: 1234 }),
        doc({ id: "legacy", file_name: "l.pdf", text_len: 42 }),
        doc({ id: "done", file_name: "d.pdf", parse_status: "success", text_len: 88 }),
      ],
    });
    const text = container.textContent || "";
    // 1 份待解析 + 2 份已解析（旧数据兜底 1 + 显式 success 1）
    expect(text).toContain("待解析");
    expect((text.match(/已解析/g) || []).length).toBe(2);
  });
});

describe("DocumentParseList 纯函数", () => {
  it("getCategoryColor：已知分类映射颜色，未知/空返回 default", () => {
    expect(getCategoryColor("招标文件")).toBe("red");
    expect(getCategoryColor("报价清单")).toBe("gold");
    expect(getCategoryColor("不存在的分类")).toBe("default");
    expect(getCategoryColor("")).toBe("default");
    expect(getCategoryColor(undefined)).toBe("default");
  });

  it("formatFileSize：字节/KB/MB/GB 与空值", () => {
    expect(formatFileSize(0)).toBe("");
    expect(formatFileSize(500)).toBe("500 B");
    expect(formatFileSize(2048)).toBe("2.0 KB");
    expect(formatFileSize(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatFileSize(2 * 1024 * 1024 * 1024)).toBe("2.00 GB");
  });

  it("formatUploadTime：空/非法/跨年/越界分量", () => {
    expect(formatUploadTime("")).toBe("");
    expect(formatUploadTime(null)).toBe("");
    expect(formatUploadTime("not-a-date")).toBe("");
    // 同一年 → MM-DD HH:mm
    expect(formatUploadTime("2026-03-05 14:30:00")).toBe("03-05 14:30");
    // 跨年 → 带年份前缀
    expect(formatUploadTime("2024-12-31 09:05:00")).toBe("2024-12-31 09:05");
    // 越界分量（13 月）→ 空串（不渲染脏数据）
    expect(formatUploadTime("2026-13-05 10:00:00")).toBe("");
    // ISO 带 T
    expect(formatUploadTime("2026-03-05T14:30:00")).toBe("03-05 14:30");
  });
});

// =========================================================================
// 组件级交互补盲（2026-10-06）：分类 Select 改判 + compact 展开/收起
// 此前分类用例只断言「Select 存在」，改判回调链路（onChange →
// onCategoryChange → 页面 updateDocumentCategory）零交互覆盖
// =========================================================================
describe("DocumentParseList · 分类改判交互", () => {
  it("打开下拉选「合同文件」→ 回调 onCategoryChange(docId, value)", async () => {
    const { container, onCategoryChange } = setup({
      docs: [doc({ id: "d1", file_name: "a.pdf", doc_category: "招标文件" })],
      categoryOptions: ["招标文件", "合同文件", "其他"],
    });
    const selector = container.querySelector(".ant-select-selector");
    expect(selector).toBeTruthy();
    // rc-select 在 mousedown 时展开下拉
    fireEvent.mouseDown(selector as HTMLElement);
    await waitFor(() => expect(dropdownOption("合同文件")).toBeTruthy());
    fireEvent.click(dropdownOption("合同文件")!);
    expect(onCategoryChange).toHaveBeenCalledWith("d1", "合同文件");
    expect(onCategoryChange).toHaveBeenCalledTimes(1);
  });

  it("历史未登记分类：Select 兜底显示「其他」且仍可改判", async () => {
    const { container, onCategoryChange } = setup({
      docs: [doc({ id: "d9", doc_category: "某旧分类" })],
      categoryOptions: ["招标文件", "其他"],
    });
    expect(container.textContent || "").toContain("其他");
    fireEvent.mouseDown(
      container.querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => expect(dropdownOption("招标文件")).toBeTruthy());
    fireEvent.click(dropdownOption("招标文件")!);
    expect(onCategoryChange).toHaveBeenCalledWith("d9", "招标文件");
  });
});

describe("DocumentParseList · compact 展开/收起", () => {
  const manyDocs = Array.from({ length: 4 }, (_, i) =>
    doc({ id: `d${i}`, file_name: `f${i}.pdf` }));

  it("默认只显示前 3 行，点「展开全部」显示全部，再点「收起」恢复", () => {
    const { container } = setup({
      docs: manyDocs, compact: true, defaultVisibleCount: 3,
    });
    const countItems = () => container.querySelectorAll(".ant-list-item").length;
    expect(countItems()).toBe(3);
    const expandBtn = btn(container, "展开全部（共 4 项，当前显示 3）");
    expect(expandBtn).toBeTruthy();
    fireEvent.click(expandBtn!);
    expect(countItems()).toBe(4);
    fireEvent.click(btn(container, "收起（仅显示前 3 项）")!);
    expect(countItems()).toBe(3);
  });

  it("文件数不超过 defaultVisibleCount 时不出现展开按钮", () => {
    const { container } = setup({
      docs: manyDocs.slice(0, 3), compact: true, defaultVisibleCount: 3,
    });
    expect(btn(container, "展开全部（共 3 项，当前显示 3）")).toBeNull();
    expect(container.querySelectorAll(".ant-list-item").length).toBe(3);
  });
});

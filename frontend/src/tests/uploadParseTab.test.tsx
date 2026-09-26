// @vitest-environment jsdom
/**
 * 「上传解析」Tab 主体 组件级交互测试。
 *
 * 覆盖此前无测试、且属于该 Tab 核心交互的行为：
 *   1. 上传区：多选文件只触发**一次** onUploadFiles，且带去重后的全部文件；
 *   2. 上传进度卡：uploadingFacts 时出现，展示文件名 Tag；
 *   3. 资料概览：按 docs 派生「共 N / 已解析 / 待解析 / 截断」，空列表不渲染；
 *   4. 批量解析按钮：文案带待解析数，点击回调 onParseAll，待解析为 0 时禁用；
 *   5. 全部重解析：先弹确认框，确认才回调 onReparseAll（取消不调）；
 *   6. 刷新按钮回调 onRefresh；
 *   7. 解析进行中提示 + 批量按钮 loading；
 *   8. 解析完成 Alert 的「去目录生成 / 去提取事实」跳转；
 *   9. 下一步按钮：无已解析文档时禁用，否则跳转；
 *  10. 并发互斥：单份解析/生成任务进行中，上传与批量解析入口禁用；
 *  11. 重解析确认框取消不回调；空列表下下一步全禁用但刷新可用。
 */
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";
import UploadParseTab from "../components/UploadParseTab";
import type { DocumentParseItem } from "../components/DocumentParseList";
import type { BaGroup, BaStoredItem } from "../utils/bidAnalysis";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

const doc = (over: Partial<DocumentParseItem>): DocumentParseItem => ({
  id: "d1",
  file_name: "a.pdf",
  text_len: 0,
  ...over,
});

type Overrides = Partial<React.ComponentProps<typeof UploadParseTab>>;

function setup(over: Overrides = {}) {
  const cbs = {
    onUploadFiles: vi.fn(),
    onParseAll: vi.fn(),
    onReparseAll: vi.fn(),
    onRefresh: vi.fn(),
    onParse: vi.fn(),
    onPreview: vi.fn(),
    onDelete: vi.fn(),
    onCategoryChange: vi.fn(),
    onNavigate: vi.fn(),
  };
  const utils = render(
    <AntdApp>
      <UploadParseTab
        docs={[]}
        categoryOptions={[]}
        generating={false}
        uploadingFacts={false}
        uploadedFiles={[]}
        parsingDocs={false}
        parsingDocId={null}
        {...cbs}
        {...over}
      />
    </AntdApp>
  );
  return { ...utils, ...cbs };
}

/** 在指定根节点内按精确文案找按钮 */
function btn(root: HTMLElement | Document, text: string): HTMLButtonElement | null {
  const b = Array.from(root.querySelectorAll("button")).find(
    (x) => (x.textContent || "").trim() === text
  );
  return (b as HTMLButtonElement) || null;
}

/** 跨弹层（document.body）找按钮 */
async function findInBody(text: string): Promise<HTMLButtonElement> {
  let found: HTMLButtonElement | null = null;
  await waitFor(() => {
    found = btn(document.body, text);
    expect(found).not.toBeNull();
  });
  return found!;
}

describe("UploadParseTab", () => {
  it("上传区：多选文件只触发一次 onUploadFiles，且携带全部文件", async () => {
    const { container, onUploadFiles } = setup();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).not.toBeNull();

    const f1 = new File(["a"], "a.pdf", { type: "application/pdf" });
    const f2 = new File(["b"], "b.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [f1, f2] } });

    await waitFor(() => expect(onUploadFiles).toHaveBeenCalledTimes(1));
    const files = onUploadFiles.mock.calls[0][0] as File[];
    expect(files.map((f) => f.name)).toEqual(["a.pdf", "b.pdf"]);
  });

  it("上传进度卡：uploadingFacts 时出现并展示文件名 Tag", () => {
    const { container } = setup({ uploadingFacts: true, uploadedFiles: ["x.pdf", "y.docx"] });
    const txt = container.textContent || "";
    expect(txt).toContain("正在保存文件");
    expect(txt).toContain("x.pdf");
    expect(txt).toContain("y.docx");
    // 上传按钮进入 loading
    expect(btn(container, "上传文件保存")!.className).toContain("ant-btn-loading");
  });

  it("资料概览：按 docs 派生计数；空列表不渲染概览", () => {
    const { container } = setup({
      docs: [
        doc({ id: "a", text_len: 100 }),
        doc({ id: "b", text_len: 200, truncated: true }),
        doc({ id: "c", text_len: 0 }),
      ],
    });
    const txt = container.textContent || "";
    expect(txt).toContain("资料概览");
    expect(txt).toContain("共 3 个文件");
    expect(txt).toContain("已解析 2");
    expect(txt).toContain("待解析 1");
    expect(txt).toContain("可能被截断 1");

    const empty = setup({ docs: [] });
    expect((empty.container.textContent || "")).not.toContain("资料概览");
  });

  it("批量解析按钮：文案带待解析数，点击回调 onParseAll；无待解析时禁用", () => {
    const { container, onParseAll } = setup({
      docs: [doc({ id: "a", text_len: 100 }), doc({ id: "b", text_len: 0 })],
    });
    const b = btn(container, "解析全部待解析（1）")!;
    expect(b).not.toBeNull();
    fireEvent.click(b);
    expect(onParseAll).toHaveBeenCalledTimes(1);

    // 全部已解析 → 禁用
    const done = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    expect(btn(done.container, "解析全部待解析")!.disabled).toBe(true);
  });

  // ✅ 2026-09-24 B4：后端 parse-all 会处理 parsed_markdown 为空的文档（含 failed），
  //    前端批量入口必须对「仅剩失败文档」的项目保持可达。
  it("仅剩解析失败文档：按钮文案为「重试失败文档（N）」、可用且回调 onParseAll", () => {
    const { container, onParseAll } = setup({
      docs: [
        doc({ id: "ok", text_len: 100, parse_status: "success" }),
        doc({ id: "bad1", text_len: 0, parse_status: "failed" }),
        doc({ id: "bad2", text_len: 0, parse_status: "failed" }),
      ],
    });
    const b = btn(container, "重试失败文档（2）")!;
    expect(b).not.toBeNull();
    expect(b.disabled).toBe(false);
    fireEvent.click(b);
    expect(onParseAll).toHaveBeenCalledTimes(1);
  });

  it("待解析与失败并存：按钮计数含两者（actionableCount）", () => {
    const { container } = setup({
      docs: [
        doc({ id: "ok", text_len: 100, parse_status: "success" }),
        doc({ id: "bad", text_len: 0, parse_status: "failed" }),
        doc({ id: "pend", text_len: 0 }),
      ],
    });
    const b = btn(container, "解析全部待解析（2）")!;
    expect(b).not.toBeNull();
    expect(b.disabled).toBe(false);
  });

  it("全部重解析：弹确认框，确认后才回调 onReparseAll", async () => {
    const { container, onReparseAll } = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    fireEvent.click(btn(container, "全部重解析")!);

    // 确认框出现
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("重新解析全部文档");
    });
    expect(onReparseAll).not.toHaveBeenCalled();

    fireEvent.click(await findInBody("开始重解析"));
    await waitFor(() => expect(onReparseAll).toHaveBeenCalledTimes(1));
  });

  // ===== 以下为 2026-09-20 审计补测：并发互斥 / 入口禁用 / 确认框取消 =====

  it("单份解析进行中：批量解析与上传入口一并禁用（防同文档并发解析）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 0 }), doc({ id: "b", text_len: 100 })],
      parsingDocId: "a",
    });
    expect(btn(container, "解析全部待解析（1）")!.disabled).toBe(true);
    // 上传入口同样处于 busy 态（禁用）
    expect(btn(container, "上传文件保存")!.disabled).toBe(true);
  });

  it("生成任务进行中（generating）：上传与批量解析入口均禁用", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 0 })],
      generating: true,
    });
    expect(btn(container, "上传文件保存")!.disabled).toBe(true);
    expect(btn(container, "解析全部待解析（1）")!.disabled).toBe(true);
  });

  it("重解析确认框点「取消」：不回调 onReparseAll", async () => {
    const { container, onReparseAll } = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    fireEvent.click(btn(container, "全部重解析")!);
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("重新解析全部文档");
    });
    fireEvent.click(await findInBody("取 消"));
    // 留给确认框关闭动画一拍，再断言从未回调
    await new Promise((r) => setTimeout(r, 50));
    expect(onReparseAll).not.toHaveBeenCalled();
  });

  it("空列表：下一步三个入口全禁用且提示先导文件；刷新仍可用", () => {
    const { container, onRefresh, onNavigate } = setup({ docs: [] });
    expect(btn(container, "下一步：提取项目（全表）")!.disabled).toBe(true);
    expect(btn(container, "直接去目录生成")!.disabled).toBe(true);
    expect(btn(container, "提取全局事实")!.disabled).toBe(true);
    expect(container.textContent || "").toContain("先导入至少一份资料文件");
    // 禁用态下点击不产生跳转回调
    fireEvent.click(btn(container, "提取全局事实")!);
    expect(onNavigate).not.toHaveBeenCalled();
    // 空列表时刷新不被锁死
    expect(btn(container, "刷新")!.disabled).toBe(false);
    fireEvent.click(btn(container, "刷新")!);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("刷新按钮：回调 onRefresh", () => {
    const { container, onRefresh } = setup({ docs: [doc({ id: "a", text_len: 1 })] });
    fireEvent.click(btn(container, "刷新")!);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("解析进行中：显示提示，批量按钮 loading", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 0 })],
      parsingDocs: true,
    });
    expect(container.textContent || "").toContain("正在解析文档");
    expect(btn(container, "解析全部待解析（1）")!.className).toContain("ant-btn-loading");
  });

  it("解析完成 Alert：跳转回调正确", () => {
    const { container, onNavigate } = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    expect(container.textContent || "").toContain("全部 1 个文档已解析完成");
    fireEvent.click(btn(container, "去目录生成")!);
    expect(onNavigate).toHaveBeenCalledWith("outline");
    fireEvent.click(btn(container, "提取事实")!);
    expect(onNavigate).toHaveBeenCalledWith("facts");
  });

  it("下一步按钮：无已解析文档时禁用；有则跳转；项数随后端定义动态生成", () => {
    const none = setup({ docs: [doc({ id: "a", text_len: 0 })] });
    // 定义未加载（parseGroups 缺省）：不报数，避免硬编码「18 项」在后端增减项后失真
    const nextDisabled = btn(none.container, "下一步：提取项目（全表）")!;
    expect(nextDisabled.disabled).toBe(true);
    expect((none.container.textContent || "")).toContain("至少解析一份文档后才能进入后续步骤");

    const some = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    // 2026-09-23 合并后：未传 onSwitchToExtract 时回退到 outline（bidAnalysis 顶层 Tab 已废除）
    fireEvent.click(btn(some.container, "下一步：提取项目（全表）")!);
    expect(some.onNavigate).toHaveBeenCalledWith("outline");

    // 传入 onSwitchToExtract 时，「下一步」改为切换同 Tab 内的提取子页，不再触发顶层跳转
    const switchSub = vi.fn();
    const withSub = setup({
      docs: [doc({ id: "a", text_len: 100 })],
      onSwitchToExtract: switchSub,
    });
    fireEvent.click(btn(withSub.container, "下一步：提取项目（全表）")!);
    // 关键断言：子 Tab 切换被触发；顶层 Tab 跳转未发生（bidAnalysis 已不在顶层）
    expect(switchSub).toHaveBeenCalledTimes(1);
    expect(withSub.onNavigate).not.toHaveBeenCalled();

    // 传入分组定义后：按钮文案带真实项数（此处 2+3=5，非 18）
    const groups: BaGroup[] = [
      { key: "g1", label: "分类一", items: [{ item_id: "a" }, { item_id: "b" }] as any },
      { key: "g2", label: "分类二", items: [{ item_id: "c" }, { item_id: "d" }, { item_id: "e" }] as any },
    ];
    const dyn = setup({ docs: [doc({ id: "a", text_len: 100 })], parseGroups: groups });
    expect(btn(dyn.container, "下一步：提取项目（5 项全表）")).not.toBeNull();
  });

  // ===== 以下为 2026-09-21 上传解析模块专项审计补测 =====

  it("全部重解析：空列表时禁用（无文档可重解析，确认框不可触发）", () => {
    const { container } = setup({ docs: [] });
    expect(btn(container, "全部重解析")!.disabled).toBe(true);
  });

  it("全部重解析：单份解析进行中时禁用（防与行内解析并发互斥）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 })],
      parsingDocId: "a",
    });
    expect(btn(container, "全部重解析")!.disabled).toBe(true);
  });

  it("全部重解析：批量解析进行中时禁用（同口径 busy 互斥）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 }), doc({ id: "b", text_len: 200 })],
      parsingDocs: true,
    });
    expect(btn(container, "全部重解析")!.disabled).toBe(true);
  });

  it("全部重解析：单份解析进行中仍禁用（busy 防并发），但不是由 parsingDocId 单独锁死", () => {
    const busy = setup({
      docs: [doc({ id: "a", text_len: 100 })],
      parsingDocId: "a",
    });
    expect(btn(busy.container, "全部重解析")!.disabled).toBe(true);

    const idle = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    expect(btn(idle.container, "全部重解析")!.disabled).toBe(false);
  });

  it("刷新按钮：批量解析 / 单份解析进行中禁用（防止解析未完成时刷新覆盖状态）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 0 })],
      parsingDocs: true,
    });
    expect(btn(container, "刷新")!.disabled).toBe(true);
  });

  // ✅ 2026-09-23 加固：刷新按钮的禁用口径必须与「上传 / 全部解析 / 全部重解析」
  // 一致（统一走组件内的 busy 汇总）。旧实现写死 generating || parsingDocs，
  // 漏掉 parsingDocId（单份解析，OCR 可达数分钟）与 uploadingFacts 两个分支 ——
  // 这两态下点刷新会调用 loadDocuments 覆盖进行中的 docList，前端与后端瞬时不一致。
  it("刷新按钮：单份解析进行中（parsingDocId）禁用", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 }), doc({ id: "b", text_len: 0 })],
      parsingDocs: false,
      parsingDocId: "b",
    });
    expect(btn(container, "刷新")!.disabled).toBe(true);
  });

  it("刷新按钮：文件上传中（uploadingFacts）禁用", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 })],
      uploadingFacts: true,
    });
    expect(btn(container, "刷新")!.disabled).toBe(true);
  });

  it("刷新按钮：无任何进行中的任务时保持可用（不误锁）", () => {
    const { container } = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    expect(btn(container, "刷新")!.disabled).toBe(false);
  });

  it("解析完成 Alert：仅当 docs 非空且全部解析时出现；有待解析或空列表均不出现", () => {
    const done = setup({ docs: [doc({ id: "a", text_len: 100 })] });
    expect(done.container.textContent || "").toContain("全部 1 个文档已解析完成");

    const partial = setup({
      docs: [doc({ id: "a", text_len: 100 }), doc({ id: "b", text_len: 0 })],
    });
    expect(partial.container.textContent || "").not.toContain("全部 1 个文档已解析完成");
    expect(partial.container.textContent || "").not.toContain("全部 2 个文档已解析完成");

    const empty = setup({ docs: [] });
    expect(empty.container.textContent || "").not.toContain("个文档已解析完成");
  });

  it("资料概览：截断数为 0 时不渲染截断 Tag（避免误导用户去重解析）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 }), doc({ id: "b", text_len: 200 })],
    });
    expect(container.textContent || "").not.toContain("可能被截断");
  });

  it("解析中提示：parsingDocs 为 true 但无待解析文档时仍显示提示（真实解析在进行中）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 })],
      parsingDocs: true,
    });
    expect(container.textContent || "").toContain("正在解析文档");
    // busy 态下所有会改动资料的入口均锁死
    expect(btn(container, "上传文件保存")!.disabled).toBe(true);
    expect(btn(container, "全部重解析")!.disabled).toBe(true);
  });

  // ===== 2026-09-20 上传解析模块审计新增：待解析引导 Alert =====

  it("待解析引导 Alert：有文件已保存但无已解析文档时显示引导文案", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 0 }), doc({ id: "b", text_len: 0 })],
    });
    // 2026-09-23 下午：Alert 从带 description 的完整模式改为 banner 紧凑单行，
    // message 文案合并为 "2 个文件已保存，点击下方「解析全部待解析」开始（扫描件自动走 OCR）"
    expect(container.textContent || "").toContain("2 个文件已保存");
    expect(container.textContent || "").toContain("解析全部待解析");
  });

  it("待解析引导 Alert：已有已解析文档时不显示（避免重复提示）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 }), doc({ id: "b", text_len: 0 })],
    });
    expect(container.textContent || "").not.toContain("还没有解析任何文档");
  });

  it("待解析引导 Alert：全部已解析时不显示", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 100 }), doc({ id: "b", text_len: 200 })],
    });
    expect(container.textContent || "").not.toContain("还没有解析任何文档");
  });

  it("待解析引导 Alert：解析进行中时隐藏（避免与解析中提示冲突）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 0 })],
      parsingDocs: true,
    });
    expect(container.textContent || "").not.toContain("还没有解析任何文档");
    expect(container.textContent || "").toContain("正在解析文档");
  });

  it("待解析引导 Alert：空列表时不显示（无文件可引导）", () => {
    const { container } = setup({ docs: [] });
    expect(container.textContent || "").not.toContain("还没有解析任何文档");
  });

  // ===== 2026-09-23 展示方式变更：信息显示窗口（原「解析信息分类显示栏」，取代原「目录树」位置）=====

  it("分类栏：未注入 onParseSelectItem 时不渲染（默认保持既有行为，向后兼容）", () => {
    const { container } = setup({ docs: [] });
    expect(container.textContent || "").not.toContain("信息显示窗口");
  });

  const PARSE_GROUPS: BaGroup[] = [
    {
      key: "project_info",
      label: "项目级基本信息",
      items: [
        { item_id: "projectBasicInfo", label: "项目级基本信息", required: 1, output_type: "json" },
      ],
    },
    {
      key: "scheme_info",
      label: "方案级基本信息",
      items: [
        { item_id: "schemeBasicInfo", label: "方案级基本信息", required: 1, output_type: "markdown" },
      ],
    },
  ];
  const PARSE_ITEMS: BaStoredItem[] = [
    { item_id: "projectBasicInfo", status: "success", content: '{"project_name":"X"}' },
    { item_id: "schemeBasicInfo", status: "pending", content: "" },
  ];

  it("分类栏（detail-only）：注入后渲染标题、分类数「2 个分类」与空态引导（分类列表在左栏 list-only 渲染）", () => {
    const { container } = setup({
      docs: [],
      parseGroups: PARSE_GROUPS,
      parseItems: PARSE_ITEMS,
      onParseSelectItem: vi.fn(),
    });
    const txt = container.textContent || "";
    expect(txt).toContain("信息显示窗口");
    expect(txt).toContain("2 个分类");
    // detail-only 未选中时显示引导占位；分类列表由左栏 list-only 负责，不应在此渲染
    expect(txt).toContain("从左侧选择一个分类");
    expect(txt).not.toContain("项目级基本信息");
  });

  it("分类栏（detail-only）：选中解析项后详情区展示其标签并联动「人工校正」回调", () => {
    const onParseEditItem = vi.fn();
    const { container } = setup({
      docs: [],
      parseGroups: PARSE_GROUPS,
      parseItems: PARSE_ITEMS,
      parseSelectedItem: {
        item_id: "projectBasicInfo",
        label: "项目级基本信息",
        required: 1,
        output_type: "json",
        status: "success",
        content: '{"project_name":"X"}',
      },
      onParseSelectItem: vi.fn(),
      onParseEditItem,
    });
    const txt = container.textContent || "";
    // 详情区展示选中项标签（非分类列表名）
    expect(txt).toContain("项目级基本信息");
    fireEvent.click(btn(container, "人工校正")!);
    expect(onParseEditItem).toHaveBeenCalledTimes(1);
    expect(onParseEditItem.mock.calls[0][0].item_id).toBe("projectBasicInfo");
  });

  it("分类栏：选中项后右侧「查看完整」回调 onParseFullView", () => {
    const onParseFullView = vi.fn();
    const { container } = setup({
      docs: [],
      parseGroups: PARSE_GROUPS,
      parseItems: PARSE_ITEMS,
      parseSelectedItem: {
        item_id: "projectBasicInfo",
        label: "项目级基本信息",
        required: 1,
        output_type: "json",
        status: "success",
        content: '{"project_name":"X"}',
      },
      onParseSelectItem: vi.fn(),
      onParseFullView,
    });
    fireEvent.click(btn(container, "查看完整")!);
    expect(onParseFullView).toHaveBeenCalledTimes(1);
  });

  it("分类栏：解析结果加载失败时展示错误态与重试入口", () => {
    const onParseRefresh = vi.fn();
    const { container } = setup({
      docs: [],
      parseGroups: [],
      parseItems: [],
      parseError: "加载解析结果失败",
      onParseSelectItem: vi.fn(),
      onParseRefresh,
    });
    expect(container.textContent || "").toContain("解析信息加载失败");
    fireEvent.click(btn(container, "重试")!);
    expect(onParseRefresh).toHaveBeenCalledTimes(1);
  });

  it("分类栏：不改变原有资料管理功能（文档列表与批量解析按钮仍在）", () => {
    const { container } = setup({
      docs: [doc({ id: "a", text_len: 0 })],
      parseGroups: PARSE_GROUPS,
      parseItems: PARSE_ITEMS,
      onParseSelectItem: vi.fn(),
    });
    expect(container.textContent || "").toContain("资料与解析");
    expect(btn(container, "解析全部待解析（1）")).not.toBeNull();
  });
});

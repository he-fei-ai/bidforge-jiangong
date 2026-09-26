// @vitest-environment jsdom
/**
 * 「提取项目」Tab · 组件级交互测试。
 *
 * 锁定的行为（此前整段 Tab 内联在 8000 行页面里、零测试覆盖）：
 *   1. 前置条件（无已解析文档）→ 警示 + 「开始提取」禁用 + 可跳「上传解析」；
 *   2. 分类栏 + 扁平任务列表走后端 groups（不再前端硬编码）：每行含全局序号、
 *      所属分类、状态、摘要；点击分类可筛选该分类下解析项；行状态标签口径与
 *      后端一致 —— success 且内容为「未提取到」/json 全「没有提及」→「⚠ 无内容」；
 *   3. 单项重跑按钮：回调带该项定义、且**不**触发 onSelectItem（stopPropagation）；
 *      运行中禁用；
 *   4. 运行中：显示停止按钮、进度百分比、「配置」禁用；汇总缺失必选项时显式提示；
 *   5. 下一步：无任何成功项时禁用；必选项齐备 / 有成功项时可点；
 *   6. 多标段检测结果：未命中 info、命中 warning（含标段标识）、关闭回调；
 *   7. 内容区：json 键值表（「没有提及」→（无））、markdown 渲染、空选提示、
 *      「查看完整」回调；另有 复制原始结果 / 上一项与下一项切换 / 分类归属标记；
 *      窄屏（<768）点行进详情、「返回列表」回退。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, waitFor, cleanup } from "@testing-library/react";
import React from "react";
import BidAnalysisTab from "../components/BidAnalysisTab";
import type { BaItemDef } from "../utils/bidAnalysis";

// ✅ 2026-09-23：显式卸载组件。
// 本文件的用例会触发组件内的 window.setTimeout（重跑冷却、复制反馈复位），
// 若不卸载，定时器会跨用例残留，并在测试环境拆除后回调 → vitest 报
// 「Uncaught Exception ... processTimers」（全量跑时表现为 1~2 个 unhandled error）。
// 组件侧已加卸载清理（timersRef），这里再保证用例之间互不干扰。
afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Progress）会用到
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

const DEFS: BaItemDef[] = [
  { item_id: "projectBasicInfo", label: "项目级基本信息", required: 1, output_type: "json", group: "project_info" },
  { item_id: "schemeBasicInfo", label: "方案级基本信息", required: 1, output_type: "markdown", group: "scheme_info" },
  { item_id: "resourceAllocation", label: "资源配置", required: 0, output_type: "markdown", group: "resource" },
];
const GROUPS = [
  { group: "project_info", label: "项目级基本信息", items: [DEFS[0]] },
  { group: "scheme_info", label: "方案级基本信息", items: [DEFS[1]] },
  { group: "resource", label: "资源配置", items: [DEFS[2]] },
];

function setup(over: Record<string, any> = {}) {
  const handlers = {
    onStart: vi.fn(),
    onStop: vi.fn(),
    onOpenConfig: vi.fn(),
    onCheckSections: vi.fn(),
    onRefresh: vi.fn(),
    onSelectItem: vi.fn(),
    onRerunItem: vi.fn(),
    onOpenFullView: vi.fn(),
    onEditItem: vi.fn(),
    onDismissSectionResult: vi.fn(),
    onGoImport: vi.fn(),
    onGoOutline: vi.fn(),
    onGoFacts: vi.fn(),
  };
  const utils = render(
    <BidAnalysisTab
      defs={DEFS}
      groups={GROUPS}
      items={[]}
      summary={null}
      running={false}
      progress={0}
      progressMsg=""
      parsedDocCount={1}
      selectedItem={null}
      sectionChecking={false}
      sectionCheckResult={null}
      {...handlers}
      {...over}
    />,
  );
  return { ...utils, ...handlers };
}

/** 精确文案按钮（避免「重试」误匹配等） */
function btn(root: ParentNode, text: string): HTMLButtonElement | null {
  const b = Array.from(root.querySelectorAll("button")).find(
    x => (x.textContent || "").trim() === text,
  );
  return (b as HTMLButtonElement) || null;
}

function rerunBtn(container: HTMLElement, label: string): HTMLButtonElement | null {
  return container.querySelector(`[aria-label="重新提取 ${label}"]`);
}

describe("BidAnalysisTab", () => {
  it("前置条件未满足：警示 + 开始提取禁用 + 可跳解析提取", () => {
    const { container, onGoImport, onStart } = setup({ parsedDocCount: 0 });
    expect(container.textContent).toContain("前置条件未满足");
    const start = btn(container, "开始提取")!;
    expect(start.disabled).toBe(true);
    fireEvent.click(start);
    expect(onStart).not.toHaveBeenCalled();
    fireEvent.click(btn(container, "前往解析提取")!);
    expect(onGoImport).toHaveBeenCalledTimes(1);
  });

  it("分类栏 + 扁平任务列表：分类走后端 groups，行含序号/分类/状态/摘要", () => {
    const { container } = setup({
      items: [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"b"}' },
        { item_id: "schemeBasicInfo", status: "success", content: "未提取到" },
        { item_id: "resourceAllocation", status: "error", content: "", error: "限流" },
      ],
    });
    // 分类栏：全部 + 13 分组（此处 3 分组），均带项数角标
    const cats = Array.from(container.querySelectorAll(".ba-cat-item")).map(
      h => (h.textContent || "").trim(),
    );
    expect(cats).toEqual(["全部3", "项目级基本信息1", "方案级基本信息1", "资源配置1"]);
    expect(container.textContent).toContain("解析项任务列表（3 项");

    const rows = container.querySelectorAll(".ba-item-row");
    expect(rows.length).toBe(3);
    // 全局序号与所属分类标签：不再依赖分组表头，每行自带
    expect(rows[0].querySelector(".ba-item-seq")?.textContent).toBe("1");
    expect(rows[2].querySelector(".ba-item-seq")?.textContent).toBe("3");
    expect(rows[0].querySelector(".ba-row-cat-tag")?.textContent).toBe("项目级基本信息");
    expect(rows[2].querySelector(".ba-row-cat-tag")?.textContent).toBe("资源配置");
    // 摘要：成功项取内容首行前缀；无内容/失败态有专属文案
    expect(rows[0].querySelector(".ba-row-summary")?.textContent).toContain("a");
    expect(rows[1].querySelector(".ba-row-summary")?.textContent).toBe("已完成，但未提取到有效内容");
    expect(rows[2].querySelector(".ba-row-summary")?.textContent).toBe("限流");
    // 有内容的 success → 绿标字数；「未提取到」→ 无内容（不得显示为已完成）
    expect(rows[0].textContent).toContain("字");
    expect(rows[1].textContent).toContain("无内容");
    expect(rows[2].textContent).toContain("失败");
  });

  it("点击分类→筛选该分类下解析项；点「全部」回退；序号保持全局口径", () => {
    const { container } = setup();
    expect(container.querySelectorAll(".ba-item-row").length).toBe(3);
    fireEvent.click(container.querySelector('.ba-cat-item[data-group="resource"]')!);
    const rows = container.querySelectorAll(".ba-item-row");
    expect(rows.length).toBe(1);
    expect(rows[0].getAttribute("data-item-id")).toBe("resourceAllocation");
    // 全局序号不随筛选重排：资源配置是第 3 项
    expect(rows[0].querySelector(".ba-item-seq")?.textContent).toBe("3");
    expect(container.textContent).toContain("当前分类：资源配置（1 项）");
    // 回退「全部」恢复完整列表
    fireEvent.click(container.querySelector('.ba-cat-item[data-group="all"]')!);
    expect(container.querySelectorAll(".ba-item-row").length).toBe(3);
  });

  it("json 必选项全「没有提及」→ 行内显示「无内容」（与后端缺失判定一致）", () => {
    const { container } = setup({
      items: [{ item_id: "projectBasicInfo", status: "success", content: '{"a":"没有提及"}' }],
    });
    const row = container.querySelector('.ba-item-row[data-item-id="projectBasicInfo"]')!;
    expect(row.textContent).toContain("无内容");
    expect(row.textContent).not.toContain("✓");
  });

  it("点击行 → onSelectItem 收到「定义 + 结果」合并对象", () => {
    const { container, onSelectItem } = setup({
      items: [{ item_id: "schemeBasicInfo", status: "success", content: "正文内容" }],
    });
    fireEvent.click(container.querySelector('.ba-item-row[data-item-id="schemeBasicInfo"]')!);
    expect(onSelectItem).toHaveBeenCalledWith(
      expect.objectContaining({
        item_id: "schemeBasicInfo",
        label: "方案级基本信息",
        status: "success",
        content: "正文内容",
      }),
    );
  });

  it("单项重跑：回调带该项定义，且不触发选中（stopPropagation）", () => {
    const { container, onRerunItem, onSelectItem } = setup({
      items: [{ item_id: "schemeBasicInfo", status: "success", content: "正文内容" }],
    });
    fireEvent.click(rerunBtn(container, "方案级基本信息")!);
    expect(onRerunItem).toHaveBeenCalledWith(
      expect.objectContaining({ item_id: "schemeBasicInfo" }),
    );
    expect(onSelectItem).not.toHaveBeenCalled();
  });

  it("运行中：显示停止按钮、进度百分比、配置禁用、重跑禁用", () => {
    const { container, onStop } = setup({ running: true, progress: 0.5, progressMsg: "已完成 3/9" });
    expect(btn(container, "开始提取")).toBeNull();
    expect(btn(container, "配置")!.disabled).toBe(true);
    expect(container.textContent).toContain("50%");
    expect(container.textContent).toContain("已完成 3/9");
    expect(rerunBtn(container, "方案级基本信息")!.disabled).toBe(true);
    fireEvent.click(btn(container, "停止")!);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("汇总：必选项缺失与全部就绪两种态互斥展示", () => {
    const missing = setup({
      summary: {
        total: 3, success: 1, errors: 0, running: 0, pending: 2,
        missing_required: ["方案级基本信息"], all_required_done: false,
      },
    });
    expect(missing.container.textContent).toContain("必选项缺失 1");
    expect(missing.container.textContent).not.toContain("全部就绪");

    const done = setup({
      summary: {
        total: 3, success: 3, errors: 0, running: 0, pending: 0,
        missing_required: [], all_required_done: true,
      },
    });
    expect(done.container.textContent).toContain("全部就绪");
  });

  it("下一步：无成功项时禁用；有成功项时可点并跳转", () => {
    const none = setup({
      summary: {
        total: 3, success: 0, errors: 0, running: 0, pending: 3,
        missing_required: ["项目级基本信息"], all_required_done: false,
      },
    });
    expect(btn(none.container, "下一步：目录生成")!.disabled).toBe(true);

    const some = setup({
      summary: {
        total: 3, success: 1, errors: 0, running: 0, pending: 2,
        missing_required: ["方案级基本信息"], all_required_done: false,
      },
    });
    const next = btn(some.container, "下一步：目录生成")!;
    expect(next.disabled).toBe(false);
    fireEvent.click(next);
    expect(some.onGoOutline).toHaveBeenCalledTimes(1);
    expect(some.container.textContent).toContain("必选项未全部完成");

    fireEvent.click(btn(some.container, "去提取全局事实")!);
    expect(some.onGoFacts).toHaveBeenCalledTimes(1);
  });

  it("多标段检测：未命中 info 提示并可关闭；命中 warning 且列出标段标识", () => {
    const info = setup({ sectionCheckResult: { has_multiple: false, detected_count: 0 } });
    expect(info.container.textContent).toContain("未发现多标段特征");
    const close = info.container.querySelector(".ant-alert-close-icon") as HTMLElement;
    fireEvent.click(close);
    expect(info.onDismissSectionResult).toHaveBeenCalledTimes(1);

    const hit = setup({
      sectionCheckResult: {
        has_multiple: true, total_declared: 3,
        sections: ["标段:1", "标段:2", "标段:3"],
      },
    });
    expect(hit.container.textContent).toContain("检测到疑似多标段（约 3 个）");
    expect(hit.container.textContent).toContain("标段:1、标段:2、标段:3");
  });

  it("多标段检测按钮：无已解析文档时禁用；点击触发回调", () => {
    const disabled = setup({ parsedDocCount: 0 });
    expect(btn(disabled.container, "多标段检测")!.disabled).toBe(true);

    const enabled = setup();
    fireEvent.click(btn(enabled.container, "多标段检测")!);
    expect(enabled.onCheckSections).toHaveBeenCalledTimes(1);
  });

  it("结果阅读区：未选中显示引导文案；json 项渲染键值（「没有提及」→（无））", () => {
    const empty = setup();
    expect(empty.container.textContent).toContain("从左侧选择一个解析项");

    const json = setup({
      selectedItem: {
        item_id: "projectBasicInfo", label: "项目级基本信息", output_type: "json",
        content: '{"project_name":"测试项目","contractor":"没有提及"}',
      },
    });
    expect(json.container.textContent).toContain("测试项目");
    expect(json.container.textContent).toContain("（无）");

    fireEvent.click(btn(json.container, "查看完整")!);
    expect(json.onOpenFullView).toHaveBeenCalledTimes(1);
  });

  it("项目级基本信息：左列显示中文字段名而非英文键（键名存储不变）", () => {
    const { container } = setup({
      selectedItem: {
        item_id: "projectBasicInfo", label: "项目级基本信息", output_type: "json",
        content:
          '{"project_name":"上海市某工程","contractor":"某建工集团","chief_supervision_engineer":"张三"}',
      },
    });
    const labels = Array.from(container.querySelectorAll(".ba-item-body > div > div > div:first-child"))
      .map(el => (el.textContent || "").trim())
      .filter(Boolean);
    expect(labels).toEqual(["项目名称", "施工单位", "总监理工程师"]);
    expect(container.textContent).not.toContain("project_name");
    expect(container.textContent).not.toContain("contractor");
    // 值不受影响
    expect(container.textContent).toContain("上海市某工程");
  });

  it("项目级基本信息：未登记键回退原始键（不丢字段）；其它 json 项不翻译", () => {
    const fallback = setup({
      selectedItem: {
        item_id: "projectBasicInfo", output_type: "json",
        content: '{"unknown_field":"x"}',
      },
    });
    expect(fallback.container.textContent).toContain("unknown_field");

    const other = setup({
      selectedItem: {
        item_id: "someOtherJsonItem", output_type: "json",
        content: '{"project_name":"y"}',
      },
    });
    expect(other.container.textContent).toContain("project_name");
  });

  it("结果阅读区：失败项显示 AI 报错原因（此前只有「暂无内容」，无从排查）", () => {
    const { container } = setup({
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        status: "error", content: "", error: "AI 调用超时（300s）",
      },
    });
    // ✅ 2026-09-20 回归修正：组件文案已从「该项提取失败」细化为「该项本轮提取失败」
    //    （强调"仅本轮"，历史成功结果未被清除），测试同步对齐。
    expect(container.textContent).toContain("该项本轮提取失败");
    expect(container.textContent).toContain("AI 调用超时（300s）");
  });

  it("结果阅读区：markdown 项走统一渲染器（不直接暴露 ## 源码）", async () => {
    const { container } = setup({
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        content: "## 方案标识\n\n深基坑支护方案",
      },
    });
    await waitFor(() => {
      expect(container.querySelector(".markdown-body h2")?.textContent).toBe("方案标识");
    });
  });

  it("解析项定义未就绪：显示加载态，不渲染任何行", () => {
    const { container } = setup({ defs: [], groups: [] });
    expect(container.querySelectorAll(".ba-item-row").length).toBe(0);
    expect(container.textContent).toContain("解析项定义加载中");
  });

  // =========================================================================
  // ✅ 2026-09-20 新增（口径 / 人工校正 / 提取规模）
  // =========================================================================

  it("口径文案动态生成：不再硬编码「18 项 / 12+6」（增减解析项不失真）", () => {
    const { container } = setup();
    // DEFS = 3 项，2 必选 + 1 可选
    expect(container.textContent).toContain("3 项结构化提取（2 必选 + 1 可选）");
    // 旧实现的两处硬编码必须消失
    expect(container.textContent).not.toContain("12 方案信息项 + 6 施工组织设计项");
  });

  it("口径文案随定义变化：自定义 5 项（3 必选 + 2 可选）", () => {
    const defs = [
      { item_id: "a", label: "A", required: 1, output_type: "markdown", group: "g" },
      { item_id: "b", label: "B", required: 1, output_type: "markdown", group: "g" },
      { item_id: "c", label: "C", required: 1, output_type: "markdown", group: "g" },
      { item_id: "d", label: "D", required: 0, output_type: "markdown", group: "g" },
      { item_id: "e", label: "E", required: 0, output_type: "markdown", group: "g" },
    ];
    const { container } = setup({
      defs,
      groups: [{ group: "g", label: "G", items: defs }],
    });
    expect(container.textContent).toContain("5 项结构化提取（3 必选 + 2 可选）");
  });

  it("汇总：完成有效 / 完成无内容 / 人工校正 三种徽标互不掩盖", () => {
    const { container } = setup({
      summary: {
        total: 5, success: 4, success_valid: 2, manual_count: 2,
        errors: 0, running: 0, pending: 1,
        missing_required: ["E"], all_required_done: false,
      },
    });
    expect(container.textContent).toContain("✓ 有效 2");
    expect(container.textContent).toContain("⚠ 完成无内容 2");
    expect(container.textContent).toContain("✎ 人工校正 2");
    // 旧实现只有一个「✓ 完成 N」，无法区分「完成但内容为空标记」
    expect(container.textContent).not.toContain("✓ 完成 4");
  });

  it("人工校正徽标：source='manual' 的行显示「✎ 人工」而非绿色 ✓", () => {
    const { container } = setup({
      items: [
        { item_id: "schemeBasicInfo", status: "success", content: "人工改写内容", source: "manual" },
        { item_id: "resourceAllocation", status: "success", content: "AI 提取内容", source: "ai" },
      ],
    });
    const manualRow = container.querySelector('.ba-item-row[data-item-id="schemeBasicInfo"]')!;
    expect(manualRow.textContent).toContain("✎ 人工");
    expect(manualRow.textContent).not.toContain("✓");
    const aiRow = container.querySelector('.ba-item-row[data-item-id="resourceAllocation"]')!;
    expect(aiRow.textContent).toContain("✓");
    expect(aiRow.textContent).not.toContain("✎ 人工");
  });

  it("下一步：完成项内容全为空标记（success_valid=0）→ 禁用并提示不会被下游使用", () => {
    const { container } = setup({
      summary: {
        total: 3, success: 2, success_valid: 0, errors: 0, running: 0, pending: 1,
        missing_required: ["项目级基本信息"], all_required_done: false,
      },
    });
    expect(btn(container, "下一步：目录生成")!.disabled).toBe(true);
    expect(container.textContent).toContain("内容均为空标记");
  });

  it("人工校正入口：选中项有内容时出现并回调；运行中禁用；未提供回调则不渲染", () => {
    const item = {
      item_id: "schemeBasicInfo", label: "方案级基本信息",
      output_type: "markdown", status: "success", content: "旧内容",
    };

    const withHandler = setup({ selectedItem: item });
    fireEvent.click(btn(withHandler.container, "人工校正")!);
    expect(withHandler.onEditItem).toHaveBeenCalledWith(item);

    // 运行中禁止改写（避免覆盖正在写入的项）
    const busy = setup({ selectedItem: item, running: true });
    expect(btn(busy.container, "人工校正")!.disabled).toBe(true);

    // 未传回调（如内嵌场景）→ 不渲染按钮，「查看完整」仍在
    const noHandler = setup({ selectedItem: item, onEditItem: undefined });
    expect(noHandler.container.textContent).not.toContain("人工校正");
    expect(btn(noHandler.container, "查看完整")).not.toBeNull();

    // 无内容项不显示入口（无可校正对象）
    const empty = setup({ selectedItem: { ...item, content: "" } });
    expect(empty.container.textContent).not.toContain("人工校正");
  });

  it("提取规模（text_stats）：字数 / 段数 / 项数 / 预估模型调用次数全部可见", () => {
    const { container } = setup({
      running: true,
      progress: 0.2,
      textStats: {
        total_chars: 500000, segment_count: 32, item_count: 18,
        est_model_calls: 576, chunk_size: 16000,
      },
    });
    expect(container.textContent).toContain("提取规模");
    expect(container.textContent).toContain("500,000");
    expect(container.textContent).toContain("16,000");
    expect(container.textContent).toContain("32");
    expect(container.textContent).toContain("576");
  });

  it("提取规模为 0 / 未推送时不渲染该行", () => {
    const zero = setup({ textStats: { total_chars: 0, segment_count: 0, item_count: 0, est_model_calls: 0 } });
    expect(zero.container.textContent).not.toContain("提取规模");
    const none = setup({ textStats: null });
    expect(none.container.textContent).not.toContain("提取规模");
  });

  it("失败项保留了上一轮内容时：仍展示本轮错误原因（旧实现完全不可见）", () => {
    const { container } = setup({
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        status: "error", content: "上一轮的旧内容", error: "429 限流",
      },
    });
    expect(container.textContent).toContain("该项本轮提取失败");
    expect(container.textContent).toContain("429 限流");
    expect(container.textContent).toContain("下方仍展示上一轮内容，便于对照");
    // 上一轮内容仍在渲染（不被 error 覆盖）
    expect(container.textContent).toContain("上一轮的旧内容");
  });

  it("行内失败标签：hover 后显示该轮的失败原因（旧实现只写一个红标、无从排查）", async () => {
    const { container } = setup({
      items: [
        { item_id: "schemeBasicInfo", status: "error", content: "", error: "AI 调用超时" },
      ],
    });
    const row = container.querySelector('.ba-item-row[data-item-id="schemeBasicInfo"]')!;
    expect(row.textContent).toContain("✗ 失败");
    // ✅ 2026-09-23 展示方式变更：失败原因同步上浮到行摘要（不必先 hover 才能发现）
    expect(row.querySelector(".ba-row-summary")?.textContent).toContain("AI 调用超时");

    const tag = row.querySelector(".ant-tag") as HTMLElement;
    fireEvent.mouseEnter(tag);
    await waitFor(() => {
      expect(document.body.textContent).toContain("AI 调用超时");
    });
  });

  // ✅ 以下三项为「页面接线回归护栏」：此前的缺陷是后端 / 组件能力齐备但
  //    SchemeWorkbenchPage 未传 onEditItem / textStats，导致「人工校正」按钮与
  //    提取规模在生产环境从不显示（组件单测因显式传 prop 而绿，掩盖了断链）。
  //    这些用例把「页面必须传 prop」的契约钉在组件层，删 prop 即失败。

  it("人工校正：json 项（projectBasicInfo）有内容时入口可见，回调携带 output_type 供页面做 JSON 校验", () => {
    const jsonItem = {
      item_id: "projectBasicInfo", label: "项目级基本信息",
      output_type: "json", status: "success",
      content: JSON.stringify({ project_name: "测试项目", total_cost: "1.2 亿" }),
    };
    const { container, onEditItem } = setup({ selectedItem: jsonItem });
    const editBtn = btn(container, "人工校正");
    expect(editBtn).not.toBeNull();
    expect(editBtn!.disabled).toBe(false);
    fireEvent.click(editBtn!);
    // 回调必须把完整 item（含 output_type=json）交给页面，页面据此走 JSON 预校验
    expect(onEditItem).toHaveBeenCalledWith(expect.objectContaining({
      item_id: "projectBasicInfo",
      output_type: "json",
    }));
  });

  it("人工校正 + 查看完整：选中项有内容且两种回调均提供时，两按钮在阅读区共存", () => {
    const item = {
      item_id: "schemeBasicInfo", label: "方案级基本信息",
      output_type: "markdown", status: "success", content: "方案内容…",
    };
    const { container } = setup({ selectedItem: item });
    expect(btn(container, "人工校正")).not.toBeNull();
    expect(btn(container, "查看完整")).not.toBeNull();
  });

  it("提取规模公式：est_model_calls = item_count × segment_count，三项数值与「≈ N 次模型调用」同时可见", () => {
    const { container } = setup({
      running: true,
      textStats: {
        total_chars: 48000, segment_count: 3, item_count: 16,
        est_model_calls: 48, chunk_size: 16000,
      },
    });
    const txt = container.textContent || "";
    expect(txt).toContain("48,000");
    expect(txt).toContain("16,000");
    // 段数 × 项数 = 预估调用次数（3 × 16 = 48）
    expect(txt).toContain("3");
    expect(txt).toContain("16");
    expect(txt).toContain("48");
    expect(txt).toContain("次模型调用");
  });

  // =========================================================================
  // ✅ 2026-09-23 新增（展示方式变更：分类栏 + 固定 18 项列表 + 内容区）
  // =========================================================================

  it("内容区头部：展示序号 / 分类归属 / 状态，与列表选中项一致", () => {
    const { container } = setup({
      items: [{ item_id: "schemeBasicInfo", status: "success", content: "正文内容", updated_at: "2026-09-23 10:00:00" }],
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", required: 1,
        output_type: "markdown", group: "scheme_info",
        status: "success", content: "正文内容",
      },
    });
    const header = container.querySelector(".ba-detail-pane")!;
    expect(header.textContent).toContain("#2"); // 全局序号（defs 顺序第 2）
    expect(header.textContent).toContain("方案级基本信息");
    expect(header.textContent).toContain("✓ 4字"); // 状态与列表同口径
    expect(header.textContent).toContain("更新于 2026-09-23 10:00:00");
  });

  it("复制：点击复制按钮将原始解析结果写入剪贴板并显示「已复制」反馈", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    try {
      const { container } = setup({
        selectedItem: {
          item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
          status: "success", content: "原文内容 A\nB 行",
        },
      });
      const copyBtn = btn(container, "复制");
      expect(copyBtn).not.toBeNull();
      fireEvent.click(copyBtn!);
      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith("原文内容 A\nB 行");
      });
      await waitFor(() => {
        expect(btn(container, "已复制")).not.toBeNull();
      });
    } finally {
      Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    }
  });

  it("切换：上一项/下一项按当前列表循环 onSelectItem（携带定义+结果合并对象）", () => {
    const { container, onSelectItem } = setup({
      items: [
        { item_id: "schemeBasicInfo", status: "success", content: "正文内容" },
        { item_id: "resourceAllocation", status: "success", content: "资源配置内容" },
      ],
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        status: "success", content: "正文内容",
      },
    });
    fireEvent.click(container.querySelector('[aria-label="下一解析项"]')!);
    expect(onSelectItem).toHaveBeenLastCalledWith(
      expect.objectContaining({ item_id: "resourceAllocation", content: "资源配置内容" }),
    );
    // selectedItem 受控于页面：本例中保持不变，再点上一项仍从第 2 项往前走到第 1 项
    fireEvent.click(container.querySelector('[aria-label="上一解析项"]')!);
    expect(onSelectItem).toHaveBeenLastCalledWith(
      expect.objectContaining({ item_id: "projectBasicInfo", content: "" }),
    );
  });

  it("窄屏（<768）：默认只显示列表，点行进详情带「返回列表」，回退后重新显示列表", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 500, configurable: true });
    try {
      const { container, onSelectItem } = setup({
        items: [{ item_id: "schemeBasicInfo", status: "success", content: "正文内容" }],
        selectedItem: {
          item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
          status: "success", content: "正文内容",
        },
      });
      // 默认：列表在、详情隐藏
      expect(container.querySelector(".ba-task-pane")).not.toBeNull();
      expect(container.querySelector(".ba-detail-pane")).toBeNull();
      // 点行 → 上抛选中 + 切入详情（带返回列表按钮）
      fireEvent.click(container.querySelector('.ba-item-row[data-item-id="schemeBasicInfo"]')!);
      expect(onSelectItem).toHaveBeenCalled();
      expect(container.querySelector(".ba-detail-pane")).not.toBeNull();
      expect(container.querySelector(".ba-task-pane")).toBeNull();
      const back = btn(container, "返回列表");
      expect(back).not.toBeNull();
      fireEvent.click(back!);
      expect(container.querySelector(".ba-task-pane")).not.toBeNull();
      expect(container.querySelector(".ba-detail-pane")).toBeNull();
    } finally {
      Object.defineProperty(window, "innerWidth", { value: original, configurable: true });
    }
  });

  // =========================================================================
  // ✅ 2026-09-23 遗留项修复（定义加载失败态 / 来源位置溯源 / 来源方案展示）
  // =========================================================================

  it("定义加载失败：错误态 + 重试入口；未出错时仍显「加载中」", () => {
    const onRetryDefs = vi.fn();
    const failed = setup({
      defs: [], groups: [], defsError: "后端 500：内部错误", onRetryDefs,
    });
    expect(failed.container.textContent).toContain("解析项定义加载失败");
    expect(failed.container.textContent).toContain("后端 500：内部错误");
    expect(failed.container.textContent).not.toContain("解析项定义加载中");
    fireEvent.click(btn(failed.container, "重试")!);
    expect(onRetryDefs).toHaveBeenCalledTimes(1);

    // 无错误且定义未到位：保持既有加载中文案，不出错误态
    const loading = setup({ defs: [], groups: [] });
    expect(loading.container.textContent).toContain("解析项定义加载中…");
    expect(loading.container.textContent).not.toContain("解析项定义加载失败");
    expect(btn(loading.container, "重试")).toBeNull();
  });

  it("来源位置：有 evidence 时详情区展示文档/标题/行号/摘录；无证据时整块隐藏", () => {
    const evidence = JSON.stringify([
      {
        doc: "招标文件.pdf", line: 6, heading: "第一章 招标公告 › 1.1 工程概况",
        quote: "基坑深度 8.5m，支护形式为钻孔灌注桩", match: "基坑深度 8.5m",
      },
      {
        doc: "答疑纪要.docx", line: 3, heading: "", field: "支护形式",
        quote: "塔吊型号为 QTZ80", match: "QTZ80",
      },
    ]);
    const { container } = setup({
      items: [{ item_id: "schemeBasicInfo", status: "success", content: "正文内容", evidence }],
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        status: "success", content: "正文内容", evidence,
      },
    });
    const list = container.querySelector(".ba-evidence-list");
    expect(list).not.toBeNull();
    const txt = list!.textContent || "";
    expect(txt).toContain("来源位置");
    expect(txt).toContain("共 2 处");
    expect(txt).toContain("招标文件.pdf");
    expect(txt).toContain("第一章 招标公告 › 1.1 工程概况");
    expect(txt).toContain("第 6 行");
    expect(txt).toContain("基坑深度 8.5m，支护形式为钻孔灌注桩");
    expect(txt).toContain("支护形式"); // json 项字段路径 Tag
    // 无 evidence（旧数据/未匹配/人工校正后）：整块不渲染，不留空壳
    const bare = setup({
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        status: "success", content: "正文内容", source: "manual",
      },
    });
    expect(bare.container.querySelector(".ba-evidence-list")).toBeNull();
  });

  it("来源方案：详情头部展示 scheme_name + 项目级共享说明；后端未带时隐藏", () => {
    const { container } = setup({
      items: [{
        item_id: "schemeBasicInfo", status: "success", content: "正文内容",
        scheme_name: "深基坑专项方案",
      }],
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        status: "success", content: "正文内容",
      },
    });
    const header = container.querySelector(".ba-detail-pane")!;
    expect(header.textContent).toContain("来源方案：深基坑专项方案（项目级共享）");
    // 旧后端未返回 scheme_name：不展示该段（向后兼容，不留空壳）
    const old = setup({
      items: [{ item_id: "schemeBasicInfo", status: "success", content: "正文内容" }],
      selectedItem: {
        item_id: "schemeBasicInfo", label: "方案级基本信息", output_type: "markdown",
        status: "success", content: "正文内容",
      },
    });
    expect(old.container.querySelector(".ba-detail-pane")!.textContent)
      .not.toContain("来源方案");
  });
  // =====================================================================
  // 2026-09-23 加固：操作区禁用口径 + 单项重跑幂等
  // =====================================================================

  it("刷新按钮：提取进行中禁用（防止快照覆盖进行中的任务进度）", () => {
    const { container, onRefresh } = setup({ running: true });
    const refresh = btn(container, "刷新")!;
    expect(refresh.disabled).toBe(true);
    fireEvent.click(refresh);
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("刷新按钮：无任务运行时保持可用", () => {
    const { container, onRefresh } = setup({ running: false });
    const refresh = btn(container, "刷新")!;
    expect(refresh.disabled).toBe(false);
    fireEvent.click(refresh);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("单项重跑：快速连点两次只触发一次 onRerunItem（幂等守卫）", () => {
    const { container, onRerunItem } = setup({ running: false });
    const b = rerunBtn(container, "项目级基本信息")!;
    // 用同一个事件对象连续触发两次，模拟双击 / 键盘连按
    fireEvent.click(b);
    fireEvent.click(b);
    expect(onRerunItem).toHaveBeenCalledTimes(1);
    expect(onRerunItem.mock.calls[0][0]).toMatchObject({ item_id: "projectBasicInfo" });
  });

  it("单项重跑：冷却期内其它解析项仍可重跑（守卫按 item_id 隔离）", () => {
    const { container, onRerunItem } = setup({ running: false });
    fireEvent.click(rerunBtn(container, "项目级基本信息")!);
    fireEvent.click(rerunBtn(container, "方案级基本信息")!);
    expect(onRerunItem).toHaveBeenCalledTimes(2);
  });

  it("单项重跑：冷却期过后守卫自清理，同一项可再次重跑", () => {
    const { container, onRerunItem } = setup({ running: false });
    vi.useFakeTimers();
    try {
      fireEvent.click(rerunBtn(container, "项目级基本信息")!);
      fireEvent.click(rerunBtn(container, "项目级基本信息")!);
      expect(onRerunItem).toHaveBeenCalledTimes(1);

      vi.advanceTimersByTime(1600);
      fireEvent.click(rerunBtn(container, "项目级基本信息")!);
      expect(onRerunItem).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

});

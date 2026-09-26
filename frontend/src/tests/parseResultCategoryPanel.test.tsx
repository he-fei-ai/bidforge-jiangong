// @vitest-environment jsdom
/**
 * 「解析信息分类显示栏」(ParseResultCategoryPanel) 组件测试。
 *
 * 覆盖 2026-09-23 上传解析模块展示方式变更的核心行为：
 *   1. 分类栏渲染 13 分组 + 数量角标（已完成/总数）；
 *   2. 分类展开/收起；点击解析项上抛 onSelectItem（合并定义+结果）；
 *   3. 选中项在右侧显示详情 + 查看/编辑（人工校正）/复制；
 *   4. 复制调用剪贴板（成功提示）；
 *   5. 加载 / 空 / 错误 三态（错误可重试）；
 *   6. 窄屏：分类栏收进抽屉（桌面端为左栏 + 右内容区）；
 *   7. 纯函数 groupCounts / baStatusTag / copyTextToClipboard（降级路径）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";
import ParseResultCategoryPanel, {
  copyTextToClipboard,
  groupCounts,
  baStatusTag,
} from "../components/ParseResultCategoryPanel";
import type { BaGroup, BaStoredItem } from "../utils/bidAnalysis";

// ===== jsdom 环境补齐（与 uploadParseTab.test 同口径）=====
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
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

/** 控制 matchMedia 返回值（窄屏/桌面切换） */
function setMatchMedia(matches: boolean) {
  (window as any).matchMedia = (query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

beforeEach(() => {
  setMatchMedia(false); // 默认桌面端
  // 剪贴板 API（jsdom 默认没有）
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

const GROUPS: BaGroup[] = [
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
      { item_id: "overviewParams", label: "工程概况与设计参数", required: 1, output_type: "markdown" },
    ],
  },
];

const ITEMS: BaStoredItem[] = [
  { item_id: "projectBasicInfo", status: "success", content: '{"project_name":"某项目"}' },
  { item_id: "schemeBasicInfo", status: "pending", content: "" },
];

type Overrides = Partial<React.ComponentProps<typeof ParseResultCategoryPanel>>;

function setup(over: Overrides = {}) {
  const onSelectItem = vi.fn();
  const utils = render(
    <AntdApp>
      <ParseResultCategoryPanel
        groups={GROUPS}
        items={ITEMS}
        selectedItem={null}
        onSelectItem={onSelectItem}
        {...over}
      />
    </AntdApp>,
  );
  return { ...utils, onSelectItem };
}

function btn(root: HTMLElement | Document, text: string): HTMLButtonElement | null {
  const b = Array.from(root.querySelectorAll("button")).find(
    (x) => (x.textContent || "").trim() === text,
  );
  return (b as HTMLButtonElement) || null;
}

describe("ParseResultCategoryPanel · 分类栏与数量角标", () => {
  it("渲染分类名称与「已完成/总数」数量角标", () => {
    const { container } = setup();
    const txt = container.textContent || "";
    expect(txt).toContain("项目级基本信息");
    expect(txt).toContain("方案级基本信息");
    // project_info：1 项已 success → 1/1；scheme_info：0 项 success → 0/2
    expect(txt).toContain("1/1");
    expect(txt).toContain("0/2");
    // 默认收起：解析项行不渲染
    expect(container.querySelectorAll('.parse-cat-items .parse-cat-item-row').length).toBe(0);
  });

  it("点击分类头展开/收起：展开后出现该分类解析项", () => {
    const { container } = setup();
    const header = container.querySelector('.parse-cat-header[data-group-key="scheme_info"]')!;
    expect(header).not.toBeNull();
    expect(header.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(header);
    expect(header.getAttribute("aria-expanded")).toBe("true");
    const rows = container.querySelectorAll('.parse-cat-items .parse-cat-item-row');
    expect(rows.length).toBe(2);
    expect(container.textContent || "").toContain("工程概况与设计参数");

    fireEvent.click(header);
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelectorAll('.parse-cat-items .parse-cat-item-row').length).toBe(0);
  });

  it("点击解析项：上抛 onSelectItem，且携带「定义 + 结果」合并对象", () => {
    const { container, onSelectItem } = setup();
    fireEvent.click(container.querySelector('.parse-cat-header[data-group-key="project_info"]')!);
    const row = container.querySelector('.parse-cat-item-row[data-item-id="projectBasicInfo"]')!;
    expect(row).not.toBeNull();
    fireEvent.click(row);

    expect(onSelectItem).toHaveBeenCalledTimes(1);
    const arg = onSelectItem.mock.calls[0][0];
    expect(arg.item_id).toBe("projectBasicInfo");
    expect(arg.label).toBe("项目级基本信息");
    expect(arg.output_type).toBe("json");
    expect(arg.content).toBe('{"project_name":"某项目"}');
    expect(arg.status).toBe("success");
  });

  it("点击分类头：右侧主区展示该分类的项目列表（含状态与摘要）", () => {
    const { container } = setup();
    fireEvent.click(container.querySelector('.parse-cat-header[data-group-key="scheme_info"]')!);
    // 主区标题 + 该分类下两项（主区列表同样使用 parse-cat-item-row）
    expect(container.textContent || "").toContain("方案级基本信息（2 项，点击查看详情）");
    const contentRows = container.querySelectorAll('.parse-cat-content .parse-cat-item-row');
    expect(contentRows.length).toBe(2);
  });
});

describe("ParseResultCategoryPanel · 选中详情与操作", () => {
  const selected = {
    item_id: "projectBasicInfo",
    label: "项目级基本信息",
    required: 1,
    output_type: "json",
    status: "success",
    content: '{"project_name":"某项目"}',
  };

  it("选中项：右侧显示详情与 复制 / 人工校正 / 查看完整 操作", () => {
    const onEditItem = vi.fn();
    const onOpenFullView = vi.fn();
    const { container } = setup({ selectedItem: selected, onEditItem, onOpenFullView });
    const txt = container.textContent || "";
    expect(txt).toContain("项目级基本信息");
    expect(btn(container, "复制")).not.toBeNull();
    expect(btn(container, "人工校正")).not.toBeNull();
    expect(btn(container, "查看完整")).not.toBeNull();

    fireEvent.click(btn(container, "人工校正")!);
    expect(onEditItem).toHaveBeenCalledWith(selected);

    fireEvent.click(btn(container, "查看完整")!);
    expect(onOpenFullView).toHaveBeenCalledWith(selected);
  });

  it("未传 onEditItem/onOpenFullView 时隐藏对应操作（仅保留复制）", () => {
    const { container } = setup({ selectedItem: selected });
    expect(btn(container, "复制")).not.toBeNull();
    expect(btn(container, "人工校正")).toBeNull();
    expect(btn(container, "查看完整")).toBeNull();
  });

  // ✅ 2026-09-23 加固：AI 提取进行中禁止「人工校正」。
  // 后端 _update_item_status 的任何 AI 写入都会把 source 复位为 'ai'，
  // 若允许在提取进行中写入 source='manual'，AI 完成落库后人工结果被覆盖、
  // 前端仍显示「✎ 人工」徽标 → 用户无从分辨当前值到底是 AI 抽的还是自己改的。
  it("人工校正：AI 提取进行中（running=true）禁用且不触发回调", () => {
    const onEditItem = vi.fn();
    const { container } = setup({ selectedItem: selected, onEditItem, running: true });
    const edit = btn(container, "人工校正")!;
    expect(edit.disabled).toBe(true);
    fireEvent.click(edit);
    expect(onEditItem).not.toHaveBeenCalled();
  });

  it("人工校正：未运行（running 缺省）保持可用（默认行为不变）", () => {
    const onEditItem = vi.fn();
    const { container } = setup({ selectedItem: selected, onEditItem });
    const edit = btn(container, "人工校正")!;
    expect(edit.disabled).toBe(false);
    fireEvent.click(edit);
    expect(onEditItem).toHaveBeenCalledWith(selected);
  });

  it("人工校正：running=false 显式传入时同样可用（不依赖缺省值）", () => {
    const onEditItem = vi.fn();
    const { container } = setup({ selectedItem: selected, onEditItem, running: false });
    expect(btn(container, "人工校正")!.disabled).toBe(false);
  });

  it("复制：不受 running 影响（提取中仍可查看/复制已有结果）", () => {
    const { container } = setup({ selectedItem: selected, running: true });
    const copy = btn(container, "复制")!;
    expect(copy.disabled).toBe(false);
  });

  it("复制：调用剪贴板写入原文，并提示成功", async () => {
    const { container } = setup({ selectedItem: selected });
    fireEvent.click(btn(container, "复制")!);
    await waitFor(() => {
      expect((navigator.clipboard.writeText as any)).toHaveBeenCalledWith(selected.content);
    });
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("已复制到剪贴板");
    });
  });

  it("选中项自动激活并展开其所属分类（左侧高亮一致）", () => {
    const { container } = setup({ selectedItem: selected });
    const header = container.querySelector('.parse-cat-header[data-group-key="project_info"]')!;
    expect(header.getAttribute("aria-expanded")).toBe("true");
    const row = container.querySelector('.parse-cat-items .parse-cat-item-row[data-item-id="projectBasicInfo"]')!;
    expect(row).not.toBeNull();
  });
});

describe("ParseResultCategoryPanel · 加载 / 空 / 错误 三态", () => {
  it("加载中且无分组：展示加载态", () => {
    const { container } = setup({ groups: [], items: [], loading: true });
    expect(container.textContent || "").toContain("解析信息加载中");
  });

  it("空：无分组且非加载/错误：展示空态", () => {
    const { container } = setup({ groups: [], items: [] });
    expect(container.textContent || "").toContain("暂无解析分类");
  });

  it("错误：展示错误 Alert，点「重试」回调 onRefresh", () => {
    const onRefresh = vi.fn();
    const { container } = setup({ groups: [], items: [], error: "网络异常", onRefresh });
    expect(container.textContent || "").toContain("解析分类加载失败");
    expect(container.textContent || "").toContain("网络异常");
    fireEvent.click(btn(container, "重试")!);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

describe("ParseResultCategoryPanel · 窄屏抽屉", () => {
  it("窄屏：桌面左栏隐藏，点击「解析分类」打开抽屉展示分类", async () => {
    setMatchMedia(true);
    const { container } = setup();
    expect(container.querySelector(".parse-cat-bar-wrap")).toBeNull();
    const trigger = btn(container, "解析分类");
    expect(trigger).not.toBeNull();

    fireEvent.click(trigger!);
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("项目级基本信息");
    });
    expect(document.body.querySelector(".ant-drawer")).not.toBeNull();
  });

  it("桌面端：展示内联左栏，不出现抽屉触发按钮", () => {
    const { container } = setup();
    expect(container.querySelector(".parse-cat-bar-wrap")).not.toBeNull();
    expect(btn(container, "解析分类")).toBeNull();
  });
});

describe("ParseResultCategoryPanel · 纯函数", () => {
  it("groupCounts：统计已完成/总数", () => {
    const map = new Map<string, BaStoredItem>([
      ["projectBasicInfo", { item_id: "projectBasicInfo", status: "success" }],
    ]);
    expect(groupCounts(GROUPS[0], map)).toEqual({ total: 1, done: 1 });
    expect(groupCounts(GROUPS[1], map)).toEqual({ total: 2, done: 0 });
  });

  it("baStatusTag：success/pending/error/manual/无内容 口径", () => {
    expect((baStatusTag({ item_id: "x", status: "success", content: "abc" }, { output_type: "markdown" }) as any).props.color).toBe("green");
    expect((baStatusTag({ item_id: "x", status: "success", content: "未提取到" }, { output_type: "markdown" }) as any).props.color).toBe("volcano");
    expect((baStatusTag({ item_id: "x", status: "success", content: "abc", source: "manual" }, { output_type: "markdown" }) as any).props.color).toBe("purple");
    // error 态外层包裹 Tooltip（展示失败原因），红 Tag 在其 children 上
    const errEl = baStatusTag({ item_id: "x", status: "error" }, { output_type: "markdown" }) as any;
    expect(errEl.props.children.props.color).toBe("red");
    expect((baStatusTag(undefined, { output_type: "markdown" }) as any).props.color).toBe("default");
  });

  it("copyTextToClipboard：无 clipboard API 时降级 execCommand", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const exec = vi.fn().mockReturnValue(true);
    (document as any).execCommand = exec;
    const ok = await copyTextToClipboard("hello");
    expect(ok).toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
  });
});

// ===== 2026-09-25 补充：list-only / detail-only 跨实例联动（sharedState）=====
// 页面实际用法：左栏 list-only + 右侧 UploadParseTab 内置 detail-only 通过
// 同一 sharedParseState 对象串联展开/激活状态。此前单测只覆盖 full 变体，
// 这条关键的跨实例联动路径无任何测试。
describe("ParseResultCategoryPanel · list-only/detail-only 联动（sharedState）", () => {
  it("左栏 list-only 点击分类 → 右栏 detail-only 经 sharedState 同步展示该分类解析项", async () => {
    const onSelectItem = vi.fn();
    const sharedState = { expanded: {} as Record<string, boolean>, activeKey: null as string | null };
    const utils = render(
      <AntdApp>
        <div data-testid="list">
          <ParseResultCategoryPanel
            variant="list-only"
            groups={GROUPS}
            items={ITEMS}
            selectedItem={null}
            onSelectItem={onSelectItem}
            sharedState={sharedState}
          />
        </div>
        <div data-testid="detail">
          <ParseResultCategoryPanel
            variant="detail-only"
            groups={GROUPS}
            items={ITEMS}
            selectedItem={null}
            onSelectItem={onSelectItem}
            sharedState={sharedState}
          />
        </div>
      </AntdApp>,
    );
    const listEl = utils.getByTestId("list");
    const detailEl = utils.getByTestId("detail");

    // 初始：detail-only 未激活任何分类，应显示引导占位（不渲染具体解析项）
    expect(detailEl.textContent || "").toContain("从左侧选择一个分类");

    // 左栏点击「项目级基本信息」分类头
    const header = listEl.querySelector('.parse-cat-header[data-group-key="project_info"]');
    expect(header).not.toBeNull();
    fireEvent.click(header!);

    // 右栏 detail-only 应通过 sharedState.activeKey 联动展示该分类下的解析项
    await waitFor(() => {
      expect(detailEl.textContent || "").toContain("项目级基本信息");
    });
    // 且联动不应误触发 onSelectItem（选中由点击具体解析项触发）
    expect(onSelectItem).not.toHaveBeenCalled();
  });
});

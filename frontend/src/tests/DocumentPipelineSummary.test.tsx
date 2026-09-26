// @vitest-environment jsdom
/**
 * 解析质量（四层存储）概要 组件级交互测试。
 *
 * 锁定的行为：
 *   1. loading 态：显示"正在读取解析质量"，不渲染数据区；
 *   2. error 态：降级为非阻断告警（正文预览仍可用），展示失败原因；
 *   3. 空态：status 为 null → "暂无解析质量信息"；
 *   4. 数据态：解析状态/版本/页数/耗时/提取状态/有效期逐项渲染；
 *   5. 质量分：0~1 比值归一为百分数并按阈值着色；null → "未评估"（不显示 0%）；
 *   6. 四层产物：有产物 → 蓝色 Tag + 数量；无产物 → 中性 Tag + 0；
 *      meta_on_disk=false → 附加"meta 缺失"告警 Tag；
 *   7. 纯函数边界：layerFileCount / qualityPercent / formatDurationMs 对脏数据不抛异常。
 */
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import React from "react";
import DocumentPipelineSummary, {
  layerFileCount,
  parseStatusColor,
  qualityColor,
  qualityPercent,
  formatDurationMs,
  isActionAvailable,
  type PipelineActionKey,
} from "../components/DocumentPipelineSummary";

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

const status = (over: Record<string, unknown> = {}) => ({
  parse_status: "success",
  parse_version: "v2",
  page_count: 12,
  parse_duration_ms: 1500,
  extract_status: "pending",
  quality_score: 0.856,
  expires_at: "",
  meta_on_disk: true,
  layers: { raw: ["a.pdf"], parsed: ["x.md", "x_meta.json"], extracted: [], semantic: [] },
  ...over,
});

describe("DocumentPipelineSummary", () => {
  it("loading 态：显示读取中提示，不渲染质量数据", () => {
    const { container } = render(<DocumentPipelineSummary loading status={null} />);
    const txt = container.textContent || "";
    expect(txt).toContain("正在读取解析质量");
    expect(txt).not.toContain("解析状态");
  });

  it("error 态：降级为非阻断告警并展示原因", () => {
    const { container } = render(
      <DocumentPipelineSummary status={null} error="磁盘 meta 读取失败" />
    );
    const txt = container.textContent || "";
    expect(txt).toContain("解析质量信息读取失败");
    expect(txt).toContain("磁盘 meta 读取失败");
  });

  it("空态：status 为 null 显示占位，不渲染数据区", () => {
    const { container } = render(<DocumentPipelineSummary status={null} />);
    expect(container.textContent || "").toContain("暂无解析质量信息");
  });

  it("数据态：逐项渲染状态/版本/页数/耗时/提取状态", () => {
    const { container } = render(<DocumentPipelineSummary status={status()} />);
    const txt = container.textContent || "";
    expect(txt).toContain("success");
    expect(txt).toContain("版本 v2");
    expect(txt).toContain("12 页");
    expect(txt).toContain("1.5 s");
    expect(txt).toContain("pending");
  });

  it("质量分：0~1 比值显示为百分数；null 显示「未评估」而非 0%", () => {
    const ok = render(<DocumentPipelineSummary status={status({ quality_score: 0.856 })} />);
    expect(ok.container.textContent || "").toContain("86%");

    const none = render(<DocumentPipelineSummary status={status({ quality_score: null })} />);
    const txt = none.container.textContent || "";
    expect(txt).toContain("未评估");
    expect(txt).not.toContain("0%");
  });

  it("四层产物：有产物蓝色 Tag 带数量，无产物中性 Tag；meta 缺失时追加告警", () => {
    const { container } = render(<DocumentPipelineSummary status={status()} />);
    const tags = Array.from(container.querySelectorAll(".ant-tag")).map(
      (t) => t.textContent || ""
    );
    expect(tags.join("|")).toContain("原文层 1");
    expect(tags.join("|")).toContain("解析层 2");
    expect(tags.join("|")).toContain("提取层 0");
    expect(tags.join("|")).toContain("语义层 0");
    expect(tags.join("|")).not.toContain("meta 缺失");

    const warn = render(
      <DocumentPipelineSummary status={status({ meta_on_disk: false })} />
    );
    expect((warn.container.textContent || "")).toContain("meta 缺失");
  });
});

// ---------------------------------------------------------------------------
// 纯函数单测（边界 / 脏数据防御）
// ---------------------------------------------------------------------------

describe("layerFileCount", () => {
  it("正常计数；非数组 / 缺层 / 非对象一律 0", () => {
    expect(layerFileCount({ parsed: ["a", "b"] }, "parsed")).toBe(2);
    expect(layerFileCount({ parsed: "not-array" }, "parsed")).toBe(0);
    expect(layerFileCount({}, "parsed")).toBe(0);
    expect(layerFileCount(null, "parsed")).toBe(0);
    expect(layerFileCount(undefined, "parsed")).toBe(0);
    expect(layerFileCount("string", "parsed")).toBe(0);
    expect(layerFileCount(42, "parsed")).toBe(0);
  });
});

describe("parseStatusColor", () => {
  it("成功绿 / 失败红 / 进行中橙 / 未知中性（大小写与空白容错）", () => {
    expect(parseStatusColor("success")).toBe("green");
    expect(parseStatusColor("Parsed")).toBe("green");
    expect(parseStatusColor(" completed ")).toBe("green");
    expect(parseStatusColor("failed")).toBe("red");
    expect(parseStatusColor("error")).toBe("red");
    expect(parseStatusColor("pending")).toBe("orange");
    expect(parseStatusColor("processing")).toBe("orange");
    expect(parseStatusColor("weird")).toBe("default");
    expect(parseStatusColor("")).toBe("default");
    expect(parseStatusColor(undefined)).toBe("default");
  });
});

describe("qualityPercent / qualityColor", () => {
  it("0~1 比值 → 百分数；>1 按百分制兼容", () => {
    expect(qualityPercent(0.856)).toBe(86);
    expect(qualityPercent(0)).toBe(0);
    expect(qualityPercent(0.999)).toBe(100);
    expect(qualityPercent(85.6)).toBe(86); // 历史百分制脏数据兼容
  });

  it("无法识别（null/NaN/非数字）→ null，不误报 0%", () => {
    expect(qualityPercent(null)).toBeNull();
    expect(qualityPercent(undefined)).toBeNull();
    expect(qualityPercent(Number.NaN)).toBeNull();
    expect(qualityPercent("0.9" as unknown as number)).toBeNull();
  });

  it("颜色阈值：≥80 绿 / ≥60 橙 / 其余红 / 未评估灰", () => {
    expect(qualityColor(0.9)).toBe("green");
    expect(qualityColor(0.8)).toBe("green");
    expect(qualityColor(0.7)).toBe("orange");
    expect(qualityColor(0.3)).toBe("red");
    expect(qualityColor(null)).toBe("default");
  });
});

describe("formatDurationMs", () => {
  it("毫秒 / 秒分档；0、缺失、非法值返回空串", () => {
    expect(formatDurationMs(850)).toBe("850 ms");
    expect(formatDurationMs(1500)).toBe("1.5 s");
    expect(formatDurationMs(0)).toBe("");
    expect(formatDurationMs(undefined)).toBe("");
    expect(formatDurationMs(Number.NaN)).toBe("");
    expect(formatDurationMs(-100)).toBe("");
    expect(formatDurationMs("12" as unknown as number)).toBe("");
  });
});

// ---------------------------------------------------------------------------
// 组件级交互测试：四层管线操作按钮（2026-09-21 接线调用链时补）
// ---------------------------------------------------------------------------

/** 在容器内按精确文案找按钮 */
function btnByText(root: HTMLElement, text: string): HTMLButtonElement | null {
  const b = Array.from(root.querySelectorAll("button")).find(
    (x) => (x.textContent || "").trim() === text
  );
  return (b as HTMLButtonElement) || null;
}

describe("DocumentPipelineSummary 管线操作", () => {
  it("未传 onAction：不渲染任何操作按钮（纯展示向后兼容）", () => {
    const { container } = render(<DocumentPipelineSummary status={status()} />);
    expect(container.textContent || "").not.toContain("管线操作");
    expect(btnByText(container, "重新解析")).toBeNull();
  });

  it("传入 onAction 且解析成功：四个动作按钮齐全，点击回传对应 key", () => {
    const onAction = vi.fn();
    const { container } = render(
      <DocumentPipelineSummary status={status()} onAction={onAction} />
    );
    expect(container.textContent || "").toContain("管线操作");
    for (const label of ["重新解析", "物化提取层", "交叉校验", "刷新质量"]) {
      expect(btnByText(container, label)).not.toBeNull();
    }
    fireEvent.click(btnByText(container, "重新解析")!);
    expect(onAction).toHaveBeenCalledWith("reparse");
    fireEvent.click(btnByText(container, "物化提取层")!);
    expect(onAction).toHaveBeenCalledWith("syncExtractions");
    fireEvent.click(btnByText(container, "交叉校验")!);
    expect(onAction).toHaveBeenCalledWith("crossCheck");
    fireEvent.click(btnByText(container, "刷新质量")!);
    expect(onAction).toHaveBeenCalledWith("refreshQuality");
  });

  it("未解析成功：隐藏「物化提取层」（无源可物化），其余动作仍在", () => {
    const { container } = render(
      <DocumentPipelineSummary
        status={status({ parse_status: "pending" })}
        onAction={() => {}}
      />
    );
    expect(btnByText(container, "物化提取层")).toBeNull();
    expect(btnByText(container, "重新解析")).not.toBeNull();
    expect(btnByText(container, "刷新质量")).not.toBeNull();
  });

  it("actionBusy：当前动作按钮 loading，其余按钮禁用（防并发重入）", () => {
    const { container } = render(
      <DocumentPipelineSummary
        status={status()}
        onAction={() => {}}
        actionBusy="reparse"
      />
    );
    const reparse = btnByText(container, "重新解析")!;
    const cross = btnByText(container, "交叉校验")!;
    expect(reparse.className).toContain("ant-btn-loading");
    expect(cross.disabled).toBe(true);
  });

  it("loading / error 态：操作区不渲染（数据未到不可操作）", () => {
    const l = render(
      <DocumentPipelineSummary loading onAction={() => {}} status={null} />
    );
    expect(l.container.textContent || "").not.toContain("管线操作");
    const e = render(
      <DocumentPipelineSummary error="读取失败" onAction={() => {}} status={null} />
    );
    expect(e.container.textContent || "").not.toContain("管线操作");
  });
});

describe("isActionAvailable", () => {
  it("syncExtractions 仅解析成功后可用；其余动作恒可用", () => {
    const ok: PipelineActionKey[] = ["reparse", "syncExtractions", "crossCheck", "refreshQuality"];
    expect(isActionAvailable("syncExtractions", { parse_status: "success" })).toBe(true);
    expect(isActionAvailable("syncExtractions", { parse_status: "pending" })).toBe(false);
    expect(isActionAvailable("syncExtractions", { parse_status: "SUCCESS" })).toBe(true);
    expect(isActionAvailable("syncExtractions", null)).toBe(false);
    for (const a of ok.filter((x) => x !== "syncExtractions")) {
      expect(isActionAvailable(a, { parse_status: "pending" })).toBe(true);
      expect(isActionAvailable(a, null)).toBe(true);
    }
  });
});


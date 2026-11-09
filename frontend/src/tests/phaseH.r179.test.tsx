// @vitest-environment jsdom
/**
 * R179 Phase H（前端）：文种标签卡 / 统一缺陷账本 / 待确认清单 / 可复核底稿入口。
 *
 * 覆盖验收口径：
 * - 文种标签的 yes / no / undetermined 三态可区分，缺参数不冒充「非危大」；
 * - 缺陷账本 passed 与 undetermined 分列，fix_policy=protected 的「执行」按钮禁用；
 * - 待确认清单聚合事实与缺陷，数据源不可达时显示「无法判定」；
 * - 底稿导出触发下载，后端开关 404 时显式提示未启用。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { App } from "antd";
import { GenreTagCard } from "../components/review/GenreTagCard";
import { DefectLedgerPanel } from "../components/review/DefectLedgerPanel";
import { PendingConfirmPanel } from "../components/review/PendingConfirmPanel";
import { DossierExportCard } from "../components/review/DossierExportCard";
import { genreApi, defectsApi, factsApi, dossierApi } from "../api";

// ✅ 与全项目测试约定一致：补齐 jsdom 缺失的 matchMedia / ResizeObserver
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

vi.mock("../api", () => ({
  genreApi: { get: vi.fn(), evaluate: vi.fn() },
  defectsApi: {
    list: vi.fn(),
    summary: vi.fn(),
    sync: vi.fn(),
    verify: vi.fn(),
    apply: vi.fn(),
    decide: vi.fn(),
  },
  factsApi: { list: vi.fn() },
  dossierApi: { export: vi.fn() },
}));

const GENRE = {
  scheme_name: "深基坑专项施工方案",
  category_ids: ["deep_pit"],
  category_names: ["深基坑工程"],
  sub_ids: ["deep_pit_3m"],
  sub_names: ["开挖深度超过 3m"],
  hazard: "undetermined",
  oversize: "no",
  missing_params: ["基坑深度"],
  template_key: "deep_pit",
  labels: [],
  needs_calc: null,
  expert_review_required: null,
  source: "rules",
  rule_trace: [
    {
      layer: 1,
      rule_id: "GENRE-PIT",
      conclusion: "undetermined",
      evidence: ["方案名称命中深基坑"],
      missing_params: ["基坑深度"],
    },
  ],
  layer3: [],
};

const DEFECTS = [
  {
    id: "d-pass",
    title: "临边防护已满足",
    rule_id: "SAFE-1",
    dimension: "安全",
    severity: "low",
    status: "applied",
    verdict: "yes",
    fix_policy: "free",
    after_text: "",
    source_kind: "preflight",
    section_title: "第5章",
  },
  {
    id: "d-fail",
    title: "缺少应急预案",
    rule_id: "SAFE-2",
    dimension: "安全",
    severity: "high",
    status: "suggested",
    verdict: "no",
    fix_policy: "free",
    after_text: "补充应急预案。",
    source_kind: "preflight",
    section_title: "第6章",
  },
  {
    id: "d-undet",
    title: "几何参数无法核对",
    rule_id: "GEO-1",
    dimension: "一致性",
    severity: "medium",
    status: "open",
    verdict: "undetermined",
    fix_policy: "protected",
    after_text: "将 3.5m 改为 4.0m",
    source_kind: "consistency",
    section_title: "全文",
  },
];

const SUMMARY = {
  total: 3,
  passed: 1,
  failed: 1,
  undetermined: 1,
  pending: 0,
  counts: { yes: 1, no: 1, undetermined: 1 },
  pass_rule: "passed 仅统计 verdict=yes；undetermined 单列",
};

beforeEach(() => {
  vi.clearAllMocks();
  (genreApi.get as any).mockResolvedValue({ data: { enabled: true, genre: GENRE } });
  (defectsApi.list as any).mockResolvedValue({ data: { items: DEFECTS, total: 3 } });
  (defectsApi.summary as any).mockResolvedValue({ data: SUMMARY });
  (factsApi.list as any).mockResolvedValue({
    data: {
      groups: [
        {
          id: "geo",
          title: "工程概况",
          category: "engineering",
          content: "",
          items: [
            {
              fact_id: "f1",
              name: "基坑深度",
              value: "3.5",
              category: "engineering",
              is_simulated: true,
              is_resolved: false,
              has_conflict: false,
              is_stale: false,
              confidence: 0.55,
            },
          ],
        },
      ],
      stats: {},
      total_groups: 1,
    },
  });
  (dossierApi.export as any).mockResolvedValue({
    data: new Blob(["docx"]),
    headers: {},
  });
});

describe("GenreTagCard 三态", () => {
  it("undetermined 与 no 分开显示，缺参数有独立提示", async () => {
    render(
      <App>
        <GenreTagCard schemeId="s1" projectId="p1" />
      </App>,
    );
    expect(await screen.findByText("危大等级无法判定")).toBeTruthy();
    expect(screen.getAllByTestId("genre-tri-undetermined").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("genre-tri-no").length).toBeGreaterThan(0);
    const missing = screen.getByTestId("genre-missing-params");
    expect(missing.textContent).toContain("基坑深度");
    expect(missing.textContent).not.toContain("非危大");
  });

  it("后端开关关闭（404）显示未启用而不是空标签", async () => {
    (genreApi.get as any).mockRejectedValue({ response: { status: 404 } });
    render(
      <App>
        <GenreTagCard schemeId="s1" projectId="p1" />
      </App>,
    );
    expect(await screen.findByText("文种标签功能未启用")).toBeTruthy();
  });
});

describe("DefectLedgerPanel 三态与禁改", () => {
  it("passed / undetermined 分列；受保护缺陷禁用自动执行", async () => {
    render(
      <App>
        <DefectLedgerPanel schemeId="s1" />
      </App>,
    );
    expect(await screen.findByTestId("defect-summary-undetermined")).toBeTruthy();
    expect(screen.getByTestId("defect-summary-passed").textContent).toContain("通过 1");
    expect(screen.getByTestId("defect-summary-undetermined").textContent).toContain("无法判定 1");
    expect(screen.getByTestId("defect-verdict-undetermined")).toBeTruthy();
    expect(screen.getByTestId("defect-verdict-yes")).toBeTruthy();

    const protectedApply = screen.getByTestId("defect-apply-d-undet") as HTMLButtonElement;
    expect(protectedApply.disabled).toBe(true);
    expect(protectedApply.getAttribute("disabled")).not.toBeNull();

    const freeApply = screen.getByTestId("defect-apply-d-fail") as HTMLButtonElement;
    expect(freeApply.disabled).toBe(false);

    // 建议 / 执行标记必须分开
    expect(screen.getByText("建议（未执行）")).toBeTruthy();
    expect(screen.getByText("已执行（正文已写入）")).toBeTruthy();
    expect(screen.getByTestId("defect-protected-d-undet")).toBeTruthy();
  });

  it("受保护缺陷走人工裁决，不调用自动执行", async () => {
    (defectsApi.decide as any).mockResolvedValue({ data: {} });
    render(
      <App>
        <DefectLedgerPanel schemeId="s1" />
      </App>,
    );
    const accept = (await screen.findByTestId("defect-accept-d-undet")) as HTMLButtonElement;
    fireEvent.click(accept);
    fireEvent.click(await screen.findByText("确认接受"));
    await waitFor(() => expect(defectsApi.decide).toHaveBeenCalled());
    expect(defectsApi.apply).not.toHaveBeenCalled();
    expect((defectsApi.decide as any).mock.calls[0][1]).toMatchObject({ decision: "accept" });
  });
});

describe("PendingConfirmPanel 待确认清单", () => {
  it("聚合同一方案的事实与缺陷待确认项", async () => {
    render(
      <App>
        <PendingConfirmPanel schemeId="s1" />
      </App>,
    );
    expect(await screen.findByTestId("pending-count")).toBeTruthy();
    expect(screen.getByTestId("pending-count").textContent).toContain("待确认 3 项");
    expect(screen.getByText("未确认")).toBeTruthy();
    expect(screen.getByText("低置信度 0.55")).toBeTruthy();
    expect(screen.getByText("命中禁改白名单")).toBeTruthy();
    expect(screen.getByTestId("pending-standards-note")).toBeTruthy();
  });

  it("缺陷账本 404 时显示未启用，而不是 0 条", async () => {
    (defectsApi.list as any).mockRejectedValue({ response: { status: 404 } });
    render(
      <App>
        <PendingConfirmPanel schemeId="s1" />
      </App>,
    );
    expect(await screen.findByText("缺陷账本未启用")).toBeTruthy();
  });
});

describe("DossierExportCard 底稿导出", () => {
  it("点击后调用 Phase D 接口并触发下载", async () => {
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(
      <App>
        <DossierExportCard schemeId="s1" />
      </App>,
    );
    fireEvent.click(screen.getByTestId("dossier-export"));
    // ✅ R195 重锚：在飞请求带 AbortSignal（切方案/卸载可中止）
    await waitFor(() => expect(dossierApi.export).toHaveBeenCalledWith("s1", expect.anything()));
    await waitFor(() => expect(clickSpy).toHaveBeenCalledTimes(1));
    clickSpy.mockRestore();
  });

  it("后端开关关闭（404）提示未启用", async () => {
    (dossierApi.export as any).mockRejectedValue({ response: { status: 404 } });
    render(
      <App>
        <DossierExportCard schemeId="s1" />
      </App>,
    );
    fireEvent.click(screen.getByTestId("dossier-export"));
    expect(await screen.findByText("可复核底稿导出未启用")).toBeTruthy();
  });

  // ✅ R195：409 互斥 / 503 转换繁忙走 resolveExportError 差异化文案（不裸抛 message）
  it("409（生成互斥）展示差异化文案", async () => {
    (dossierApi.export as any).mockRejectedValue({
      message: "Request failed with status code 409",
      response: { status: 409, data: undefined },
    });
    render(
      <App>
        <DossierExportCard schemeId="s1" />
      </App>,
    );
    fireEvent.click(screen.getByTestId("dossier-export"));
    expect(await screen.findByText(/生成中/)).toBeTruthy();
  });

  it("卸载/切方案时中止在飞请求", async () => {
    let captured: AbortSignal | undefined;
    (dossierApi.export as any).mockImplementation((_sid: string, signal?: AbortSignal) => {
      captured = signal;
      return new Promise(() => {}); // 永不 resolve，模拟在飞
    });
    const { unmount } = render(
      <App>
        <DossierExportCard schemeId="s1" />
      </App>,
    );
    fireEvent.click(screen.getByTestId("dossier-export"));
    await waitFor(() => expect(captured).toBeTruthy());
    unmount();
    expect(captured!.aborted).toBe(true);
  });
});

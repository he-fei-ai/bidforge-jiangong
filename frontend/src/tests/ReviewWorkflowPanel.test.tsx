// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, act, cleanup } from "@testing-library/react";
import { App } from "antd";
import ReviewWorkflowPanel from "../components/review/ReviewWorkflowPanel";
import { reviewApi } from "../api";
import { clearActivity, getActivityItems } from "../utils/activityCenter";

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

const SCHEME = "scheme-x";
const DATA = vi.hoisted(() => ({
  CHECKLIST: {
    items: [
      { id: "sec-1", title: "第3章 基坑支护", word_count: 12000, review_status: "pending", review_status_label: "待审核", level: 1, last_reviewer: null, last_comment: "", last_reviewed_at: "" },
      { id: "sec-2", title: "第4章 降水", word_count: 8000, review_status: "", review_status_label: "未纳入审核", level: 1, last_reviewer: null, last_comment: "", last_reviewed_at: "" },
    ],
  },
  SUMMARY: { total_sections: 2, approved_sections: 0, reviewed_all: false, progress: 0, review_status: null, counts: {}, labels: {}, scheme_name: "示例方案", scheme_status: "目录已确认" },
  RECORDS: {
    items: [
      { id: "rec-1", section_id: "sec-1", section_title: "第3章 基坑支护", from_status: "pending", to_status: "approved", reviewer: "张三", comment: "同意", created_at: "2026-09-20T10:00:00", from_label: "待审核", to_label: "已通过" },
    ],
  },
}));

vi.mock("../api", () => ({
  reviewApi: {
    checklist: vi.fn(async () => ({ data: DATA.CHECKLIST })),
    summary: vi.fn(async () => ({ data: DATA.SUMMARY })),
    reviewSection: vi.fn(async () => ({ data: { ok: true } })),
    records: vi.fn(async () => ({ data: DATA.RECORDS })),
    batch: vi.fn(async () => ({ data: { changed: 2 } })),
    submit: vi.fn(async () => ({ data: { ok: true } })),
  },
}));

beforeEach(() => {
  localStorage.clear();
  (reviewApi.checklist as any).mockReset().mockResolvedValue({ data: DATA.CHECKLIST });
  (reviewApi.summary as any).mockReset().mockResolvedValue({ data: DATA.SUMMARY });
  (reviewApi.reviewSection as any).mockReset().mockResolvedValue({ data: { ok: true } });
  (reviewApi.records as any).mockReset().mockResolvedValue({ data: DATA.RECORDS });
  (reviewApi.batch as any).mockReset().mockResolvedValue({ data: { changed: 2 } });
  (reviewApi.submit as any).mockReset().mockResolvedValue({ data: { ok: true } });
});
afterEach(() => { cleanup(); localStorage.clear(); });

const norm = (s: string) => (s || "").replace(/\s+/g, "");
function btnByText(container: HTMLElement, text: string): HTMLButtonElement | null {
  return (Array.from(container.querySelectorAll("button")).find(
    (b) => norm(b.textContent).includes(norm(text)),
  ) as HTMLButtonElement | undefined) ?? null;
}
function confirmOk(): HTMLButtonElement | null {
  return (document.querySelector(".ant-modal-confirm-btns .ant-btn-primary") as HTMLButtonElement) ?? null;
}
function rowBtn(row: Element, label: string): HTMLButtonElement | null {
  return (Array.from(row.querySelectorAll("button")).find(
    (b) => norm(b.textContent).includes(norm(label)),
  ) as HTMLButtonElement | undefined) ?? null;
}
function setReviewer(container: HTMLElement, name: string) {
  const input = container.querySelector('input[placeholder="填写评审人姓名"]') as HTMLInputElement;
  if (!input) throw new Error("未找到评审人输入框");
  fireEvent.change(input, { target: { value: name } });
}

describe("ReviewWorkflowPanel · 章节审核工作流", () => {
  it("加载并渲染章节审核清单与进度", async () => {
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    expect(container.textContent || "").toContain("第4章 降水");
    expect(container.textContent || "").toContain("0/2 已通过");
    expect(reviewApi.checklist).toHaveBeenCalledWith(SCHEME);
    expect(reviewApi.summary).toHaveBeenCalledWith(SCHEME);
  });

  it("未填评审人 → 点击「通过」被前置拦截，弹窗不打开、reviewSection 不被调用", async () => {
    // ✅ BUG 修复回归：评审人必填，避免空评审人落库
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    const firstRow = container.querySelector(".ant-table-tbody .ant-table-row")!;
    fireEvent.click(rowBtn(firstRow, "通过")!);
    // 弹窗不应出现
    expect(confirmOk()).toBeNull();
    // 消息提示"请先填写评审人"
    await new Promise((r) => setTimeout(r, 50));
    expect(reviewApi.reviewSection).not.toHaveBeenCalled();
  });

  it("评审人填写 + 通过章节 → reviewApi.reviewSection(schemeId, secId, {to_status:'approved', reviewer})", async () => {
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "张三");
    const firstRow = container.querySelector(".ant-table-tbody .ant-table-row")!;
    fireEvent.click(rowBtn(firstRow, "通过")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.reviewSection).toHaveBeenCalled());
    const [sid, secId, body] = (reviewApi.reviewSection as any).mock.calls[0];
    expect(sid).toBe(SCHEME);
    expect(secId).toBe("sec-1");
    expect(body.to_status).toBe("approved");
    expect(body.reviewer).toBe("张三");
  });

  it("驳回未填理由 → OK 禁用、校验拦截，reviewApi.reviewSection 不被调用；填写理由后可提交", async () => {
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "张三");
    const firstRow = container.querySelector(".ant-table-tbody .ant-table-row")!;
    fireEvent.click(rowBtn(firstRow, "驳回")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    // 驳回理由必填：未填时确认按钮应为禁用
    expect((confirmOk() as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { fireEvent.click(confirmOk()!); });
    expect(reviewApi.reviewSection).not.toHaveBeenCalled();
    // 填写理由 → 启用 OK → 提交
    const ta = document.querySelector("textarea")!;
    await act(async () => { fireEvent.change(ta, { target: { value: "支护方案偏保守" } }); });
    await waitFor(() => expect((confirmOk() as HTMLButtonElement).disabled).toBe(false));
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.reviewSection).toHaveBeenCalled());
    const [, secId, body] = (reviewApi.reviewSection as any).mock.calls[0];
    expect(secId).toBe("sec-1");
    expect(body.to_status).toBe("rejected");
    expect(body.comment).toBe("支护方案偏保守");
  });

  it("reviewApi 局部失败（Promise.allSettled）→ 单侧失败不阻塞另一侧渲染", async () => {
    // ✅ BUG 修复回归：任一接口失败不该让整块空白
    (reviewApi.summary as any).mockRejectedValueOnce(new Error("进度服务异常"));
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    // checklist 成功 → 章节行仍应渲染
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    // summary 失败 → 但页面不至于空
    expect(container.textContent || "").toContain("章节审核工作流");
  });

  it("评审轨迹抽屉 → 调用 reviewApi.records 并渲染时间线", async () => {
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    fireEvent.click(btnByText(container, "评审轨迹")!);
    // ✅ G8 契约：轨迹分页（limit=50, offset=0），旧断言 (SCHEME, "", 100) 已随分页改造过时
    await waitFor(() => expect(reviewApi.records).toHaveBeenCalledWith(SCHEME, "", 50, 0));
    // 抽屉显示时间线内容
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("张三");
      expect(document.body.textContent || "").toContain("已通过");
    });
  });

  it("批量通过：勾选多行 → 弹窗批量审核，调用 reviewApi.batch", async () => {
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "李四");
    // ✅ 行级 checkbox：排除 antd scroll.y 下的粘滞表头「全选」复选框
    const rowCbs = Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-row"))
      .map((row) => row.querySelector("input[type='checkbox']") as HTMLInputElement);
    expect(rowCbs.length).toBe(2);
    fireEvent.click(rowCbs[0]!);
    fireEvent.click(rowCbs[1]!);
    await waitFor(() => expect(container.textContent || "").toContain("已选 2 个章节"));
    fireEvent.click(btnByText(container, "批量通过")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.batch).toHaveBeenCalled());
    const [sid, body] = (reviewApi.batch as any).mock.calls[0];
    expect(sid).toBe(SCHEME);
    expect(body.section_ids).toHaveLength(2);
    expect(body.to_status).toBe("approved");
    expect(body.reviewer).toBe("李四");
  });

  it("方案级提交（未全部审核）→ 弹窗内显示 warning；确认后调用 submit", async () => {
    // ✅ BUG 修复回归：「提交审核通过」前置提示
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "王五");
    fireEvent.click(btnByText(container, "提交审核通过")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("尚无任何章节完成审核");
    });
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.submit).toHaveBeenCalled());
    const [sid, body] = (reviewApi.submit as any).mock.calls[0];
    expect(sid).toBe(SCHEME);
    expect(body.to_status).toBe("approved");
    expect(body.reviewer).toBe("王五");
  });

  it("方案级提交未填评审人 → 被前置拦截，弹窗不打开", async () => {
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    fireEvent.click(btnByText(container, "提交审核通过")!);
    expect(confirmOk()).toBeNull();
    await new Promise((r) => setTimeout(r, 50));
    expect(reviewApi.submit).not.toHaveBeenCalled();
  });

  it("reviewApi.submit 抛 422 → catch 提示错误，提交按钮复位", async () => {
    (reviewApi.submit as any).mockRejectedValueOnce(new Error("存在交付阻断项"));
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "王五");
    fireEvent.click(btnByText(container, "提交审核通过")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.submit).toHaveBeenCalled());
    // 提交失败后 submitting 应复位（按钮不再 loading）
    await waitFor(() => {
      const btn = btnByText(container, "提交审核通过");
      expect(btn!.disabled).toBe(false);
    });
  });

  it("切换 schemeId：旧方案迟到的审核响应不得覆盖新方案", async () => {
    let resolveChecklist: ((value: any) => void) | undefined;
    let resolveSummary: ((value: any) => void) | undefined;
    const oldChecklist = new Promise<any>((resolve) => { resolveChecklist = resolve; });
    const oldSummary = new Promise<any>((resolve) => { resolveSummary = resolve; });
    (reviewApi.checklist as any).mockImplementation((sid: string) =>
      sid === "old" ? oldChecklist : Promise.resolve({
        data: { items: [{ ...DATA.CHECKLIST.items[0], id: "new-1", title: "新方案第一章" }] },
      }));
    (reviewApi.summary as any).mockImplementation((sid: string) =>
      sid === "old" ? oldSummary : Promise.resolve({
        data: { ...DATA.SUMMARY, total_sections: 1, approved_sections: 1, progress: 100 },
      }));
    const { container, rerender } = render(
      <App><ReviewWorkflowPanel schemeId="old" /></App>);
    await waitFor(() => expect(reviewApi.checklist).toHaveBeenCalledWith("old"));
    rerender(<App><ReviewWorkflowPanel schemeId="new" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("新方案第一章"));
    await waitFor(() => expect(container.textContent || "").toContain("1/1 已通过"));
    await act(async () => {
      resolveChecklist?.({ data: { items: [
        { ...DATA.CHECKLIST.items[0], id: "old-1", title: "旧方案第一章" },
      ] } });
      resolveSummary?.({ data: { ...DATA.SUMMARY, total_sections: 1, approved_sections: 0 } });
      await Promise.resolve();
    });
    expect(container.textContent || "").toContain("新方案第一章");
    expect(container.textContent || "").not.toContain("旧方案第一章");
    expect(container.textContent || "").toContain("1/1 已通过");
  });

  it("空方案态：checklist 返回空 → 显示「方案暂无章节」", async () => {
    (reviewApi.checklist as any).mockResolvedValueOnce({ data: { items: [] } });
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("方案暂无章节"));
  });

  it("评审人姓名持久化到 localStorage；下次渲染自动带入", async () => {
    const { container, unmount } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "赵六");
    await waitFor(() => {
      expect(localStorage.getItem("scheme_review_reviewer_name")).toBe("赵六");
    });
    unmount();
    // 新组件读取 localStorage 里的评审人
    const { container: c2 } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(c2.textContent || "").toContain("第3章 基坑支护"));
    const input = c2.querySelector('input[placeholder="填写评审人姓名"]') as HTMLInputElement;
    expect(input.value).toBe("赵六");
  });

  it("重置按钮：已审核章节可回到 pending", async () => {
    const APPROVED_ITEM = {
      items: [
        { id: "sec-1", title: "第3章 基坑支护", word_count: 12000,
          review_status: "approved", review_status_label: "已通过", level: 1,
          last_reviewer: "张三", last_comment: "同意", last_reviewed_at: "2026-09-20T10:00:00" },
      ],
    };
    (reviewApi.checklist as any).mockResolvedValueOnce({ data: APPROVED_ITEM });
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "王五");
    const firstRow = container.querySelector(".ant-table-tbody .ant-table-row")!;
    expect(rowBtn(firstRow, "重置")).toBeTruthy();
    fireEvent.click(rowBtn(firstRow, "重置")!);
    await waitFor(() => expect(reviewApi.reviewSection).toHaveBeenCalled());
    const [, secId, body] = (reviewApi.reviewSection as any).mock.calls[0];
    expect(secId).toBe("sec-1");
    expect(body.to_status).toBe("pending");
    expect(body.comment).toBe("重置为待审核");
  });

  it("onChanged 回调：审核操作后触发，供父组件刷新目录树", async () => {
    const onChanged = vi.fn();
    const { container } = render(
      <App><ReviewWorkflowPanel schemeId={SCHEME} onChanged={onChanged} /></App>,
    );
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "张三");
    const firstRow = container.querySelector(".ant-table-tbody .ant-table-row")!;
    fireEvent.click(rowBtn(firstRow, "通过")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.reviewSection).toHaveBeenCalled());
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("多选勾选 → selected 数量展示，取消选择可清空", async () => {
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    const rowCbs = Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-row"))
      .map((row) => row.querySelector("input[type='checkbox']") as HTMLInputElement);
    fireEvent.click(rowCbs[0]!);
    await waitFor(() => expect(container.textContent || "").toContain("已选 1 个章节"));
    fireEvent.click(btnByText(container, "取消选择")!);
    await waitFor(() => {
      expect(container.textContent || "").not.toContain("已选 1 个章节");
    });
  });

  // ===== ✅ BUG-A 回归（2026-09-23）：成功消息按目标状态取全量文案 =====
  it("重置操作消息 → 「已标记为待审核」而非误报驳回", async () => {
    clearActivity();
    const APPROVED_ITEM = {
      items: [
        { id: "sec-1", title: "第3章 基坑支护", word_count: 12000,
          review_status: "approved", review_status_label: "已通过", level: 1,
          last_reviewer: "张三", last_comment: "同意", last_reviewed_at: "2026-09-20T10:00:00" },
      ],
    };
    (reviewApi.checklist as any).mockResolvedValueOnce({ data: APPROVED_ITEM });
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "赵一");
    const firstRow = container.querySelector(".ant-table-tbody .ant-table-row")!;
    fireEvent.click(rowBtn(firstRow, "重置")!);
    await waitFor(() => expect(reviewApi.reviewSection).toHaveBeenCalled());
    await waitFor(() => expect(getActivityItems().length).toBeGreaterThan(0));
    const latest = getActivityItems()[0];
    expect(latest.kind).toBe("success");
    expect(latest.text).toContain("已标记为「待审核」");
    expect(latest.text).not.toContain("驳回");
  });

  it("开始审核（reviewing）消息 → 显示「审核中」而非驳回", async () => {
    clearActivity();
    const REVIEWING_ITEM = {
      items: [
        { id: "sec-1", title: "第3章 基坑支护", word_count: 12000,
          review_status: "pending", review_status_label: "待审核", level: 1,
          last_reviewer: null, last_comment: "", last_reviewed_at: "" },
      ],
    };
    (reviewApi.checklist as any).mockResolvedValueOnce({ data: REVIEWING_ITEM });
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "钱二");
    const firstRow = container.querySelector(".ant-table-tbody .ant-table-row")!;
    fireEvent.click(rowBtn(firstRow, "开始审核")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.reviewSection).toHaveBeenCalled());
    const [, , body] = (reviewApi.reviewSection as any).mock.calls[0];
    expect(body.to_status).toBe("reviewing");
    await waitFor(() => expect(getActivityItems().length).toBeGreaterThan(0));
    const latest = getActivityItems()[0];
    expect(latest.kind).toBe("success");
    expect(latest.text).toContain("已标记为「审核中」");
    expect(latest.text).not.toContain("驳回");
  });

  // ===== ✅ BUG-B 回归（2026-09-23）：批量部分失败必须给出明细反馈 =====
  it("批量通过部分被状态机拦截 → 降级 warning 并带明细，不再静默吞掉", async () => {
    clearActivity();
    (reviewApi.batch as any).mockResolvedValueOnce({
      data: { changed: 1, changed_ids: ["sec-1"], skipped: ["sec-2"],
        not_found: [], nochange: [], total_ids: 2, truncated: false },
    });
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "李四");
    const rowCbs = Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-row"))
      .map((row) => row.querySelector("input[type='checkbox']") as HTMLInputElement);
    fireEvent.click(rowCbs[0]!);
    fireEvent.click(rowCbs[1]!);
    await waitFor(() => expect(container.textContent || "").toContain("已选 2 个章节"));
    fireEvent.click(btnByText(container, "批量通过")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.batch).toHaveBeenCalled());
    await waitFor(() => expect(getActivityItems().length).toBeGreaterThan(0));
    const latest = getActivityItems()[0];
    expect(latest.kind).toBe("warning");
    expect(latest.text).toContain("已通过 1 个章节");
    expect(latest.text).toContain("1 个被状态机拦截");
  });

  it("批量操作全部无变更 → warning 提示而非成功假象", async () => {
    clearActivity();
    (reviewApi.batch as any).mockResolvedValueOnce({
      data: { changed: 0, changed_ids: [], skipped: [],
        not_found: [], nochange: ["sec-1", "sec-2"], total_ids: 2, truncated: false },
    });
    const { container } = render(<App><ReviewWorkflowPanel schemeId={SCHEME} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("第3章 基坑支护"));
    setReviewer(container, "孙七");
    const rowCbs = Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-row"))
      .map((row) => row.querySelector("input[type='checkbox']") as HTMLInputElement);
    fireEvent.click(rowCbs[0]!);
    fireEvent.click(rowCbs[1]!);
    await waitFor(() => expect(container.textContent || "").toContain("已选 2 个章节"));
    fireEvent.click(btnByText(container, "批量驳回")!);
    await waitFor(() => expect(confirmOk()).toBeTruthy());
    await act(async () => { fireEvent.click(confirmOk()!); });
    await waitFor(() => expect(reviewApi.batch).toHaveBeenCalled());
    await waitFor(() => expect(getActivityItems().length).toBeGreaterThan(0));
    const latest = getActivityItems()[0];
    expect(latest.kind).toBe("warning");
    expect(latest.text).toContain("没有章节发生状态变更");
    expect(latest.text).toContain("2 个已是目标状态");
  });
});
// @vitest-environment jsdom
/**
 * ✅ R174 F3/F4（2026-11-03）：方案表单校验回归（ProjectDetailPage）。
 *
 * 旧缺陷：
 *   F3：「从清单选择」路径名称非必填 → 空串 / 纯空格方案名落库，侧栏树出空白条目；
 *   F4：word_budget 用 <Input type="number"> 无上下界 → -1 / 99999999 直接落库。
 *
 * 修复：名称必填 + whitespace 规则（antd 原生支持纯空格拒绝），提交前 trim；
 * word_budget 改 InputNumber（min 1000 / max 1000000）+ 范围 rule。
 * 注：后端 models.py 的 name: str / word_budget: int 无约束，由后端批次负责，
 * 本批仅前端兜底 —— 因此本文件只断言前端行为（拦截提交、错误提示）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ProjectDetailPage, {
  WORD_BUDGET_MIN,
  WORD_BUDGET_MAX,
  WORD_BUDGET_RULES,
} from "../pages/ProjectDetailPage";
import { schemesApi } from "../api";

// jsdom 缺 matchMedia / ResizeObserver / scrollIntoView，antd 会用到
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
if (!(HTMLElement.prototype as any).scrollIntoView) {
  (HTMLElement.prototype as any).scrollIntoView = () => {};
}

const h = vi.hoisted(() => ({
  project: { id: "p1", name: "示范项目" },
  schemes: [],
  docs: [],
}));

vi.mock("../api", () => {
  const ok = (data: unknown) => vi.fn(async () => ({ data }) as never);
  return {
    projectsApi: { get: ok(h.project), list: ok([h.project]) },
    schemesApi: {
      list: ok([]),
      create: vi.fn(async () => ({ data: { id: "s-new", name: "x" } }) as never),
      update: vi.fn(async () => ({ data: {} }) as never),
      remove: vi.fn(async () => ({ data: {} }) as never),
    },
    schemeCatalogApi: {
      list: ok([]),
      get: ok({}),
    },
    knowledgeApi: {
      list: ok([]),
      create: vi.fn(async () => ({ data: {} }) as never),
      update: vi.fn(async () => ({ data: {} }) as never),
      delete: vi.fn(async () => ({ data: {} }) as never),
    },
  };
});

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => cleanup());

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/project/p1"]}>
      <Routes>
        <Route path="/project/:id" element={<ProjectDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function getModalOkButton(text: string): HTMLButtonElement {
  const b = Array.from(document.querySelectorAll<HTMLButtonElement>(".ant-modal button")).find(
    (x) => x.textContent!.replace(/\s/g, "").includes(text),
  )!;
  expect(b).toBeTruthy();
  return b;
}

async function openCreateModal() {
  renderPage();
  // 页面首屏加载后出现「新建方案」入口
  const btn = await waitFor(() => {
    const b = screen.queryByText("新建方案");
    expect(b).toBeTruthy();
    return b as HTMLElement;
  });
  fireEvent.click(btn);
  await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
}

describe("ProjectDetailPage · 方案名称必填校验（R174 F3）", () => {
  it("空名称提交 → 拦截并提示「不能为空或纯空格」，不调 create", async () => {
    await openCreateModal();
    fireEvent.click(getModalOkButton("创建方案"));
    await waitFor(() => {
      expect(document.body.textContent).toContain("不能为空或纯空格");
    });
    expect(schemesApi.create).not.toHaveBeenCalled();
  });

  it("纯空格名称提交 → 同样拦截（whitespace 规则）", async () => {
    await openCreateModal();
    const input = document.querySelector<HTMLInputElement>(
      ".ant-modal input#name, .ant-modal .ant-form-item input",
    )!;
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(getModalOkButton("创建方案"));
    await waitFor(() => {
      expect(document.body.textContent).toContain("不能为空或纯空格");
    });
    expect(schemesApi.create).not.toHaveBeenCalled();
  });

  it("合法名称 + 前后空格 → 提交前 trim 后调 create", async () => {
    await openCreateModal();
    const input = document.querySelector<HTMLInputElement>(".ant-modal .ant-form-item input")!;
    fireEvent.change(input, { target: { value: "  深基坑方案  " } });
    fireEvent.click(getModalOkButton("创建方案"));
    await waitFor(() => expect(schemesApi.create).toHaveBeenCalled());
    const payload = vi.mocked(schemesApi.create).mock.calls[0];
    const body = payload[payload.length - 1] as Record<string, unknown>;
    expect(body.name).toBe("深基坑方案");
  });
});

describe("ProjectDetailPage · 目标字数范围校验（R174 F4）", () => {
  // 实测行为（R174 调试取证）：antd InputNumber min/max 有两层防护 ——
  //  ① blur 时把越界输入钳制到边界（store 只可能是合法值，提交被 clamp 兜住）；
  //  ② 未 blur 直接提交时，form rule 拦截并展示范围错误提示。
  it("越界输入在 blur 时被钳制到下界（min=1000 防护层）", async () => {
    await openCreateModal();
    // data-testid 在 InputNumber 包装层，实际输入用其内部 input；
    // rc-input-number 的 onChange 由 input 事件驱动（fireEvent.input）
    const budgetInput = document.querySelector<HTMLInputElement>(
      ".ant-modal .ant-input-number input",
    )!;
    expect(budgetInput).toBeTruthy();
    fireEvent.input(budgetInput, { target: { value: "100" } });
    fireEvent.blur(budgetInput);
    const nameInput = document.querySelector<HTMLInputElement>(".ant-modal .ant-form-item input")!;
    fireEvent.change(nameInput, { target: { value: "方案A" } });
    fireEvent.click(getModalOkButton("创建方案"));
    await waitFor(() => expect(schemesApi.create).toHaveBeenCalled());
    const payload = vi.mocked(schemesApi.create).mock.calls[0];
    const body = payload[payload.length - 1] as Record<string, unknown>;
    expect(body.word_budget).toBe(1000);
  });

  it("越界输入在 blur 时被钳制到上界（max=1000000 防护层）", async () => {
    await openCreateModal();
    const budgetInput = document.querySelector<HTMLInputElement>(
      ".ant-modal .ant-input-number input",
    )!;
    fireEvent.input(budgetInput, { target: { value: "2000000" } });
    fireEvent.blur(budgetInput);
    const nameInput = document.querySelector<HTMLInputElement>(".ant-modal .ant-form-item input")!;
    fireEvent.change(nameInput, { target: { value: "方案B" } });
    fireEvent.click(getModalOkButton("创建方案"));
    await waitFor(() => expect(schemesApi.create).toHaveBeenCalled());
    const payload = vi.mocked(schemesApi.create).mock.calls[0];
    const body = payload[payload.length - 1] as Record<string, unknown>;
    expect(body.word_budget).toBe(1000000);
  });

  it("form rule 层：导出的 WORD_BUDGET_RULES 校验器拒绝越界值并提示范围（防护层②）", async () => {
    // rc-input-number 在测试环境下仅 blur 时提交（clamp 后值恒合法），
    // rule 层按导出校验器直接单测（越界提交被拦的兜底逻辑）
    const validator = WORD_BUDGET_RULES[0].validator;
    await expect(validator(null, 100)).rejects.toThrow("1,000 ~ 1,000,000");
    await expect(validator(null, 0)).rejects.toThrow();
    await expect(validator(null, -1)).rejects.toThrow();
    await expect(validator(null, 1000001)).rejects.toThrow();
    await expect(validator(null, 999999999)).rejects.toThrow();
    await expect(validator(null, NaN)).rejects.toThrow();
    // 合法值 / 空值放行（空值由 InputNumber initialValue=30000 兜底）
    await expect(validator(null, 30000)).resolves.toBeUndefined();
    await expect(validator(null, 1000)).resolves.toBeUndefined();
    await expect(validator(null, 1000000)).resolves.toBeUndefined();
    await expect(validator(null, undefined)).resolves.toBeUndefined();
    // 常量边界与组件 min/max 一致（DOM 钳制层）
    expect(WORD_BUDGET_MIN).toBe(1000);
    expect(WORD_BUDGET_MAX).toBe(1000000);
  });
});

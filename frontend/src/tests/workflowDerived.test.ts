// @vitest-environment node
/**
 * workflowDerived.ts 纯函数测试。
 *
 * 覆盖：
 *   - computeDocStats：空数组、全解析、部分解析、截断、脏数据防御
 *   - pickInitialTab：优先级链每一阶 + factsTotal=null 时延迟决策
 *   - NEXT_TAB / PREV_TAB：顺序映射完整性
 */
import { describe, it, expect } from "vitest";
import {
  computeDocStats,
  pickInitialTab,
  summarizeUploadResult,
  buildUploadNotice,
  NEXT_TAB,
  PREV_TAB,
  selectPlacedExportCharts,
  findDefaultExportPreset,
  canShrinkSection,
  WORD_OVER_RATIO,
  type DocRecord,
} from "../utils/workflowDerived";
import { isDocParsed, isDocFailed } from "../utils/workflowDerived";

// -------------- computeDocStats --------------
describe("computeDocStats", () => {
  const mkDoc = (overrides: Partial<DocRecord> = {}): DocRecord => ({
    id: "d1",
    text_len: 5000,
    truncated: false,
    ...overrides,
  });

  it("空数组：parsed=0 pending=0 truncated=0 failed=0 allParsed=false（空≠全部就绪）", () => {
    expect(computeDocStats([])).toEqual({
      parsedCount: 0,
      pendingCount: 0,
      truncatedCount: 0,
      failedCount: 0,
      actionableCount: 0,
      allParsed: false,
    });
  });

  it("null / undefined / 非数组：同空数组返回", () => {
    // @ts-expect-error 故意传入非数组验证防御性
    expect(computeDocStats(null)).toEqual({ parsedCount: 0, pendingCount: 0, truncatedCount: 0, failedCount: 0, actionableCount: 0, allParsed: false });
    // @ts-expect-error
    expect(computeDocStats(undefined)).toEqual({ parsedCount: 0, pendingCount: 0, truncatedCount: 0, failedCount: 0, actionableCount: 0, allParsed: false });
  });

  it("全解析：allParsed=true，pending=0", () => {
    const docs = [mkDoc(), mkDoc({ truncated: true }), mkDoc()];
    const stats = computeDocStats(docs);
    expect(stats).toEqual({
      parsedCount: 3,
      pendingCount: 0,
      truncatedCount: 1,
      failedCount: 0,
      actionableCount: 0,
      allParsed: true,
    });
  });

  it("部分解析：1 已解析 + 1 待解析 + 1 截断已解析", () => {
    const docs = [
      mkDoc({ id: "a", text_len: 3000 }),
      mkDoc({ id: "b", text_len: 0 }),          // 待解析
      mkDoc({ id: "c", text_len: 8000, truncated: true }),
      mkDoc({ id: "d" }),                       // 默认已解析
    ];
    const stats = computeDocStats(docs);
    expect(stats.parsedCount).toBe(3);
    expect(stats.pendingCount).toBe(1);
    expect(stats.truncatedCount).toBe(1);
    expect(stats.allParsed).toBe(false);
  });

  it("所有文档都待解析（全 0 / 缺失 text_len）", () => {
    const docs: DocRecord[] = [
      mkDoc({ text_len: 0 }),
      mkDoc({ text_len: undefined }),
      // 完全没有 text_len 字段（后端老数据或异常值）
      { id: "x", truncated: false },
    ];
    const stats = computeDocStats(docs);
    expect(stats.parsedCount).toBe(0);
    expect(stats.pendingCount).toBe(3);
    expect(stats.allParsed).toBe(false);
  });

  it("truncated 只在已解析文档中计数（待解析的 truncated 不产生噪声）", () => {
    const docs = [
      mkDoc({ text_len: 0, truncated: true }),  // 待解析但标记截断 → 不算
      mkDoc({ text_len: 5000, truncated: true }), // 已解析 + 截断 → 算
    ];
    expect(computeDocStats(docs).truncatedCount).toBe(1);
  });

  it("text_len 为 NaN：按待解析处理（防御性）", () => {
    const docs = [mkDoc({ text_len: Number.NaN }), mkDoc({ text_len: 100 })];
    const stats = computeDocStats(docs);
    expect(stats.parsedCount).toBe(1);
    expect(stats.pendingCount).toBe(1);
  });

  it("parse_status='failed' 单独计数：不计入已解析/待解析", () => {
    const docs: DocRecord[] = [
      mkDoc({ id: "ok", text_len: 100, parse_status: "success" }),
      mkDoc({ id: "bad", parse_status: "failed" }),   // 解析失败
      mkDoc({ id: "pend", text_len: 0 }),              // 待解析
    ];
    const stats = computeDocStats(docs);
    expect(stats.parsedCount).toBe(1);
    expect(stats.pendingCount).toBe(1);
    expect(stats.failedCount).toBe(1);
    // 待解析 + 失败都在「解析全部」处理范围内（与后端 parse-all 选取口径一致）
    expect(stats.actionableCount).toBe(2);
    // 有失败文档 → 不算全部就绪
    expect(stats.allParsed).toBe(false);
  });

  it("actionableCount：全待解析=文档数；含失败=待解析+失败；全成功=0", () => {
    // 全待解析
    expect(computeDocStats([
      mkDoc({ id: "a", text_len: 0 }),
      mkDoc({ id: "b", text_len: 0 }),
    ]).actionableCount).toBe(2);
    // 仅剩失败文档（批量按钮必须仍可用，文案为「重试失败文档」）
    const onlyFailed = computeDocStats([
      mkDoc({ id: "a", parse_status: "failed" }),
      mkDoc({ id: "b", parse_status: "failed" }),
    ]);
    expect(onlyFailed.actionableCount).toBe(2);
    expect(onlyFailed.pendingCount).toBe(0);
    // 全部成功 → 批量入口禁用
    expect(computeDocStats([mkDoc(), mkDoc()]).actionableCount).toBe(0);
  });

  it("全部失败（无成功/待解析）：parsed=0 pending=0 failed=N allParsed=false", () => {
    const docs: DocRecord[] = [
      mkDoc({ id: "a", parse_status: "failed" }),
      mkDoc({ id: "b", parse_status: "failed" }),
    ];
    const stats = computeDocStats(docs);
    expect(stats.parsedCount).toBe(0);
    expect(stats.pendingCount).toBe(0);
    expect(stats.failedCount).toBe(2);
    expect(stats.allParsed).toBe(false);
  });

  it("无 parse_status 的旧数据：回退 text_len 口径（向后兼容）", () => {
    const docs: DocRecord[] = [
      mkDoc({ id: "a", text_len: 100 }),     // 无 parse_status → 视为已解析
      mkDoc({ id: "b", text_len: 0 }),       // 无 parse_status → 待解析
    ];
    const stats = computeDocStats(docs);
    expect(stats.parsedCount).toBe(1);
    expect(stats.pendingCount).toBe(1);
    expect(stats.failedCount).toBe(0);
  });
});

// -------------- pickInitialTab --------------
describe("pickInitialTab", () => {
  const d = (overrides?: Partial<DocRecord>): DocRecord => ({
    id: "x",
    text_len: 1000,
    truncated: false,
    ...overrides,
  });

  it("factsTotal=null → null（数据未加载，暂不决策）", () => {
    expect(
      pickInitialTab({ docs: [d()], tree: ["t1"], factsTotal: null })
    ).toBeNull();
  });

  // ✅ 用户要求（2026-09-17）：打开工作台时优先进「上传解析」（原「文件导入」），不管数据状态
  it("数据已加载 → 固定返回 import（用户要求优先进上传解析）", () => {
    // 全空 → import
    expect(pickInitialTab({ docs: [], tree: [], factsTotal: 0 })).toBe("import");
    // 有文档但待解析 → import
    expect(pickInitialTab({ docs: [d({ text_len: 0 })], tree: [], factsTotal: 0 })).toBe("import");
    // 有已解析文档 + 有目录 + 有事实 → import
    expect(pickInitialTab({ docs: [d()], tree: ["t1"], factsTotal: 3 })).toBe("import");
    // 全就绪（有文档 + 有目录 + 有事实）→ import
    expect(pickInitialTab({ docs: [d()], tree: ["t1"], factsTotal: 3 })).toBe("import");
  });
});

// -------------- NEXT_TAB / PREV_TAB --------------
describe("NEXT_TAB", () => {
  // 2026-09-23 合并后：bidAnalysis 已收进 import 子 Tab，工作流降为 6 步
  it("6 步顺序：import→outline→facts→content→review→export", () => {
    expect(NEXT_TAB.import).toBe("outline");
    expect(NEXT_TAB.outline).toBe("facts");
    expect(NEXT_TAB.facts).toBe("content");
    expect(NEXT_TAB.content).toBe("review");
    expect(NEXT_TAB.review).toBe("export");
  });

  it("export 是终点，自指", () => {
    expect(NEXT_TAB.export).toBe("export");
  });

  it("6 个 key 全覆盖，且不含已合并的 bidAnalysis/parse", () => {
    const allKeys = Object.keys(NEXT_TAB);
    expect(allKeys).toHaveLength(6);
    for (const k of ["import", "outline", "facts", "content", "review", "export"]) {
      expect(allKeys).toContain(k);
    }
    expect(allKeys).not.toContain("parse");
    // 关键回归：合并后 bidAnalysis 不能再作为顶层 Tab key
    expect(allKeys).not.toContain("bidAnalysis");
  });
});

describe("PREV_TAB", () => {
  it("import 无前置（null），outline 回退到 import（合并后 bidAnalysis 不再是独立层）", () => {
    expect(PREV_TAB.import).toBeNull();
    expect(PREV_TAB.outline).toBe("import");
    expect(PREV_TAB.facts).toBe("outline");
    expect(PREV_TAB.content).toBe("facts");
    expect(PREV_TAB.review).toBe("content");
    expect(PREV_TAB.export).toBe("review");
  });
});

// -------------- summarizeUploadResult --------------
describe("summarizeUploadResult", () => {
  it("正常上传：只统计 savedCount / replaced，无拒绝项", () => {
    const fb = summarizeUploadResult({ saved_count: 3, replaced: 1 });
    expect(fb).toEqual({ savedCount: 3, replaced: 1, rejected: [], details: [] });
  });

  it("六类未保存原因全部纳入 rejected（旧实现漏报后三类）", () => {
    const fb = summarizeUploadResult({
      saved_count: 2,
      unsupported: ["a.rar"],
      oversize: ["b.zip"],
      signature_invalid: ["c.pdf"],
      empty: ["d.txt"],
      too_many: ["e.docx", "f.docx"],
      quota_exceeded: true,
    });
    expect(fb.savedCount).toBe(2);
    expect(fb.rejected).toHaveLength(6);
    expect(fb.rejected.join("|")).toContain("文件头校验不通过");
    expect(fb.rejected.join("|")).toContain("超出单次 20 个上限");
    expect(fb.rejected.join("|")).toContain("累计体积超过 200MB");
  });

  it("details 透传后端 warnings 并过滤空串", () => {
    const fb = summarizeUploadResult({
      saved_count: 1,
      warnings: ["以下文件为空（0 字节）已忽略：x.txt", "", "替换了 1 个同名旧文件"],
    });
    expect(fb.details).toEqual([
      "以下文件为空（0 字节）已忽略：x.txt",
      "替换了 1 个同名旧文件",
    ]);
  });

  it("空 / null / 脏数据：不抛异常，返回零值", () => {
    expect(summarizeUploadResult(null)).toEqual({
      savedCount: 0, replaced: 0, rejected: [], details: [],
    });
    expect(summarizeUploadResult(undefined)).toEqual({
      savedCount: 0, replaced: 0, rejected: [], details: [],
    });
    // 非数组的数组字段 → 按 0 处理
    // @ts-expect-error 故意传入非法类型验证防御性
    const fb = summarizeUploadResult({ saved_count: 0, oversize: "not-array", warnings: null });
    expect(fb.rejected).toEqual([]);
    expect(fb.details).toEqual([]);
  });

  it("quota_exceeded 附带 quota_files 数量（2026-09-21 起后端不再混入 oversize）", () => {
    const fb = summarizeUploadResult({
      saved_count: 1,
      quota_exceeded: true,
      quota_files: ["c.txt", "d.txt"],
    });
    expect(fb.rejected.join("|")).toContain("累计体积超过 200MB 上限（2 个文件未保存）");
  });
});

// -------------- buildUploadNotice --------------
describe("buildUploadNotice", () => {
  it("全部保存成功：tone=success，文案含成功数，无拒绝项", () => {
    const n = buildUploadNotice({ saved_count: 3 });
    expect(n.tone).toBe("success");
    expect(n.text).toBe("已保存 3 个文件（待解析）");
    expect(n.details).toEqual([]);
  });

  it("部分保存：成功数 + 替换数 + 拒绝摘要（含配额超限）按序拼接", () => {
    const n = buildUploadNotice({
      saved_count: 2,
      replaced: 1,
      unsupported: ["a.rar"],
      quota_exceeded: true,
      quota_files: ["b.txt"],
    });
    expect(n.tone).toBe("success");
    expect(n.text).toBe(
      "已保存 2 个文件（待解析），替换 1 个同名旧文件，" +
        "1 个格式不支持，累计体积超过 200MB 上限（1 个文件未保存）"
    );
  });

  it("一个都没保存：tone=error，文案为「没有文件被保存：<原因>」", () => {
    const n = buildUploadNotice({
      saved_count: 0,
      unsupported: ["a.exe"],
      oversize: ["b.zip"],
      signature_invalid: ["c.pdf"],
    });
    expect(n.tone).toBe("error");
    expect(n.text).toContain("没有文件被保存：");
    expect(n.text).toContain("1 个格式不支持");
    expect(n.text).toContain("1 个超过 30MB");
    expect(n.text).toContain("1 个文件头校验不通过");
  });

  it("一个都没保存且后端未说明原因：兜底「没有文件被保存，请重试」", () => {
    const n = buildUploadNotice({ saved_count: 0 });
    expect(n.tone).toBe("error");
    expect(n.text).toBe("没有文件被保存，请重试");
  });

  it("details 透传后端逐文件 warnings（供错误 toast 附带明细）", () => {
    const n = buildUploadNotice({
      saved_count: 0,
      warnings: ["以下文件为空（0 字节）已忽略：x.txt", ""],
    });
    expect(n.details).toEqual(["以下文件为空（0 字节）已忽略：x.txt"]);
  });

  it("null / undefined / 空对象：不抛异常，按 0 保存处理", () => {
    for (const input of [null, undefined, {}]) {
      const n = buildUploadNotice(input as any);
      expect(n.tone).toBe("error");
      expect(n.text).toBe("没有文件被保存，请重试");
      expect(n.details).toEqual([]);
    }
  });
});

describe("导出派生逻辑", () => {
  it("只保留有代码且正文已放置的图表，兼容旧数据缺少 placed 字段", () => {
    const items = [
      { chart_type: "flowchart", code: "graph TD;A-->B", placed: true },
      { chart_type: "gantt", code: '{"type":"gantt"}', placed: false },
      { chart_type: "layout", code: "", placed: true },
      { chart_type: "timeline", code: '{"type":"timeline"}' },
    ];
    expect(selectPlacedExportCharts(items)).toEqual([
      items[0], items[3],
    ]);
    expect(selectPlacedExportCharts(null)).toEqual([]);
  });

  it("选择项目默认预设；无默认时返回 null", () => {
    const presets = [
      { id: "a", is_default: false },
      { id: "b", is_default: true },
    ];
    expect(findDefaultExportPreset(presets)?.id).toBe("b");
    expect(findDefaultExportPreset([{ id: "a" }])).toBeNull();
    expect(findDefaultExportPreset(null)).toBeNull();
  });
});

// -------------- 解析状态判定（单一口径，2026-09-25） --------------
describe("isDocParsed / isDocFailed", () => {
  it("parse_status=success 视为已解析（后端权威口径）", () => {
    expect(isDocParsed({ id: "a", parse_status: "success", text_len: 0 })).toBe(true);
    expect(isDocParsed({ id: "a", parse_status: "success" })).toBe(true);
  });

  it("旧数据兜底：无 parse_status 但有正文视为已解析", () => {
    expect(isDocParsed({ id: "a", text_len: 123 })).toBe(true);
    expect(isDocParsed({ id: "a", text_len: 1 })).toBe(true);
  });

  it("pending 即使有正文也不算已解析（状态列为准，后端会自修复）", () => {
    expect(isDocParsed({ id: "a", parse_status: "pending", text_len: 123 })).toBe(false);
    expect(isDocParsed({ id: "a", parse_status: "pending", text_len: 0 })).toBe(false);
  });

  it("failed 不算已解析", () => {
    expect(isDocParsed({ id: "a", parse_status: "failed", text_len: 500 })).toBe(false);
  });

  it("旧数据无正文不算已解析", () => {
    expect(isDocParsed({ id: "a", text_len: 0 })).toBe(false);
    expect(isDocParsed({ id: "a" })).toBe(false);
  });

  it("text_len 脏值（NaN / 字符串 / 负数）不得误判为已解析", () => {
    expect(isDocParsed({ id: "a", text_len: NaN })).toBe(false);
    expect(isDocParsed({ id: "a", text_len: "123" as unknown as number })).toBe(false);
    expect(isDocParsed({ id: "a", text_len: -5 })).toBe(false);
  });

  it("空入参一律返回 false（防御性，组件可直接传可能为空的项）", () => {
    expect(isDocParsed(null)).toBe(false);
    expect(isDocParsed(undefined)).toBe(false);
    expect(isDocFailed(null)).toBe(false);
    expect(isDocFailed(undefined)).toBe(false);
  });

  it("isDocFailed 只认 failed", () => {
    expect(isDocFailed({ id: "a", parse_status: "failed" })).toBe(true);
    expect(isDocFailed({ id: "a", parse_status: "pending" })).toBe(false);
    expect(isDocFailed({ id: "a", parse_status: "success" })).toBe(false);
    expect(isDocFailed({ id: "a" })).toBe(false);
  });
});

describe("解析状态：统计条与列表标签同口径（回归护栏）", () => {
  /**
   * 背景：DocumentParseList 的行标签与 computeDocStats 的统计条此前各写一份
   * isParsed 判定，一处改动即产生「统计条说 0 待解析、列表仍显示待解析」的
   * 分裂，且两侧单测都发现不了。这里用同一批脏数据交叉断言，把口径钉死。
   */
  const cases = [
    { id: "s", parse_status: "success", text_len: 100 },
    { id: "p", parse_status: "pending", text_len: 0 },
    { id: "f", parse_status: "failed", text_len: 0 },
    { id: "legacy", text_len: 42 },
    { id: "legacy-empty", text_len: 0 },
    { id: "stale", parse_status: "pending", text_len: 1234 },
    { id: "nan", parse_status: "success", text_len: NaN },
  ] as DocRecord[];

  it("每一条文档的 isDocParsed 结论必须与统计条计数一致", () => {
    const parsedByFn = cases.filter((d) => isDocParsed(d)).length;
    const failedByFn = cases.filter((d) => isDocFailed(d)).length;
    const stats = computeDocStats(cases);
    expect(stats.parsedCount).toBe(parsedByFn);
    expect(stats.failedCount).toBe(failedByFn);
    expect(stats.pendingCount).toBe(cases.length - parsedByFn - failedByFn);
  });

  it("同一文档不得同时被判定为已解析与失败", () => {
    for (const d of cases) {
      expect(isDocParsed(d) && isDocFailed(d)).toBe(false);
    }
  });

  it("actionableCount 等于待解析 + 失败（与后端 parse-all 选取口径一致）", () => {
    const stats = computeDocStats(cases);
    expect(stats.actionableCount).toBe(stats.pendingCount + stats.failedCount);
  });
});

// ============================================================
// canShrinkSection —— 阈值必须与后端压缩端点准入一致（> 预算 130%）
//   后端：routers/sections.py:1558 `before_wc <= word_budget * WORD_OVER_RATIO` → 400
//   旧前端：word_count > word_budget → 100%~130% 区间按钮可点、后端必 400
// ============================================================
describe("canShrinkSection", () => {
  it("阈值常量与后端同口径（1.3）", () => {
    expect(WORD_OVER_RATIO).toBe(1.3);
  });

  it("130% 以内不可压缩（旧实现会误判为可点，后端必 400）", () => {
    expect(canShrinkSection({ content: "x", word_count: 1300, word_budget: 1000 })).toBe(false);
    expect(canShrinkSection({ content: "x", word_count: 1000, word_budget: 1000 })).toBe(false);
    expect(canShrinkSection({ content: "x", word_count: 1001, word_budget: 1000 })).toBe(false);
  });

  it("超过 130% 才可压缩", () => {
    expect(canShrinkSection({ content: "x", word_count: 1301, word_budget: 1000 })).toBe(true);
    expect(canShrinkSection({ content: "x", word_count: 3000, word_budget: 1000 })).toBe(true);
  });

  it("无正文 / 空对象一律不可压缩（脏数据防御）", () => {
    for (const bad of [null, undefined, {}, { content: "" }, { content: "   ", word_count: 99999 }]) {
      expect(canShrinkSection(bad as any)).toBe(false);
    }
  });

  it("word_budget 缺失时按默认 1500 计（与后端 row['word_budget'] or 1500 对齐）", () => {
    expect(canShrinkSection({ content: "x", word_count: 1951 })).toBe(true);
    expect(canShrinkSection({ content: "x", word_count: 1950 })).toBe(false);
  });
});


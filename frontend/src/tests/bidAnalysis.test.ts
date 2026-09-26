/**
 * 「提取项目」纯函数层测试（utils/bidAnalysis）。
 *
 * 锁定的口径（与后端 bid_analysis_service.is_missing_result / list_analysis_results 对齐）：
 *   1. 缺失判定：markdown 的「未提取到」+ json 的「全字段没有提及」都算缺失
 *      —— 旧实现只认 `content === "未提取到"`，导致前端行内绿标 ✓ 而后端汇总判缺失；
 *   2. 汇总重算：必选项缺失 / 计数与后端 total=定义项数 一致；
 *   3. 分组归一化：优先后端 groups，缺失时按 defs.group 推导（不再前端硬编码分组表）。
 */
import { describe, expect, it } from "vitest";
import {
  findFirstDoneBaItem,
  isMissingBaResult,
  mergeBaItem,
  normalizeBaGroups,
  recomputeBaSummary,
  type BaItemDef,
} from "../utils/bidAnalysis";
import { baScopeText } from "../components/BidAnalysisTab";

const defs: BaItemDef[] = [
  { item_id: "projectBasicInfo", label: "项目级基本信息", required: 1, output_type: "json", group: "project_info", sort_order: 1 },
  { item_id: "schemeBasicInfo", label: "方案级基本信息", required: 1, output_type: "markdown", group: "scheme_info", sort_order: 2 },
  { item_id: "resourceAllocation", label: "资源配置", required: 0, output_type: "markdown", group: "resource", sort_order: 8 },
];

describe("isMissingBaResult", () => {
  it("空 / 全空白 → 缺失", () => {
    expect(isMissingBaResult("", "markdown")).toBe(true);
    expect(isMissingBaResult("   \n ", "markdown")).toBe(true);
    expect(isMissingBaResult(undefined, "markdown")).toBe(true);
  });

  it("markdown 项：恰为「未提取到」→ 缺失，有内容 → 不缺失", () => {
    expect(isMissingBaResult("未提取到", "markdown")).toBe(true);
    expect(isMissingBaResult("  未提取到  ", "markdown")).toBe(true);
    expect(isMissingBaResult("## 方案级信息\n深基坑支护方案", "markdown")).toBe(false);
  });

  it("json 项：全字段「没有提及」/空 → 缺失（旧实现漏判）", () => {
    expect(isMissingBaResult('{"project_name":"没有提及","contractor":"没有提及"}', "json")).toBe(true);
    expect(isMissingBaResult('{"a":null,"b":"无"}', "json")).toBe(true);
    expect(isMissingBaResult("```json\n{\"a\":\"没有提及\"}\n```", "json")).toBe(true);
  });

  it("json 项：任一字段有有效值 → 不缺失（含 0 这类有意义的假值）", () => {
    expect(isMissingBaResult('{"a":"没有提及","b":"某某项目"}', "json")).toBe(false);
    expect(isMissingBaResult('{"a":0}', "json")).toBe(false);
  });

  it("json 项：解析失败 / 非对象 → 不判缺失（不误伤坏格式但有内容的场景）", () => {
    expect(isMissingBaResult('{"a": "没有提及"', "json")).toBe(false);
    expect(isMissingBaResult("[1,2]", "json")).toBe(false);
  });
});

describe("recomputeBaSummary", () => {
  it("必选项缺失（未跑 / 失败 / 未提取到 / json 全空）都计入 missing_required", () => {
    const s = recomputeBaSummary(
      [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"没有提及"}' },
        { item_id: "schemeBasicInfo", status: "success", content: "未提取到" },
      ],
      defs,
    );
    expect(s.total).toBe(3);
    expect(s.success).toBe(2);
    expect(s.missing_required).toEqual(["项目级基本信息", "方案级基本信息"]);
    expect(s.all_required_done).toBe(false);
  });

  it("计数与待执行数：total 取定义项数，pending = total - success - error - running", () => {
    const s = recomputeBaSummary(
      [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"b"}' },
        { item_id: "schemeBasicInfo", status: "error", content: "" },
        { item_id: "resourceAllocation", status: "running", content: "" },
      ],
      defs,
    );
    expect(s.total).toBe(3);
    expect(s.success).toBe(1);
    expect(s.errors).toBe(1);
    expect(s.running).toBe(1);
    expect(s.pending).toBe(0);
    // 可选型缺失不计入 missing_required
    expect(s.missing_required).toEqual(["方案级基本信息"]);
  });

  it("必选项齐备 → all_required_done=true", () => {
    const s = recomputeBaSummary(
      [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"b"}' },
        { item_id: "schemeBasicInfo", status: "success", content: "正文" },
      ],
      defs,
    );
    expect(s.all_required_done).toBe(true);
    expect(s.missing_required).toEqual([]);
  });

  it("success_valid：完成项里有有效内容的数量（排除空标记 / json 全空）", () => {
    const s = recomputeBaSummary(
      [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"某某项目"}' },
        { item_id: "schemeBasicInfo", status: "success", content: "未提取到" },
        { item_id: "resourceAllocation", status: "error", content: "" },
      ],
      defs,
    );
    expect(s.success).toBe(2);
    expect(s.success_valid).toBe(1);
  });

  it("success_valid 与 json 项同口径：全「没有提及」不计入有效成果", () => {
    const s = recomputeBaSummary(
      [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"没有提及","b":null}' },
      ],
      defs,
    );
    expect(s.success).toBe(1);
    expect(s.success_valid).toBe(0);
  });

  it("manual_count：统计 source='manual' 的项数（AI 重跑后回退为 0）", () => {
    const manual = recomputeBaSummary(
      [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"b"}', source: "manual" },
        { item_id: "schemeBasicInfo", status: "success", content: "正文", source: "manual" },
        { item_id: "resourceAllocation", status: "success", content: "正文", source: "ai" },
      ],
      defs,
    );
    expect(manual.manual_count).toBe(2);

    // 重跑后 source 回到 ai
    const rerun = recomputeBaSummary(
      [
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"b"}', source: "ai" },
      ],
      defs,
    );
    expect(rerun.manual_count).toBe(0);
  });
});

describe("baScopeText（口径文案动态生成）", () => {
  it("N 项 = M 必选 + K 可选；0 项回退为纯标签", () => {
    expect(baScopeText(defs)).toBe("3 项结构化提取（2 必选 + 1 可选）");
    expect(baScopeText([])).toBe("结构化提取");
  });

  it("全部必选 / 全部可选时不算错", () => {
    const allRequired: BaItemDef[] = [
      { item_id: "a", label: "A", required: 1, output_type: "markdown", group: "g" },
      { item_id: "b", label: "B", required: 1, output_type: "markdown", group: "g" },
    ];
    expect(baScopeText(allRequired)).toBe("2 项结构化提取（2 必选 + 0 可选）");

    const allOptional: BaItemDef[] = [
      { item_id: "a", label: "A", required: 0, output_type: "markdown", group: "g" },
    ];
    expect(baScopeText(allOptional)).toBe("1 项结构化提取（0 必选 + 1 可选）");
  });

  it("对齐后端 /items 的实际口径（18 项 / 17 必选 / 1 可选）", () => {
    const real = Array.from({ length: 18 }, (_, i) => ({
      item_id: `i${i}`,
      label: `L${i}`,
      required: i === 17 ? 0 : 1,
      output_type: i === 0 ? "json" : "markdown",
      group: "g",
    }));
    expect(baScopeText(real)).toBe("18 项结构化提取（17 必选 + 1 可选）");
  });
});

describe("normalizeBaGroups", () => {
  it("优先使用后端 groups（顺序 / 中文名以后端为准）", () => {
    const groups = normalizeBaGroups(
      [
        { group: "project_info", label: "项目级基本信息", items: [defs[0]] },
        { group: "scheme_info", label: "方案级基本信息", items: [defs[1]] },
      ],
      defs,
    );
    expect(groups.map(g => g.key)).toEqual(["project_info", "scheme_info"]);
    expect(groups[0].label).toBe("项目级基本信息");
    expect(groups[0].items.map(i => i.item_id)).toEqual(["projectBasicInfo"]);
  });

  it("后端 groups 的 items 缺失时按 defs.group 回退过滤", () => {
    const groups = normalizeBaGroups([{ group: "resource", label: "资源配置" }], defs);
    expect(groups[0].items.map(i => i.item_id)).toEqual(["resourceAllocation"]);
  });

  it("无 groups → 按 defs.group 首次出现顺序推导（去重）", () => {
    const groups = normalizeBaGroups([], defs);
    expect(groups.map(g => g.key)).toEqual(["project_info", "scheme_info", "resource"]);
    expect(groups[0].items[0].label).toBe("项目级基本信息");
  });

  it("空输入 → 空分组（不产幽灵分组）", () => {
    expect(normalizeBaGroups([], [])).toEqual([]);
  });
});

describe("mergeBaItem / findFirstDoneBaItem", () => {
  it("合并定义与结果：结果字段优先，content 归一为空串", () => {
    const merged = mergeBaItem(defs[1], { item_id: "schemeBasicInfo", status: "success" });
    expect(merged.label).toBe("方案级基本信息");
    expect(merged.status).toBe("success");
    expect(merged.content).toBe("");
  });

  it("默认阅读项：按定义顺序取第一个「已完成且有内容」的项", () => {
    const first = findFirstDoneBaItem(
      [
        { item_id: "schemeBasicInfo", status: "success", content: "正文" },
        { item_id: "projectBasicInfo", status: "success", content: '{"a":"b"}' },
      ],
      defs,
    );
    expect(first.item_id).toBe("projectBasicInfo");
    expect(first.content).toBe('{"a":"b"}');
  });

  it("没有已完成项 → null（不强行选中空行）", () => {
    expect(
      findFirstDoneBaItem([{ item_id: "schemeBasicInfo", status: "running", content: "" }], defs),
    ).toBeNull();
  });
});

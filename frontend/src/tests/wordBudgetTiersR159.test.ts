/**
 * R159（2026-11-01）四档字数档位 · 前端唯一镜像行为护栏
 *
 * 真源 backend content_utils.WORD_BUDGET_TIERS；前端唯一镜像 utils/wordBudgetTiers.ts。
 * 数值 parity 源码锚在后端 tests/test_r159_word_tiers_ssot_20261101.py（前端 tsc
 * 环境无 node:fs，与 outlineRulesR151.test.ts 同分工）。本文件锁镜像自身行为：
 * 数值/顺序/中文名/派生默认值/下拉选项（含内嵌数值的 label）/输入区间常数。
 */
import { describe, expect, it } from "vitest";
import {
  CUSTOM_TOTAL_BUDGET_DEFAULT,
  CUSTOM_TOTAL_BUDGET_MAX,
  CUSTOM_TOTAL_BUDGET_MIN,
  DEFAULT_WORD_BUDGET,
  WORD_BUDGET_DEFAULT_TIER,
  WORD_BUDGET_MAX,
  WORD_BUDGET_MIN,
  WORD_BUDGET_OPTIONS,
  WORD_BUDGET_TIER_KEYS,
  WORD_BUDGET_TIERS,
  WORD_BUDGET_TIER_LABELS,
} from "../utils/wordBudgetTiers";

describe("wordBudgetTiers · 四档档位唯一镜像（R159）", () => {
  it("档位数值与顺序（与后端真源 dict 序一致）", () => {
    expect(WORD_BUDGET_TIERS).toEqual({ brief: 800, standard: 1200, detailed: 2000, ultra: 2500 });
    expect(WORD_BUDGET_TIER_KEYS).toEqual(["brief", "standard", "detailed", "ultra"]);
  });

  it("中文名与后端 WORD_BUDGET_TIER_LABELS_ZH 同名同序", () => {
    expect(WORD_BUDGET_TIER_LABELS).toEqual({ brief: "简短", standard: "标准", detailed: "详细", ultra: "超长" });
  });

  it("默认档=标准，DEFAULT_WORD_BUDGET 从档位表派生（1200）", () => {
    expect(WORD_BUDGET_DEFAULT_TIER).toBe("standard");
    expect(DEFAULT_WORD_BUDGET).toBe(WORD_BUDGET_TIERS[WORD_BUDGET_DEFAULT_TIER]);
    expect(DEFAULT_WORD_BUDGET).toBe(1200);
  });

  it("四档下拉选项：label 由数值拼装（R66 六项契约逐字保持）", () => {
    expect(WORD_BUDGET_OPTIONS).toEqual([
      { label: "使用目录默认值", value: "default" },
      { label: "简短 800 字", value: "brief" },
      { label: "标准 1200 字", value: "standard" },
      { label: "详细 2000 字", value: "detailed" },
      { label: "超长 2500 字", value: "ultra" },
      { label: "自定义", value: "custom" },
    ]);
  });

  it("输入区间常数（单节编辑 50~50000；自定义档=二级单元总量 100~20000，缺省 2000）", () => {
    expect(WORD_BUDGET_MIN).toBe(50);
    expect(WORD_BUDGET_MAX).toBe(50000);
    expect(CUSTOM_TOTAL_BUDGET_MIN).toBe(100);
    expect(CUSTOM_TOTAL_BUDGET_MAX).toBe(20000);
    expect(CUSTOM_TOTAL_BUDGET_DEFAULT).toBe(2000);
  });
});

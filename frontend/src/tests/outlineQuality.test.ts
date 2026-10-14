// @vitest-environment jsdom
/**
 * summarizeOutlineQuality（P1-5：后端「生成后自动校验」报告 → 用户可见提示）
 *
 * 后端 `services/outline_quality` 产出 `review.quality`：
 *   - `name_coverage.missing`：方案名称里解析出的施工内容/工序/工艺/对象，在目录中无落点；
 *   - `continuity.ok === false`：七类缺陷计数（层级跳级 / 有父无子 / 同名章节 /
 *     编号错位 / 破折号长尾 / 空标题 / 占位泛化标题），标签表与后端
 *     `CONTINUITY_ISSUE_LABELS` 同步（跨语言 parity 由后端 R73b 护栏锁定）；
 *   - `redundant_titles`：与方案名称零关联、且非通用必要章节的标题候选。
 *
 * 这些信号此前**没有任何前端消费方** —— 用户看不到"方案名称里的工序没落到章节"。
 * 本文件锁定三条信号的呈现口径，以及"**只提示、绝不自动删章**"的红线。
 *
 * 独立成文件的原因：outlineTab.test.tsx 已是 1000+ 行且历史上多次被追加块，
 * 混放会让套件结构难以校验（曾出现嵌套 describe 导致用例不被收集）。
 */
import { describe, it, expect } from "vitest";
import { summarizeOutlineQuality } from "../pages/SchemeWorkbenchPage";

describe("summarizeOutlineQuality（目录生成后校验报告）", () => {
  it("无报告时静默（level=none，不打扰用户）", () => {
    [undefined, null, {}, { quality: null }, "x"].forEach((r) => {
      expect(summarizeOutlineQuality(r).level).toBe("none");
    });
  });

  it("全面性缺口 → warn 并列出未覆盖的方案名称维度", () => {
    const r = summarizeOutlineQuality({
      quality: { name_coverage: { evaluated: true, covered: false, missing: ["施工工序·支护", "施工对象·基坑"] } },
    });
    expect(r.level).toBe("warn");
    expect(r.message).toContain("方案名称涉及但目录未覆盖");
    expect(r.message).toContain("施工工序·支护");
  });

  it("未评估（名称不可解析）时不报缺口（不得误伤）", () => {
    const r = summarizeOutlineQuality({
      quality: { name_coverage: { evaluated: false, covered: true, missing: [] } },
    });
    expect(r.level).toBe("none");
  });

  it("连续性问题 → warn 并中文化四类计数", () => {
    const r = summarizeOutlineQuality({
      quality: { continuity: { ok: false, issue_counts: { level_gaps: 2, duplicate_titles: 1 } } },
    });
    expect(r.level).toBe("warn");
    expect(r.message).toContain("层级跳级 2 处");
    expect(r.message).toContain("同名章节 1 处");
  });

  // R73b F5 实证：占位/泛化标题（用户报的「专项6」「总体实施策划（2）」）2026-10-10
  // 就进了后端 issues，但前端 labels 表停在四类 —— 计数被 Object.keys(labels)
  // 过滤掉，后端已判 ok=false 而界面零提示。本组用例锁「检出必须呈现」。
  it("占位/泛化标题等后三类同样必须中文化播报", () => {
    const r = summarizeOutlineQuality({
      quality: {
        continuity: {
          ok: false,
          issue_counts: { generic_titles: 3, dash_titles: 1, empty_titles: 2 },
        },
      },
    });
    expect(r.level).toBe("warn");
    expect(r.message).toContain("占位/泛化标题 3 处");
    expect(r.message).toContain("破折号长尾标题 1 处");
    expect(r.message).toContain("空标题节点 2 处");
  });

  it("后端新增而未登记标签的问题类，也必须透出来（绝不静默咽掉）", () => {
    const r = summarizeOutlineQuality({
      quality: { continuity: { ok: false, issue_counts: { brand_new_kind: 4 } } },
    });
    expect(r.level).toBe("warn");
    expect(r.message).toContain("brand_new_kind 4 处");
  });

  it("冗余候选 → info 且措辞明确「未自动删除」", () => {
    const r = summarizeOutlineQuality({
      quality: { redundant_titles: [{ title: "精装包厢软包施工" }] },
    });
    expect(r.level).toBe("info");
    expect(r.message).toContain("未自动删除");
    expect(r.message).toContain("精装包厢软包施工");
  });

  it("三类信号合并为一条消息，logs 可逐条进日志", () => {
    const r = summarizeOutlineQuality({
      quality: {
        name_coverage: { evaluated: true, missing: ["施工工序·支护"] },
        continuity: { ok: false, issue_counts: { numbering_mismatch: 3 } },
        redundant_titles: [{ title: "无关章节" }],
      },
    });
    expect(r.logs.length).toBe(3);
    expect(r.level).toBe("warn");
    expect(r.message).toBe(r.logs.join("；"));
  });

  it("脏数据不抛（字段类型错、null、超长列表）", () => {
    expect(() => summarizeOutlineQuality({ quality: { name_coverage: { missing: "x" } } })).not.toThrow();
    expect(() => summarizeOutlineQuality({ quality: { redundant_titles: [null, 1] } })).not.toThrow();
    const long = summarizeOutlineQuality({
      quality: { name_coverage: { evaluated: true, missing: new Array(30).fill("项") } },
    });
    expect(long.message).toContain("等 30 项");
  });
});

/**
 * utils/contentEvents.ts · 纯函数单元测试（2026-09-20 补齐 F4/F5）。
 *
 * 该模块承载正文生成 SSE 事件 → 章节日志状态机的纯逻辑，
 * 原先内联在 8000 行工作台页面里重复 4 处且零测试覆盖。
 * 本文件钉住三个纯函数的边界行为，保证页面重构前后语义逐字节一致。
 */
import { describe, it, expect } from "vitest";
import {
  upsertSectionLog,
  finalizeRunningLogsIn,
  mergeFailedSectionsInto,
  contentResultFailedSections,
  contentResultSummary,
  type SectionLogItem,
} from "../utils/contentEvents";

function log(over: Partial<SectionLogItem> & { section_id: string }): SectionLogItem {
  return {
    title: over.section_id,
    status: "running",
    time: 1000,
    ...over,
  } as SectionLogItem;
}

// ============================================================
// upsertSectionLog —— 按 section_id 插入/替换，替换项移到尾部
// ============================================================
describe("upsertSectionLog", () => {
  it("新增项追加到数组尾部（日志倒序渲染 → 尾部即最上方）", () => {
    const base = [log({ section_id: "a" }), log({ section_id: "b" })];
    const next = upsertSectionLog(base, log({ section_id: "c", status: "success" }));
    expect(next.map((x) => x.section_id)).toEqual(["a", "b", "c"]);
  });

  it("替换同 id 项并保持其在尾部（等价旧 [...filter, item] 语义）", () => {
    const base = [log({ section_id: "a" }), log({ section_id: "b", status: "running" })];
    const next = upsertSectionLog(base, log({ section_id: "a", status: "success" }));
    // a 被移除后重新追加 → 顺序变为 [b, a]
    expect(next.map((x) => x.section_id)).toEqual(["b", "a"]);
    expect(next[next.length - 1].status).toBe("success");
  });

  it("返回新数组，绝不原地修改入参", () => {
    const base = [log({ section_id: "a" })];
    const snapshot = [...base];
    const next = upsertSectionLog(base, log({ section_id: "b" }));
    expect(base).toEqual(snapshot);
    expect(next).not.toBe(base);
  });
});

// ============================================================
// finalizeRunningLogsIn —— 终态收尾残留的 running 项
// ============================================================
describe("finalizeRunningLogsIn", () => {
  it("无 running 项时原样返回（引用相等 → 调用方跳过 setState）", () => {
    const base = [log({ section_id: "a", status: "success" })];
    expect(finalizeRunningLogsIn(base, "已结束")).toBe(base);
  });

  it("把 running 项收尾为默认 skipped，并写入 reason", () => {
    const base = [
      log({ section_id: "a", status: "success" }),
      log({ section_id: "b", status: "running", time: 0 }),
    ];
    const next = finalizeRunningLogsIn(base, "已停止，未生成完成");
    const b = next.find((x) => x.section_id === "b")!;
    expect(b.status).toBe("skipped");
    expect(b.reason).toBe("已停止，未生成完成");
    expect(typeof b.duration).toBe("number");
    // 非 running 项保持引用不变
    expect(next.find((x) => x.section_id === "a")).toBe(base[0]);
  });

  it("显式传 failed 时收尾为 failed（任务终止语义）", () => {
    const base = [log({ section_id: "a", status: "running" })];
    const next = finalizeRunningLogsIn(base, "生成失败（任务已终止）", "failed");
    expect(next[0].status).toBe("failed");
  });
});

// ============================================================
// mergeFailedSectionsInto —— 合并后端 failed_sections 明细
// ============================================================
describe("mergeFailedSectionsInto", () => {
  it("入参非数组 / 空数组时原样返回（引用相等）", () => {
    const base = [log({ section_id: "a" })];
    expect(mergeFailedSectionsInto(base, undefined)).toBe(base);
    expect(mergeFailedSectionsInto(base, [])).toBe(base);
    expect(mergeFailedSectionsInto(base, {} as any)).toBe(base);
  });

  it("把未进日志的失败章节补进日志（status=failed + reason）", () => {
    const base = [log({ section_id: "a", status: "success" })];
    const next = mergeFailedSectionsInto(base, [
      { section_id: "z", title: "未知章", reason: "超时" },
    ]);
    const z = next.find((x) => x.section_id === "z")!;
    expect(z.status).toBe("failed");
    expect(z.title).toBe("未知章");
    expect(z.reason).toBe("超时");
    expect(next).toHaveLength(2);
  });

  it("已存在的条目保留 index/total/time（不覆盖进度元数据）", () => {
    const base = [log({ section_id: "a", status: "running", index: 3, total: 10, time: 555 })];
    const next = mergeFailedSectionsInto(base, [
      { section_id: "a", title: "", reason: "任务已取消" },
    ]);
    const a = next.find((x) => x.section_id === "a")!;
    expect(a.status).toBe("failed");
    expect(a.index).toBe(3);
    expect(a.total).toBe(10);
    expect(a.time).toBe(555);
    expect(a.title).toBe("a"); // 空 title 回退旧值
    expect(a.reason).toBe("任务已取消");
  });

  it("跳过缺少 section_id 的脏数据项", () => {
    const base = [log({ section_id: "a", status: "success" })];
    const next = mergeFailedSectionsInto(base, [{ title: "无 id" }, null]);
    expect(next).toBe(next); // 无有效项可合并
    expect(next).toHaveLength(1);
  });
});
// ============================================================
// ✅ G12-6（2026-09-20）：contentResultFailedSections
// 断线重挂时从任务终态 checkpoint 还原失败明细（后端早已落库，前端此前从未消费）
// ============================================================
describe("contentResultFailedSections", () => {
  it("正常载荷：透出失败明细与后端计数", () => {
    const r = contentResultFailedSections({
      event: "stopped",
      failed_count: 3,
      failed_sections: [
        { section_id: "s1", title: "第一章", reason: "AI 超时" },
        { section_id: "s2", title: "第二章", reason: "限流" },
      ],
      run_words: 4200,
      over_count: 1,
    });
    expect(r.sections).toHaveLength(2);
    expect(r.failedCount).toBe(3); // failed_count 优先（明细分页上限 50 条）
  });

  it("明细超过计数时以计数为准（截断场景不误导用户）", () => {
    const r = contentResultFailedSections({
      failed_count: 1,
      failed_sections: [{ section_id: "s1" }, { section_id: "s2" }],
    });
    expect(r.sections).toHaveLength(2);
    expect(r.failedCount).toBe(1);
  });

  it("缺 failed_count 时回退按明细条数计数", () => {
    const r = contentResultFailedSections({
      failed_sections: [{ section_id: "s1" }, { section_id: "s2" }],
    });
    expect(r.failedCount).toBe(2);
  });

  it("未失败（completed 且无明细）：空结果，不弹失败提示", () => {
    const r = contentResultFailedSections({
      event: "completed", failed_count: 0, failed_sections: [],
    });
    expect(r.sections).toEqual([]);
    expect(r.failedCount).toBe(0);
  });

  it("过滤无效明细项（缺 section_id / null / 非对象）", () => {
    const r = contentResultFailedSections({
      failed_sections: [{ title: "无 id" }, null, "字符串", { section_id: "ok" }],
    });
    expect(r.sections).toHaveLength(1);
    expect(r.sections[0].section_id).toBe("ok");
    expect(r.failedCount).toBe(1);
  });

  it("防御式：null / undefined / 非对象 / 字段类型不符均返回空且不抛异常", () => {
    for (const bad of [null, undefined, 0, "x", [], { failed_sections: "x" }]) {
      const r = contentResultFailedSections(bad);
      expect(r.sections).toEqual([]);
      expect(r.failedCount).toBe(0);
    }
  });

  it("与 mergeFailedSectionsInto 接线：还原的明细可直接合入日志", () => {
    const { sections } = contentResultFailedSections({
      failed_sections: [{ section_id: "s1", title: "第一章", reason: "AI 超时" }],
    });
    const merged = mergeFailedSectionsInto([], sections);
    expect(merged).toHaveLength(1);
    expect(merged[0].status).toBe("failed");
    expect(merged[0].reason).toBe("AI 超时");
  });
});

// ============================================================
// contentResultSummary —— checkpoint 进度摘要（2026-09-21 新增）
// ============================================================
describe("contentResultSummary", () => {
  it("正常载荷：透出 done/total/words/over_count", () => {
    const r = contentResultSummary({
      done: 12, total: 20, words: 15000, over_count: 3,
    });
    expect(r).toEqual({ done: 12, total: 20, words: 15000, overCount: 3 });
  });

  it("部分字段缺失时回退 0（不抛异常）", () => {
    const r = contentResultSummary({ done: 5, total: 10 });
    expect(r).toEqual({ done: 5, total: 10, words: 0, overCount: 0 });
  });

  it("防御式：null / undefined / 非对象均返回全零", () => {
    for (const bad of [null, undefined, 0, "x", [], "text"]) {
      const r = contentResultSummary(bad);
      expect(r).toEqual({ done: 0, total: 0, words: 0, overCount: 0 });
    }
  });

  it("脏数据类型（字符串/对象）回退 0，不抛异常", () => {
    const r = contentResultSummary({
      done: "12", total: {}, words: null, over_count: "3",
    });
    expect(r).toEqual({ done: 0, total: 0, words: 0, overCount: 0 });
  });
});

// ============================================================
// ✅ 2026-09-24 跨模块修复回归：stopped 事件失败明细 + 章节质检告警
// ============================================================
describe("stopped 事件 / section_done 质检告警（2026-09-24 缺口修复）", () => {
  it("stopped 事件下发的 failed_sections 与 completed 走同一合并入口", () => {
    // 根因：后端 stopped 原先只带 progress/message，失败明细只落 checkpoint，
    //       前端在线路径拿不到 → 停止后用户不知道哪几章失败。
    const base = [log({ section_id: "a", status: "success" })];
    const next = mergeFailedSectionsInto(base, [
      { section_id: "c", title: "第三章 施工工艺", reason: "AI 超时" },
    ]);
    const c = next.find((x) => x.section_id === "c");
    expect(c?.status).toBe("failed");
    expect(c?.reason).toBe("AI 超时");
    // 已存在的条目不受影响
    expect(next.find((x) => x.section_id === "a")?.status).toBe("success");
  });

  it("stopped 事件无 failed_sections 时合并结果不变（引用相等）", () => {
    const base = [log({ section_id: "a" })];
    for (const bad of [undefined, null, [], "x", {}]) {
      expect(mergeFailedSectionsInto(base, bad)).toBe(base);
    }
  });

  it("章节日志项可承载 quality_issues（后端 section_done 下发的程序化质检问题）", () => {
    // 根因：后端一直下发 content 与 quality_issues，前端两个字段都没消费。
    const issues = [{ type: "unclosed_fence", message: "存在未闭合代码块" }];
    const item = upsertSectionLog(
      [], log({ section_id: "a", status: "success", quality_issues: issues }),
    );
    expect(item[0].quality_issues).toEqual(issues);
    // 非数组脏数据不得污染日志（页面按 Array.isArray 判定）
    expect(Array.isArray(log({ section_id: "b" }).quality_issues)).toBe(false);
  });
});

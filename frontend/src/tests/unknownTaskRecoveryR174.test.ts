/**
 * ✅ R174 F2（2026-11-03）：SSE 重挂 10 分钟上限耗尽后的「任务态未知」可恢复状态。
 *
 * 旧缺陷（审计 R173 · D9/F2）：pollTaskUntilTerminal 200×3s 耗尽 / 连续网络失败
 * 后 return null，三条 SSE 链对 hit.final==null 无任何处理，finally 复位 generating
 * —— 进度卡消失、页面静默回空闲，但后端任务仍在跑（AI 继续计费），用户重新点
 * 生成即并发 / 重复计费。
 *
 * 修复约定（本文件锁定）：
 *   ① 轮询耗尽返回 null 的行为保持不变（不做无限轮询刷爆后端）；
 *   ② reattachTaskImpl 命中任务但 final=null → 调用方必须 markUnknownTask
 *     （页面横幅 data-testid="unknown-task-banner" + 「重新接管任务」按钮）；
 *   ③ 未知态下重新生成必须经 guardUnknownTaskGeneration 二次确认：
 *     一次性查询任务状态，running/paused → 接管不放行；终态/404 → 放行；
 *     其它 → 保持不放行。
 * 判定逻辑收敛为 taskPolling.resolveUnknownTaskNext 单一事实源（本文件单测）；
 * 页面接线（横幅/守卫）以源码锚点断言锁定（与仓内锚点测试同风格）。
 */
import { describe, expect, it } from "vitest";
import {
  pollTaskUntilTerminalImpl,
  reattachTaskImpl,
  resolveUnknownTaskNext,
} from "../pages/SchemeWorkbenchPage";

// 源码锚点：沿用仓内范式（import.meta.glob 读源码文本，不引 node:fs）
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../pages/**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, ""), v] as const),
);
const pageSource = RAW["pages/SchemeWorkbenchPage.tsx"];
// ✅ G10（2026-11-04）：未知态状态机迁至 useUnknownTaskRecovery.tsx，守卫函数体
//    与「重新接管」分支的 markUnknownTask 调用随迁；三条生成链散落在页面与
//    各生成 hook（G11/G12 后续搬迁）—— 链条锚点改为扫描 schemeWorkbench 全域，
//    call-site 计数仍逐条约束（少一处即红）。
const recoverySource = RAW["pages/schemeWorkbench/useUnknownTaskRecovery.tsx"];
const chainSources = Object.entries(RAW)
  .filter(
    ([k]) =>
      k.startsWith("pages/SchemeWorkbenchPage.tsx") || k.startsWith("pages/schemeWorkbench/"),
  )
  .map(([, v]) => v)
  .join("\n");

// ---------- ① 轮询耗尽行为：仍返回 null（不无限轮询） ----------
describe("pollTaskUntilTerminalImpl · 耗尽语义（R174 F2）", () => {
  it("maxPolls 耗尽（任务一直 running）→ 返回 null，调用次数不超过 maxPolls", async () => {
    let calls = 0;
    const fin = await pollTaskUntilTerminalImpl(
      "t-unknown",
      async () => {
        calls++;
        return { status: "running", progress: 0.5 };
      },
      undefined,
      { intervalMs: 1, maxPolls: 4 },
    );
    expect(fin).toBeNull();
    expect(calls).toBe(4);
  });

  it("连续网络失败达容错阈值 → 返回 null（不重试到天荒地老）", async () => {
    let calls = 0;
    const fin = await pollTaskUntilTerminalImpl(
      "t-netfail",
      async () => {
        calls++;
        throw new Error("network down");
      },
      undefined,
      { intervalMs: 1, maxPolls: 100, netFailTolerance: 3 },
    );
    expect(fin).toBeNull();
    expect(calls).toBe(3);
  });
});

// ---------- ② reattachTaskImpl 命中任务但拿不到终态 → { taskId, final: null } ----------
describe("reattachTaskImpl · final=null 未知态契约（R174 F2）", () => {
  it("命中任务且轮询耗尽 → 返回 { taskId, final: null }（调用方据此进入未知态）", async () => {
    const hit = await reattachTaskImpl(
      async () => ({ tasks: [] }),
      async () => null, // 模拟 pollTaskUntilTerminal 耗尽
      { taskId: "t-1" },
    );
    expect(hit).not.toBeNull();
    expect(hit!.taskId).toBe("t-1");
    expect(hit!.final).toBeNull();
  });
});

// ---------- ③ 未知态下一步判定：单一事实源 ----------
describe("resolveUnknownTaskNext · 未知态查询后三向判定（R174 F2）", () => {
  it("running / paused → reattach（任务仍在跑，接管监听，不放行新生成）", () => {
    expect(resolveUnknownTaskNext("running")).toBe("reattach");
    expect(resolveUnknownTaskNext("paused")).toBe("reattach");
  });

  it("全部终态（completed/failed/stopped/degraded）→ proceed（可安全重新生成）", () => {
    expect(resolveUnknownTaskNext("completed")).toBe("proceed");
    expect(resolveUnknownTaskNext("failed")).toBe("proceed");
    expect(resolveUnknownTaskNext("stopped")).toBe("proceed");
    expect(resolveUnknownTaskNext("degraded")).toBe("proceed");
  });

  it("空值 / 未知枚举 → keep（保持未知态，本次不放行）", () => {
    expect(resolveUnknownTaskNext(undefined)).toBe("keep");
    expect(resolveUnknownTaskNext(null)).toBe("keep");
    expect(resolveUnknownTaskNext("")).toBe("keep");
    expect(resolveUnknownTaskNext("weird_state")).toBe("keep");
  });
});

// ---------- 页面接线锚点（源码断言，锁定横幅/守卫/三链接线不被静默摘除） ----------

describe("SchemeWorkbenchPage · 未知态接线锚点（R174 F2）", () => {
  it("页面源码在 glob 覆盖内", () => {
    expect(pageSource, "SchemeWorkbenchPage.tsx 应在 glob 覆盖内").toBeTruthy();
  });
  it("页面渲染可恢复横幅（含重新接管 + 暂时隐藏入口，稳定 data-testid）", () => {
    // ✅ G10 收口（2026-11-04）：横幅 JSX 抽为 UnknownTaskBanner 组件；
    //    页面必须实际挂载它，组件内必须保留三个 testid 与按钮文案。
    const bannerSource = RAW["pages/schemeWorkbench/UnknownTaskBanner.tsx"];
    expect(pageSource).toContain("<UnknownTaskBanner");
    expect(bannerSource).toContain(`data-testid="unknown-task-banner"`);
    expect(bannerSource).toContain(`data-testid="unknown-task-reattach"`);
    expect(bannerSource).toContain(`data-testid="unknown-task-dismiss"`);
    expect(bannerSource).toContain("重新接管任务");
  });

  it("三条 SSE 链（outline/content/facts）耗尽分支都必须 markUnknownTask", () => {
    // 三链各自的 genType 标记（hit && !hit.final → 未知态）
    expect(chainSources).toContain('genType: "outline"');
    expect(chainSources).toContain('genType: "content"');
    expect(chainSources).toContain('genType: "facts"');
    // 三链各一处 + 重新接管再次耗尽一处 = 4；任一链漏掉调用即降到 3 并红
    expect(chainSources.match(/markUnknownTask\(/g)?.length).toBeGreaterThanOrEqual(4);
  });

  it("三条生成链入口都必须过 guardUnknownTaskGeneration 守卫", () => {
    // 三个生成链入口各一次调用（定义处为 `const guardUnknownTaskGeneration = (`，不匹配本正则）
    expect(chainSources.match(/guardUnknownTaskGeneration\(/g)?.length).toBeGreaterThanOrEqual(3);
    expect(chainSources).toContain('guardUnknownTaskGeneration("目录生成"');
    expect(chainSources).toContain('guardUnknownTaskGeneration("正文生成"');
    expect(chainSources).toContain('guardUnknownTaskGeneration("全局事实提取"');
  });

  it("守卫必须先查询任务状态（tasksApi.status），查询失败不得放行新生成", () => {
    // onOk 内先 status 再 resolveUnknownTaskNext（函数体已随 G10 迁入 hook）
    const guardIdx = recoverySource.indexOf("const guardUnknownTaskGeneration");
    expect(guardIdx).toBeGreaterThan(0);
    const guardBody = recoverySource.slice(guardIdx, guardIdx + 4000);
    expect(guardBody).toContain("tasksApi.status(");
    expect(guardBody).toContain("resolveUnknownTaskNext(");
    expect(guardBody).toContain("本次未发起新生成");
  });
});

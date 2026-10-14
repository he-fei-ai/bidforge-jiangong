/**
 * 断线重挂「任务类型过滤」回归测试（2026-10-14 · 维度六 兜底鲁棒性）
 *
 * 背景：`findRecentOutlineTaskImpl` 原本只被目录生成用（默认
 * typeFilter="outline_generation"），而**正文生成**与**全局事实提取**两条
 * SSE 链路在收到首个携带 task_id 的事件之前断连时，taskId 恒为空串，
 * 旧实现直接跳过 pollTaskUntilTerminal 重挂 —— 后端任务已注册并继续
 * 生成（AI 照常计费），成果只躺在 task_registry.checkpoint_json，
 * 用户只看到「SSE 连接中断」，逐章失败明细 / 失败段数 / 跨段矛盾彻底丢失。
 *
 * 修复复用同一实现 + 既有 typeFilter 参数，只切换任务类型。
 * 本文件锁定「过滤器必须精确匹配目标类型」这一契约：一旦某条链路误传了
 * 别类型的过滤器，就会把**另一个业务**的在跑任务挂进本页并消费其
 * checkpoint（正文页显示事实提取的结果 / 反之），是比不复挂更糟的事故。
 */
import { describe, expect, it } from "vitest";
import {
  findRecentOutlineTaskImpl,
  reattachTaskImpl,
} from "../pages/SchemeWorkbenchPage";

const NOW = Date.parse("2026-10-14T20:00:00Z");

/** 三种业务任务各一条，全部 running + 时间窗内，保证「唯一决定因素是类型」。 */
function mkAllTypes() {
  return {
    tasks: [
      { id: "c1", task_type: "content_generation", status: "running",
        updated_at: "2026-10-14T19:58:00Z" },
      { id: "f1", task_type: "facts_generation", status: "running",
        updated_at: "2026-10-14T19:58:00Z" },
      { id: "o1", task_type: "outline_generation", status: "running",
        updated_at: "2026-10-14T19:58:00Z" },
    ],
  };
}

describe("断线重挂任务类型过滤（typeFilter 契约）", () => {
  it("正文生成链路：只挂 content_generation，绝不串到事实/目录任务", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => mkAllTypes(),
      { typeFilter: "content_generation", nowMs: NOW },
    );
    expect(got).toBe("c1");
  });

  it("事实提取链路：只挂 facts_generation", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => mkAllTypes(),
      { typeFilter: "facts_generation", nowMs: NOW },
    );
    expect(got).toBe("f1");
  });

  it("目录生成链路：默认过滤器仍是 outline_generation（向后兼容）", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => mkAllTypes(),
      { nowMs: NOW },
    );
    expect(got).toBe("o1");
  });

  it("目标类型无在跑任务时返回 null（保持旧行为：仅提示，不乱挂）", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => ({
        tasks: [
          { id: "o9", task_type: "outline_generation", status: "running",
            updated_at: "2026-10-14T19:58:00Z" },
        ],
      }),
      { typeFilter: "content_generation", nowMs: NOW },
    );
    expect(got).toBeNull();
  });

  it("检索请求失败静默返回 null（不得阻断断线后的收尾流程）", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => { throw new Error("network down"); },
      { typeFilter: "facts_generation", nowMs: NOW },
    );
    expect(got).toBeNull();
  });
});

/**
 * reattachTaskImpl 契约（2026-10-14 · 三链收口后的统一入口）。
 *
 * 锁定的不变式：
 *   1. taskId 已有 → 跳过检索直接轮询（不浪费一次 list 请求）；
 *   2. onReattach 必须在 pollTerminal 之前调用（用户在中断刻度上要先看到
 *      「正在重新挂接」，否则多等一个轮询周期却无反馈）；
 *   3. 检索不到 → 返回 null，不轮询、不 onReattach（调用方维持「仅提示」旧行为）；
 *   4. typeFilter 透传（防某链误传别类型过滤器把别的业务任务挂进本页）；
 *   5. 检索抛错 → 静默返回 null（不阻断断线后的收尾流程）。
 */
describe("reattachTaskImpl（三链统一重挂入口）", () => {
  it("taskId 已有 → 跳过检索直接轮询；onReattach 在轮询前调用", async () => {
    const order: string[] = [];
    let fetchCalled = false;
    const hit = await reattachTaskImpl(
      async () => { fetchCalled = true; return { tasks: [] }; },
      async (tid) => { order.push("poll"); return { status: "completed", id: tid }; },
      {
        taskId: "preset-tid",
        onReattach: () => order.push("reattach"),
      },
    );
    expect(fetchCalled).toBe(false);
    expect(hit).not.toBeNull();
    expect(hit!.taskId).toBe("preset-tid");
    expect(hit!.final.status).toBe("completed");
    expect(order).toEqual(["reattach", "poll"]);
  });

  it("taskId 为空 + 检索命中 → 透传 typeFilter，返回检索到的任务", async () => {
    const order: string[] = [];
    const hit = await reattachTaskImpl(
      async () => ({
        tasks: [{
          id: "c1", task_type: "content_generation", status: "running",
          updated_at: "2026-10-14T19:58:00Z",
        }],
      }),
      async (tid) => { order.push("poll"); return { status: "completed", id: tid }; },
      {
        typeFilter: "content_generation",
        nowMs: NOW,
        onReattach: () => order.push("reattach"),
      },
    );
    expect(hit).not.toBeNull();
    expect(hit!.taskId).toBe("c1");
    expect(order).toEqual(["reattach", "poll"]);
  });

  it("检索不到 → 返回 null，不轮询、不 onReattach", async () => {
    let polled = false;
    let reattached = false;
    const hit = await reattachTaskImpl(
      async () => ({
        tasks: [{
          id: "o1", task_type: "outline_generation", status: "running",
          updated_at: "2026-10-14T19:58:00Z",
        }],
      }),
      async () => { polled = true; return null; },
      {
        typeFilter: "content_generation", nowMs: NOW,
        onReattach: () => { reattached = true; },
      },
    );
    expect(hit).toBeNull();
    expect(polled).toBe(false);
    expect(reattached).toBe(false);
  });

  it("typeFilter 透传：正文过滤器不误挂事实任务", async () => {
    const hit = await reattachTaskImpl(
      async () => ({
        tasks: [{
          id: "f1", task_type: "facts_generation", status: "running",
          updated_at: "2026-10-14T19:58:00Z",
        }],
      }),
      async (tid) => ({ status: "completed", id: tid }),
      { typeFilter: "content_generation", nowMs: NOW },
    );
    expect(hit).toBeNull();
  });

  it("检索抛错 → 静默返回 null（不阻断断线后的收尾流程）", async () => {
    const hit = await reattachTaskImpl(
      async () => { throw new Error("network down"); },
      async () => ({ status: "completed" }),
      { typeFilter: "facts_generation", nowMs: NOW },
    );
    expect(hit).toBeNull();
  });
});
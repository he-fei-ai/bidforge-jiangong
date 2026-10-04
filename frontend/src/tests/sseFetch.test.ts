/**
 * SSE 流式客户端单元测试（对应《性能瓶颈分析与优化方案.md》前端 SSE 链路基座）
 *
 * sseFetch 是工作台生成期间唯一的增量推送通道；后续"SSE 事件批处理/轮询瘦身"
 * 优化均以本客户端解析语义为准，必须锁定：
 * - 跨 chunk 边界的 data 事件仍能完整解析
 * - heartbeat 行只触发回调、不产出事件
 * - 非 JSON data 行被静默跳过
 * - 调用前已 abort 时不发起网络请求
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { sseFetch } from "../api/index";

function sseResponse(chunks: Array<Uint8Array>): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const c of chunks) controller.enqueue(c);
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } }
  );
}

const enc = new TextEncoder();

describe("sseFetch", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("按序产出全部 data 事件，heartbeat 仅触发回调", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        enc.encode(
          "data: {\"type\":\"section_start\",\"section_id\":\"s1\"}\n\n" +
            ": heartbeat\n\n" +
            "data: {\"type\":\"section_done\",\"section_id\":\"s1\"}\n\n" +
            "data: {\"type\":\"done\"}\n\n"
        ),
      ])
    );
    vi.stubGlobal("fetch", fetchMock);

    const onHeartbeat = vi.fn();
    const events: any[] = [];
    for await (const ev of sseFetch("/schemes/x/generate", {}, { onHeartbeat })) {
      events.push(ev);
    }

    expect(events).toEqual([
      { type: "section_start", section_id: "s1" },
      { type: "section_done", section_id: "s1" },
      { type: "done" },
    ]);
    expect(onHeartbeat).toHaveBeenCalledTimes(1);
    // 请求必须发到 /api/v1 前缀且为 POST JSON
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/schemes/x/generate");
    expect(init.method).toBe("POST");
  });

  it("跨 chunk 边界的事件仍完整解析", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          enc.encode("data: {\"type\":\"sec"),
          enc.encode("tion_start\"}\n\ndata: {\"type\":\"do"),
          enc.encode("ne\"}\n\n"),
        ])
      )
    );

    const events: any[] = [];
    for await (const ev of sseFetch("/t")) {
      events.push(ev);
    }
    expect(events).toEqual([{ type: "section_start" }, { type: "done" }]);
  });

  it("非 JSON data 行被静默跳过", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([enc.encode("data: ping\n\ndata: {\"ok\":true}\n\n")])
      )
    );

    const events: any[] = [];
    for await (const ev of sseFetch("/t")) {
      events.push(ev);
    }
    expect(events).toEqual([{ ok: true }]);
  });

  it("调用前已 abort 时不发起网络请求", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const ctrl = new AbortController();
    ctrl.abort();

    for await (const _ of sseFetch("/t", {}, { signal: ctrl.signal })) {
      // noop
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("HTTP 非 2xx 时抛出带 detail 的错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "方案不存在" }), { status: 404 })
      )
    );

    await expect(
      (async () => {
        for await (const _ of sseFetch("/missing")) {
          // noop
        }
      })()
    ).rejects.toThrow("方案不存在");
  });

  // ✅ 2026-10-03 事故回归（全局事实提取恒 422）：FastAPI 校验失败的
  //    detail 是**数组**，旧实现只认字符串 → 用户只看到「SSE 请求失败: 422」，
  //    无法定位缺哪个参数。现在必须把明细翻译成可读消息。
  it("422 校验明细（detail 数组）翻译成可读消息而非裸状态码", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [
              { loc: ["query", "db"], msg: "field required", type: "missing" },
              { loc: ["path", "scheme_id"], msg: "string to strict", type: "string_type" },
            ],
          }),
          { status: 422 }
        )
      )
    );

    await expect(
      (async () => {
        for await (const _ of sseFetch("/sse/generate-facts/x")) {
          // noop
        }
      })()
    ).rejects.toThrow(/缺少必填查询参数 “db”/);
  });

  it("422 明细缺失/响应体非 JSON 时回退默认消息", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("not json", { status: 422, headers: { "Content-Type": "text/plain" } })
      )
    );

    await expect(
      (async () => {
        for await (const _ of sseFetch("/t")) {
          // noop
        }
      })()
    ).rejects.toThrow("SSE 请求失败");
  });

  // ✅ 2026-09-23 事故回归：后端进程挂死时 TCP 可连上但响应头永不到达，
  //    旧实现 fetch 无限悬挂 → 用户点「生成目录」停在「正在连接...」无任何报错。
  it("后端无响应（响应头超时）时抛出明确的连接超时错误", async () => {
    // fetch 永不 resolve（模拟后端接受 TCP 但事件循环挂死）
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));

    await expect(
      (async () => {
        for await (const _ of sseFetch("/sse/generate-outline/x", undefined, { connectTimeoutMs: 30 })) {
          // noop
        }
      })()
    ).rejects.toThrow("后端连接超时");
  });

  it("响应头按时到达后，慢速流式读取不受连接超时限制", async () => {
    // 头部立即返回，但事件间隔超过 connectTimeoutMs —— 旧数据流不得被超时中断
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponseWithDelay([enc.encode("data: {\"type\":\"done\"}\n\n")], 80)
      )
    );

    const events: any[] = [];
    for await (const ev of sseFetch("/t", undefined, { connectTimeoutMs: 30 })) {
      events.push(ev);
    }
    expect(events).toEqual([{ type: "done" }]);
  });

  it("兼容 CRLF 事件边界与多行 data 字段", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      enc.encode("data: {\"type\":\r\ndata: \"split\"}\r\n\r\n")
    ])));
    const events: any[] = [];
    for await (const ev of sseFetch("/t")) events.push(ev);
    expect(events).toEqual([{ type: "split" }]);
  });

  it("响应流关闭时解析末尾未带空行的完整 data 事件", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      enc.encode("data: {\"type\":\"tail\"}")
    ])));
    const events: any[] = [];
    for await (const ev of sseFetch("/t")) events.push(ev);
    expect(events).toEqual([{ type: "tail" }]);
  });

  it("响应头已到达但流静默时按空闲超时抛错", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      new ReadableStream({ start() { /* 保持打开但不产出 */ } }),
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    )));
    const collect = (async () => {
      for await (const _ of sseFetch("/t", undefined, { idleTimeoutMs: 20 })) {
        // noop
      }
    })();
    await expect(Promise.race([
      collect,
      new Promise((_, reject) => setTimeout(
        () => reject(new Error("旧实现未按空闲超时结束")), 200
      )),
    ])).rejects.toThrow("SSE 数据流空闲超时");
  });
});

/** 延迟 delayMs 后才产出并关闭 body 的 SSE 响应（模拟慢速流） */
function sseResponseWithDelay(chunks: Array<Uint8Array>, delayMs: number): Response {
  return new Response(
    new ReadableStream({
      async start(controller) {
        await new Promise((r) => setTimeout(r, delayMs));
        for (const c of chunks) controller.enqueue(c);
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } }
  );
}

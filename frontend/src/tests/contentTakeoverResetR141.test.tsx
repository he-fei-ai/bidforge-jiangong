/// <reference types="vite/client" />
// @vitest-environment jsdom
/**
 * R141（2026-10-28）· 刷新接管循环错误复位护栏。
 *
 * 审计发现：`SchemeWorkbenchPage.tsx` 的「页面加载/刷新后接管后台任务」循环
 * （useEffect 内，`for (let i = 0; i < 400; i++)`）是全文**第三套**任务轮询实现
 * （对照：断线重连路径 reattachTaskImpl + pollTaskUntilTerminalImpl 已收敛到
 * netFailTolerance 分级 + 有界退出）。旧实现在**任意一次**状态查询失败
 * （瞬时网络抖动 / 5xx / 404 任务已清理）时 `catch { return; }` 直接放弃，
 * **且不调用 resetTakeover()** —— 而 resetTakeover 只在终态 / stuck-at-full 分支
 * 被调用。后果：刷新接管后一旦状态接口失败一次，`generating=true` / `genType` /
 * `taskPaused` / `activeTaskIdRef` 永久保持，页面停在「生成中」，暂停/停止按钮
 * 控制的是已失效任务，唯一出口是手动刷新。
 *
 * R141 最小修复：catch 块复位接管状态（resetTakeover()）再 return。
 * 本护栏用源码锚锁「修复形态」—— 断言接管 for 循环内部的 catch 块必须调用
 * resetTakeover()（且带告警日志），防止后人改回 `catch { return; }` 静默放弃。
 *
 * 之所以用源码锚而非行为级：该循环内联在 12663 行巨型页面的 useEffect 里，
 * 行为级需渲染整棵组件树 + antd（R120/R121 已证明会撞 60s 墙钟，属环境敏感红点），
 * 结构性判据是既有仓库约定（见 contentTerminalParity / outlineReviewBasisR74）。
 */
import { describe, it, expect } from "vitest";

const RAW_SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.startsWith("../") ? k.slice(3) : k, v]),
);
const PAGE = RAW_SOURCES["pages/SchemeWorkbenchPage.tsx"] || "";

/**
 * 截取接管 for 循环主体（从 `for (let i = 0; i < 400; i++)` 起，到它对应的
 * 闭合 `}` 为止）。找不到循环体时返回 ""（让断言失败，暴露"改名/重构"）。
 */
function takeoverLoopBody(): string {
  const marker = "for (let i = 0; i < 400; i++)";
  const start = PAGE.indexOf(marker);
  if (start < 0) return "";
  // 循环体起点：marker 后的第一个 '{'
  const brace = PAGE.indexOf("{", start);
  if (brace < 0) return "";
  // 简单括号匹配：仅统计 { }，忽略字符串（循环体内无字符串含大括号的复杂场景）。
  let depth = 0;
  let i = brace;
  for (; i < PAGE.length; i++) {
    const ch = PAGE[i];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) { i++; break; }
    }
  }
  return PAGE.slice(brace, i);
}

describe("R141 · 接管循环错误复位", () => {
  it("前提：页面源码取到，且接管循环确实存在（找不到循环体即红，暴露重构）", () => {
    expect(PAGE.length).toBeGreaterThan(10000);
    expect(PAGE).toContain("for (let i = 0; i < 400; i++)");
    expect(takeoverLoopBody().length).toBeGreaterThan(100);
  });

  it("FIX：接管循环的 catch 块必须复位接管状态（旧实现 catch{return} 静默放弃）", () => {
    const body = takeoverLoopBody();
    // 循环体内必须有 catch(e:any) 分支
    expect(body).toContain("catch (e: any)");
    // 必须调用 resetTakeover()（旧实现只在终态/stuck 分支调用，catch 里没有）
    expect(body).toContain("resetTakeover()");
    // 必须带告警日志（旧实现静默，用户无从定位为何放弃接管）
    expect(body).toContain("状态查询失败");
    // 旧实现的确切形态 `catch { return; }`（无参数、无复位）不得再出现
    expect(body).not.toContain("catch {");
    // catch 内的复位必须发生在 return 之前：找 resetTakeover 与最近的 return 次序
    const rst = body.indexOf("resetTakeover()");
    const ret = body.indexOf("return;", rst);
    expect(rst).toBeGreaterThan(-1);
    expect(ret).toBeGreaterThan(rst);
  });

  it("FIX2：轮询跑满 400 次（有界退出）也必须复位接管状态（旧实现静默退出）", () => {
    // 有界退出复位位于 for 循环**之后**、外层 catch 之前 —— 需取更宽区域：
    // 从循环标记起到首个 `} catch {`（外层 catch）为止。
    const marker = "for (let i = 0; i < 400; i++)";
    const start = PAGE.indexOf(marker);
    expect(start).toBeGreaterThan(-1);
    const end = PAGE.indexOf("} catch {", start);
    expect(end).toBeGreaterThan(start);
    const region = PAGE.slice(start, end);
    // 必须存在「有界退出」复位（告警 + resetTakeover）
    expect(region).toContain("轮询 400 次");
    expect(region).toContain("未达终态");
    // resetTakeover() 至少出现 2 次：catch 分支 + 有界退出分支
    const count = (region.match(/resetTakeover\(\)/g) || []).length;
    expect(count).toBeGreaterThanOrEqual(2);
    // 有界退出分支的告警与复位相邻（先告警后复位）
    const warnIdx = region.indexOf("未达终态");
    const rstIdx = region.lastIndexOf("resetTakeover()");
    expect(rstIdx).toBeGreaterThan(warnIdx);
  });

  it("反向锚：断线重连主路径仍走收敛实现（不得退回手写轮询）", () => {
    // 页面断线重连三链都收敛到 reattachTaskImpl；接管循环之外不得再出现
    // 未收敛的 `catch {`（无参数）形态的轮询放弃 —— 只允许在接管循环内已锁定。
    expect(PAGE).toContain("reattachTaskImpl(");
    expect(PAGE).toContain("pollTaskUntilTerminalImpl(");
  });
});

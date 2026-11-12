// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { ensureMermaid, assertMermaidCodeSafe } from "../components/mermaidRuntime";

const gantt = [
  "gantt",
  "    title 机电安装工程进度计划",
  "    dateFormat  YYYY-MM-DD",
  "    axisFormat  %m-%d",
  "    section 施工准备",
  "    图纸会审与深化设计       :2026-05-18, 8d",
  "    方案编制与报审           :2026-05-18, 10d",
  "    临时水电接驳             :2026-05-18, 7d",
  "    section 拆除与预留预埋",
  "    既有管线拆除             :2026-05-26, 15d",
  "    预留孔洞与套管预埋       :2026-06-01, 15d",
  "    section 管线敷设与设备安装",
  "    电气配管穿线             :2026-06-16, 60d",
  "    给排水管道安装           :2026-06-16, 60d",
  "    通风空调风管水管安装     :2026-06-16, 60d",
  "    消防管网及报警系统布线   :2026-06-16, 60d",
  "    弱电桥架及线缆敷设       :2026-06-16, 60d",
  "    配电箱柜与设备安装       :2026-08-01, 45d",
  "    section 调试与验收",
  "    单机调试                 :2026-10-16, 15d",
  "    联动调试                 :2026-10-25, 10d",
  "    消防检测与专项验收       :2026-11-01, 10d",
  "    竣工验收与移交           :2026-11-10, 9d",
  "    竣工验收里程碑           :milestone, 2026-11-18, 0d",
].join("\n");

describe("gantt 渲染复现", () => {
  it("安全闸放行", () => {
    const g = assertMermaidCodeSafe(gantt);
    expect(g.safe).toBe(true);
  });

  it("mermaid.render 能渲染 gantt", async () => {
    // jsdom 不实现 SVGElement.getBBox（真实浏览器有），打桩排除测试环境假象
    const origGetBBox = (window.SVGElement.prototype as any).getBBox;
    (window.SVGElement.prototype as any).getBBox = function () {
      return { x: 0, y: 0, width: 0, height: 0 };
    };
    const mermaid = await ensureMermaid();
    const res = await mermaid.render("gantt-repro-" + Date.now(), gantt);
    expect(res.svg.length).toBeGreaterThan(100);
    if (origGetBBox) {
      (window.SVGElement.prototype as any).getBBox = origGetBBox;
    }
  }, 30000);
});

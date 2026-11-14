const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const frontend = 'j:/编程/专项方案工具箱/frontend';

(async () => {
  const dom = new JSDOM('<!DOCTYPE html><html><body><div id="container"></div></body></html>', {
    pretendToBeVisual: true,
  });
  global.window = dom.window;
  global.document = dom.window.document;
  global.navigator = dom.window.navigator;
  global.DOMPurify = { sanitize: (x) => x };
  global.SVGElement = dom.window.SVGElement;
  global.Element = dom.window.Element;
  global.HTMLElement = dom.window.HTMLElement;
  global.Node = dom.window.Node;
  global.requestAnimationFrame = dom.window.requestAnimationFrame || (cb => setTimeout(cb, 0));
  global.cancelAnimationFrame = dom.window.cancelAnimationFrame || (id => clearTimeout(id));
  // mermaid 内部依赖
  global.CanvasRenderingContext2D = dom.window.CanvasRenderingContext2D;
  global.OffscreenCanvas = dom.window.OffscreenCanvas;

  const mermaid = (await import(path.join(frontend, 'node_modules/mermaid/dist/mermaid.esm.min.mjs'))).default;
  mermaid.initialize({ startOnLoad: false, securityLevel: 'loose', theme: 'default' });

  const gantt = [
    'gantt',
    '    title 机电安装工程进度计划',
    '    dateFormat  YYYY-MM-DD',
    '    axisFormat  %m-%d',
    '    section 施工准备',
    '    图纸会审与深化设计       :2026-05-18, 8d',
    '    方案编制与报审           :2026-05-18, 10d',
    '    临时水电接驳             :2026-05-18, 7d',
    '    section 拆除与预留预埋',
    '    既有管线拆除             :2026-05-26, 15d',
    '    预留孔洞与套管预埋       :2026-06-01, 15d',
    '    section 管线敷设与设备安装',
    '    电气配管穿线             :2026-06-16, 60d',
    '    给排水管道安装           :2026-06-16, 60d',
    '    通风空调风管水管安装     :2026-06-16, 60d',
    '    消防管网及报警系统布线   :2026-06-16, 60d',
    '    弱电桥架及线缆敷设       :2026-06-16, 60d',
    '    配电箱柜与设备安装       :2026-08-01, 45d',
    '    section 调试与验收',
    '    单机调试                 :2026-10-16, 15d',
    '    联动调试                 :2026-10-25, 10d',
    '    消防检测与专项验收       :2026-11-01, 10d',
    '    竣工验收与移交           :2026-11-10, 9d',
    '    竣工验收里程碑           :milestone, 2026-11-18, 0d',
  ].join('\n');

  try {
    const res = await mermaid.render('gmtest', gantt);
    console.log('RENDER_OK, svg length =', res.svg.length);
  } catch (e) {
    console.log('RENDER_FAIL:', e && (e.message || String(e)));
  }
})();

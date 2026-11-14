"""高保真图表规格 · 统一图表导出器 `ChartExporter`（文档 §9）

把「单图单函数」升级为统一导出器，支持：

  · 多格式：PNG（插入 Word/PPT）/ PDF（直接成册）/ SVG（矢量再编辑）
  · 高 DPI：默认 3 倍（print），`SCALE_PRESETS` 可按用途切换（screen/print/vector）
  · 批量并发：`batch_export` 用 `asyncio.gather` 一次导出整本标书全部图表
  · 字体兜底：`font_dir` 注入本地 CJK 字体，解决服务器无中文字体的「豆腐块」
  · 数据注入：`inject_data` 把 JSON 载荷替换进 HTML 模板的 `const data = {...};`
  · 容错：元素缺失时 `query_selector` 返回 None，调用方应捕获并标记失败

两条渲染轨：
  ① HTML 模板轨（Playwright，文档 §9 主轨）：HTML 模板 + 数据注入 → 截图。
  ② 结构化数据轨（复用既有 PIL v2 商业级渲染器）：Mermaid/JSON 代码 →
     `render_mermaid_to_bytes`（300 DPI + 2.5x 超采样）→ PNG bytes。

调用方按载荷形态选轨；两轨互不侵入既有生成链路。

⚠️ 本模块**可选依赖** Playwright：未安装时 HTML 轨抛出
   `RuntimeError("Playwright 未安装，HTML 模板轨不可用")`，调用方应回退
   到结构化数据轨（既有 PIL 渲染）—— 这与既有「HTTP → PIL 降级链」同向。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from app.services.ai.image_engine import IMAGE_SCALE_PRESETS

__all__ = [
    "ChartExporter",
    "SCALE_PRESETS",
    "inject_chart_data",
    "route_hifi_spec",
    "render_hifi_png",
]


# 多倍率 DPI 预设（文档 §9）：screen 预览 / print 打印评审 / vector 矢量级
# ✅ R242：改为**转发引用** image_engine.IMAGE_SCALE_PRESETS —— R237 曾两处
#    各定义一份同值字面量（「第二份副本」反模式）；收敛后 `is` 同一对象，
#    改一处即两处生效。
SCALE_PRESETS: dict[str, int] = IMAGE_SCALE_PRESETS


def inject_chart_data(html: str, data: dict) -> str:
    """把 JSON 载荷注入 HTML 模板的 ``const data = {...};`` 占位。

    文档 §5 的策略：``html.replace('const data = {...};', f'const data = {json.dumps(...)}')``。
    未找到占位符时**原样返回**（不静默破坏模板）。

    Args:
        html: HTML 模板文本。
        data: 图表 JSON 载荷（dict）。

    Returns:
        注入后的 HTML 文本；未命中占位符时原样返回。
    """
    placeholder = "const data = {...};"
    if placeholder not in html:
        return html
    return html.replace(
        placeholder,
        f"const data = {json.dumps(data, ensure_ascii=False)};",
    )


class ChartExporter:
    """统一图表导出器：结构化图表 → PNG / PDF / SVG，支持批量与高 DPI。

    Attributes:
        scale: 截图 device_scale_factor（默认 3，打印级）。
        font_dir: 可选本地 CJK 字体目录，解决服务器缺字。
        timeout_ms: 页面等待/截图超时（毫秒）。
    """

    SCALE_PRESETS = SCALE_PRESETS

    def __init__(
        self,
        scale: int = 3,
        font_dir: str | None = None,
        timeout_ms: int = 15000,
    ) -> None:
        self.scale = scale
        self.font_dir = font_dir
        self.timeout_ms = timeout_ms

    # ------------------------------------------------------------------
    # HTML 模板轨（Playwright）
    # ------------------------------------------------------------------
    def _font_face(self) -> str:
        """构造本地 CJK 字体 @font-face 注入（解决服务器无中文字体）。"""
        if not self.font_dir:
            return ""
        faces = []
        d = Path(self.font_dir)
        if d.is_dir():
            for f in sorted(d.glob("*.ttf")):
                path = f.resolve().as_posix()
                # 拼接而非 .format()：CSS 花括号会被 format 误判为占位符
                faces.append(
                    '<style>@font-face{font-family:"CJK";src:url("file:///'
                    + path
                    + '");font-display:swap;}</style>'
                )
        return "".join(faces)

    async def _render_html(
        self,
        html: str,
        selector: str,
        full: bool = False,
    ) -> bytes:
        """用 Playwright 渲染 HTML 并截图返回 PNG bytes。

        Args:
            html: 完整 HTML（已注入数据）。
            selector: 截图目标元素选择器（如 ``.gantt-container``）。
            full: True 截全页，False 截元素。

        Returns:
            PNG bytes；元素缺失时抛 ``RuntimeError``（调用方应捕获标记失败，
            而非静默产出空白图）。
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - 环境缺 playwright
            raise RuntimeError("Playwright 未安装，HTML 模板轨不可用") from exc

        if self.font_dir:
            face = self._font_face()
            if face:
                html = html.replace("</head>", face + "</head>")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=["--font-render-hinting=none", "--force-color-profile=srgb"]
            )
            page = await browser.new_page(
                device_scale_factor=self.scale,
                viewport={"width": 1500, "height": 1000},
            )
            await page.set_content(html, wait_until="networkidle", timeout=self.timeout_ms)
            await page.wait_for_timeout(400)  # 等字体与 SVG 渲染完成
            if full:
                result = await page.screenshot(full_page=True, omit_background=False)
            else:
                el = await page.query_selector(selector)
                if el is None:
                    await browser.close()
                    raise RuntimeError(f"截图目标元素缺失：{selector}")
                result = await el.screenshot(omit_background=False)
            await browser.close()
        return result

    async def to_png(
        self,
        html: str,
        out: str | Path,
        selector: str = ".gantt-container",
        full: bool = False,
    ) -> str:
        """HTML 轨 → PNG 文件。"""
        data = await self._render_html(html, selector, full=full)
        out_p = Path(out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_bytes(data)
        return str(out_p)

    async def to_pdf(
        self,
        html: str,
        out: str | Path,
        selector: str = ".gantt-container",
    ) -> str:
        """HTML 轨 → PDF 文件（固定 A4 + 16mm 页边距，图卡居中）。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Playwright 未安装，HTML 模板轨不可用") from exc

        if self.font_dir:
            face = self._font_face()
            if face:
                html = html.replace("</head>", face + "</head>")

        out_p = Path(out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--font-render-hinting=none"])
            page = await browser.new_page(
                device_scale_factor=self.scale,
                viewport={"width": 1500, "height": 1000},
            )
            await page.set_content(html, wait_until="networkidle", timeout=self.timeout_ms)
            await page.wait_for_timeout(400)
            await page.pdf(
                path=str(out_p),
                print_background=True,
                format="A4",
                margin={
                    "top": "16mm",
                    "bottom": "16mm",
                    "left": "16mm",
                    "right": "16mm",
                },
            )
            await browser.close()
        return str(out_p)

    async def to_svg(
        self,
        html: str,
        out: str | Path,
        selector: str = ".gantt-container",
    ) -> str:
        """HTML 轨 → 提取内联 SVG 导出为独立 .svg（矢量再编辑）。

        fail-closed：SVG 壳内**无实质绘制内容**（无 path/rect/circle/line/polygon/
        text/g 等图形元素）时抛 ``RuntimeError``，避免调用方拿到 50 字节的空白矢量
        文件却当作成功 —— 与「元素缺失抛错而非静默空白图」同族。div 渲染的节点
        （甘特任务条/组织节点/工艺节点）不在 SVG 层内，本导出只覆盖连线层。
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Playwright 未安装，HTML 模板轨不可用") from exc

        out_p = Path(out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1500, "height": 1000})
            await page.set_content(html, wait_until="networkidle", timeout=self.timeout_ms)
            svg = await page.eval_on_selector(
                selector,
                "el => el.querySelector('svg') ? el.querySelector('svg').outerHTML : ''",
            )
            await browser.close()
        if not svg:
            raise RuntimeError(f"目标元素内无内联 SVG：{selector}")
        if not any(tok in svg for tok in ("<path", "<rect", "<circle", "<line", "<polygon", "<text", "<g ")):
            raise RuntimeError(f"目标 SVG 无实质绘制内容（可能是 div 渲染模板，SVG 层为空）：{selector}")
        out_p.write_text(svg, encoding="utf-8")
        return str(out_p)

    # ------------------------------------------------------------------
    # 结构化数据轨（复用既有 PIL v2 商业级渲染器）
    # ------------------------------------------------------------------
    async def render_code_to_png(
        self,
        code: str,
        chart_type: str = "flowchart",
        out: str | Path | None = None,
        **render_kw,
    ) -> bytes | None:
        """Mermaid/JSON 代码 → PNG bytes（复用既有 PIL 渲染轨）。

        与 HTML 轨不同，本轨不依赖 Playwright —— 走
        ``render_mermaid_to_bytes``（300 DPI + 2.5x 超采样）。

        Args:
            code: Mermaid 语法或 chart-json 载荷。
            chart_type: 图表类型（flowchart/gantt/architecture/...）。
            out: 可选输出路径（提供则同时写盘）。
            **render_kw: 透传渲染器（duration/unit/tick/allow_pil 等）。

        Returns:
            PNG bytes；渲染失败返回 None（调用方按「生成失败」处理）。
        """
        from app.services.ai.mermaid_renderer import render_mermaid_to_bytes

        buf = await asyncio.to_thread(
            render_mermaid_to_bytes,
            code,
            chart_type,
            **render_kw,
        )
        if buf is None:
            return None
        data = buf.getvalue()
        if out is not None:
            out_p = Path(out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(data)
        return data

    # ------------------------------------------------------------------
    # 批量导出
    # ------------------------------------------------------------------
    async def batch_export(
        self,
        jobs: list[dict],
        fmt: str = "png",
    ) -> list[str]:
        """批量并发导出。

        Args:
            jobs: ``[{"html":..., "out":..., "selector":..., "full":...}, ...]``。
            fmt: ``"png"|"pdf"|"svg"``。

        Returns:
            导出文件路径列表（顺序与 jobs 一致）。
        """
        async def _one(j: dict) -> str:
            sel = j.get("selector", ".gantt-container")
            out = j["out"]
            if fmt == "png":
                return await self.to_png(j["html"], out, sel, full=j.get("full", False))
            if fmt == "pdf":
                return await self.to_pdf(j["html"], out, sel)
            if fmt == "svg":
                return await self.to_svg(j["html"], out, sel)
            raise ValueError(f"未知导出格式：{fmt}")

        return list(await asyncio.gather(*[_one(j) for j in jobs]))

# ============================================================
# ✅ R242（2026-11-13 · hifi 通道）：主链载荷 → hifi 模板轨的分发入口
# ============================================================
# R237 落地的 ChartExporter/三模板此前零消费（「服务层就绪 ≠ 功能可达」）；
# 本入口被 export._prepare_export_render._render_one 消费：载荷含 hifi 特征键
# 时优先走 HTML 模板轨（Playwright），失败降级既有 PIL 轨——降级语义与
# 「HTTP → PIL」既有降级链同向。判据**窄收**（宁缺勿误）：主链 AI 常规载荷
# （steps/edges 无坐标、天数域 gantt、label/children 树）一律不命中，
# 只有无歧义 hifi 特征（level1 平铺 / YYYY-MM-DD 日期域 / 全节点数值坐标）才路由。
_HIFI_SELECTORS = {
    "gantt": ".gantt-container",
    "org_matrix": ".org-chart-container",
    "flow_complex": ".flow-container",
}
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# Playwright 环境级不可用（未安装/浏览器缺失）的 TTL 短路：避免每图重试
# （同 mermaid_service.all_backends_probed_unavailable 的保守短路模式；
#   仅环境级失败置位，单图渲染失败不置位不影响后续图）。
_HIFI_UNAVAILABLE_UNTIL = 0.0
_HIFI_PROBE_TTL = 300.0


def route_hifi_spec(code: str) -> tuple[str, dict] | None:
    """主链图表载荷 → hifi 模板路由（窄判据）。

    Returns:
        ``(route, payload)``：route ∈ {"gantt", "org_matrix", "flow_complex"}；
        非结构化 JSON / 无 hifi 特征 → ``None``（调用方走既有渲染轨）。
    """
    if not isinstance(code, str):
        return None
    s = code.strip()
    if not s.startswith("{"):
        return None
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # org_matrix：nodes 为 dict 且 level1 平铺非空（与主链「nodes=list」形态互斥）
    nodes_obj = data.get("nodes")
    if isinstance(nodes_obj, dict) and isinstance(nodes_obj.get("level1"), list) and nodes_obj["level1"]:
        return ("org_matrix", data)
    # gantt spec：日期域载荷（start_date 为 YYYY-MM-DD）——主链天数域载荷
    # （start: 1 / days: 5）不含 start_date 键，互斥可辨
    sd = data.get("start_date")
    if (isinstance(sd, str) and _DATE_PREFIX_RE.match(sd) and isinstance(data.get("tasks"), list) and data["tasks"]):
        return ("gantt", data)
    # flow_complex：steps/nodes 数组 ≥2 节点且**全部**携带数值 x/y（坐标化特征）
    arr = data.get("steps") if isinstance(data.get("steps"), list) and data.get("steps") else data.get("nodes")
    if isinstance(arr, list) and len(arr) >= 2 and all(
        isinstance(nd, dict) and isinstance(nd.get("x"), (int, float)) and isinstance(nd.get("y"), (int, float))
        for nd in arr
    ):
        return ("flow_complex", data)
    return None


async def render_hifi_png(route: str, payload: dict, *, scale_preset: str = "print") -> bytes:
    """按路由渲染 hifi 模板轨 PNG（环境级不可用 TTL 短路；调用方捕获降级 PIL 轨）。

    Args:
        route: :func:`route_hifi_spec` 产出的路由键。
        payload: 原始 spec 载荷（原样注入模板 `const data = {...};`）。
        scale_preset: IMAGE_SCALE_PRESETS 键（screen/print/vector），缺省 print。

    Raises:
        RuntimeError：Playwright 未安装 / 模板缺失 / 截图元素缺失。
    """
    global _HIFI_UNAVAILABLE_UNTIL
    if time.monotonic() < _HIFI_UNAVAILABLE_UNTIL:
        raise RuntimeError("hifi HTML 轨近期探测不可用（TTL 短路中）")
    tpl_dir = Path(__file__).resolve().parent / "templates"
    html = (tpl_dir / f"{route}.html").read_text(encoding="utf-8")
    html = inject_chart_data(html, payload)
    exporter = ChartExporter(scale=SCALE_PRESETS.get(scale_preset, 3))
    try:
        # 直取 _render_html 的 PNG bytes（to_png 是「bytes → 落盘」封装，
        # 主链渲染轨需要内存字节，不落盘零临时文件）。
        png = await exporter._render_html(html, _HIFI_SELECTORS[route])
    except RuntimeError as exc:
        if "Playwright 未安装" in str(exc):
            _HIFI_UNAVAILABLE_UNTIL = time.monotonic() + _HIFI_PROBE_TTL
        raise
    if not png or len(png) <= 100:
        raise RuntimeError("hifi HTML 轨产出过小（疑似空白截图），按失败处理")
    return png

# 文档图表缺失检测与自动补全系统（docchart）

读取 Markdown / HTML / DOCX 文档 → 语义化检测"应配图但缺图"的位置 → 自动抽取数据、
推荐图表类型、渲染并精准回插 → 导出保留原格式的新文档，并输出结构化报告与校验结果。

## 安装

```powershell
cd chart-gap-filler
pip install -r requirements.txt
```

## 运行命令

```powershell
# 1) 只检测，输出缺失清单（可写 JSON）
python -m docchart.cli analyze 文档.docx --json gaps.json

# 2) 全流程：检测 + 生成图表 + 回插 + 导出新文档 + 重解析校验
python -m docchart.cli fill 文档.docx -o 新文档.docx --report report.json

# 3) 使用自定义配置（字体/尺寸/阈值，见 config.example.yaml）
python -m docchart.cli fill 文档.md --config config.example.yaml
```

## 测试

```powershell
python -m pytest --basetemp=C:\Users\ADMINI~1\AppData\Local\Temp\pt_cgf -p no:cacheprovider
```

## 检测能力（六类信号，非纯关键词匹配）

| 类型 | 说明 |
| --- | --- |
| `dangling_reference` | "如图4-2所示/如下图所示"但窗口内无图（结合章节与图注编号联动判断） |
| `caption_without_image` | 存在"图 x-x 标题"图注但前后无实际图片 |
| `empty_placeholder` | 【插入图表】等空占位符 |
| `table_without_chart` | 表格数值密度达标却无配套图（全【待补充】的表降级为"需补数"提示） |
| `semantic_visualization` | 趋势/占比/对比/流程/架构/里程碑语义 + 就地可取数值 |
| `broken_figure_number` | 同章节图注编号序列有空缺（如 1-1、1-3 缺 1-2），仅报告不自动出图 |

每处缺失输出：位置（锚点块+章节）、原因、置信度、建议图型、所需数据。

## 生成链路

意图识别 → 数据源定位（锚点表格 / 邻近表格 / 正文"标签-数值"对）→ 提取清洗
（千分位、单位、百分号、占位符）→ 类型推荐（时间序→折线、合计≈100→饼图、
多系列→分组柱状、流程文本→流程图）→ 渲染（Matplotlib，中文微软雅黑，PNG/SVG）
→ 回插（md 按行号 / html 按字符偏移 / docx 按原生段落 XML 前后插入，原格式零重排）
→ 校验（重新解析导出文档，核对图片数与图注）。

数据不足时不硬造数值图：报告给出 `skip_reason` 与补充数据建议，或降级生成
示意性流程图/结构图并标注"供核对"。DOCX 场景下即使配置 svg 也会强制降级为
png（python-docx 无法嵌入 SVG）。

## 编程接口（供其他模块调用）

```python
from docchart import analyze, fill, save_report, Config

doc, gaps = analyze("方案.docx")          # 只检测，返回 (Document, list[ChartGap])
report = fill("方案.docx", out_path="新.docx")   # 全链路，返回可 JSON 序列化报告
```

包级 API 惰性导入，`import docchart` 不会拉起 matplotlib 等重依赖；
组件间以 `models.Document/Block/ChartGap` 为数据契约（Block.id 恒等于块序索引）。

## 扩展点

- 新格式：`docchart.parsers.register_parser(".pdf", fn)` 运行时注册，
  或在 `parsers/__init__.py` 注册表加一行（PDF 预留）；
- 新图型：`docchart/generation/renderer.py` 用 `@builder("name")` 注册（Mermaid/Graphviz 可平替）；
- 新导出器：`docchart/insertion/exporters.py` 的 `register_exporter(fmt, fn)`。

## 目录结构

```
docchart/
├── models.py config.py cli.py pipeline.py __main__.py __init__.py（公共 API）
├── parsers/    markdown_parser.py html_parser.py docx_parser.py
├── detection/  rules.py detector.py
├── generation/ datasource.py recommender.py renderer.py
└── insertion/  exporters.py
examples/       sample_report.md sample_page.html
tests/          test_parsers.py test_detection.py test_generation.py
                test_pipeline_e2e.py test_exporters.py test_cli.py test_interactions.py
```

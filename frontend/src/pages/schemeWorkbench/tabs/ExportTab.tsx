import type { Dispatch, SetStateAction } from "react";
import type { FormInstance } from "antd";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Divider,
  Form,
  Input,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { ExportOutlined, FilePdfOutlined, ReloadOutlined } from "@ant-design/icons";
import type { ExportPresetItem, ExportCacheStatus } from "../useExportPresets";
import type { TreeNode } from "../outlineTreeModel";
import type { ExportGateResult } from "../../../utils/exportCharts";
import { EXPORT_STAGE_LABEL, EXPORT_STAGE_ORDER } from "../exportShared";

const { Text } = Typography;

/** 「导出文档」Tab（Tab 6）面板：页面级状态 / handler / setter 全部显式注入。 */
export interface ExportTabProps {
  // 导出配置表单 + 导出进度
  exportForm: FormInstance;
  exporting: boolean;
  exportPhase: string;
  exportPct: number;
  exportStage: "idle" | "charts" | "docx" | "pdf";
  exportGate: ExportGateResult;
  handleExport: (format?: "docx" | "pdf") => Promise<void>;
  handleCancelExport: () => void;
  // 格式预设库
  exportPresets: ExportPresetItem[];
  selectedPresetId: string;
  setSelectedPresetId: (v: string) => void;
  presetOpLoading: string | null;
  handleRefreshPresets: () => Promise<void>;
  handleSavePreset: () => Promise<void>;
  handleSetDefaultPreset: (pid: string) => Promise<void>;
  handleDeletePreset: (pid: string) => void;
  // 目录结构导出
  tree: TreeNode[];
  generating: boolean;
  exportingTree: boolean;
  handleExportTree: () => Promise<void>;
  // 导出缓存
  cacheStatus: ExportCacheStatus | null;
  cacheLoading: boolean;
  loadCacheStatus: () => Promise<void>;
  // 跳转审核与预检
  setActiveTab: Dispatch<SetStateAction<string>>;
}

export function ExportTab({
  exportForm,
  exporting,
  exportPhase,
  exportPct,
  exportStage,
  exportGate,
  handleExport,
  handleCancelExport,
  exportPresets,
  selectedPresetId,
  setSelectedPresetId,
  presetOpLoading,
  handleRefreshPresets,
  handleSavePreset,
  handleSetDefaultPreset,
  handleDeletePreset,
  tree,
  generating,
  exportingTree,
  handleExportTree,
  cacheStatus,
  cacheLoading,
  loadCacheStatus,
  setActiveTab,
}: ExportTabProps) {
  return (
    // ✅ 修复：导出配置表单（基本设置/正文样式/各级标题样式 + 统计 + 缓存）较长，
    //    Form 根元素需可滚动，避免低视口下底部按钮（导出预检/导出 DOCX/PDF）被裁剪
    <Form
      form={exportForm}
      layout="vertical"
      className="scroll-area"
      style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}
      initialValues={{
        font_name: "宋体",
        font_size: 12,
        show_page_number: true,
        show_title_page: false,
        show_toc: false,
        bidder_name: "",
        allow_pil_fallback: false,
        // ✅ 新增排版选项（默认值 = 标准专项方案版式）
        chapter_page_break: true,
        line_spacing: 1.15,
        page_number_style: "simple",
        toc_depth: 3,
        heading_styles: {
          "1": { font_name: "宋体", font_size: 14, bold: true },
          "2": { font_name: "宋体", font_size: 14, bold: true },
          "3": { font_name: "宋体", font_size: 14, bold: true },
          "4": { font_name: "宋体", font_size: 14, bold: true },
          "5": { font_name: "宋体", font_size: 12, bold: false },
          "6": { font_name: "宋体", font_size: 12, bold: false },
          "7": { font_name: "宋体", font_size: 12, bold: false },
        },
        // ✅ 增强 v7：封面项目信息表 + 页边距（缺省值与标准版式一致，向后兼容）
        margins: { left: 2.5, right: 2.5, top: 2.5, bottom: 2.5 },
        cover_info: { 工程名称: "", 编制单位: "", 方案编号: "", 版本: "" },
        // ✅ 容错与增强开关：默认值与后端 export.py 的 config.get 默认逐项一致
        //    （ai_image_auto_generate=True，其余 False），未改动任何既有行为。
        ai_image_auto_generate: true,
        // ✅ 2026-10-02（第二十三轮）：专项施工方案法定前置表单四开关。
        //    默认全 false —— 不传时后端 scheme_forms 规范化为空 dict，
        //    产物与本轮之前逐字一致（向后兼容）。
        scheme_forms: {
          compilation_note: false,
          approval: false,
          expert_review: false,
          drawing_appendix: false,
        },
        chart_fail_placeholder: false,
        auto_rewrite_content: false,
        auto_fix_unclosed_fences: false,
        heading_border: false,
        // ✅ 2026-10-11（《文档生成规则》4.2 导出标准）：版式预设，
        //    缺省 "" = 不启用，产物与本轮之前逐字一致（向后兼容）。
        layout_preset: "",
      }}
    >
      {/* ✅ 导出格式预设库：同项目排版格式一键复用（对标 OpenBidKit exportFormatPresets） */}
      <Card size="small" style={{ marginBottom: 8 }} title="格式预设">
        <Space wrap>
          <Select
            style={{ minWidth: 200 }}
            placeholder="选择格式预设"
            value={selectedPresetId || undefined}
            onChange={(pid: string) => {
              setSelectedPresetId(pid);
              const p = exportPresets.find((x) => x.id === pid);
              if (p) exportForm.setFieldsValue(p.config);
            }}
            options={exportPresets.map((p) => ({
              value: p.id,
              label: (p.is_default ? "★ " : "") + p.name,
            }))}
          />
          {/* ✅ R-1（2026-10-04）：4 个预设操作按钮统一由 presetOpLoading 单键控制
                  loading + disabled。此前无 loading，快速双击会并发 POST（尤其保存预设）。 */}
          <Button
            size="small"
            loading={presetOpLoading === "refresh"}
            disabled={!!presetOpLoading}
            onClick={handleRefreshPresets}
          >
            刷新
          </Button>
          <Button
            size="small"
            type="primary"
            loading={presetOpLoading === "save"}
            disabled={!!presetOpLoading}
            onClick={handleSavePreset}
          >
            保存当前为预设
          </Button>
          {selectedPresetId && (
            <>
              <Button
                size="small"
                loading={presetOpLoading === "setDefault"}
                disabled={!!presetOpLoading}
                onClick={() => handleSetDefaultPreset(selectedPresetId)}
              >
                设为默认
              </Button>
              <Button
                size="small"
                danger
                loading={presetOpLoading === "delete"}
                disabled={!!presetOpLoading}
                onClick={() => handleDeletePreset(selectedPresetId)}
              >
                删除
              </Button>
            </>
          )}
        </Space>
      </Card>
      <Collapse
        defaultActiveKey={["basic", "body", "headings"]}
        size="small"
        style={{ marginBottom: 8 }}
        items={[
          {
            key: "basic",
            label: "基本设置",
            children: (
              <>
                <Row gutter={[12, 0]}>
                  {/* 封面页 + 目录页 同行 */}
                  <Col span={12}>
                    <Form.Item
                      name="show_title_page"
                      label="封面页"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name="show_toc"
                      label="目录页"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  {/* 页眉 + 页脚 同行 */}
                  <Col span={12}>
                    <Form.Item name="page_header" label="页眉" style={{ marginBottom: 8 }}>
                      <Input size="small" placeholder="如：XX项目深基坑专项方案" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item name="page_footer" label="页脚" style={{ marginBottom: 8 }}>
                      <Input size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item name="bidder_name" label="编制单位" style={{ marginBottom: 8 }}>
                      {/* ⚠️ 2026-10-01 定位切换：label 由「投标单位」改为「编制单位」——
                            专项施工方案的封面主体是编制/施工单位，不是投标人。
                            字段名 bidder_name 保持不变 —— 它是导出 config 的
                            历史契约（export.py 参数名 + 已存配置），改名字段会
                            让老方案的封面单位变成空白（AGENTS.md §3.2 禁止破坏契约）。
                            真正该补的是「编制/审核/批准人」三个岗位字段（见遗留项 L-0）。 */}
                      <Input size="small" placeholder="如：XX建设有限公司" />
                    </Form.Item>
                  </Col>
                  <Col span={6}>
                    <Form.Item
                      name="show_page_number"
                      label="显示页码"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={6}>
                    <Form.Item
                      name="allow_pil_fallback"
                      label="PIL兜底渲染"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                      tooltip="默认关闭：图表仅用 mermaid 原生引擎渲染（前端预览同款/后端高清服务），失败图表默认整张跳过且不占图号（成稿不会出现红字报错段，可配合「渲染失败占位」开关定位失败图）。开启后失败图表改用内置 PIL 渲染器兜底（版式与预览略有差异）。总平面布置图/里程碑时间线始终使用内置渲染器，不受此开关影响。"
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  {/* ✅ 新增：章节分页 / 页码样式 / 目录层级 */}
                  <Col span={8}>
                    <Form.Item
                      name="chapter_page_break"
                      label="章节另起一页"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                      tooltip="开启后每个一级章节从新页开始（专项方案标准版式）。首个章节不会额外分页，封面与目录之后也不会多出空白页。"
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="page_number_style"
                      label="页码样式"
                      style={{ marginBottom: 8 }}
                      tooltip="需要「显示页码」开启后生效。Word 打开文档时会自动刷新页码域。"
                    >
                      <Select
                        size="small"
                        options={[
                          { label: "简单（- 1 -）", value: "simple" },
                          { label: "第 X 页 / 共 Y 页", value: "page_of_total" },
                        ]}
                      />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="toc_depth"
                      label="目录收录层级"
                      style={{ marginBottom: 8 }}
                      tooltip="自动目录收录到第几级标题，默认 3 级（章 / 节 / 小节）。"
                    >
                      <Select
                        size="small"
                        options={[
                          { label: "1 级", value: 1 },
                          { label: "2 级", value: 2 },
                          { label: "3 级", value: 3 },
                          { label: "4 级", value: 4 },
                          { label: "5 级", value: 5 },
                        ]}
                      />
                    </Form.Item>
                  </Col>
                </Row>
                {/* ✅ 缺口修复（2026-09-24）：后端 export.py 已支持 5 个容错/增强开关，
                      但前端表单从未下发 —— 用户无法关闭「导出时自动生成配图」、
                      无法开启「渲染失败仍占位」「正文自动改写」「未闭合代码块自动修复」
                      「标题边框」，只能被迫接受后端默认值。现补齐下发通道，
                      默认值与后端 config.get 默认逐项一致（见 export.py
                      2905 / 3025 / 3054 / 3079 / 3303 / 3305），旧行为不变。 */}
                <Divider orientation="left" style={{ margin: "4px 0 8px" }}>
                  容错与增强
                </Divider>
                <Row gutter={[12, 0]}>
                  <Col span={8}>
                    <Form.Item
                      name="ai_image_auto_generate"
                      label="自动生成配图"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                      tooltip="导出时为正文中的「配图」占位自动生成插图（后端默认开启）。关闭则保留占位描述、不调用生图服务（可节省配额与时间）。"
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="chart_fail_placeholder"
                      label="渲染失败占位"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                      tooltip="开启后渲染失败的图表保留占位（红字图题「图 X-Y — 渲染失败」，图号照占）便于排查定位；默认关闭时失败图表整张跳过且图号回收，交付文档不出现红字。仅影响渲染失败降级方式，不影响正常图表。"
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="auto_rewrite_content"
                      label="正文自动改写"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                      tooltip="导出时对正文做规范化改写（清理残留标记/口语化表述）。默认关闭：导出内容以生成稿为准。"
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="auto_fix_unclosed_fences"
                      label="修复未闭合代码块"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                      tooltip="导出前尝试补齐正文里未闭合的代码围栏（AI 截断导致），避免图表/公式整段丢失。默认关闭。"
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="heading_border"
                      label="标题边框"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                      tooltip="为各级标题添加边框（部分送审版式要求）。默认关闭。"
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                </Row>
              </>
            ),
          },
          {
            key: "cover",
            label: "封面与页面",
            children: (
              <>
                <Divider orientation="left" style={{ margin: "4px 0 8px" }}>
                  页边距（cm）
                </Divider>
                <Row gutter={[12, 0]}>
                  <Col span={6}>
                    <Form.Item name={["margins", "top"]} label="上" style={{ marginBottom: 8 }}>
                      <InputNumber
                        size="small"
                        min={1}
                        max={5}
                        step={0.1}
                        style={{ width: "100%" }}
                      />
                    </Form.Item>
                  </Col>
                  <Col span={6}>
                    <Form.Item name={["margins", "bottom"]} label="下" style={{ marginBottom: 8 }}>
                      <InputNumber
                        size="small"
                        min={1}
                        max={5}
                        step={0.1}
                        style={{ width: "100%" }}
                      />
                    </Form.Item>
                  </Col>
                  <Col span={6}>
                    <Form.Item name={["margins", "left"]} label="左" style={{ marginBottom: 8 }}>
                      <InputNumber
                        size="small"
                        min={1}
                        max={5}
                        step={0.1}
                        style={{ width: "100%" }}
                      />
                    </Form.Item>
                  </Col>
                  <Col span={6}>
                    <Form.Item name={["margins", "right"]} label="右" style={{ marginBottom: 8 }}>
                      <InputNumber
                        size="small"
                        min={1}
                        max={5}
                        step={0.1}
                        style={{ width: "100%" }}
                      />
                    </Form.Item>
                  </Col>
                </Row>
                <Divider orientation="left" style={{ margin: "4px 0 8px" }}>
                  封面信息（需开启「封面页」）
                </Divider>
                <Row gutter={[12, 0]}>
                  <Col span={12}>
                    <Form.Item
                      name={["cover_info", "工程名称"]}
                      label="工程名称"
                      style={{ marginBottom: 8 }}
                    >
                      <Input size="small" placeholder="如：XX项目深基坑工程" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name={["cover_info", "编制单位"]}
                      label="编制单位"
                      style={{ marginBottom: 8 }}
                    >
                      <Input size="small" placeholder="如：XX建设集团有限公司" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name={["cover_info", "方案编号"]}
                      label="方案编号"
                      style={{ marginBottom: 8 }}
                    >
                      <Input size="small" placeholder="如：FA-2026-001" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name={["cover_info", "版本"]}
                      label="版本"
                      style={{ marginBottom: 8 }}
                    >
                      <Input size="small" placeholder="如：A版 / V1.0" />
                    </Form.Item>
                  </Col>
                </Row>
                {/* ✅ 2026-10-02（第二十三轮 · L-0）：专项施工方案法定前置表单。
                        依据住建部令第37号第十一条（审核/审查签字盖章）、
                        第十二条（专家不得少于 5 名）、第十三条（论证报告三选一结论），
                        以及建办质〔2018〕31号 第九条（计算书及相关施工图纸）。
                        全部默认关闭（向后兼容，开启后产物新增对应前置页）。 */}
                <Divider orientation="left" style={{ margin: "4px 0 8px" }}>
                  专项施工方案法定前置表单
                </Divider>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 8, fontSize: 12 }}
                  message="签字栏一律留空，由各方线下手签；系统不预填人名与证书编号。"
                  description="审批表依据住建部令第37号第十一条；专家论证报告仅在「超过一定规模的危大工程」时需要，论证结论为通过 / 修改后通过 / 不通过三选一。"
                />
                <Row gutter={[12, 0]}>
                  <Col span={12}>
                    <Form.Item
                      name={["scheme_forms", "compilation_note"]}
                      label="编制说明"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name={["scheme_forms", "approval"]}
                      label="专项施工方案审批表"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name={["scheme_forms", "expert_review"]}
                      label="专家论证报告"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name={["scheme_forms", "drawing_appendix"]}
                      label="施工图纸附件清单"
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Switch size="small" />
                    </Form.Item>
                  </Col>
                </Row>
              </>
            ),
          },
          {
            key: "body",
            label: "正文样式",
            children: (
              <Row gutter={[12, 0]}>
                {/* 正文字体 + 正文字号 同行 */}
                <Col span={12}>
                  <Form.Item name="font_name" label="正文字体" style={{ marginBottom: 8 }}>
                    <Select
                      size="small"
                      options={[
                        { label: "微软雅黑", value: "微软雅黑" },
                        { label: "宋体", value: "宋体" },
                        { label: "仿宋", value: "仿宋" },
                        { label: "黑体", value: "黑体" },
                      ]}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="font_size" label="正文字号" style={{ marginBottom: 8 }}>
                    <Select
                      size="small"
                      options={[
                        { label: "小四 (12pt)", value: 12 },
                        { label: "五号 (10.5pt)", value: 10.5 },
                        { label: "四号 (14pt)", value: 14 },
                        { label: "小三 (15pt)", value: 15 },
                      ]}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="line_spacing"
                    label="正文行距"
                    style={{ marginBottom: 8 }}
                    tooltip="正文段落行距，首行自动缩进 2 个字符（随字号自适应）。"
                  >
                    <Select
                      size="small"
                      options={[
                        { label: "单倍 (1.0)", value: 1 },
                        { label: "1.15 倍", value: 1.15 },
                        { label: "1.5 倍（推荐）", value: 1.5 },
                        { label: "1.75 倍", value: 1.75 },
                        { label: "双倍 (2.0)", value: 2 },
                      ]}
                    />
                  </Form.Item>
                </Col>
                {/* ✅ 新增（2026-10-11 · 《文档生成规则》4.2 导出标准）：版式预设，
                        一键整体切换行距 + 四边距；已在「正文样式」里手动细调的
                        边距值不被预设强改（后端 _resolve_layout_preset 补缺不覆盖）。 */}
                <Col span={12}>
                  <Form.Item
                    name="layout_preset"
                    label="版式预设"
                    style={{ marginBottom: 8 }}
                    tooltip="选择后整体应用配套的正文行距与页边距（如工程公文：上下 2.54cm / 左右 3.18cm / 1.5 倍行距）。默认「不启用」，产物版式与既往完全一致；你在「页边距」里显式改过的数值不会被预设覆盖。"
                  >
                    <Select
                      size="small"
                      allowClear
                      options={[
                        { label: "不启用（保持标准版式）", value: "" },
                        { label: "工程公文（上下2.54 / 左右3.18 / 1.5倍）", value: "engineering" },
                      ]}
                    />
                  </Form.Item>
                </Col>
              </Row>
            ),
          },
          {
            key: "headings",
            label: "各级标题样式",
            children: (
              <>
                {[1, 2, 3, 4, 5, 6, 7].map((lvl) => (
                  <div
                    key={lvl}
                    style={{
                      display: "flex",
                      gap: 8,
                      alignItems: "center",
                      marginBottom: 4,
                    }}
                  >
                    <Tag style={{ minWidth: 48, textAlign: "center", marginInlineEnd: 0 }}>
                      {lvl}级标题
                    </Tag>
                    <Form.Item
                      name={["heading_styles", String(lvl), "font_name"]}
                      style={{ flex: 1, marginBottom: 0 }}
                    >
                      <Select
                        size="small"
                        options={[
                          { label: "宋体", value: "宋体" },
                          { label: "微软雅黑", value: "微软雅黑" },
                          { label: "黑体", value: "黑体" },
                          { label: "仿宋", value: "仿宋" },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item
                      name={["heading_styles", String(lvl), "font_size"]}
                      style={{ width: 100, marginBottom: 0 }}
                    >
                      <Select
                        size="small"
                        options={[
                          { label: "三号 (16pt)", value: 16 },
                          { label: "小三 (15pt)", value: 15 },
                          { label: "四号 (14pt)", value: 14 },
                          { label: "小四 (12pt)", value: 12 },
                          { label: "五号 (10.5pt)", value: 10.5 },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item
                      name={["heading_styles", String(lvl), "bold"]}
                      style={{ width: 70, marginBottom: 0 }}
                      valuePropName="checked"
                    >
                      <Switch size="small" checkedChildren="加粗" unCheckedChildren="常规" />
                    </Form.Item>
                  </div>
                ))}
              </>
            ),
          },
        ]}
      />
      {exporting && exportPhase && (
        <Alert
          type="info"
          showIcon
          message={exportPhase}
          // ✅ D2：真实进度条 + 取消入口。
          //    旧实现只有一句静态文案，文档生成阶段（可达数十秒）零进度反馈。
          description={
            <div>
              <Progress
                percent={exportPct}
                size="small"
                status="active"
                style={{ marginBottom: 6 }}
              />
              <div
                style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
              >
                <span>
                  <Tag color="processing">
                    阶段 {EXPORT_STAGE_ORDER[exportStage] ?? 1}/3 ·{" "}
                    {EXPORT_STAGE_LABEL[exportStage]}
                  </Tag>
                  大文档导出可能需要 10-30 秒，图表渲染和文档构建在后台进行，请耐心等待。
                </span>
                <Button size="small" onClick={handleCancelExport}>
                  取消导出
                </Button>
              </div>
            </div>
          }
          style={{ marginBottom: 12 }}
        />
      )}
      {/* ✅ 2026-10-01：审核 / 预检不再作为导出的前置条件，此处只做非阻断提示。
              未完成预检、存在 high 问题、就绪度未放行均不影响导出按钮。 */}
      {exportGate.highIssueCount > 0 && (
        <Alert
          type="warning"
          showIcon
          message={exportGate.reason}
          description="审核与预检结论仅供参考，不限制导出；如用于正式交付，建议先整改上述问题。"
          action={
            <Button size="small" onClick={() => setActiveTab("review")}>
              前往审核与预检
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}
      <Space>
        <Button
          type="primary"
          icon={<ExportOutlined />}
          onClick={() => handleExport("docx")}
          loading={exporting}
          title="导出 DOCX（不受审核与预检状态限制）"
        >
          {exporting ? "导出中..." : "导出 DOCX"}
        </Button>
        <Button
          icon={<FilePdfOutlined />}
          onClick={() => handleExport("pdf")}
          loading={exporting}
          title="先生成 DOCX 再转换为 PDF（需安装 Microsoft Word 或 LibreOffice）"
        >
          导出 PDF
        </Button>
      </Space>

      {/* ✅ 功能归位：目录结构导出也是"导出"，从「目录生成」页收敛到本页，
              让导出类操作集中在一处（目录生成页专注编辑）。 */}
      <Card
        size="small"
        title={
          <Space>
            <ExportOutlined />
            <Text strong>目录结构导出</Text>
          </Space>
        }
        style={{ marginTop: 12 }}
        extra={
          <Button
            size="small"
            icon={<ExportOutlined />}
            onClick={handleExportTree}
            loading={exportingTree}
            disabled={generating || exporting || tree.length === 0}
          >
            导出目录 JSON
          </Button>
        }
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          导出按后端权威编号（如 1 / 1.1 / 1.1.1）整理的目录树 JSON，可直接给外部工具或存档使用，
          <b>不改动数据库</b>。 与上方 DOCX / PDF
          文档导出是两种产物：这里是"目录文件"，上方是"完整方案文档"。
        </Text>
      </Card>

      {/* ===== ✅ 导出缓存状态（exportApi.cacheStatus）===== */}
      <Card
        size="small"
        title="导出缓存"
        style={{ marginTop: 12 }}
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={cacheLoading}
            onClick={loadCacheStatus}
          >
            查询缓存
          </Button>
        }
      >
        {!cacheStatus ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            点击「查询缓存」查看该方案可复用的历史导出结果。内容与配置未变时会直接命中缓存，秒级返回。
          </Text>
        ) : (
          <>
            <Space style={{ marginBottom: 8 }} wrap>
              <Tag color={cacheStatus.total > 0 ? "green" : "default"}>
                有效缓存 {cacheStatus.total} 条
              </Tag>
              {cacheStatus.stale > 0 && (
                <Tooltip title="结果文件已被保留策略（最多 5 份）清理、磁盘文件已丢失，或为导出失败遗留的孤儿产物。下次导出会自动清理这些记录，无需手工处理。">
                  <Tag color="orange">失效 {cacheStatus.stale} 条</Tag>
                </Tooltip>
              )}
              <Text type="secondary" style={{ fontSize: 12 }}>
                变更版本号或导出配置即视为未命中
              </Text>
            </Space>
            {cacheStatus.items?.length > 0 ? (
              <Table
                size="small"
                rowKey="id"
                pagination={false}
                dataSource={cacheStatus.items}
                columns={[
                  {
                    title: "状态",
                    dataIndex: "exists",
                    width: 72,
                    render: (ok: boolean) => (
                      // ✅ 逐行标注有效/失效：失效行（文件已被保留策略清理或
                      //    为导出失败遗留的孤儿产物）与有效行不再长得一样，
                      //    避免用户误以为任何一行都能秒级复用。
                      <Tag color={ok ? "green" : "default"}>{ok ? "有效" : "失效"}</Tag>
                    ),
                  },
                  {
                    title: "生成时间",
                    dataIndex: "created_at",
                    width: 170,
                    render: (v: string) => (v || "").replace("T", " ").slice(0, 19),
                  },
                  {
                    title: "指纹",
                    dataIndex: "content_fingerprint",
                    width: 140,
                    render: (v: string) => (
                      <Tooltip title={v}>
                        <Text code style={{ fontSize: 11 }}>
                          {(v || "").slice(0, 12)}…
                        </Text>
                      </Tooltip>
                    ),
                  },
                  {
                    title: "产物路径",
                    dataIndex: "result_path",
                    ellipsis: true,
                    render: (v: string) => (
                      <Tooltip title={v}>
                        <Text style={{ fontSize: 11 }}>
                          {(v || "").split(/[\\/]/).pop() || "—"}
                        </Text>
                      </Tooltip>
                    ),
                  },
                ]}
              />
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>
                暂无有效缓存
              </Text>
            )}
          </>
        )}
      </Card>
    </Form>
  );
}

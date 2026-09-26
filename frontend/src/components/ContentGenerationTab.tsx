/**
 * 正文生成 Tab · 受控组件（从 SchemeWorkbenchPage.tsx 抽离）
 *
 * 承载 content Tab 的操作与配置 UI：生成按钮行（含暂停/恢复/停止与暂停状态）、
 * 字数与并发设置、全文一致性 Agent 修复、超字数自动压缩、「下一步：审核与预检」。
 * 与 UploadParseTab / BidAnalysisTab 同一模式：组件只做「渲染 + 回调上抛」，
 * 数据与副作用全部留在页面，正确性由组件级交互测试兜底（tests/contentTab.test.tsx）。
 */
import type { ReactNode } from "react";
import { Button, Card, Divider, InputNumber, Radio, Select, Space, Switch, Tag, Tooltip, Typography } from "antd";
import { useBreakpoint } from "../utils/ui";
// F-CONTENT-STANDARD(2026-09-26): 方案默认标准文案（与后端值域同源）
import { sectionStandardOptionLabel } from "../utils/contentStandard";
import {
  ArrowRightOutlined, CaretRightOutlined, ClearOutlined, EditOutlined, FileSearchOutlined,
  FontSizeOutlined, InfoCircleOutlined, MinusCircleOutlined, PauseOutlined, PlayCircleOutlined,
  StopOutlined, SwapOutlined, SyncOutlined,
} from "@ant-design/icons";

const { Text } = Typography;

export type ContentGenerationTabProps = {
  /** 是否有任意生成任务在跑（content/outline/facts 任一）——按钮 disabled 口径 */
  generating: boolean;
  /** 是否正在生成正文（generating && genType === "content"）——按钮 loading 口径 */
  running: boolean;
  /** 当前活跃任务是否已暂停（暂停/恢复按钮互斥 + 「已暂停」Tag） */
  taskPaused: boolean;
  /** 正在压缩本章（AI 压缩请求进行中） */
  shrinking: boolean;
  /** 目录树是否有章节（右下提示文案显隐） */
  hasTree: boolean;
  /** 是否已选中章节（生成当前章节 / 续写本章的 disabled 口径） */
  hasSelectedSection: boolean;
  /** 压缩本章是否可用（有正文且超过目标字数 130% 口径由页面计算） */
  canShrink: boolean;
  wordBudgetOption: string;
  customWordBudget: number | null;
  concurrencyOption: string;
  autoConsistencyRepair: boolean;
  consistencySeverity: string;
  autoShrinkOver: boolean;
  /** 最近一次一致性修复摘要（total>0 时显示 Tag） */
  crSummary: { total?: number; repaired?: number } | null;
  onGenerateAll: () => void;
  onGenerateCurrent: () => void;
  onGenerateMissing: () => void;
  onContinueSection: () => void;
  onShrinkSection: () => void;
  /** 重置正文：清空目录树中所有章节已生成正文（确认弹窗在页面层） */
  onReset: () => void;
  /** 重置按钮是否可用（存在已生成正文的章节且无任务运行） */
  canReset: boolean;
  onControl: (action: "pause" | "resume" | "stop") => void;
  onWordBudgetChange: (v: string) => void;
  onCustomWordBudgetChange: (v: number | null) => void;
  onConcurrencyChange: (v: string) => void;
  onAutoConsistencyChange: (v: boolean) => void;
  onSeverityChange: (v: string) => void;
  onOpenConsistencyWorkbench: () => void;
  onAutoShrinkChange: (v: boolean) => void;
  /** 「下一步：审核与预检」（页面接线 NEXT_TAB.content） */
  onNextStep: () => void;
  // F-CONTENT-STANDARD(2026-09-26): 生成标准选择状态
  generationStandard?: "inherit" | "precise" | "fuzzy";
  onGenerationStandardChange?: (v: "inherit" | "precise" | "fuzzy") => void;
  /** 「存为方案默认」：仅 precise/fuzzy 可存，PATCH 方案级默认值 */
  onSaveSchemeDefault?: () => void;
  savingSchemeDefault?: boolean;
  /** 方案级当前默认值（用于「沿用方案默认」文案与提示） */
  schemeDefault?: string;
  /** Tab 底部内容（页面渲染 SectionContentCard，保持该卡片与页面状态绑定） */
  children?: ReactNode;
};

const WORD_BUDGET_OPTIONS = [
  { label: "使用目录默认值", value: "default" },
  { label: "1000 字", value: "1000" },
  { label: "1500 字", value: "1500" },
  { label: "2000 字", value: "2000" },
  { label: "3000 字", value: "3000" },
  { label: "自定义", value: "custom" },
];

const SEVERITY_OPTIONS = [
  { label: "仅高危（推荐）", value: "high" },
  { label: "中及以上", value: "medium" },
  { label: "全部（含低危）", value: "low" },
];

export function ContentGenerationTab(p: ContentGenerationTabProps) {
  const { isPhone } = useBreakpoint();
  // 方案默认标准的展示文案（空值/脏数据统一按精准兜底，与后端默认一致）
  const schemeDefaultLabel = sectionStandardOptionLabel("", p.schemeDefault);
  return (
    // 与 UploadParseTab 同一模式：作为 flex Tab 面板内的可滚动内容区，
    // 必须 flex:1 + minHeight:0 + overflowY:auto，否则长内容会被父容器的
    // overflow:hidden 裁掉，底部「章节正文卡片」无法滚动查看。
    <div className="scroll-area" style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
      {/* 第一行：主要操作按钮 */}
      <Space style={{ marginBottom: 8 }} wrap>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={p.onGenerateAll}
          disabled={p.generating}
          loading={p.running}
        >
          一键生成全文
        </Button>
        <Button
          icon={<CaretRightOutlined />}
          onClick={p.onGenerateCurrent}
          disabled={p.generating || !p.hasSelectedSection}
          loading={p.running}
        >
          生成当前章节
        </Button>
        <Button
          icon={<EditOutlined />}
          onClick={p.onGenerateMissing}
          disabled={p.generating}
          loading={p.running}
        >
          补全生成
        </Button>
        <Tooltip title="在选中章节已有正文的基础上继续补充内容（不覆盖既有正文），对齐 OpenBidKit 的任意点续写">
          <Button
            onClick={p.onContinueSection}
            disabled={p.generating || !p.hasSelectedSection}
            loading={p.running}
          >
            续写本章
          </Button>
        </Tooltip>
        <Tooltip title="章节超出目标字数时进行 AI 压缩（只删不改：表格/图表/代码块/图片/技术参数受保护）。对齐 OpenBidKit 的字数压缩（shrink）">
          <Button
            icon={<MinusCircleOutlined />}
            onClick={p.onShrinkSection}
            disabled={p.generating || p.shrinking || !p.canShrink}
            loading={p.shrinking}
          >
            压缩本章
          </Button>
        </Tooltip>
        <Tooltip title="清空目录树中所有章节已生成的正文（含随正文生成的内联图表与章节审核状态），目录结构与字数预算保留。该操作不可恢复">
          <Button
            danger
            icon={<ClearOutlined />}
            onClick={p.onReset}
            disabled={p.generating || !p.canReset}
          >
            重置正文
          </Button>
        </Tooltip>
        {p.running && (
          <>
            <Button
              icon={<PauseOutlined />}
              onClick={() => p.onControl("pause")}
              disabled={p.taskPaused}
            >
              暂停
            </Button>
            <Button
              icon={<PlayCircleOutlined />}
              onClick={() => p.onControl("resume")}
              disabled={!p.taskPaused}
            >
              恢复
            </Button>
            <Button danger icon={<StopOutlined />} onClick={() => p.onControl("stop")}>
              停止
            </Button>
            {p.taskPaused && (
              <Tag color="warning" style={{ margin: 0 }}>
                ⏸ 已暂停（恢复后继续生成）
              </Tag>
            )}
          </>
        )}
        <Button
          type="primary"
          ghost
          icon={<ArrowRightOutlined />}
          onClick={p.onNextStep}
          disabled={p.generating}
        >
          下一步：审核与预检
        </Button>
        {p.hasTree && (
          <Text type="secondary" style={{ marginLeft: 12 }}>
            💡 左侧点击任意章节可查看/编辑已生成内容
          </Text>
        )}
      </Space>

      {/* 第二行：字数设置 / 生成模式 / 全文一致性 Agent 修复 / 超字数章节自动压缩（四列并列水平排列） */}
      <Card
        size="small"
        style={{ marginBottom: 8, background: "#fafafa" }}
        styles={{ body: { padding: "12px 18px" } }}
      >
        <div
          className="bp-content-gen-grid"
          style={{
            display: "grid",
            // ✅ 四列不再等宽：按内容宽度加权分配，避免「生成模式」三个按钮被挤扁裁切、
            //    而「字数设置」（仅一个 140px 下拉）却大片留白。minmax(0,..) 保证窄屏不溢出。
            gridTemplateColumns: isPhone
              ? "1fr"
              : "minmax(0, 1fr) minmax(0, 2.1fr) minmax(0, 1.7fr) minmax(0, 1.2fr)",
            columnGap: isPhone ? 0 : 28,
            rowGap: 16,
            width: "100%",
            minWidth: 0,
          }}
        >
          {/* 列 1：字数设置 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <FontSizeOutlined style={{ color: "#1677ff", fontSize: 14 }} />
              <Text strong style={{ fontSize: 13 }}>字数设置</Text>
              <Tooltip title="二级及以下各章节目标字数；子章节由 AI 按语义智能分配">
                <InfoCircleOutlined style={{ color: "#999", fontSize: 12, cursor: "help" }} />
              </Tooltip>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <Select
                value={p.wordBudgetOption}
                onChange={p.onWordBudgetChange}
                style={{ width: 140 }}
                size="small"
                options={WORD_BUDGET_OPTIONS}
              />
              {p.wordBudgetOption === "custom" && (
                <InputNumber
                  size="small"
                  min={100}
                  max={20000}
                  step={100}
                  value={p.customWordBudget ?? 2000}
                  onChange={p.onCustomWordBudgetChange}
                  style={{ width: 100 }}
                />
              )}
              {p.wordBudgetOption === "custom" && (
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap" }}>字 / 章节</Text>
              )}
            </div>
            {p.wordBudgetOption !== "default" && (
              <Tag color="blue" style={{ margin: 0, fontSize: 11, lineHeight: "18px" }}>
                按二级章节总字数控制
              </Tag>
            )}
          </div>

          {/* 列 2：生成模式 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <SwapOutlined style={{ color: "#1677ff", fontSize: 14 }} />
              <Text strong style={{ fontSize: 13 }}>生成模式</Text>
              <Tooltip title="长方案 / AI 易超时选精细；短方案 / 稳定时选快速">
                <InfoCircleOutlined style={{ color: "#999", fontSize: 12, cursor: "help" }} />
              </Tooltip>
            </div>
            <Radio.Group
              value={p.concurrencyOption}
              onChange={(e) => p.onConcurrencyChange(e.target.value)}
              size="small"
              buttonStyle="solid"
              style={{ width: "100%", display: "flex", gap: 6 }}
            >
              <Radio.Button value="slow" style={{ flex: "1 1 0", minWidth: 0, textAlign: "center", fontSize: 12, whiteSpace: "nowrap" }}>
                🐢 精细（并发 2）
              </Radio.Button>
              <Radio.Button value="balanced" style={{ flex: "1 1 0", minWidth: 0, textAlign: "center", fontSize: 12, whiteSpace: "nowrap" }}>
                ⚖️ 平衡（并发 3）
              </Radio.Button>
              <Radio.Button value="fast" style={{ flex: "1 1 0", minWidth: 0, textAlign: "center", fontSize: 12, whiteSpace: "nowrap" }}>
                🚀 快速（并发 5）
              </Radio.Button>
            </Radio.Group>
            <Text type="secondary" style={{ fontSize: 11, whiteSpace: "nowrap" }}>
              并发 2 / 3 / 5，控制速度与稳定性
            </Text>
          </div>

          {/* 列 3：全文一致性 Agent 修复 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <SyncOutlined style={{ color: "#1677ff", fontSize: 14 }} />
              <Text strong style={{ fontSize: 13 }}>全文一致性 Agent 修复</Text>
              <Switch
                size="small"
                checked={p.autoConsistencyRepair}
                onChange={p.onAutoConsistencyChange}
              />
              <Tooltip title="正文生成后由 AI Agent 自动扫描前后矛盾（数值 / 人名 / 型号 / 时间与承诺口径 / 与全局事实不符），按权威值优先级定向修复，修复前自动留版本快照">
                <InfoCircleOutlined style={{ color: "#999", fontSize: 12, cursor: "help" }} />
              </Tooltip>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <Select
                size="small"
                value={p.consistencySeverity}
                onChange={p.onSeverityChange}
                disabled={!p.autoConsistencyRepair}
                style={{ flex: "0 0 128px", width: 128 }}
                options={SEVERITY_OPTIONS}
              />
              <Button
                size="small"
                icon={<FileSearchOutlined />}
                onClick={p.onOpenConsistencyWorkbench}
                style={{ fontSize: 12 }}
              >
                打开修复工作台
              </Button>
            </div>
            {p.crSummary && (p.crSummary.total ?? 0) > 0 && (
              <Tag color="orange" style={{ margin: 0, fontSize: 11, lineHeight: "18px" }}>
                最近发现 {p.crSummary.total} 处冲突
                {typeof p.crSummary.repaired === "number" ? ` · 已修复 ${p.crSummary.repaired}` : ""}
              </Tag>
            )}
          </div>

          {/* 列 4：超字数章节自动压缩 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <FontSizeOutlined style={{ color: "#1677ff", fontSize: 14 }} />
              <Text strong style={{ fontSize: 13 }}>超字数章节自动压缩</Text>
              <Switch
                size="small"
                checked={p.autoShrinkOver}
                onChange={p.onAutoShrinkChange}
              />
              <Tooltip title="仅对超过目标字数 130% 的章节生效；只删不改（表格/图表/代码块/图片/技术参数受保护）。与目录树「压缩本章」同一套实现，开启后每个超字数章节最多额外产生 3 次 AI 调用">
                <InfoCircleOutlined style={{ color: "#999", fontSize: 12, cursor: "help" }} />
              </Tooltip>
            </div>
            <Text type="secondary" style={{ fontSize: 11, whiteSpace: "nowrap" }}>
              仅 &gt; 目标字数 130% 章节；只删不改
            </Text>
          </div>
        </div>
      </Card>

      {/* F-CONTENT-STANDARD(2026-09-26): 生成标准选择条 —— 精准 / 模糊 */}
      <div style={{
        display: "flex", alignItems: "center", gap: 12,
        padding: "8px 14px", marginBottom: 8,
        background: "#fafafa", borderRadius: 6, border: "1px solid #f0f0f0",
      }}>
        <Text strong style={{ fontSize: 13 }}>
          生成标准
          <Tooltip title="精准内容：正文严格按全局事实逐条编写，数值/型号/名称编号与事实完全一致，缺失数据标注【待补充：参数名】。模糊内容：以全局事实为基础，允许概括归纳与范围表述，但不得与事实矛盾。默认「按章节设置」= 章节级 → 方案默认 → 精准 逐级回落。">
            <InfoCircleOutlined style={{ color: "#999", fontSize: 12, cursor: "help", marginLeft: 4 }} />
          </Tooltip>
        </Text>
        <Radio.Group
          size="small"
          value={p.generationStandard || "inherit"}
          onChange={(e) => p.onGenerationStandardChange?.(e.target.value)}
          optionType="button"
          buttonStyle="solid"
          disabled={p.generating}
        >
          <Radio value="inherit">按章节设置</Radio>
          <Radio value="precise">精准内容</Radio>
          <Radio value="fuzzy">模糊内容</Radio>
        </Radio.Group>
        {/* ✅ F-CONTENT-STANDARD（2026-09-26 · F5）：把本次选择固化为方案默认。
            没有这一步，「生成标准」只是本次任务的临时开关，用户换个方案/换台机器
            就要重新选一遍；方案级默认也无处可设。 */}
        <Button
          size="small"
          type="link"
          onClick={p.onSaveSchemeDefault}
          loading={p.savingSchemeDefault}
          disabled={p.generating || !p.onSaveSchemeDefault || !p.generationStandard || p.generationStandard === "inherit"}
          title={p.generationStandard === "inherit"
            ? "「按章节设置」不是一个方案级取值，请先选择精准内容或模糊内容"
            : "将当前选择保存为本方案的默认生成标准"}
        >
          存为方案默认
        </Button>
        <Text type="secondary" style={{ fontSize: 11, marginLeft: "auto", textAlign: "right" }}>
          {p.generationStandard === "precise"
            ? "本次生成全部章节按精准标准（数值与全局事实严格一致）"
            : p.generationStandard === "fuzzy"
              ? "本次生成全部章节按模糊标准（允许在事实基础上概括）"
              : `本次生成按章节设置逐章回落（方案默认：${schemeDefaultLabel}）`}
        </Text>
      </div>

      {p.children}
    </div>
  );
}

export default ContentGenerationTab;

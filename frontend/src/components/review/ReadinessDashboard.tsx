/**
 * 就绪度总览仪表盘（审核与预检模块的"总入口"）
 *
 * 解决的问题
 * ----------
 * 此前「审核与预检」页是五张互不相干的卡片（规范符合性 / 一致性 / 专家预检 /
 * 导出预检 / 质量自检），每张都要单独点一次按钮，出一份独立结论。用户跑完
 * 五轮后依然无法回答最朴素的问题：**"这份方案现在能不能交付？"**
 *
 * 这里把它收敛为一个**可解释的综合分**：
 *   一键总检 → 六维加权评分 → A/B/C/D 等级 → 是否放行 → 阻断项置顶 → 逐条下钻整改
 *
 * 设计要点（对标商业级审查工具）：
 * - **一个数字 + 一个结论**：总分与「建议放行 / 先整改」放在最显眼处；
 * - **可解释**：分数不是黑箱 —— 六个维度各得多少、为什么扣分，能下钻到规则与证据；
 * - **有红线**：存在阻断项（如引用废止标准、缺计算书）时明确不放行；
 * - **看得见进步**：展示历史分数趋势，整改后分数是否上涨一目了然；
 * - **结论可带走**：一键生成整改清单报告（Markdown）。
 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  App, Alert, Button, Card, Col, Descriptions, Drawer, Empty, List, Progress,
  Row, Segmented, Space, Spin, Table, Tag, Tooltip, Typography,
} from "antd";
import {
  CheckCircleOutlined, CloseCircleOutlined, DownloadOutlined,
  ExclamationCircleOutlined, FileTextOutlined, ReloadOutlined,
  SafetyCertificateOutlined, ThunderboltOutlined, WarningOutlined,
} from "@ant-design/icons";
import { complianceApi } from "../../api";
import AutoFixModal from "./AutoFixModal";
import BatchFixModal from "./BatchFixModal";
import { useAntdMessageHub } from "../../utils/activityCenter";
import {
  GRADE_COLOR, SCORE_COLOR, SEVERITY_COLOR, SEVERITY_LABEL, SEVERITY_WEIGHT,
  type AuditDimension, type AuditRule, type PreflightFinding,
  type PreflightRunItem, type ReadinessOverview, type Severity,
} from "../../types/audit";

const { Text, Paragraph } = Typography;

/** 评分条颜色：≥90 绿 / ≥75 蓝 / ≥60 橙 / 其余红 */
function scoreColor(score: number): string {
  if (score >= 90) return "#52c41a";
  if (score >= 75) return "#1677ff";
  if (score >= 60) return "#fa8c16";
  return "#ff4d4f";
}

const SOURCE_LABEL: Record<string, string> = {
  program: "程序化规则",
  compliance: "规范符合性（AI）",
  consistency: "一致性审计（AI）",
  consistency_scan: "一致性扫描（规则）",
  expert_review: "专家论证预检（AI）",
  // ✅ G1（2026-09-21）：导出预检并入总检后新增的数据来源
  export_check: "导出预检",
};

export interface ReadinessDashboardProps {
  schemeId: string;
  /** 章节加载完成等外部数据变更后，父组件递增此值触发自动重新拉取历史 */
  refreshKey?: number;
  /**
   * ✅ 自动修复改写正文后回调（2026-09-30）：宿主页需刷新目录/正文
   * （sections.content 变了）并重新总检（结论已因内容变更而过期）。
   * 不传则只在组件内重新总检。
   */
  onContentFixed?: () => void;
}

function ReadinessDashboard({
  schemeId,
  refreshKey = 0,
  onContentFixed,
}: ReadinessDashboardProps) {
  const { message: _antdMsg } = App.useApp();
  const msg = useAntdMessageHub(_antdMsg, "审核预检");
  const [loading, setLoading] = useState(false);
  const [overview, setOverview] = useState<ReadinessOverview | null>(null);
  const [runs, setRuns] = useState<PreflightRunItem[]>([]);
  const [sevFilter, setSevFilter] = useState<string>("all");
  const [ruleOpen, setRuleOpen] = useState(false);
  const [rules, setRules] = useState<AuditRule[]>([]);
  const [dims, setDims] = useState<AuditDimension[]>([]);
  const [ruleVersion, setRuleVersion] = useState("");
  const [reporting, setReporting] = useState(false);
  // ✅ BUG 修复（2026-09-21）：正文/目录变更后的「陈旧总检」提示。
  //   旧实现：refreshKey 只在切 Tab 时递增，且 loadRuns 会刷新但 overview 不动
  //   → 用户切到正文生成/目录编辑后回来，界面上仍是**上一次总检**的评分，
  //   既不像"新数据"也不像"明确过期"，容易被误当成当前结论放行。
  //   现做法：记录当前 overview 落库时刻；若运行历史里存在更新的一次总检
  //   （或本地已过期时间戳 > runs 最新版），显示一条 warning 提示"重新总检"。
  const [overviewTs, setOverviewTs] = useState<number | null>(null);
  // ✅ 自动修复弹窗（2026-09-30）：点问题行的「自动修复」打开
  const [fixTarget, setFixTarget] = useState<PreflightFinding | null>(null);
  // ✅ 批量「一键修复全部阻断项」弹窗（2026-10-01）：收集 → 暂存预览 → 逐条接受
  const [batchOpen, setBatchOpen] = useState(false);
  const runsRequestId = useRef(0);
  const overviewRequestId = useRef(0);
  const reportRequestId = useRef(0);

  const loadRuns = useCallback(async () => {
    if (!schemeId) return;
    const requestId = ++runsRequestId.current;
    try {
      const { data } = await complianceApi.runs(schemeId, 10);
      if (requestId === runsRequestId.current) {
        setRuns(data.items || []);
      }
    } catch {
      /* 历史加载失败不阻塞主流程 */
    }
  }, [schemeId]);

  // 同一实例切换方案时立即清空旧结论，并使所有在途请求失效。
  useEffect(() => {
    runsRequestId.current += 1;
    overviewRequestId.current += 1;
    reportRequestId.current += 1;
    setOverview(null);
    setOverviewTs(null);
    setRuns([]);
    setSevFilter("all");
  }, [schemeId]);

  useEffect(() => {
    loadRuns();
  }, [loadRuns, refreshKey]);

  /**
   * ✅ STALE 判定：
   * - overview 为空 → 不算 stale（本就是空态）；
   * - runs[0]（最近一次总检）比 overview 落库时间更新 → 提示。
   *   这覆盖了：SSE 后台正文生成完成后（写入 runs）但用户没切 Tab、或切回 Tab
   *   时 overview 仍是历史那次总检的两种典型场景。
   * - 若 overview 与 runs[0] 时间戳完全一致 → 认为就是同一次总检，不算 stale。
   */
  const isStale = useMemo(() => {
    if (!overview) return false;
    if (overview.stale === true || runs[0]?.stale === true) return true;
    if (!overviewTs || runs.length === 0) return false;
    const newest = runs[0].created_at;
    if (!newest) return false;
    const newestTs = Date.parse(newest.replace(" ", "T"));
    if (Number.isNaN(newestTs)) return false;
    // 允许 5s 误差（同一次总检的两次写库时间戳可能不同）
    return newestTs > overviewTs + 5000;
  }, [overview, overviewTs, runs]);
  const effectiveReleased = Boolean(overview?.released && !isStale);

  const runOverview = useCallback(async (force = false) => {
    if (!schemeId) return;
    const requestId = ++overviewRequestId.current;
    setLoading(true);
    try {
      // ✅ G2：force=true 时跳过服务端缓存强制重算（缓存会在同内容指纹下
      // 直接返回上次结论，避免连点污染分数趋势）
      const { data } = await complianceApi.overview(schemeId, force);
      if (requestId !== overviewRequestId.current) return;
      setOverview(data as ReadinessOverview);
      // 优先用后端落库时间戳；后端未返回时回退到本地时间
      setOverviewTs(Date.parse((data.created_at || "").replace(" ", "T")) || Date.now());
      const c = data.counts || {};
      if (data.blocked) {
        msg.warning(`总检完成：${data.total} 分（${data.grade} 级）— 存在 ${c.block ?? 0} 项交付阻断项，须整改后放行`);
      } else {
        msg.success(`总检完成：${data.total} 分（${data.grade} 级）— ${data.verdict}`);
      }
      // ✅ G2：命中缓存时明确告知用户「本次没有重复落库」，避免出现
      // 「点了几次总检，历史趋势却只多出一条」的困惑
      if (data.cached && !force) {
        msg.info("正文 / 图表与上次总检一致，已直接返回上次结论（未重复计入历史趋势）");
      }
      // ✅ 缺口修复（2026-09-24）：后端 unknown_dimension_count > 0 表示有发现
      //    命中了六维之外的维度（已一律计入「可交付性」）—— 规则库与评分口径
      //    发生漂移的数据质量信号。此前前端完全丢弃该字段，漂移永远无人知晓。
      if (typeof data.unknown_dimension_count === "number" && data.unknown_dimension_count > 0) {
        msg.warning(
          `本次总检有 ${data.unknown_dimension_count} 条发现命中了未登记的评分维度，` +
          `已统一计入「可交付性」——请核对规则库与后端 audit_rules.DIMENSIONS 是否一致`,
        );
      }
      loadRuns();
    } catch (e: any) {
      msg.error(e?.message || "总检失败");
    } finally {
      if (requestId === overviewRequestId.current) setLoading(false);
    }
  }, [schemeId, msg, loadRuns]);

  const openRules = useCallback(async () => {
    setRuleOpen(true);
    if (rules.length) return;
    try {
      const { data } = await complianceApi.rules();
      setRules(data.items || []);
      setDims(data.dimensions || []);
      setRuleVersion(data.rule_version || "");
    } catch (e: any) {
      msg.error(e?.message || "规则目录加载失败");
    }
  }, [rules.length, msg]);

  const exportReport = useCallback(async () => {
    if (!schemeId) return;
    const requestId = ++reportRequestId.current;
    setReporting(true);
    try {
      const { data } = await complianceApi.report(schemeId);
      if (requestId !== reportRequestId.current) return;
      const md: string = data.content || "";
      const name: string = data.filename || "审核预检报告.md";
      // ✅ 编码修复：纯文本/Markdown 下载前置 UTF-8 BOM（\uFEFF）。
      //    否则在 Windows 记事本 / 被当作文本导入 Excel 时中文会乱码。
      const blob = new Blob(["\uFEFF" + md], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      msg.success("整改清单报告已导出");
      // ✅ G3：报告可能基于旧正文 —— 抄进评审意见前必须先重跑总检，
      // 否则按过期结论整改就是白干
      if (data.stale) {
        msg.warning("注意：本次报告基于的正文已发生变更，结论可能已过期，建议先重新总检再导出");
      }
    } catch (e: any) {
      msg.error(e?.message || "报告导出失败");
    } finally {
      if (requestId === reportRequestId.current) setReporting(false);
    }
  }, [schemeId, msg]);

  const filtered = useMemo(() => {
    const list = overview?.findings || [];
    if (sevFilter === "all") return list;
    if (sevFilter === "block_high") {
      return list.filter((f) => f.severity === "block" || f.severity === "high");
    }
    return list.filter((f) => f.severity === sevFilter);
  }, [overview, sevFilter]);

  // ✅ 批量修复入口（2026-10-01）：仅当有「阻断 + 可自动修复」项时开放按钮
  const blockingFixable = useMemo(
    () => (overview?.findings || []).filter(
      (f) => f.severity === "block" && (f.autofix as PreflightFinding["autofix"])?.fixable),
    [overview],
  );

  const trend = useMemo(() => {
    if (runs.length < 2) return null;
    const cur = runs[0].total;
    const prev = runs[1].total;
    return cur - prev;
  }, [runs]);

  return (
    <Card
      size="small"
      title={
        <Space>
          <SafetyCertificateOutlined style={{ color: "#1677ff" }} />
          <Text strong>交付就绪度总检</Text>
          {overview && (
            <Tag color={effectiveReleased ? "green" : "red"}>
              {effectiveReleased ? "建议放行" : "暂不放行"}
            </Tag>
          )}
        </Space>
      }
      extra={
        <Space size={4} wrap>
          <Button size="small" onClick={openRules}>规则说明</Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            loading={reporting}
            disabled={!overview && runs.length === 0}
            onClick={exportReport}
          >
            导出整改清单
          </Button>
          {blockingFixable.length > 0 && (
            <Button
              size="small"
              type="primary"
              danger
              icon={<ThunderboltOutlined />}
              onClick={() => setBatchOpen(true)}
            >
              一键修复全部阻断项（{blockingFixable.length}）
            </Button>
          )}
          <Button
            size="small"
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={loading}
            // ✅ G2：注意这里不能直接传 onClick={runOverview} —— antd 会把
            // MouseEvent 当成 force 实参传进来，导致每次都强制重算
            onClick={() => runOverview(false)}
          >
            一键总检
          </Button>
        </Space>
      }
      style={{ marginBottom: 12 }}
    >
      <Text type="secondary" style={{ fontSize: 12 }}>
        聚合「程序化规则预检 + 导出预检 + AI 规范符合性 + 一致性审计 + 专家论证预检」，按内容完整性、
        规范符合性、安全措施有效性、全文一致性、可追溯性、可交付性六个维度加权评分，
        给出可否交付的明确结论。规则依据住建部令第37号、建办质〔2018〕31号及现行工程建设标准。
      </Text>

      {loading && (
        <div style={{ textAlign: "center", padding: "24px 0" }}>
          <Spin />
          <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
            正在扫描全部章节：结构完整性、标准时效性、计算书、查重、控制字符…
          </div>
        </div>
      )}

      {!loading && !overview && (
        <div style={{ marginTop: 12 }}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <span style={{ fontSize: 12, color: "#999" }}>
                尚未总检。点击右上角「一键总检」获取综合评分与放行结论。
                {runs.length > 0 ? "（可点击「导出整改清单」获取历史报告）" : ""}
              </span>
            }
          />
        </div>
      )}

      {!loading && overview && (
        <>
          {/* ===== 总分 + 结论 ===== */}
          <Row gutter={[16, 12]} align="middle" style={{ marginTop: 12 }}>
            <Col flex="none">
              <Progress
                type="dashboard"
                percent={Math.round(overview.total)}
                strokeColor={scoreColor(overview.total)}
                size={132}
                format={(p) => (
                  <span>
                    <span style={{ fontSize: 26, fontWeight: 700 }}>{p}</span>
                    <span style={{ fontSize: 12, color: "#999" }}> 分</span>
                  </span>
                )}
              />
            </Col>
            <Col flex="auto">
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Space wrap>
                  <Tag
                    color="transparent"
                    style={{
                      color: "#fff", fontWeight: 700, fontSize: 14,
                      background: GRADE_COLOR[overview.grade] || "#999",
                      border: "none", padding: "1px 10px",
                    }}
                  >
                    {overview.grade} 级
                  </Tag>
                  <Text strong style={{ fontSize: 14 }}>{overview.verdict}</Text>
                  {trend !== null && (
                    <Tooltip title="与上一次总检相比">
                      <Tag color={trend > 0 ? "green" : trend < 0 ? "red" : "default"}>
                        {trend > 0 ? `↑ +${trend.toFixed(1)}` : trend < 0 ? `↓ ${trend.toFixed(1)}` : "持平"}
                      </Tag>
                    </Tooltip>
                  )}
                </Space>
                <Space size={4} wrap>
                  {(overview.sources || []).map((s) => (
                    <Tag key={s} style={{ marginRight: 0, fontSize: 11 }}>
                      {SOURCE_LABEL[s] || s}
                      {/* ✅ 数据链补齐（2026-09-23）：后端 G1 已在 stats 单列
                          export_issue_count（"这分里有几项来自导出预检"），
                          此前前端未消费，现挂到来源标签上显式告知。 */}
                      {s === "export_check" && (overview.stats?.export_issue_count ?? 0) > 0
                        ? ` ${overview.stats!.export_issue_count} 项` : ""}
                    </Tag>
                  ))}
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    规则版本 {overview.rule_version}
                  </Text>
                </Space>
                {overview.weakest && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    最弱维度：
                    <Text strong>
                      {overview.dimensions.find((d) => d.key === overview.weakest)?.label
                        || overview.weakest}
                    </Text>
                    —— 优先整改该维度收效最大
                  </Text>
                )}
              </Space>
            </Col>
          </Row>

          {/* ===== 阻断项 ===== */}
          {(overview.blockers || []).length > 0 && (
            <Alert
              type="error"
              showIcon
              icon={<CloseCircleOutlined />}
              style={{ marginTop: 12 }}
              message={`存在 ${(overview.blockers || []).length} 项交付阻断项，不建议进入论证 / 交付环节`}
              description={
                <List
                  size="small"
                  dataSource={overview.blockers}
                  renderItem={(b) => (
                    <List.Item style={{ padding: "4px 0", border: "none" }}>
                      <Space direction="vertical" size={0} style={{ width: "100%" }}>
                        <Space size={4} wrap>
                          <Tag color="volcano" style={{ marginRight: 0 }}>{b.rule_id}</Tag>
                          <Text strong style={{ fontSize: 12 }}>{b.title}</Text>
                          {b.section_title && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              @{b.section_title}
                            </Text>
                          )}
                        </Space>
                        <Text style={{ fontSize: 12 }}>{b.detail}</Text>
                        {b.suggestion && (
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            整改建议：{b.suggestion}
                          </Text>
                        )}
                      </Space>
                    </List.Item>
                  )}
                />
              }
            />
          )}

          {/* ===== 六维得分 ===== */}
          <div style={{ marginTop: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>维度得分（括号内为权重）</Text>
            <Row gutter={[8, 8]} style={{ marginTop: 6 }}>
              {(overview.dimensions || []).map((d) => (
                <Col span={8} key={d.key}>
                  <Tooltip title={`${d.label}：扣 ${d.penalty} 分，命中 ${d.issue_count} 个问题`}>
                    <div
                      style={{
                        border: "1px solid #f0f0f0", borderRadius: 6,
                        padding: "6px 8px", background: "#fafafa",
                      }}
                    >
                      <div style={{ fontSize: 11, color: "#888" }}>
                        {d.label}
                        <span style={{ color: "#bbb" }}>（{d.weight}）</span>
                        {d.block_count > 0 && (
                          <Tag color="volcano" style={{ marginLeft: 4, fontSize: 10 }}>
                            阻断 {d.block_count}
                          </Tag>
                        )}
                      </div>
                      <div style={{ fontSize: 16, fontWeight: 600, color: scoreColor(d.score) }}>
                        {d.score}
                      </div>
                      <Progress
                        percent={d.score}
                        showInfo={false}
                        size="small"
                        strokeColor={SCORE_COLOR[d.key] || "#1677ff"}
                      />
                    </div>
                  </Tooltip>
                </Col>
              ))}
            </Row>
          </div>

          {/* ===== 客观统计 ===== */}
          {overview.stats && (
            <Row gutter={[12, 8]} style={{ marginTop: 12 }}>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>章节</div>
                <div style={{ fontSize: 15, fontWeight: 600 }}>
                  {overview.stats.generated_count}/{overview.stats.section_count}
                </div>
              </Col>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>总字数</div>
                <div style={{ fontSize: 15, fontWeight: 600 }}>
                  {(overview.stats.total_words || 0).toLocaleString()}
                </div>
              </Col>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>空章节占比</div>
                <div style={{
                  fontSize: 15, fontWeight: 600,
                  color: (overview.stats.empty_ratio || 0) > 20 ? "#E8836B" : "#52c41a",
                }}>
                  {overview.stats.empty_ratio || 0}%
                </div>
              </Col>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>图表完成</div>
                <div style={{
                  fontSize: 15, fontWeight: 600,
                  color: (overview.stats.chart_total || 0) > 0
                    && (overview.stats.chart_done || 0) < (overview.stats.chart_total || 0)
                    ? "#E8836B" : "#52c41a",
                }}>
                  {overview.stats.chart_done || 0}/{overview.stats.chart_total || 0}
                </div>
              </Col>
            </Row>
          )}

          {/* ===== 发现清单 ===== */}
          {/* ✅ BUG 修复（2026-09-21）：陈旧总检提示。
              正文/目录在其它 Tab 修改、或后台 SSE 生成完成写入 runs 后，
              用户界面上看到的仍是上一次的评分——容易被误当作当前结论放行。
              这里显式提示 + 一键重跑，避免静默过期。 */}
          {isStale && (
            <Alert
              type="warning"
              showIcon
              icon={<ExclamationCircleOutlined />}
              style={{ marginTop: 12 }}
              message={
                <Space wrap>
                  <Text style={{ fontSize: 12 }}>
                    {overview.stale === true || runs[0]?.stale === true
                      ? "本次总检对应的正文或图表已发生变化，当前评分已失效。"
                      : "本次总检之后已有更新的运行记录，评分可能已过期。"}
                  </Text>
                  <Button
                    size="small"
                    type="primary"
                    icon={<ReloadOutlined />}
                    loading={loading}
                    // ✅ G2：过期即内容已变 → 强制重算（跳过服务端缓存）
                    onClick={() => runOverview(true)}
                  >
                    重新总检
                  </Button>
                </Space>
              }
            />
          )}
          {(overview.findings || []).length > 0 && (
            <div style={{ marginTop: 12 }}>
              <Space style={{ marginBottom: 6 }} wrap>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  问题清单（{(overview.findings || []).length}）
                </Text>
                <Segmented
                  size="small"
                  value={sevFilter}
                  onChange={(v) => setSevFilter(String(v))}
                  options={[
                    { label: "全部", value: "all" },
                    { label: "阻断+严重", value: "block_high" },
                    { label: "一般", value: "medium" },
                    { label: "提示", value: "low" },
                  ]}
                />
              </Space>
              <Table<PreflightFinding>
                size="small"
                rowKey={(r) => [r.rule_id || "f", r.section_id || "", r.title || "", r.detail || ""].join("|")}
                pagination={filtered.length > 10 ? { pageSize: 10, size: "small" } : false}
                // ✅ 性能优化：虚拟滚动。表体固定 320px，不开 virtual 时视口外的
                // 行也会全量建 DOM 并参与 layout/paint，长列表滚动明显掉帧。
                scroll={{ y: 320 }}
                virtual
                dataSource={filtered}
                expandable={{
                  expandedRowRender: (r) => (
                    <div style={{ fontSize: 12 }}>
                      {r.evidence?.length > 0 && (
                        <div style={{ marginBottom: 4 }}>
                          <Text type="secondary">证据：</Text>
                          {r.evidence.map((e, i) => (
                            <Tag key={i} color="orange" style={{ marginBottom: 2 }}>{e}</Tag>
                          ))}
                        </div>
                      )}
                      <div style={{ marginBottom: 4 }}>
                        <Text type="secondary">整改建议：</Text>
                        {r.suggestion || "—"}
                      </div>
                      {r.basis && (
                        <div>
                          <Text type="secondary">行业依据：</Text>
                          {r.basis}
                        </div>
                      )}
                    </div>
                  ),
                  rowExpandable: (r) => Boolean(
                    r.evidence?.length || r.suggestion || r.basis),
                }}
                columns={[
                  {
                    title: "严重度", dataIndex: "severity", width: 76,
                    sorter: (a, b) => (SEVERITY_WEIGHT[b.severity] ?? 0) - (SEVERITY_WEIGHT[a.severity] ?? 0),
                    // ✅ 语义确认（2026-09-21）：sorter 用 `b - a`，返回负值 → a 排前。
                    //    与 defaultSortOrder:'ascend' 配合：权重大的先返回负值 → a 排前，
                    //    即「阻断」先、「提示」后——符合「阻断项置顶」的产品语义。
                    //    此排序**正确**，此处仅补注释说明避免后续被误改。
                    defaultSortOrder: "ascend",
                    render: (s: Severity) => (
                      <Tag color={SEVERITY_COLOR[s] || "default"} style={{ marginRight: 0 }}>
                        {SEVERITY_LABEL[s] || s}
                      </Tag>
                    ),
                  },
                  {
                    title: "规则", dataIndex: "rule_id", width: 78,
                    render: (v: string) => v
                      ? <Text code style={{ fontSize: 11 }}>{v}</Text>
                      : "—",
                  },
                  {
                    title: "问题", dataIndex: "title", width: 160, ellipsis: true,
                    render: (v: string, r) => (
                      <Tooltip title={r.detail}>{v}</Tooltip>
                    ),
                  },
                  {
                    title: "说明", dataIndex: "detail", ellipsis: true,
                    render: (v: string) => v || "—",
                  },
                  {
                    title: "来源", dataIndex: "mode", width: 68,
                    render: (m: string) => (
                      <Tag style={{ marginRight: 0, fontSize: 11 }}>
                        {m === "ai" ? "AI" : "规则"}
                      </Tag>
                    ),
                  },
                  {
                    // ✅ 自动修复入口（2026-09-30）：可用性完全取自后端
                    //    `finding.autofix`（能力表唯一事实源），前端不自行猜测。
                    title: "修复", key: "autofix", width: 92,
                    render: (_: unknown, r: PreflightFinding) => {
                      const cap = r.autofix;
                      if (!cap) {
                        return (
                          <Text type="secondary" style={{ fontSize: 11 }}>—</Text>
                        );
                      }
                      if (!cap.fixable) {
                        // 不支持自动修复：把「去哪处理」讲清楚（悬停可见）
                        return (
                          <Tooltip title={cap.reason || "该问题需人工处理"}>
                            <Tag style={{ marginRight: 0, fontSize: 11 }}>需人工</Tag>
                          </Tooltip>
                        );
                      }
                      return (
                        <Tooltip
                          title={cap.mode === "auto"
                            ? "程序化确定性修复，不调用 AI"
                            : "定位矛盾位置后调用 AI 做最小必要修改"}
                        >
                          <Button
                            size="small"
                            type="link"
                            icon={<ThunderboltOutlined />}
                            onClick={() => setFixTarget(r)}
                          >
                            自动修复
                          </Button>
                        </Tooltip>
                      );
                    },
                  },
                ]}
              />
            </div>
          )}

          {(overview.findings || []).length === 0 && (
            <Alert
              type="success"
              showIcon
              icon={<CheckCircleOutlined />}
              style={{ marginTop: 12 }}
              message="未检出任何问题，方案已具备交付条件"
            />
          )}
        </>
      )}

      {/* ===== 历史趋势 ===== */}
      {runs.length > 1 && (
        <div style={{ marginTop: 12 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            历史总检（最近 {Math.min(runs.length, 5)} 次）
          </Text>
          <Space size={4} wrap style={{ marginTop: 4 }}>
            {runs.slice(0, 5).map((r, i) => (
              <Tooltip
                key={r.id}
                title={`${r.created_at?.replace("T", " ").slice(0, 19)} · ${r.verdict}${r.stale ? "（正文已变更，结论已过期）" : ""}`}
              >
                <Tag
                  // ✅ G3：过期记录用 volcano 醒目标注，避免拿旧高分误判
                  color={r.blocked || r.stale ? "volcano" : scoreColor(r.total) === "#52c41a" ? "green" : "blue"}
                  style={{ marginRight: 0 }}
                >
                  {i === 0 ? "本次" : `#${i}`} {r.total}（{r.grade}）
                  {r.stale ? " 已过期" : ""}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        </div>
      )}

      {/* ===== 规则目录抽屉 ===== */}
      <Drawer
        title="审核规则说明"
        width={720}
        open={ruleOpen}
        onClose={() => setRuleOpen(false)}
      >
        <Paragraph type="secondary" style={{ fontSize: 12 }}>
          规则依据《危险性较大的分部分项工程安全管理规定》（住建部令第37号）、
          《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》
          （建办质〔2018〕31号）及现行工程建设标准制定，规则版本 {ruleVersion || "—"}。
        </Paragraph>
        {dims.map((d) => (
          <div key={d.key} style={{ marginBottom: 16 }}>
            <Space size={6} wrap>
              <Text strong>{d.label}</Text>
              <Tag color="blue" style={{ marginRight: 0 }}>权重 {d.weight}</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>{d.desc}</Text>
            </Space>
            <Table<AuditRule>
              size="small"
              style={{ marginTop: 6 }}
              rowKey="rule_id"
              pagination={false}
              dataSource={rules.filter((r) => r.dimension === d.key)}
              columns={[
                {
                  title: "编号", dataIndex: "rule_id", width: 74,
                  render: (v: string) => <Text code style={{ fontSize: 11 }}>{v}</Text>,
                },
                {
                  title: "规则", dataIndex: "title", width: 150,
                  render: (v: string, r) => (
                    <Tooltip title={r.detail}>
                      <Text style={{ fontSize: 12 }}>{v}</Text>
                    </Tooltip>
                  ),
                },
                {
                  title: "严重度", dataIndex: "severity", width: 72,
                  render: (s: Severity) => (
                    <Tag color={SEVERITY_COLOR[s] || "default"} style={{ marginRight: 0 }}>
                      {SEVERITY_LABEL[s] || s}
                    </Tag>
                  ),
                },
                {
                  title: "判定", dataIndex: "mode", width: 60,
                  render: (m: string) => (
                    <Tag style={{ marginRight: 0, fontSize: 11 }}>
                      {m === "ai" ? "AI" : "规则"}
                    </Tag>
                  ),
                },
                {
                  title: "行业依据", dataIndex: "basis", ellipsis: true,
                  render: (v: string) => (
                    <Text type="secondary" style={{ fontSize: 11 }}>{v || "—"}</Text>
                  ),
                },
              ]}
            />
          </div>
        ))}
        {rules.length === 0 && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="规则目录加载中…" />
        )}
      </Drawer>

      {/* ===== 自动修复弹窗（定位 → AI 改写 → 对比 / 回滚） ===== */}
      <AutoFixModal
        schemeId={schemeId}
        finding={fixTarget}
        open={!!fixTarget}
        onClose={() => setFixTarget(null)}
        onFixed={() => {
          // 正文已改写 → 宿主页刷新正文/目录 + 本组件强制重算总检
          // （服务端内容指纹已变，不 force 会命中旧结论缓存）
          onContentFixed?.();
          void runOverview(true);
        }}
      />

      {/* ===== 批量「一键修复全部阻断项」弹窗（收集 → 暂存预览 → 逐条接受） ===== */}
      <BatchFixModal
        schemeId={schemeId}
        open={batchOpen}
        onClose={() => setBatchOpen(false)}
        onFixed={() => {
          // 正文已改写 → 宿主页刷新正文/目录 + 本组件强制重算总检
          onContentFixed?.();
          void runOverview(true);
        }}
      />
    </Card>
  );
}

// ✅ 性能优化：默认导出用 memo 包裹。宿主页（方案工作台）在目录/正文生成的 SSE
//    期间 progress 每帧变化会重渲整棵树，而本组件 props 仅为 {schemeId, refreshKey}
//    两个原始值 —— memo 后可完整跳过 700 行的评分卡 + 问题清单 Table 的渲染，
//    这是「审核预检列表」滚动/交互卡顿的根因之一。仅在真正需要刷新时由宿主页
//    递增 refreshKey 触发（切入 Tab 或外部数据变更）。
export default memo(ReadinessDashboard);

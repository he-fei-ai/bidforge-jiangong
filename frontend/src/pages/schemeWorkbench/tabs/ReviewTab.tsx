import type { Dispatch, SetStateAction } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Divider,
  Input,
  Progress,
  Row,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  AuditOutlined,
  CheckCircleOutlined,
  FileSearchOutlined,
  ReloadOutlined,
  SafetyOutlined,
  StopOutlined,
} from "@ant-design/icons";
import ReadinessDashboard from "../../../components/review/ReadinessDashboard";
import ReviewWorkflowPanel from "../../../components/review/ReviewWorkflowPanel";
import PlaceholderReportPanel from "../../../components/PlaceholderReportPanel";
import { NumberingConsistencyPanel } from "../NumberingConsistencyPanel";
import { DEFAULT_COMPLIANCE_CHECKLIST } from "../reviewShared";
import { EXPORT_ISSUE_LABEL, describeExportIssue } from "../exportShared";
import {
  SEVERITY_COLOR,
  SEVERITY_LABEL,
  type AuditRule,
  type Severity,
} from "../../../types/audit";
import type { ExportIssueGate, QualityCheckResult, ComplianceHistoryItem } from "../workbenchTypes";
import type { ExportStats, ExportPreflight, ExportRerunPlan } from "../useExportPresets";
import type {
  ComplianceResultItem,
  ConsistencyAuditResult,
  ExpertReviewResult,
} from "../useReviewAudit";
import type { PlaceholderReport } from "../../../utils/placeholderReport";

const { Text } = Typography;

/** 「审核与预检」Tab（Tab 5）面板：页面级状态 / handler / setter 全部显式注入。 */
export interface ReviewTabProps {
  id: string | undefined;
  load: () => Promise<void>;
  reviewTick: number;
  setReviewTick: Dispatch<SetStateAction<number>>;
  // 规范符合性检查
  checklistText: string;
  setChecklistText: Dispatch<SetStateAction<string>>;
  setChecklistDirty: (v: boolean) => void;
  aiRules: AuditRule[];
  checkingCompliance: boolean;
  complianceResults: ComplianceResultItem[];
  setComplianceResults: Dispatch<SetStateAction<ComplianceResultItem[]>>;
  complianceHistory: ComplianceHistoryItem[];
  historyLoading: boolean;
  loadComplianceHistory: () => Promise<void>;
  handleComplianceCheck: () => Promise<void>;
  handleCancelComplianceCheck: () => void;
  // 全文一致性审计
  consistencyResult: ConsistencyAuditResult | null;
  auditRunning: boolean;
  loadConsistencyLatest: () => Promise<void>;
  handleConsistencyAudit: () => Promise<void>;
  handleCancelConsistencyAudit: () => void;
  // 专家论证预检
  expertResult: ExpertReviewResult | null;
  expertItems: string[];
  expertRunning: boolean;
  handleExpertReview: () => Promise<void>;
  handleCancelExpertReview: () => void;
  // 导出预检
  exportIssues: ExportIssueGate[];
  setExportIssues: Dispatch<SetStateAction<ExportIssueGate[]>>;
  exportStats: ExportStats | null;
  setExportStats: Dispatch<SetStateAction<ExportStats | null>>;
  exportPreflight: ExportPreflight | null;
  setExportPreflight: Dispatch<SetStateAction<ExportPreflight | null>>;
  placeholderReport: PlaceholderReport | null;
  setPlaceholderReport: Dispatch<SetStateAction<PlaceholderReport | null>>;
  rerunPlan: ExportRerunPlan | null;
  setRerunPlan: Dispatch<SetStateAction<ExportRerunPlan | null>>;
  checkingExport: boolean;
  exportCheckIncomplete: boolean;
  handleExportCheck: () => Promise<void>;
  handlePlaceholderJump: (sectionId: string) => void;
  rerunning: boolean;
  handleRerunAffected: () => Promise<void>;
  // 正文质量自检
  qualityResult: QualityCheckResult | null;
  setQualityResult: Dispatch<SetStateAction<QualityCheckResult | null>>;
  checkingQuality: boolean;
  handleQualityCheck: () => Promise<void>;
}

export function ReviewTab({
  id,
  load,
  reviewTick,
  setReviewTick,
  checklistText,
  setChecklistText,
  setChecklistDirty,
  aiRules,
  checkingCompliance,
  complianceResults,
  setComplianceResults,
  complianceHistory,
  historyLoading,
  loadComplianceHistory,
  handleComplianceCheck,
  handleCancelComplianceCheck,
  consistencyResult,
  auditRunning,
  loadConsistencyLatest,
  handleConsistencyAudit,
  handleCancelConsistencyAudit,
  expertResult,
  expertItems,
  expertRunning,
  handleExpertReview,
  handleCancelExpertReview,
  exportIssues,
  setExportIssues,
  exportStats,
  setExportStats,
  exportPreflight,
  setExportPreflight,
  placeholderReport,
  setPlaceholderReport,
  rerunPlan,
  setRerunPlan,
  checkingExport,
  exportCheckIncomplete,
  handleExportCheck,
  handlePlaceholderJump,
  rerunning,
  handleRerunAffected,
  qualityResult,
  setQualityResult,
  checkingQuality,
  handleQualityCheck,
}: ReviewTabProps) {
  return (
    // ✅ 修复：审核与预检内容（两张大卡片 + 检查结果表格 + 历史记录）较长，
    //    容器必须可滚动，避免低视口下底部内容被裁剪
    <div
      className="scroll-area"
      style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}
    >
      {/* ===== ✅ 交付就绪度总检（六维加权评分 + 阻断项 + 放行结论）=====
               放在最上方：用户进入本页首先要的是"能不能交付"这一个答案，
               下面的分项检查是拿到答案后的下钻入口。 */}
      <ReadinessDashboard
        schemeId={id || ""}
        refreshKey={reviewTick}
        onContentFixed={() => {
          load();
          setReviewTick((v) => v + 1);
        }}
        onLocateSection={handlePlaceholderJump}
      />

      {/* ✅ R174 F1（2026-11-03）：编号一致性检查/修复/版本/回滚面板。
              后端 4 端点 + api 封装早已存在但界面零入口（审计 R173 · D1/F1），
              现接线于审核 Tab —— 导出前可发现并修复「正文编号与目录漂移」。 */}
      <NumberingConsistencyPanel schemeId={id || ""} />

      {/* ===== ✅ 规范符合性检查（complianceApi.check）===== */}
      <Card
        size="small"
        title={
          <Space>
            <SafetyOutlined />
            <Text strong>规范符合性检查（AI 逐条判定）</Text>
            <Tag color="blue">
              {checklistText.split("\n").filter((s) => s.trim()).length} 项清单
            </Tag>
          </Space>
        }
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={loadComplianceHistory}
            loading={historyLoading}
          >
            刷新历史
          </Button>
        }
        style={{ marginBottom: 12 }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          每行一条检查项，AI 会逐条扫描方案正文并给出命中/缺失、风险等级与修改建议（结果自动落库）
        </Text>
        <Input.TextArea
          rows={4}
          value={checklistText}
          onChange={(e) => {
            setChecklistText(e.target.value);
            setChecklistDirty(true);
          }}
          placeholder="每行一条检查项"
          style={{ marginTop: 8, fontFamily: "Consolas, monospace", fontSize: 12 }}
        />
        <Space style={{ marginTop: 8 }} wrap>
          <Button
            type="primary"
            icon={<SafetyOutlined />}
            loading={checkingCompliance}
            onClick={handleComplianceCheck}
          >
            开始合规检查
          </Button>
          {/* ✅ R106 FIX-5：运行中给出取消入口 */}
          {checkingCompliance && (
            <Button danger icon={<StopOutlined />} onClick={handleCancelComplianceCheck}>
              取消检查
            </Button>
          )}
          <Button
            onClick={() => {
              setChecklistText(
                aiRules.length
                  ? aiRules.map((r) => r.title).join("\n")
                  : DEFAULT_COMPLIANCE_CHECKLIST.join("\n"),
              );
              setChecklistDirty(false);
            }}
            disabled={checkingCompliance}
          >
            恢复规则库清单
          </Button>
          {complianceResults.length > 0 && (
            <Button onClick={() => setComplianceResults([])}>清除本次结果</Button>
          )}
        </Space>

        {complianceResults.length > 0 && (
          <Table
            size="small"
            rowKey={(r, i) => `${r.rule_id || "r"}-${i}`}
            style={{ marginTop: 12 }}
            pagination={false}
            // ✅ 性能优化：虚拟滚动（pagination=false + 固定表体，
            // 全量渲染会把视口外行也建成 DOM）
            scroll={{ y: 320 }}
            virtual
            dataSource={complianceResults}
            columns={[
              {
                title: "判定",
                dataIndex: "hit",
                width: 80,
                render: (hit) => (
                  <Tag color={hit === true ? "green" : hit === false ? "red" : "default"}>
                    {hit === true ? "命中" : hit === false ? "缺失" : "疑似"}
                  </Tag>
                ),
              },
              {
                title: "风险",
                dataIndex: "severity",
                width: 80,
                // ✅ BUG 修复（2026-09-23）：旧实现只处理 high/medium，
                //    AI 命中 block（阻断）时显示成蓝色与 low 无法区分，
                //    且展示英文原词。现统一用规则库的严重度色板/中文标签。
                render: (s: string) =>
                  s ? (
                    <Tag color={SEVERITY_COLOR[s as Severity] ?? "default"}>
                      {SEVERITY_LABEL[s as Severity] ?? s}
                    </Tag>
                  ) : (
                    "-"
                  ),
              },
              { title: "检查项", dataIndex: "item", ellipsis: true },
              {
                title: "命中位置/证据",
                dataIndex: "evidence",
                ellipsis: true,
                render: (v: string) => v || "—",
              },
              {
                title: "修改建议",
                dataIndex: "suggestion",
                ellipsis: true,
                render: (v: string) => v || "—",
              },
            ]}
          />
        )}

        {complianceHistory.length > 0 && (
          <Collapse
            ghost
            style={{ marginTop: 8 }}
            items={[
              {
                key: "history",
                label: <Text type="secondary">历史检查记录（{complianceHistory.length} 条）</Text>,
                children: (
                  <Table
                    size="small"
                    rowKey="id"
                    pagination={{ pageSize: 10, size: "small" }}
                    dataSource={complianceHistory}
                    columns={[
                      {
                        title: "时间",
                        dataIndex: "created_at",
                        width: 170,
                        render: (v: string) => (v || "").replace("T", " ").slice(0, 19),
                      },
                      {
                        title: "规则",
                        dataIndex: "rule_id",
                        width: 80,
                        render: (v: string) => v || "—",
                      },
                      {
                        title: "检查项",
                        dataIndex: "item",
                        ellipsis: true,
                        render: (v: string) => v || "—",
                      },
                      {
                        title: "风险",
                        dataIndex: "severity",
                        width: 80,
                        // ✅ BUG 修复（2026-09-23）：同上，历史表补 block/low 色与中文标签
                        render: (s: string) =>
                          s ? (
                            <Tag color={SEVERITY_COLOR[s as Severity] ?? "default"}>
                              {SEVERITY_LABEL[s as Severity] ?? s}
                            </Tag>
                          ) : (
                            "—"
                          ),
                      },
                      {
                        title: "建议",
                        dataIndex: "suggestion",
                        ellipsis: true,
                        render: (v: string) => v || "—",
                      },
                    ]}
                  />
                ),
              },
            ]}
          />
        )}
      </Card>

      <Divider style={{ margin: "8px 0 16px" }} />

      {/* ===== ✅ 全文一致性审计（consistency_audit 表，§3.12.1）===== */}
      <Card
        size="small"
        title={
          <Space>
            <FileSearchOutlined />
            <Text strong>全文一致性审计（事实 vs 正文）</Text>
            {consistencyResult && (
              <Tag
                color={
                  consistencyResult.score >= 90
                    ? "green"
                    : consistencyResult.score >= 70
                      ? "orange"
                      : "red"
                }
              >
                评分 {consistencyResult.score}
              </Tag>
            )}
          </Space>
        }
        extra={
          <Space>
            <Button size="small" onClick={loadConsistencyLatest}>
              载入上次结果
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<FileSearchOutlined />}
              loading={auditRunning}
              onClick={handleConsistencyAudit}
            >
              开始审计
            </Button>
            {/* ✅ R106 FIX-5：运行中给出取消入口 */}
            {auditRunning && (
              <Button
                size="small"
                danger
                icon={<StopOutlined />}
                onClick={handleCancelConsistencyAudit}
              >
                取消
              </Button>
            )}
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          将「全局事实」（唯一可信数据源）与正文逐项比对：数值 / 单位 / 时间 / 术语名称 /
          章节间逻辑。需先完成「事实」页的全局事实提取。
          {consistencyResult?.created_at
            ? `（上次审计：${consistencyResult.created_at.replace("T", " ").slice(0, 19)}）`
            : ""}
        </Text>
        {auditRunning && (
          <div style={{ marginTop: 8 }}>
            <Progress percent={70} status="active" showInfo={false} size="small" />
            <Text type="secondary" style={{ fontSize: 12 }}>
              AI 正在逐项比对正文与项目事实，长方案约需 1-3 分钟…
            </Text>
          </div>
        )}
        {consistencyResult &&
          !auditRunning &&
          (consistencyResult.issues.length === 0 ? (
            <div style={{ marginTop: 8 }}>
              <Tag color="green">未发现不一致项</Tag>
            </div>
          ) : (
            <Table
              size="small"
              style={{ marginTop: 8 }}
              rowKey={(_, i) => String(i)}
              pagination={{ pageSize: 8, size: "small" }}
              dataSource={consistencyResult.issues}
              columns={[
                {
                  title: "风险",
                  dataIndex: "severity",
                  width: 80,
                  render: (s: string) => (
                    <Tag color={s === "high" ? "red" : s === "medium" ? "orange" : "blue"}>
                      {s || "-"}
                    </Tag>
                  ),
                },
                {
                  title: "维度",
                  dataIndex: "dimension",
                  width: 110,
                  render: (v: string) => v || "—",
                },
                {
                  title: "所在章节",
                  dataIndex: "section_title",
                  width: 140,
                  ellipsis: true,
                  render: (v: string) => v || "—",
                },
                {
                  title: "事实值",
                  dataIndex: "fact",
                  ellipsis: true,
                  render: (v: string) => v || "—",
                },
                {
                  title: "正文引用",
                  dataIndex: "content_quote",
                  ellipsis: true,
                  render: (v: string) => v || "—",
                },
                {
                  title: "修改建议",
                  dataIndex: "suggestion",
                  ellipsis: true,
                  render: (v: string) => v || "—",
                },
              ]}
            />
          ))}
      </Card>

      <Divider style={{ margin: "8px 0 16px" }} />

      <Card
        size="small"
        title={
          <Space>
            <AuditOutlined />
            <Text strong>专家论证预检（危大工程）</Text>
          </Space>
        }
        extra={
          <Space>
            <Button
              icon={<AuditOutlined />}
              loading={expertRunning}
              disabled={expertRunning}
              onClick={handleExpertReview}
            >
              开始预检
            </Button>
            {/* ✅ R106 FIX-5：运行中给出取消入口 */}
            {expertRunning && (
              <Button
                size="small"
                danger
                icon={<StopOutlined />}
                onClick={handleCancelExpertReview}
              >
                取消
              </Button>
            )}
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          面向需专项论证的危大工程，按论证必要项清单检查方案就绪度并给出缺失项与补充建议。
          与「导出」页的「导出预检」（检查章节完整性）用途不同。
        </Text>
      </Card>
      {expertItems.length > 0 && (
        <Card title="论证必要项清单" size="small" style={{ marginBottom: 12 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {expertItems.map((item: string) => {
              const ready = expertResult?.ready?.includes(item);
              const missing = expertResult?.missing?.includes(item);
              return (
                <Tag
                  key={item}
                  color={ready ? "green" : missing ? "red" : "default"}
                  style={{ margin: 0 }}
                >
                  {item}
                  {ready ? " ✓" : missing ? " ✗" : ""}
                </Tag>
              );
            })}
          </div>
        </Card>
      )}
      {expertResult && (
        <Card title="预检结果" size="small">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="就绪度评分">
              <Tag
                color={
                  expertResult.score! >= 80 ? "green" : expertResult.score! >= 60 ? "orange" : "red"
                }
              >
                {expertResult.score}
              </Tag>
            </Descriptions.Item>
            {(expertResult.missing?.length ?? 0) > 0 && (
              <Descriptions.Item label="缺失项">
                {expertResult.missing!.join("、")}
              </Descriptions.Item>
            )}
            {(expertResult.suggestions?.length ?? 0) > 0 && (
              <Descriptions.Item label="补充建议">
                {expertResult.suggestions!.join("；")}
              </Descriptions.Item>
            )}
          </Descriptions>
        </Card>
      )}

      <Divider style={{ margin: "8px 0 16px" }} />

      {/* ✅ 功能归位：导出预检本质是"预检"，与规范符合性检查 / 专家论证预检同属
              「审核与预检」这一步，统一收敛到本页；「导出」页只负责生成文档。
              旧实现把它放在导出页，导致"两个预检分居两页"，用户不知道该在哪一层做检查。 */}
      <Card
        size="small"
        title={
          <Space>
            <SafetyOutlined />
            <Text strong>导出预检（章节完整性）</Text>
            {exportIssues.length > 0 && <Tag color="orange">{exportIssues.length} 个问题</Tag>}
          </Space>
        }
        extra={
          <Space size={4}>
            {(exportStats || exportIssues.length > 0) && (
              <Button
                size="small"
                type="link"
                onClick={() => {
                  setExportStats(null);
                  setExportIssues([]);
                  setExportPreflight(null);
                  setPlaceholderReport(null);
                  setRerunPlan(null);
                }}
              >
                清除结果
              </Button>
            )}
            <Button
              size="small"
              type="primary"
              icon={<SafetyOutlined />}
              loading={checkingExport}
              onClick={handleExportCheck}
            >
              开始预检
            </Button>
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          检查空章节、孤立节点、章节字数达标度与图表完成率，确认无误后再到「导出」页生成 DOCX /
          PDF。 本项与上方「规范符合性检查」（AI 逐条判定内容是否合规）用途不同。
        </Text>
        {exportIssues.length > 0 && (
          <Alert
            type="warning"
            message={`导出预检发现 ${exportIssues.length} 个问题`}
            description={
              <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12 }}>
                {exportIssues.map((iss, idx) => (
                  <li key={idx}>
                    {/* 后端契约：预检 issue 一定携带英文 type 键（ExportIssueItem 未收录） */}
                    {EXPORT_ISSUE_LABEL[iss.type!] || iss.type}：
                    <Text strong>{describeExportIssue(iss)}</Text>
                  </li>
                ))}
              </ul>
            }
            showIcon
            style={{ marginTop: 12 }}
          />
        )}
        {/* ✅ 《待补充清单》（2026-09-24，人工补录兜底层）：按字段/按章节聚合
                展示正文中的【待补充】占位，点击章节跳转「正文生成」页定位补录；
                有补录数据时支持「重跑受影响章节」（只重跑可补齐的叶子章节） */}
        <PlaceholderReportPanel
          report={placeholderReport}
          onJumpToSection={handlePlaceholderJump}
          schemeId={id || undefined}
          rerunCount={rerunPlan?.rerunnable_count || 0}
          rerunning={rerunning}
          onRerunAffected={handleRerunAffected}
        />
        {exportStats && (
          <Card size="small" style={{ marginTop: 12 }} title="方案完整性统计">
            <Row gutter={[12, 8]}>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>总字数</div>
                <div style={{ fontSize: 16, fontWeight: 600 }}>
                  {exportStats.total_words?.toLocaleString() || 0}
                </div>
              </Col>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>已生成章节</div>
                <div style={{ fontSize: 16, fontWeight: 600 }}>
                  {exportStats.generated_count || 0}/{exportStats.section_count || 0}
                </div>
              </Col>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>空章节占比</div>
                <div
                  style={{
                    fontSize: 16,
                    fontWeight: 600,
                    color: (exportStats.empty_ratio || 0) > 20 ? "#E8836B" : "#52c41a",
                  }}
                >
                  {exportStats.empty_ratio || 0}%
                </div>
              </Col>
              <Col span={6}>
                <div style={{ fontSize: 11, color: "#888" }}>图表完成率</div>
                <div
                  style={{
                    fontSize: 16,
                    fontWeight: 600,
                    color:
                      (exportStats.chart_total || 0) > 0 &&
                      (exportStats.chart_done || 0) < (exportStats.chart_total || 0)
                        ? "#E8836B"
                        : "#52c41a",
                  }}
                >
                  {exportStats.chart_total
                    ? `${Math.round(((exportStats.chart_done || 0) / exportStats.chart_total) * 100)}%`
                    : "—"}
                </div>
              </Col>
            </Row>
            {(exportStats.empty_ratio || 0) > 0 ||
            (exportStats.chart_total || 0) > (exportStats.chart_done || 0) ? (
              <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
                💡 仍有空章节 /
                未完成图表：可回「正文生成」页用「补全生成」，或在章节正文中手动补图后重新预检。
              </div>
            ) : (
              <div style={{ marginTop: 8, fontSize: 12, color: "#52c41a" }}>
                ✓ 章节与图表均已就绪，可前往「导出」页生成文档。
              </div>
            )}
          </Card>
        )}
        {/* ✅ G1：导出预检与就绪度总检共用同一份数据，结论直接在此呈现，
                用户不必在两个页面之间来回比对「到底能不能交付」 */}
        {exportPreflight && (
          <Card
            size="small"
            style={{ marginTop: 12 }}
            title="就绪度总检结论（与「审核与预检」页同一份规则与数据）"
          >
            <Space size={12} wrap>
              <span
                style={{
                  fontSize: 20,
                  fontWeight: 700,
                  color: exportPreflight.released ? "#52c41a" : "#E8836B",
                }}
              >
                {exportPreflight.total ?? 0}
                <span style={{ fontSize: 11, fontWeight: 400, color: "#999" }}> / 100</span>
              </span>
              <Tag
                color={
                  exportPreflight.released ? "green" : exportPreflight.stale ? "volcano" : "orange"
                }
                style={{ marginRight: 0 }}
              >
                {exportPreflight.grade || "—"} 级
                {exportPreflight.released ? " · 建议放行" : " · 不建议放行"}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {exportPreflight.verdict || "—"}
              </Text>
              {exportPreflight.stale && (
                <Tag color="volcano" style={{ marginRight: 0 }}>
                  结论已过期
                </Tag>
              )}
            </Space>
            <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
              {exportPreflight.stale
                ? "⚠️ 本次总检之后正文 / 图表已发生变更，结论可能失效；请去「审核与预检」页重新执行一键总检，再回来导出。"
                : "详细评分、问题清单与整改报告见「审核与预检」页；两处使用同一份规则与同一套数据。"}
              {exportPreflight.rule_version ? ` 规则版本 ${exportPreflight.rule_version}` : ""}
            </div>
          </Card>
        )}
        {!exportStats &&
          exportIssues.length === 0 &&
          (exportCheckIncomplete ? (
            <div style={{ marginTop: 8, fontSize: 12, color: "#E8836B" }}>
              ⚠️ 预检未完成：后端未能读取方案数据（数据库连接异常），本次没有检查到任何内容，
              不能视为「无问题」。请稍后重新预检。
            </div>
          ) : (
            <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
              尚未预检。点击右上角「开始预检」检查章节完整性与图表完成率。
            </div>
          ))}
      </Card>

      <Divider style={{ margin: "8px 0 16px" }} />

      {/* ✅ 接线 GET /schemes/{id}/sections/quality（原本是后端孤儿接口：
              能力已实现、前端从未调用 —— 典型的"信息算得出但传不到用户眼前"） */}
      <Card
        size="small"
        title={
          <Space>
            <CheckCircleOutlined />
            <Text strong>正文质量自检（交付前）</Text>
            {(qualityResult?.summary?.problem_sections || 0) > 0 && (
              <Tag color="orange">{qualityResult!.summary!.problem_sections} 个章节待修</Tag>
            )}
          </Space>
        }
        extra={
          <Space size={4}>
            {qualityResult && (
              <Button size="small" type="link" onClick={() => setQualityResult(null)}>
                清除结果
              </Button>
            )}
            <Button
              size="small"
              type="primary"
              icon={<CheckCircleOutlined />}
              loading={checkingQuality}
              onClick={handleQualityCheck}
            >
              开始自检
            </Button>
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          扫描全部已生成正文，检出两类不可交付问题：口语化 / AI 腔残留（正常生成落库前已清洗，
          命中说明手工编辑过）、引用已废止或被替代的标准编号（须替换为现行版本）。
        </Text>

        {qualityResult && (
          <Row gutter={[12, 8]} style={{ marginTop: 12 }}>
            <Col span={6}>
              <div style={{ fontSize: 11, color: "#888" }}>已扫描章节</div>
              <div style={{ fontSize: 16, fontWeight: 600 }}>
                {qualityResult.summary?.checked ?? 0}
              </div>
            </Col>
            <Col span={6}>
              <div style={{ fontSize: 11, color: "#888" }}>问题章节</div>
              <div
                style={{
                  fontSize: 16,
                  fontWeight: 600,
                  color: (qualityResult.summary?.problem_sections || 0) > 0 ? "#E8836B" : "#52c41a",
                }}
              >
                {qualityResult.summary?.problem_sections ?? 0}
              </div>
            </Col>
            <Col span={6}>
              <div style={{ fontSize: 11, color: "#888" }}>口语化残留</div>
              <div
                style={{
                  fontSize: 16,
                  fontWeight: 600,
                  color: (qualityResult.summary?.colloquial || 0) > 0 ? "#E8836B" : "#52c41a",
                }}
              >
                {qualityResult.summary?.colloquial ?? 0}
              </div>
            </Col>
            <Col span={6}>
              <div style={{ fontSize: 11, color: "#888" }}>废止标准</div>
              <div
                style={{
                  fontSize: 16,
                  fontWeight: 600,
                  color:
                    (qualityResult.summary?.abolished_standards || 0) > 0 ? "#E8836B" : "#52c41a",
                }}
              >
                {qualityResult.summary?.abolished_standards ?? 0}
              </div>
            </Col>
          </Row>
        )}

        {qualityResult && (qualityResult.items?.length || 0) > 0 && (
          <Table
            size="small"
            rowKey={(r) => r.section_id!}
            style={{ marginTop: 12 }}
            pagination={false}
            // ✅ 性能优化：虚拟滚动（同上方：pagination=false + 固定表体）
            scroll={{ y: 260 }}
            virtual
            dataSource={qualityResult.items || []}
            columns={[
              { title: "章节", dataIndex: "title", ellipsis: true },
              {
                title: "口语化 / AI 腔残留",
                dataIndex: "colloquial_hits",
                width: 220,
                render: (hits: string[]) =>
                  hits?.length
                    ? hits.map((h) => (
                        <Tag key={h} color="orange" style={{ marginBottom: 2 }}>
                          {h}
                        </Tag>
                      ))
                    : "—",
              },
              {
                title: "已废止 / 被替代标准",
                dataIndex: "abolished_standards",
                width: 220,
                render: (hits: string[]) =>
                  hits?.length
                    ? hits.map((h) => (
                        <Tag key={h} color="red" style={{ marginBottom: 2 }}>
                          {h}
                        </Tag>
                      ))
                    : "—",
              },
            ]}
          />
        )}

        {qualityResult && (qualityResult.items?.length || 0) === 0 && (
          <div style={{ marginTop: 12, fontSize: 12, color: "#52c41a" }}>
            ✓ 未检出口语化残留与废止标准，正文可直接交付。
          </div>
        )}

        {qualityResult && (
          <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
            标准库版本 {qualityResult.standard_db_version || "—"}
            {qualityResult.standard_db_checked_at
              ? `（核对于 ${(qualityResult.standard_db_checked_at || "").replace("T", " ").slice(0, 10)}）`
              : ""}
          </div>
        )}

        {!qualityResult && (
          <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
            尚未自检。点击右上角「开始自检」扫描全部已生成正文。
          </div>
        )}
      </Card>

      <Divider style={{ margin: "8px 0 16px" }} />

      {/* ===== ✅ 章节审核工作流（人工评审）=====
               机器检查（上方各卡片）之后是人工评审：逐章通过/驳回并留痕，
               方案级提交时后端会校验预检是否存在阻断项，有硬伤不放行评审。 */}
      <ReviewWorkflowPanel
        schemeId={id || ""}
        refreshKey={reviewTick}
        onChanged={() => {
          // ✅ R179（E8）：逐章通过/驳回后，六维总检必须重算 —— 旧实现只刷新
          //    章节清单（load），ReadinessDashboard 仍展示上一次总检，用户会
          //    拿过期结论判断能否交付。
          load();
          setReviewTick((v) => v + 1);
        }}
      />
    </div>
  );
}

import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Progress, Space, Tag, Tooltip } from "antd";
import { Typography } from "antd";
const { Title, Text } = Typography;

/** 章节覆盖率行（GET /global-facts/chapters → chapters[]；仅声明渲染读取的键） */
export interface ChapterCoverageRow {
  order?: number;
  key?: string;
  title?: string;
  coverage?: number;
  missing_fields?: string[];
}

/** 危大诊断报告（POST /global-facts/danger-check；仅声明渲染读取的键） */
export interface DangerReportLike {
  classification?: { is_hazardous?: boolean; is_oversize?: boolean };
  missing_params?: string[];
}

/**
 * ✅ BUG-08 修复（R176 · 2026-11-04）：危大判定三态，禁止塌缩成布尔。
 *
 * 旧实现把「请求失败 / 分类为空」与「未识别为危大工程」合并成同一分支：
 * `classification.is_hazardous ? "危大工程" : "未识别为危大工程"`。
 * danger-check 失败时 `dangerReport` 为 null、classification 为 `{}`，
 * 界面因此输出**否定结论**「未识别为危大工程」—— 对危大工程是致命假阴性
 * （用户会据此认为无需专家论证/不需按危大管控）。
 *
 * 新判据：只有后端确实返回**布尔** `is_hazardous` 才允许给出肯定/否定结论；
 * 报告缺席或字段非布尔（请求失败 / 分类为空 / 契约漂移）一律为 `"unknown"`。
 */
export type DangerTriState = "hazardous" | "safe" | "unknown";

export function dangerTriState(report: DangerReportLike | null | undefined): DangerTriState {
  const v = report?.classification?.is_hazardous;
  if (typeof v !== "boolean") return "unknown";
  return v ? "hazardous" : "safe";
}

/** 超规模判定同样三态（字段非布尔 → 无法判定，不得输出「未超过」）。 */
export function oversizeTriState(report: DangerReportLike | null | undefined): DangerTriState {
  const v = report?.classification?.is_oversize;
  if (typeof v !== "boolean") return "unknown";
  return v ? "hazardous" : "safe";
}

/** 章节覆盖度报告（GET /global-facts/chapters 的响应体） */
export interface FactsChapterReport {
  chapters?: ChapterCoverageRow[];
}

/**
 * ✅ R177（2026-11-04）：`undefined` 与 `null` 都表示「本次数据缺席/无法判定」。
 * 刷新失败时调用方会把报告置 `null`；`mergeFactsDiagnosticsSettled` 的键缺席
 * 语义则是 `undefined` —— 面板必须同时接受两种「缺席」表达，不能因类型过窄
 * 逼调用方用 `as any` 绕过。
 */
export interface FactsDiagnosticsPanelProps {
  loading: boolean;
  chapterReport: FactsChapterReport | null | undefined;
  dangerReport: DangerReportLike | null | undefined;
  onRefresh: () => void;
}

export function FactsDiagnosticsPanel({
  loading,
  chapterReport,
  dangerReport,
  onRefresh,
}: FactsDiagnosticsPanelProps) {
  const chapters = chapterReport?.chapters || [];
  const dangerState = dangerTriState(dangerReport);
  const oversizeState = oversizeTriState(dangerReport);
  const missing: string[] = dangerReport?.missing_params || [];
  return (
    <Card
      size="small"
      title="九大章节完整性 · 危大诊断"
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          // ✅ R193（D-11）：不把 MouseEvent 当 options 透传——旧写法
          //    `onClick={onRefresh}` 会把事件对象传进 loadFactsDiagnostics 的
          //    `options?: { signal }` 形参（.signal 读到 undefined，侥幸无害
          //    但属契约噪声）。
          onClick={() => void onRefresh()}
        >
          刷新诊断
        </Button>
      }
      style={{ marginBottom: 12 }}
    >
      {!chapterReport && !dangerReport ? (
        <Text type="secondary">暂无诊断数据</Text>
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space wrap>
            {dangerState === "unknown" ? (
              <Tooltip title="危大分类请求失败或返回数据不完整，无法给出结论，请刷新重试">
                <Tag color="warning">无法判定（危大分类数据不可用）</Tag>
              </Tooltip>
            ) : dangerState === "hazardous" ? (
              <Tag color="red">危大工程</Tag>
            ) : (
              <Tag color="default">未识别为危大工程</Tag>
            )}
            {oversizeState === "unknown" ? (
              <Tooltip title="超规模判定数据不可用，无法给出结论">
                <Tag color="warning">无法判定（超规模）</Tag>
              </Tooltip>
            ) : oversizeState === "hazardous" ? (
              <Tag color="volcano">超过一定规模</Tag>
            ) : (
              <Tag color="default">未超过一定规模</Tag>
            )}
            {missing.map((p: string) => (
              <Tag key={p} color="orange">
                缺参：{p}
              </Tag>
            ))}
          </Space>
          {/* ✅ R177（2026-11-04）：章节覆盖度刷新失败必须清空为「无法判定」，
              不得继续渲染上一次的覆盖率数字（null 即本次无数据，不靠时间戳猜）。 */}
          {chapterReport ? (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                gap: 6,
              }}
            >
              {chapters.map((ch) => (
                <Tooltip
                  key={ch.key}
                  title={`缺失字段：${(ch.missing_fields || []).join("、") || "无"}`}
                >
                  <div>
                    <Text style={{ fontSize: 12 }}>
                      {ch.order}. {ch.title}
                    </Text>
                    <Progress percent={Math.round((ch.coverage || 0) * 100)} size="small" />
                  </div>
                </Tooltip>
              ))}
            </div>
          ) : (
            <Tooltip title="章节覆盖度请求失败或返回数据不完整，无法给出覆盖率，请刷新重试">
              <Text type="warning">无法判定（章节覆盖度数据不可用）</Text>
            </Tooltip>
          )}
        </Space>
      )}
    </Card>
  );
}

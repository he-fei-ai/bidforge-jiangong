import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Progress, Space, Tag, Tooltip } from "antd";
import { Typography } from "antd";
const { Title, Text } = Typography;

/** 章节覆盖率行（GET /global-facts/chapters → chapters[]；仅声明渲染读取的键） */
interface ChapterCoverageRow {
  order?: number;
  key?: string;
  title?: string;
  coverage?: number;
  missing_fields?: string[];
}

/** 危大诊断报告（POST /global-facts/danger-check；仅声明渲染读取的键） */
interface DangerReportLike {
  classification?: { is_hazardous?: boolean; is_oversize?: boolean };
  missing_params?: string[];
}

export function FactsDiagnosticsPanel({ loading, chapterReport, dangerReport, onRefresh }: {
  loading: boolean;
  chapterReport: { chapters?: ChapterCoverageRow[] } | null;
  dangerReport: DangerReportLike | null;
  onRefresh: () => void;
}) {
  const chapters = chapterReport?.chapters || [];
  const classification: { is_hazardous?: boolean; is_oversize?: boolean } =
    dangerReport?.classification || {};
  const missing: string[] = dangerReport?.missing_params || [];
  return (
    <Card
      size="small"
      title="九大章节完整性 · 危大诊断"
      extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={onRefresh}>刷新诊断</Button>}
      style={{ marginBottom: 12 }}
    >
      {!chapterReport && !dangerReport ? (
        <Text type="secondary">暂无诊断数据</Text>
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space wrap>
            <Tag color={classification.is_hazardous ? "red" : "default"}>
              {classification.is_hazardous ? "危大工程" : "未识别为危大工程"}
            </Tag>
            <Tag color={classification.is_oversize ? "volcano" : "default"}>
              {classification.is_oversize ? "超过一定规模" : "未超过一定规模"}
            </Tag>
            {missing.map((p: string) => <Tag key={p} color="orange">缺参：{p}</Tag>)}
          </Space>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 6 }}>
            {chapters.map((ch) => (
              <Tooltip key={ch.key} title={`缺失字段：${(ch.missing_fields || []).join("、") || "无"}`}>
                <div>
                  <Text style={{ fontSize: 12 }}>{ch.order}. {ch.title}</Text>
                  <Progress percent={Math.round((ch.coverage || 0) * 100)} size="small" />
                </div>
              </Tooltip>
            ))}
          </div>
        </Space>
      )}
    </Card>
  );
}


import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Input,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { ReloadOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { genreApi } from "../../api";
import type { GenreRuleTrace, GenreTag, TriStateValue } from "../../types/genre";

const { Text } = Typography;

/** 三态标签：yes/no/undetermined 必须视觉可区分，缺参不得显示成「非危大」 */
function triTag(
  value: TriStateValue | string,
  labels: { yes: string; no: string; undetermined: string },
) {
  if (value === "yes") {
    return (
      <Tag color="red" data-testid="genre-tri-yes">
        {labels.yes}
      </Tag>
    );
  }
  if (value === "no") {
    return (
      <Tag color="green" data-testid="genre-tri-no">
        {labels.no}
      </Tag>
    );
  }
  return (
    <Tag color="gold" data-testid="genre-tri-undetermined">
      {labels.undetermined}
    </Tag>
  );
}

export interface GenreTagCardProps {
  schemeId: string;
  projectId: string;
}

/**
 * 文种标签卡（R179 Phase A 的前端出口）。
 *
 * 后端开关 `genre_tag_enabled` 默认关闭 → 路由 404，本卡显式显示「未启用」，
 * 不把 404 当成空结果。规则零命中且走 LLM 建议时，来源标记为
 * `llm_suggested`，危大/超规模仍保持「无法判定」。
 */
export function GenreTagCard({ schemeId, projectId }: GenreTagCardProps) {
  const { message } = App.useApp();
  const [genre, setGenre] = useState<GenreTag | null>(null);
  const [loading, setLoading] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState("");
  const [extraText, setExtraText] = useState("");

  const load = useCallback(async () => {
    if (!schemeId || !projectId) return;
    setLoading(true);
    setError("");
    try {
      const { data } = await genreApi.get(projectId, schemeId);
      setDisabled(!data?.enabled);
      setGenre(data?.genre || null);
    } catch (e: unknown) {
      const err = e as { response?: { status?: number }; message?: string };
      if (err?.response?.status === 404) {
        setDisabled(true);
        setGenre(null);
      } else {
        setError(err?.message || "文种标签读取失败");
      }
    } finally {
      setLoading(false);
    }
  }, [schemeId, projectId]);

  useEffect(() => {
    load();
  }, [load]);

  const evaluate = async () => {
    if (!schemeId || !projectId) return;
    setEvaluating(true);
    try {
      const { data } = await genreApi.evaluate(projectId, schemeId, {
        extra_text: extraText.trim(),
        text: extraText.trim(),
      });
      setDisabled(!data?.enabled);
      setGenre(data?.genre || null);
      message.success("文种标签已评估并保存");
    } catch (e: unknown) {
      const err = e as { response?: { status?: number }; message?: string };
      if (err?.response?.status === 404) {
        setDisabled(true);
      } else {
        message.error(err?.message || "文种标签评估失败");
      }
    } finally {
      setEvaluating(false);
    }
  };

  if (disabled) {
    return (
      <Card
        size="small"
        style={{ marginBottom: 12 }}
        title={
          <Space>
            <SafetyCertificateOutlined />
            <Text strong>文种标签（结构化）</Text>
          </Space>
        }
      >
        <Alert
          type="warning"
          showIcon
          message="文种标签功能未启用"
          description="后端开关 genre_tag_enabled 默认关闭；开启后由规则引擎输出类别、危大/超规模三态结论与判定依据。"
        />
      </Card>
    );
  }

  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      title={
        <Space>
          <SafetyCertificateOutlined />
          <Text strong>文种标签（结构化）</Text>
          {genre?.source === "llm_suggested" && <Tag color="purple">LLM 建议类别</Tag>}
        </Space>
      }
      extra={
        <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={load}>
          刷新
        </Button>
      }
    >
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 8 }} />}
      {!genre && !error && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          尚无已落库的文种标签。按方案名称执行规则评估，或在下方补充文本后评估。
        </Text>
      )}
      {genre && (
        <Descriptions column={1} size="small" bordered>
          <Descriptions.Item label="类别">
            {genre.category_names?.length ? (
              <Space wrap>
                {genre.category_names.map((name, i) => (
                  <Tag key={`${name}-${i}`} color="blue">
                    {name}
                    {genre.sub_names?.[i] ? ` · ${genre.sub_names[i]}` : ""}
                  </Tag>
                ))}
              </Space>
            ) : (
              <Text type="secondary">未识别到类别（危大等级无法判定）</Text>
            )}
          </Descriptions.Item>
          <Descriptions.Item label="危大判定">
            {triTag(genre.hazard, {
              yes: "危大工程",
              no: "非危大工程",
              undetermined: "危大等级无法判定",
            })}
          </Descriptions.Item>
          <Descriptions.Item label="超规模判定">
            {triTag(genre.oversize, {
              yes: "超过一定规模危大工程",
              no: "非超危大",
              undetermined: "超规模等级无法判定",
            })}
          </Descriptions.Item>
          <Descriptions.Item label="模板 / 来源">
            <Space size={8} wrap>
              <Tag>{genre.template_key || "general"}</Tag>
              <Tag color={genre.source === "rules" ? "geekblue" : "purple"}>
                {genre.source === "rules" ? "规则引擎" : genre.source}
              </Tag>
              {genre.needs_calc ? <Tag color="orange">需计算书</Tag> : null}
              {genre.expert_review_required ? <Tag color="volcano">需专家论证</Tag> : null}
            </Space>
          </Descriptions.Item>
        </Descriptions>
      )}

      {(genre?.missing_params?.length ?? 0) > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
          data-testid="genre-missing-params"
          message="存在缺失参数，危大/超规模结论保持「无法判定」"
          description={`缺失：${genre!.missing_params.join("、")}`}
        />
      )}

      {(genre?.rule_trace?.length ?? 0) > 0 && (
        <Table
          size="small"
          style={{ marginTop: 8 }}
          rowKey={(r) => `${r.layer}-${r.rule_id}-${r.sub_id || ""}-${r.conclusion || ""}`}
          pagination={false}
          dataSource={genre!.rule_trace}
          columns={[
            { title: "规则", dataIndex: "rule_id", width: 110 },
            { title: "子类", dataIndex: "sub_id", width: 110, render: (v: string) => v || "—" },
            {
              title: "结论",
              dataIndex: "conclusion",
              width: 150,
              render: (v: TriStateValue | undefined) =>
                v
                  ? triTag(v, {
                      yes: "命中",
                      no: "未命中",
                      undetermined: "无法判定",
                    })
                  : "—",
            },
            {
              title: "判定依据",
              dataIndex: "evidence",
              render: (evidence: string[] | undefined) =>
                evidence?.length ? evidence.join("；") : "—",
            },
          ]}
        />
      )}

      {(genre?.layer3?.length ?? 0) > 0 && (
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            第三层规则（编制主体 / 论证前置 / 禁止表述）：{genre!.layer3.length} 条记录
          </Text>
          <div style={{ marginTop: 4 }}>
            {genre!.layer3.map((r: GenreRuleTrace, i: number) => (
              <Tag key={`${r.rule_id}-${i}`} color="default" style={{ marginBottom: 4 }}>
                {r.rule_id || "规则"}：{r.conclusion || "无法判定"}
              </Tag>
            ))}
          </div>
        </div>
      )}

      <Input.TextArea
        rows={2}
        value={extraText}
        onChange={(e) => setExtraText(e.target.value)}
        placeholder="可选：粘贴编制说明 / 正文片段作为补充匹配文本"
        style={{ marginTop: 8, fontSize: 12 }}
      />
      <Button
        type="primary"
        size="small"
        style={{ marginTop: 8 }}
        loading={evaluating}
        onClick={evaluate}
        data-testid="genre-evaluate"
      >
        评估文种标签
      </Button>
    </Card>
  );
}

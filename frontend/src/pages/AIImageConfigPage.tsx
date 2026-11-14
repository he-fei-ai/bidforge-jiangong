// 图像模型配置页（R238 · SDD Task 7）
// 三段式：图像模型配置 / AI 配图协同决策流 / 文生图 Prompt 模板。
// 消费 imageApi（Task 6 封装）与 ImageConfigResponse/ImageConfigSavePayload 契约；
// 载荷使用**裸字段**（provider/model/base_url/api_key/enabled/default_size/
// concurrency/price_per_image/max_ai_images），与后端 ImageConfigIn 对齐
// （见 frontend/src/types/imageConfig.ts 的 N1 契约修复注释）。
import { useEffect, useState } from "react";
import {
  App,
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Typography,
} from "antd";
import { PictureOutlined, ThunderboltOutlined, FileTextOutlined } from "@ant-design/icons";
import { imageApi } from "../api";
import type { ImageConfigResponse, ImageConfigSavePayload } from "../types/imageConfig";

const { Title, Paragraph, Text } = Typography;

/** 5 家展示厂商（display_provider 键与后端 PROVIDER_PRESETS 一一对应） */
const DISPLAY_OPTIONS = [
  { value: "dalle3", label: "DALL-E-3" },
  { value: "tongyi_wanxiang", label: "通义万相" },
  { value: "doubao_seedream", label: "豆包 Seedream" },
  { value: "tencent_hunyuan", label: "腾讯混元" },
  { value: "custom", label: "自定义厂商" },
];

/** 协同决策流三段默认说明（后端未载入时兜底展示；route 字面量即判定语义键） */
const DECISION_ROUTES_FALLBACK = [
  { route: "structured", desc: "命中图表生成关键词（gantt/architecture/flowchart）→ 结构化图表管线" },
  { route: "illustration", desc: "未命中图表关键词 → AI 概念配图（文生图）" },
  { route: "none", desc: "两者皆不命中 → 不出图" },
];

export default function AIImageConfigPage() {
  const { message: msg } = App.useApp();
  const [form] = Form.useForm();
  const [data, setData] = useState<ImageConfigResponse | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const load = async () => {
    try {
      const { data: d } = await imageApi.getConfig();
      setData(d);
      // 反推 display_provider：以 DB 实际 provider 匹配预设表取展示键；未命中 → "custom"。
      // 同时回填隐藏 provider 字段，避免「下拉显示自定义但持有非 custom 厂商 URL」的错误组合。
      const displayProvider =
        Object.entries(d.presets).find(([, p]) => p.provider === d.config.image_provider)?.[0] ?? "custom";
      form.setFieldsValue({
        display_provider: displayProvider,
        provider: d.config.image_provider,
        enabled: d.config.image_enabled,
        base_url: d.config.image_base_url,
        model: d.config.image_model,
        api_key: "",
        default_size: d.config.image_default_size,
        concurrency: d.config.image_max_concurrency,
        price_per_image: d.config.image_price_per_image,
        max_ai_images: d.config.max_ai_images,
      });
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "加载图像配置失败");
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** display_provider 切换 → 从后端预设回填 provider/base_url/model */
  const onProviderChange = (v: string) => {
    const preset = data?.presets?.[v];
    if (preset) {
      form.setFieldsValue({ provider: preset.provider, base_url: preset.base_url, model: preset.model });
    }
  };

  const handleSave = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      const payload: ImageConfigSavePayload = {
        provider: values.provider || "custom",
        model: values.model ?? "",
        base_url: values.base_url ?? "",
        api_key: values.api_key ?? "",
        enabled: !!values.enabled,
        default_size: values.default_size ?? "1K",
        concurrency: Number(values.concurrency ?? 1),
        price_per_image: Number(values.price_per_image ?? 0),
        max_ai_images: Number(values.max_ai_images ?? 0),
      };
      await imageApi.saveConfig(payload);
      msg.success("图像模型配置已保存");
      void load();
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    const values = await form.validateFields();
    setTesting(true);
    try {
      const { data: r } = await imageApi.testConnection({
        provider: values.provider || "custom",
        base_url: values.base_url ?? "",
        api_key: values.api_key ?? "",
      });
      if (r.ok) msg.success("连接成功");
      else msg.warning(r.message || "连接失败");
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "测试失败");
    } finally {
      setTesting(false);
    }
  };

  const routes = data?.decision?.routes?.length ? data.decision.routes : DECISION_ROUTES_FALLBACK;

  return (
    <div>
      <Title level={4}>
        <PictureOutlined /> 图像模型配置
      </Title>
      <Paragraph type="secondary">
        配置图像模型（AI 概念配图），并明确「数据驱动结构化图表 vs AI 概念配图」的分工决策流与文生图 Prompt 模板。
      </Paragraph>

      <Card title="图像模型配置" size="small" style={{ marginBottom: 12 }}>
        <Form form={form} layout="vertical">
          {/* provider 为隐藏注册字段：onProviderChange 经预设回填，validateFields 才能带回 */}
          <Form.Item name="provider" hidden>
            <Input />
          </Form.Item>
          <Space wrap>
            <Form.Item name="display_provider" label="提供商" rules={[{ required: true }]}>
              <Select style={{ width: 200 }} options={DISPLAY_OPTIONS} onChange={onProviderChange} />
            </Form.Item>
            <Form.Item name="enabled" label="开启生图" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true, message: "请填写 Base URL" }]}>
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item name="model" label="模型名">
            <Input placeholder="dall-e-3" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key（留空保持原样）">
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Space wrap>
            <Form.Item name="default_size" label="默认尺寸">
              <Select
                style={{ width: 120 }}
                options={[
                  { value: "1K", label: "1K" },
                  { value: "2K", label: "2K" },
                  { value: "3K", label: "3K" },
                  { value: "4K", label: "4K" },
                ]}
              />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数">
              <InputNumber min={1} max={8} />
            </Form.Item>
            <Form.Item name="price_per_image" label="单张价格">
              <InputNumber min={0} step={0.01} />
            </Form.Item>
            <Form.Item name="max_ai_images" label="全局配图预算（0=关闭）">
              <InputNumber min={0} max={1000} />
            </Form.Item>
          </Space>
          <Space>
            <Button type="primary" loading={saving} onClick={handleSave}>
              保存
            </Button>
            <Button loading={testing} onClick={handleTest}>
              测试连接
            </Button>
          </Space>
        </Form>
      </Card>

      <Card title={<><ThunderboltOutlined /> AI 配图协同决策流</>} size="small" style={{ marginBottom: 12 }}>
        {routes.map((r) => (
          <Alert key={r.route} type="info" showIcon style={{ marginBottom: 8 }} message={`${r.route} — ${r.desc}`} />
        ))}
      </Card>

      <Card title={<><FileTextOutlined /> 文生图 Prompt 模板</>} size="small">
        <Paragraph>
          <Text code>{data?.decision?.prompt_template || "[IMG_GEN_PROMPT: 主题=…；风格=…；构图=…；比例=…；负面词=…]"}</Text>
        </Paragraph>
        <Paragraph type="secondary">字段：{data?.decision?.prompt_fields?.join(" / ")}</Paragraph>
      </Card>
    </div>
  );
}

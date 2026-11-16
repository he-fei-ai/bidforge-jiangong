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
  Modal,
  Select,
  Space,
  Switch,
  Table,
  type TableColumnsType,
  Tag,
  Typography,
} from "antd";
import { PictureOutlined, ThunderboltOutlined, FileTextOutlined } from "@ant-design/icons";
import { imageApi } from "../api";
import type {
  ImageConfigResponse,
  ImageConfigSavePayload,
  ImageModelItem,
  ImageModelSavePayload,
  ImageSceneRouteItem,
} from "../types/imageConfig";

const { Title, Paragraph, Text } = Typography;

/** 9 家展示厂商（display_provider 键与后端 PROVIDER_PRESETS 一一对应） */
const DISPLAY_OPTIONS = [
  { value: "dalle3", label: "DALL-E-3" },
  { value: "tongyi_wanxiang", label: "通义万相" },
  { value: "doubao_seedream", label: "豆包 Seedream" },
  { value: "tencent_hunyuan", label: "腾讯混元" },
  // ✅ TB-3（2026-11-16 · 展示面补齐）：4 家已注册适配器此前前端下拉未展示
  //    （后端 ProviderRegistry 已支持）。加法式补展示，默认端点/模型由后端预设回填。
  { value: "zhipu", label: "智谱 CogView" },
  { value: "stepfun", label: "阶跃 StepFun" },
  { value: "baidu", label: "百度文心一格" },
  { value: "agnes", label: "Agnes AI" },
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
      // P2 消歧（2026-11-14）：dalle3 与 custom 共享 provider="custom"，若按 provider 直接匹配，
      // find 恒取首个（dalle3）→「自定义厂商 + 自定义 URL」被显示为 DALL-E-3（显示回归）。
      // 仅当 base_url+model 与 dalle3 预设完全一致才取 dalle3，否则取 custom；非 custom provider 按 provider 匹配。
      let displayProvider = "custom";
      if (d.config.image_provider === "custom") {
        const d3 = d.presets["dalle3"];
        if (d3 && d.config.image_base_url === d3.base_url && d.config.image_model === d3.model) {
          displayProvider = "dalle3";
        }
      } else {
        const hit = Object.entries(d.presets).find(([, preset]) => preset.provider === d.config.image_provider);
        displayProvider = hit?.[0] ?? "custom";
      }
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

      <MultiModelManager />
    </div>
  );
}

/** R252 · 多模型管理（多配置 CRUD / 设为默认 / 场景路由 / 参数模板 / 审计）。 */
function MultiModelManager() {
  const { message: msg } = App.useApp();
  const [form] = Form.useForm();
  const [sceneForm] = Form.useForm();
  const [items, setItems] = useState<ImageModelItem[]>([]);
  const [scenes, setScenes] = useState<ImageSceneRouteItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string>("");
  const [testing, setTesting] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [{ data: list }, { data: sr }] = await Promise.all([
        imageApi.listModels(),
        imageApi.listSceneRoutes(),
      ]);
      setItems(list.items);
      setScenes(sr.items);
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "加载图像模型列表失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openCreate = () => {
    setEditingId("");
    form.setFieldsValue({
      provider: "custom", model: "", base_url: "", api_key: "",
      enabled: true, default_size: "2K", concurrency: 2, price_per_image: 0,
      max_ai_images: 0, is_default: false, env: "", priority: 0, remark: "",
      resolution: "16:9", steps: 20, cfg: 7, negative_prompt: "低清,模糊,文字,水印", style: "写实",
    });
    setModalOpen(true);
  };

  const openEdit = (it: ImageModelItem) => {
    setEditingId(it.id);
    form.setFieldsValue({
      provider: it.provider, model: it.model, base_url: it.base_url,
      api_key: "", enabled: it.enabled, default_size: it.default_size,
      concurrency: it.concurrency, price_per_image: it.price_per_image,
      max_ai_images: it.max_ai_images, is_default: it.is_default,
      env: it.env, priority: it.priority, remark: it.remark,
      resolution: (it.default_params.resolution as string) || "16:9",
      steps: (it.default_params.steps as number) ?? 20,
      cfg: (it.default_params.cfg as number) ?? 7,
      negative_prompt: (it.default_params.negative_prompt as string) || "",
      style: (it.default_params.style as string) || "写实",
    });
    setModalOpen(true);
  };

  const saveModel = async () => {
    const v = await form.validateFields();
    const payload: ImageModelSavePayload = {
      provider: v.provider || "custom",
      model: v.model ?? "",
      base_url: v.base_url ?? "",
      api_key: v.api_key ?? "",
      enabled: !!v.enabled,
      default_size: v.default_size ?? "2K",
      concurrency: Number(v.concurrency ?? 2),
      price_per_image: Number(v.price_per_image ?? 0),
      max_ai_images: Number(v.max_ai_images ?? 0),
      is_default: !!v.is_default,
      status: "active",
      env: v.env ?? "",
      priority: Number(v.priority ?? 0),
      remark: v.remark ?? "",
      default_params: {
        resolution: v.resolution || "16:9",
        steps: Number(v.steps ?? 20),
        cfg: Number(v.cfg ?? 7),
        negative_prompt: v.negative_prompt ?? "",
        style: v.style ?? "写实",
      },
    };
    try {
      if (editingId) await imageApi.updateModel(editingId, payload);
      else await imageApi.createModel(payload);
      msg.success(editingId ? "配置已更新（热更新生效）" : "配置已创建");
      setModalOpen(false);
      void load();
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "保存失败");
    }
  };

  const setDefault = async (id: string) => {
    try {
      await imageApi.setDefaultModel(id);
      msg.success("已设为默认主配置");
      void load();
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "设置默认失败");
    }
  };

  const removeModel = async (id: string) => {
    try {
      await imageApi.deleteModel(id);
      msg.success("配置已删除");
      void load();
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "删除失败");
    }
  };

  const testModel = async () => {
    const v = await form.validateFields();
    setTesting(true);
    try {
      const { data: r } = await imageApi.testModelConnection({
        provider: v.provider || "custom",
        base_url: v.base_url ?? "",
        api_key: v.api_key ?? "",
      });
      if (r.ok) msg.success("连接成功");
      else msg.warning(r.message || "连接失败");
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "测试失败");
    } finally {
      setTesting(false);
    }
  };

  const saveSceneRoutes = async () => {
    const v = await sceneForm.validateFields();
    try {
      const routes = Object.entries(v)
        .filter(([k]) => k.startsWith("scene_"))
        .map(([k, val]) => ({ scene: k.replace("scene_", ""), config_id: (val as string) ?? "" }));
      await imageApi.saveSceneRoutes(routes);
      msg.success("场景路由已保存（热更新生效）");
      void load();
    } catch (e: unknown) {
      msg.error((e as { message?: string }).message || "保存场景路由失败");
    }
  };

  const columns: TableColumnsType<ImageModelItem> = [
    { title: "厂商", dataIndex: "provider", key: "provider" },
    { title: "模型", dataIndex: "model", key: "model" },
    { title: "环境", dataIndex: "env", key: "env", render: (v: string) => v || "通用" },
    { title: "状态", dataIndex: "status", key: "status", render: (v: string) => (
      <Tag color={v === "active" ? "green" : "red"}>{v}</Tag>
    ) },
    { title: "默认", dataIndex: "is_default", key: "is_default", render: (v: boolean) => (
      v ? <Tag color="blue">默认</Tag> : null
    ) },
    { title: "密钥", key: "key", render: (_: unknown, it: ImageModelItem) => (
      it.key_broken ? <Tag color="red">密钥失效</Tag>
        : it.has_key ? <Text code>****{it.key_hint}</Text> : <Tag>未配置</Tag>
    ) },
    { title: "操作", key: "op", render: (_: unknown, it: ImageModelItem) => (
      <Space>
        <Button size="small" onClick={() => openEdit(it)}>编辑</Button>
        {!it.is_default && <Button size="small" onClick={() => setDefault(it.id)}>设默认</Button>}
        <Button size="small" danger onClick={() => removeModel(it.id)}>删除</Button>
      </Space>
    ) },
  ];

  return (
    <Card title="多模型管理（场景路由 / 参数模板 / 审计）" size="small" style={{ marginTop: 12 }}>
      <Space style={{ marginBottom: 12 }}>
        <Button type="primary" onClick={openCreate}>新建配置</Button>
        <Button onClick={load} loading={loading}>刷新</Button>
      </Space>
      <Table rowKey="id" size="small" loading={loading} dataSource={items}
        columns={columns} pagination={false} />

      <Card title="场景模型路由" size="small" style={{ marginTop: 12 }}>
        <Form form={sceneForm} layout="vertical">
          {scenes.map((s) => (
            <Form.Item key={s.scene} name={`scene_${s.scene}`} label={s.label}
              initialValue={s.config_id}>
              <Select
                allowClear
                placeholder="未路由（共用默认主配置）"
                options={[{ value: "", label: "未路由（共用默认主配置）" },
                  ...items.map((it) => ({
                    value: it.id,
                    label: `${it.provider}/${it.model || "默认"}${it.is_default ? "（默认）" : ""}`,
                  }))]}
              />
            </Form.Item>
          ))}
          <Button onClick={saveSceneRoutes}>保存场景路由</Button>
        </Form>
      </Card>

      <Modal title={editingId ? "编辑配置" : "新建配置"} open={modalOpen} onOk={saveModel}
        onCancel={() => setModalOpen(false)} confirmLoading={loading}>
        <Form form={form} layout="vertical">
          <Space wrap>
            <Form.Item name="provider" label="提供商" rules={[{ required: true }]}>
              <Select style={{ width: 180 }} options={DISPLAY_OPTIONS} />
            </Form.Item>
            <Form.Item name="env" label="环境">
              <Select style={{ width: 120 }} allowClear
                options={[{ value: "dev", label: "开发" }, { value: "test", label: "测试" },
                  { value: "prod", label: "生产" }]} />
            </Form.Item>
            <Form.Item name="enabled" label="开启生图" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="is_default" label="设为默认" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item name="model" label="模型名">
            <Input placeholder="dall-e-3" />
          </Form.Item>
          <Form.Item name="api_key" label={editingId ? "API Key（留空保持原样）" : "API Key"}>
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Space wrap>
            <Form.Item name="default_size" label="默认尺寸">
              <Select style={{ width: 100 }} options={[
                { value: "1K", label: "1K" }, { value: "2K", label: "2K" },
                { value: "3K", label: "3K" }, { value: "4K", label: "4K" }]} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数">
              <InputNumber min={1} max={8} />
            </Form.Item>
            <Form.Item name="price_per_image" label="单张价格">
              <InputNumber min={0} step={0.01} />
            </Form.Item>
            <Form.Item name="max_ai_images" label="配图预算（0=关闭）">
              <InputNumber min={0} max={1000} />
            </Form.Item>
          </Space>
          <Card title="参数模板" size="small">
            <Space wrap>
              <Form.Item name="resolution" label="分辨率">
                <Select style={{ width: 100 }} options={[
                  { value: "16:9", label: "16:9" }, { value: "1:1", label: "1:1" },
                  { value: "9:16", label: "9:16" }, { value: "4:3", label: "4:3" }]} />
              </Form.Item>
              <Form.Item name="steps" label="采样步数">
                <InputNumber min={1} max={100} />
              </Form.Item>
              <Form.Item name="cfg" label="CFG">
                <InputNumber min={0} max={30} step={0.5} />
              </Form.Item>
              <Form.Item name="style" label="风格 Tag">
                <Input style={{ width: 140 }} />
              </Form.Item>
            </Space>
            <Form.Item name="negative_prompt" label="固定负面提示词">
              <Input.TextArea rows={2} />
            </Form.Item>
          </Card>
          <Form.Item name="remark" label="备注">
            <Input />
          </Form.Item>
          <Button loading={testing} onClick={testModel}>测试连接</Button>
        </Form>
      </Modal>
    </Card>
  );
}

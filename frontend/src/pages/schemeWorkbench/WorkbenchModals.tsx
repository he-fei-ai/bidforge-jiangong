/**
 * 工作台弹窗集合（G4a-c · 2026-11-04 从 SchemeWorkbenchPage 原样抽取，零行为变更）。
 *
 * 覆盖：事实分组编辑 / 单条事实编辑 / 目录库套用 / 解析内容预览 /
 *       结构化提取配置 / 提取项完整内容 / 提取项人工校正。
 *
 * 纯面板：props 进、回调出。除「目录库套用」弹窗内既有的一次列表查询外，
 * 不直接调用 api、不读页面 state（该查询与抽取前位于同一处、逻辑逐字未改）。
 */
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Radio,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import type { FormInstance } from "antd";
import { AimOutlined, DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import type { DocRecord } from "../../utils/workflowDerived";
import { normalizeBaGroups } from "../../utils/bidAnalysis";
import type { BaGroup, BaItemDef, BaStoredItem } from "../../utils/bidAnalysis";
import { previewNotices } from "../../utils/parseSourceNotice";
import { outlineLibraryApi } from "../../api";
import DocumentPipelineSummary from "../../components/DocumentPipelineSummary";
import type {
  DocumentPipelineStatusLike,
  PipelineActionKey,
} from "../../components/DocumentPipelineSummary";
import { BidAnalysisItemBody } from "../../components/BidAnalysisTab";
import { FACT_VALUE_KIND_OPTIONS } from "./factsShared";
import type { DocPreviewData } from "./useDocumentWorkflow";
import type { BaEditItemLike, OutlineLibraryItem } from "./workbenchTypes";
import type { FactItemLike } from "../../types/facts";

const { Text } = Typography;

export interface WorkbenchModalsProps {
  // ---- 事实分组编辑 ----
  factEditingId: string | null;
  factModalOpen: boolean;
  factSubmitting: boolean;
  setFactModalOpen: (open: boolean) => void;
  setFactEditingId: (id: string | null) => void;
  setFactEditingCategory: (category: string) => void;
  submitFact: () => void;
  factForm: FormInstance;
  factCategoryOptions: { value: string; label: string }[];

  // ---- 单条事实编辑 ----
  factItemModalOpen: boolean;
  factItemSubmitting: boolean;
  factItemEditing: (FactItemLike & { groupId?: string; groupTitle?: string }) | null;
  setFactItemModalOpen: (open: boolean) => void;
  setFactItemEditing: (
    item: (FactItemLike & { groupId?: string; groupTitle?: string }) | null,
  ) => void;
  submitFactItem: () => void;
  factItemForm: FormInstance;

  // ---- 目录库套用 ----
  libOpen: boolean;
  setLibOpen: (open: boolean) => void;
  libKeyword: string;
  setLibKeyword: (keyword: string) => void;
  libLoading: boolean;
  setLibLoading: (loading: boolean) => void;
  libItems: OutlineLibraryItem[];
  setLibItems: (items: OutlineLibraryItem[]) => void;
  libError: string;
  setLibError: (error: string) => void;
  libApplying: boolean;
  onApplyLibrary: (lib: OutlineLibraryItem) => void;

  // ---- 解析内容预览 ----
  previewDoc: DocRecord | null;
  setPreviewDoc: (doc: DocRecord | null) => void;
  previewLoading: boolean;
  previewData: DocPreviewData | null;
  previewText: string;
  pipelineLoading: boolean;
  pipelineStatus: DocumentPipelineStatusLike | null;
  pipelineError: string | null;
  pipelineAction: PipelineActionKey | null;
  onPipelineAction: (key: PipelineActionKey) => void;

  // ---- 结构化提取配置 ----
  baConfigOpen: boolean;
  setBaConfigOpen: (open: boolean) => void;
  onStartBaFromConfig: () => void;
  baModeOpt: "key" | "full" | "custom";
  setBaModeOpt: (mode: "key" | "full" | "custom") => void;
  baDefs: BaItemDef[];
  baGroups: BaGroup[];
  baSelectedIds: string[];
  setBaSelectedIds: (ids: string[]) => void;
  baForceRerun: boolean;
  setBaForceRerun: (force: boolean) => void;

  // ---- 提取项完整内容 / 人工校正 ----
  baFullItem: BaStoredItem | null;
  setBaFullItem: (item: BaStoredItem | null) => void;
  baEditItem: BaEditItemLike | null;
  setBaEditItem: (item: BaEditItemLike | null) => void;
  baEditSaving: boolean;
  baEditValue: string;
  setBaEditValue: (value: string) => void;
  onBaSaveEdit: () => void;
  onBaClearEdit: () => void;
}

export function WorkbenchModals(props: WorkbenchModalsProps) {
  const {
    factEditingId,
    factModalOpen,
    factSubmitting,
    setFactModalOpen,
    setFactEditingId,
    setFactEditingCategory,
    submitFact,
    factForm,
    factCategoryOptions,
    factItemModalOpen,
    factItemSubmitting,
    factItemEditing,
    setFactItemModalOpen,
    setFactItemEditing,
    submitFactItem,
    factItemForm,
    libOpen,
    setLibOpen,
    libKeyword,
    setLibKeyword,
    libLoading,
    setLibLoading,
    libItems,
    setLibItems,
    libError,
    setLibError,
    libApplying,
    onApplyLibrary,
    previewDoc,
    setPreviewDoc,
    previewLoading,
    previewData,
    previewText,
    pipelineLoading,
    pipelineStatus,
    pipelineError,
    pipelineAction,
    onPipelineAction,
    baConfigOpen,
    setBaConfigOpen,
    onStartBaFromConfig,
    baModeOpt,
    setBaModeOpt,
    baDefs,
    baGroups,
    baSelectedIds,
    setBaSelectedIds,
    baForceRerun,
    setBaForceRerun,
    baFullItem,
    setBaFullItem,
    baEditItem,
    setBaEditItem,
    baEditSaving,
    baEditValue,
    setBaEditValue,
    onBaSaveEdit,
    onBaClearEdit,
  } = props;

  return (
    <>
      <Modal
        title={factEditingId ? "编辑事实分组" : "新增事实分组"}
        open={factModalOpen}
        forceRender
        onOk={submitFact}
        confirmLoading={factSubmitting}
        okText={factEditingId ? "保存" : "新增"}
        cancelText="取消"
        onCancel={() => {
          // ✅ 修复：旧实现不重置 factEditingCategory —— 上次「编辑分组」的
          //    分类会残留，下次「新增分组」若用户不选分类，submitFact 会兜底
          //    使用旧分类，新分组被误继承归类。
          setFactModalOpen(false);
          setFactEditingId(null);
          setFactEditingCategory("");
        }}
        width={640}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="此处内容会作为全篇锚点，正文生成时用于保持工期、参数、设备型号等事实一致。建议用 Markdown 无序列表逐条书写。"
        />
        <Form form={factForm} layout="vertical">
          <Form.Item
            name="title"
            label="分组名称"
            rules={[{ required: true, message: "请输入分组名称" }]}
          >
            <Input placeholder="如：工期安排 / 技术路线与设备配置" />
          </Form.Item>
          <Form.Item
            name="category"
            label="事实分类"
            tooltip="用于事实清单归类与正文注入顺序；留空按「其他事实」处理"
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择分类（可选）"
              options={factCategoryOptions}
            />
          </Form.Item>
          <Form.Item
            name="content"
            label="事实内容"
            rules={[{ required: true, message: "请输入事实内容" }]}
          >
            <Input.TextArea
              rows={10}
              placeholder={"- 本方案项目总工期为 90 日历天\n- 挖掘机选用 PC200-8 型"}
              style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* ===== ✅ 单条事实编辑（item_updates，不重建分组）===== */}
      <Modal
        title="编辑事实条目"
        open={factItemModalOpen}
        forceRender
        onOk={submitFactItem}
        confirmLoading={factItemSubmitting}
        okText="保存"
        cancelText="取消"
        onCancel={() => {
          setFactItemModalOpen(false);
          setFactItemEditing(null);
        }}
        width={560}
      >
        {factItemEditing?.groupTitle && (
          <div style={{ fontSize: 12, color: "#999", marginBottom: 8 }}>
            所属分组：{factItemEditing.groupTitle}
          </div>
        )}
        <Form form={factItemForm} layout="vertical">
          <Form.Item
            name="name"
            label="事实名称"
            rules={[{ required: true, message: "请输入事实名称" }]}
          >
            <Input placeholder="如：项目总工期" />
          </Form.Item>
          <Form.Item
            name="value"
            label="事实值"
            rules={[{ required: true, message: "请输入事实值" }]}
          >
            <Input.TextArea rows={3} placeholder="如：90 日历天" />
          </Form.Item>
          <Form.Item name="category" label="事实分类">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择分类（可选）"
              options={factCategoryOptions}
            />
          </Form.Item>
          <Form.Item
            name="is_simulated"
            label="模拟值"
            valuePropName="checked"
            tooltip="标记为模拟值后，该事实会回到「待审核」，未经人工确认不会注入正文"
          >
            <Switch checkedChildren="模拟值" unCheckedChildren="实际值" />
          </Form.Item>
          {/* ✅ R81：要求 vs 事实口径（与后端 value_kind 白名单同源） */}
          <Form.Item
            name="value_kind"
            label="事实口径"
            tooltip="本方案口径：本方案已采用的值，正文可直接引用；要求口径：招标/规范要求原文，正文引用时须写「满足/响应要求」，不得当作既定值"
          >
            <Select
              allowClear
              placeholder="未区分（历史行默认）"
              options={FACT_VALUE_KIND_OPTIONS}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* ===== ✅ 目录库选择弹窗：套用到当前方案 ===== */}
      <Modal
        title="从目录库套用（仅显示已审核通过的目录）"
        open={libOpen}
        onCancel={() => setLibOpen(false)}
        footer={null}
        width={720}
      >
        <Space style={{ marginBottom: 12 }}>
          <Input
            placeholder="按名称搜索..."
            value={libKeyword}
            onChange={(e) => setLibKeyword(e.target.value)}
            allowClear
            style={{ width: 280 }}
          />
          <Button
            icon={<ReloadOutlined />}
            onClick={async () => {
              setLibLoading(true);
              try {
                const { data } = await outlineLibraryApi.list({
                  review_status: "已通过",
                  ...(libKeyword ? { keyword: libKeyword } : {}),
                });
                setLibItems(data.items || []);
                setLibError(
                  (data.items || []).length === 0 ? "没有匹配的目录库（可能是全部未审核通过）" : "",
                );
              } catch (e: unknown) {
                const err = e as { message?: string };
                setLibError(err.message || "加载失败");
              } finally {
                setLibLoading(false);
              }
            }}
          >
            搜索
          </Button>
        </Space>

        {libError && (
          <Alert type="warning" showIcon message={libError} style={{ marginBottom: 12 }} />
        )}

        <Spin spinning={libLoading || libApplying}>
          <div style={{ maxHeight: 380, overflowY: "auto" }}>
            {libItems.length === 0 && !libLoading ? (
              <Empty description="暂无可套用的目录库" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <List
                size="small"
                dataSource={libItems}
                renderItem={(lib) => (
                  <List.Item
                    actions={[
                      <Button
                        key="apply"
                        size="small"
                        type="primary"
                        icon={<AimOutlined />}
                        onClick={() => onApplyLibrary(lib)}
                      >
                        套用
                      </Button>,
                    ]}
                  >
                    <List.Item.Meta
                      title={
                        <Space wrap>
                          <span style={{ fontWeight: 500 }}>{lib.name}</span>
                          {lib.version && <Tag color="blue">{lib.version}</Tag>}
                          {lib.type && <Tag>{lib.type}</Tag>}
                          {(lib.ref_count || 0) > 0 && (
                            <Tag color="green">引用 {lib.ref_count}</Tag>
                          )}
                        </Space>
                      }
                      description={
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {lib.engineering_type || "通用"} · {lib.profession || "不限专业"}
                          {lib.source ? ` · 来源：${lib.source}` : ""}
                        </Text>
                      }
                    />
                  </List.Item>
                )}
              />
            )}
          </div>
        </Spin>
      </Modal>

      {/* ===== ✅ 上传解析：解析内容预览（前 N 字，不下载整份文件） ===== */}
      <Modal
        title={
          previewDoc
            ? `解析内容预览 · ${(previewDoc as { file_name?: string }).file_name}`
            : "解析内容预览"
        }
        open={!!previewDoc}
        onCancel={() => setPreviewDoc(null)}
        footer={null}
        width={760}
      >
        <Spin spinning={previewLoading}>
          {/* ✅ 解析可信度提示：区分「文档内容真的不完整」（warning，需行动）
              与「只是本弹窗没显示全」（info，完整内容仍在库里），二者互不覆盖 */}
          {!previewLoading && previewData?.is_parsed && (
            <div style={{ marginBottom: 12 }}>
              {previewNotices(previewData).map((n, i) => (
                <Alert
                  key={i}
                  type={n.kind}
                  showIcon
                  message={n.title}
                  description={n.detail}
                  style={{ marginBottom: 8 }}
                />
              ))}
            </div>
          )}
          {/* ✅ 四层存储解析质量概览 + 管线操作（修复 docPipelineApi/组件此前无调用点的断链） */}
          <div style={{ marginBottom: 12 }}>
            <DocumentPipelineSummary
              loading={pipelineLoading}
              status={pipelineStatus}
              error={pipelineError}
              onAction={onPipelineAction}
              actionBusy={pipelineAction}
            />
          </div>
          {previewText ? (
            <div
              className="markdown-body"
              style={{ maxHeight: "60vh", overflowY: "auto", whiteSpace: "pre-wrap", fontSize: 13 }}
            >
              {previewText}
            </div>
          ) : (
            !previewLoading && <Empty description="该文档尚未解析，请先执行解析" />
          )}
        </Spin>
      </Modal>

      {/* ===== ✅ 提取项目：启动配置（模式 / 勾选项 / 强制重跑） ===== */}
      <Modal
        title="结构化提取配置"
        open={baConfigOpen}
        onOk={onStartBaFromConfig}
        okText="开始提取"
        cancelText="取消"
        onCancel={() => setBaConfigOpen(false)}
        width={640}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <div>
            <Text strong>提取范围：</Text>
            <Radio.Group
              value={baModeOpt}
              onChange={(e) => setBaModeOpt(e.target.value)}
              style={{ marginLeft: 8 }}
            >
              {/* 项数随后端 /items 定义动态生成（旧实现硬编码 17/18）；未加载时不报数 */}
              <Radio.Button value="key">
                必选项{baDefs.length ? `（${baDefs.filter((d) => d.required).length} 项）` : ""}
              </Radio.Button>
              <Radio.Button value="full">
                全部{baDefs.length ? `（${baDefs.length} 项）` : ""}
              </Radio.Button>
              <Radio.Button value="custom">自定义勾选</Radio.Button>
            </Radio.Group>
          </div>
          {baModeOpt === "custom" && (
            <div
              style={{
                maxHeight: 320,
                overflowY: "auto",
                border: "1px solid #f0f0f0",
                borderRadius: 6,
                padding: 8,
              }}
            >
              {normalizeBaGroups(baGroups, baDefs).map((g) => (
                <div key={g.key} style={{ marginBottom: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {g.label}
                  </Text>
                  <Checkbox.Group
                    style={{ display: "flex", flexDirection: "column", paddingLeft: 12 }}
                    value={baSelectedIds}
                    onChange={(vals) => setBaSelectedIds(vals as string[])}
                    options={g.items.map((d) => ({
                      label: (d.required ? "★ " : "") + (d.label || d.item_id),
                      value: d.item_id,
                    }))}
                  />
                </div>
              ))}
            </div>
          )}
          <div>
            <Checkbox checked={baForceRerun} onChange={(e) => setBaForceRerun(e.target.checked)}>
              强制重跑（清空本次所选解析项的已有结果后重新提取）
            </Checkbox>
          </div>
          <Alert
            type="info"
            showIcon
            message="提取结果为项目级共享：同项目下的多个方案共用一套提取结果；单项可稍后在列表行内单独重跑。"
          />
        </Space>
      </Modal>

      {/* ===== ✅ 提取项目：查看完整结果（与右侧阅读区共用渲染器） ===== */}
      <Modal
        title={baFullItem ? `${baFullItem.label || baFullItem.item_id} · 完整内容` : "完整内容"}
        open={!!baFullItem}
        onCancel={() => setBaFullItem(null)}
        footer={null}
        width={820}
      >
        {baFullItem && (
          <div style={{ maxHeight: "65vh", overflowY: "auto" }}>
            <BidAnalysisItemBody item={baFullItem} />
          </div>
        )}
      </Modal>

      {/* ===== ✅ 提取项目：人工校正（直接改写单项提取结果，source='manual'）=====
          AI 抽错关键参数（基坑深度、支护形式、工期）时，旧实现唯一出路是整项重跑
          （贵且不稳定），抽错的值会继续被下游 format_downstream_context 消费。
          此弹窗是唯一可靠出口：校正值落库后标记 source='manual'，下游优先使用。
          入口：「提取项目」Tab 与「上传解析」→「解析信息分类显示栏」右侧详情。 */}
      <Modal
        title={baEditItem ? `人工校正：${baEditItem.label || baEditItem.item_id}` : "人工校正"}
        open={!!baEditItem}
        onCancel={() => setBaEditItem(null)}
        onOk={onBaSaveEdit}
        okText="保存校正"
        confirmLoading={baEditSaving}
        cancelText="取消"
        width={760}
      >
        {baEditItem && (
          <div>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="人工校正结果将标记为「人工校正」，下游目录/正文生成优先使用该校正值"
              description={
                baEditItem.output_type === "json"
                  ? "该项为 JSON 结构（项目级基本信息），请提交合法 JSON 对象/数组。"
                  : "该项为 Markdown 文本，可直接编辑。"
              }
            />
            <div style={{ marginBottom: 8 }}>
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={baEditSaving}
                onClick={onBaClearEdit}
              >
                撤销校正并清空（回退到待提取）
              </Button>
            </div>
            <Input.TextArea
              value={baEditValue}
              onChange={(e) => setBaEditValue(e.target.value)}
              autoSize={{ minRows: 10, maxRows: 24 }}
              style={{ fontFamily: "monospace" }}
              placeholder={baEditItem.output_type === "json" ? "{ ... }" : "请输入校正后的内容"}
            />
          </div>
        )}
      </Modal>
    </>
  );
}

import { useEffect, useRef, useState } from "react";
import {App, Card, Button, List, Tag, Modal, Form, Input, Select, Space,
  message, Descriptions, Tabs, Typography, Empty, Tree, Spin,
  Drawer, Table, Popconfirm, Tooltip, Alert,} from "antd";
import { PlusOutlined, CopyOutlined, InboxOutlined, SearchOutlined, DeleteOutlined, EditOutlined, BookOutlined, ProjectOutlined, FileTextOutlined, ApiOutlined } from "@ant-design/icons";
import { useParams, useNavigate } from "react-router-dom";
import { projectsApi, schemesApi, schemeCatalogApi, knowledgeApi } from "../api";
import { hookAntdMessage } from "../utils/activityCenter";

import { PageHero, StatCards, StatItem } from "../utils/ui";

const { Title } = Typography;

const SCHEME_TYPES = [
  "深基坑", "高支模", "脚手架", "塔吊", "施工电梯", "临时用电",
  "消防", "安全文明", "绿色施工", "质量创优", "进度计划",
  "应急预案", "施工组织设计", "钢结构吊装", "降水", "土方开挖",
  "模板工程", "混凝土工程", "有限空间",
];

export default function ProjectDetailPage() {
  const { id } = useParams();
  const { message: _antdMsg, modal } = App.useApp();
  const msg = hookAntdMessage(_antdMsg, "项目详情");
  const navigate = useNavigate();
  const [project, setProject] = useState<any>(null);
  const [schemes, setSchemes] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  const [catalogData, setCatalogData] = useState<Record<string, any[]>>({});
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogKeyword, setCatalogKeyword] = useState("");
  const [selectedCatalogId, setSelectedCatalogId] = useState<string>("");
  const [selectedCatalogName, setSelectedCatalogName] = useState<string>("");
  const [outlinePreview, setOutlinePreview] = useState<any[]>([]);
  const [outlineLoading, setOutlineLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // 方案编辑（重命名/改类型/专业/字数）—— 补齐 schemesApi.update 的前端入口
  const [editOpen, setEditOpen] = useState(false);
  const [editing, setEditing] = useState<any>(null);
  const [editForm] = Form.useForm();
  const [editSubmitting, setEditSubmitting] = useState(false);
  // ✅ 竞态守卫：快速切换项目/目录时，旧请求的响应不应覆盖新数据
  const loadSeqRef = useRef(0);
  const previewSeqRef = useRef(0);

  const load = async () => {
    if (!id) return;
    const seq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const [{ data: p }, { data: s }] = await Promise.all([
        projectsApi.get(id),
        schemesApi.list(id),
      ]);
      if (seq !== loadSeqRef.current) return; // 已有更新的请求发出，丢弃旧响应
      setProject(p);
      setSchemes(s.items || []);
    } catch (e: any) {
      if (seq === loadSeqRef.current) msg.error(e.message || "加载失败");
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  };

  const loadCatalog = async (keyword?: string) => {
    setCatalogLoading(true);
    try {
      const { data } = await schemeCatalogApi.list(keyword ? { keyword } : undefined);
      setCatalogData(data.categories || {});
    } catch (e: any) {
      msg.error(e.message || "加载清单失败");
    }
    setCatalogLoading(false);
  };

  const loadOutlinePreview = async (libraryId: string) => {
    const seq = ++previewSeqRef.current;
    setOutlineLoading(true);
    try {
      const { data } = await schemeCatalogApi.get(libraryId);
      if (seq !== previewSeqRef.current) return; // 快速切换目录，丢弃旧响应
      const outline = typeof data.outline_json === "string"
        ? JSON.parse(data.outline_json) : data.outline_json;
      setOutlinePreview(outline || []);
    } catch (e: any) {
      if (seq === previewSeqRef.current) {
        setOutlinePreview([]);
        if (e?.message) msg.warning(`目录预览加载失败：${e.message}`);
      }
    } finally {
      if (seq === previewSeqRef.current) setOutlineLoading(false);
    }
  };

  useEffect(() => { load(); }, [id]);

  useEffect(() => {
    if (modalOpen) {
      loadCatalog();
    }
  }, [modalOpen]);

  const handleCreateScheme = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      const payload: any = { ...values };
      if (selectedCatalogId) {
        payload.outline_source = "library";
        payload.library_ids = [selectedCatalogId];
      }
      const { data } = await schemesApi.create(id!, payload);
      // ✅ 断链修复：后端现在会在创建阶段直接套用所选目录库并写入 sections。
      //    这里把实际落库的章节数反馈给用户，并给出「进入编制」的直达入口
      //    （旧实现创建完方案章节树为空，用户进工作台只能看到空态，不知从何下手）。
      const applied = data?.applied_library;
      if (applied) {
        msg.success(
          `方案创建成功，已套用「${applied.name}」共 ${applied.count} 个章节`);
        modal.confirm({
          title: "目录已套用",
          content: `已按「${applied.name}」（${applied.version}）生成 ${applied.count} 个章节，是否立即进入编制？`,
          okText: "进入编制",
          cancelText: "稍后",
          onOk: () => navigate(`/scheme/${data.id}`),
        });
      } else {
        msg.success("方案创建成功");
      }
      setModalOpen(false);
      form.resetFields();
      setSelectedCatalogId("");
      setSelectedCatalogName("");
      setOutlinePreview([]);
      setCatalogKeyword("");
      load();
    } catch (e: any) {
      if (e.errorFields) return;
      msg.error(e.message || "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleDuplicate = async (sid: string) => {
    try {
      await schemesApi.duplicate(id!, sid);
      // ✅ 修复（2026-09-18）：后端 duplicate 只复制目录结构与事实，**不复制正文**
      //    （新方案章节 status 一律 empty）。旧文案「方案已复制」会让用户以为
      //    正文也在，打开副本发现全空。改为如实说明。
      msg.success("已复制目录结构（副本正文需重新生成）");
      load();
    } catch (e: any) {
      msg.error(e.message || "复制失败");
    }
  };

  const handleArchive = async (sid: string) => {
    try {
      await schemesApi.archive(id!, sid);
      msg.success("操作成功");
      load();
    } catch (e: any) {
      msg.error(e.message || "操作失败");
    }
  };

  /** 打开编辑弹窗并预填当前方案字段 */
  const openEdit = (s: any) => {
    setEditing(s);
    editForm.setFieldsValue({
      name: s.name,
      type: s.type,
      profession: s.profession,
      word_budget: s.word_budget,
    });
    setEditOpen(true);
  };

  /** 保存方案改名/改类型（PATCH schemesApi.update） */
  const handleUpdateScheme = async () => {
    try {
      const values = await editForm.validateFields();
      setEditSubmitting(true);
      await schemesApi.update(id!, editing.id, values);
      msg.success("方案已更新");
      setEditOpen(false);
      setEditing(null);
      load();
    } catch (e: any) {
      if (e.errorFields) return; // 校验未通过
      msg.error(e.message || "更新失败");
    } finally {
      setEditSubmitting(false);
    }
  };

  /** 删除专项方案（危险操作，二次确认；清理章节/事实/图表/导出缓存与文件） */
  const handleDeleteScheme = (sid: string, name: string) => {
    modal.confirm({
      title: "删除专项方案",
      icon: <DeleteOutlined style={{ color: "#ff4d4f" }} />,
      content: (
        <div>
          <p>确定删除「{name}」？该操作<b>不可恢复</b>，将一并删除：</p>
          <ul style={{ paddingLeft: 20, margin: "4px 0" }}>
            <li>全部章节正文与目录</li>
            <li>全局事实（含未确认的模拟值）</li>
            <li>图表数据与历史导出缓存/文件</li>
          </ul>
          <p style={{ color: "#999", fontSize: 12 }}>
            项目资料文档（project_documents）为项目级共享，不会被删除。
          </p>
        </div>
      ),
      okText: "确认删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await schemesApi.delete(id!, sid);
          msg.success(`方案「${data.name || name}」已删除`);
          load();
        } catch (e: any) {
          msg.error(e.message || "删除失败");
        }
      },
    });
  };

  const handleSelectCatalog = (libraryId: string, name: string) => {
    setSelectedCatalogId(libraryId);
    setSelectedCatalogName(name);
    form.setFieldValue("name", name.replace("标准目录", ""));
    loadOutlinePreview(libraryId);
  };

  const handleCatalogSearch = () => {
    loadCatalog(catalogKeyword);
  };

  const statusColor: Record<string, string> = {
    "草稿": "default", "目录生成中": "processing", "目录已确认": "blue",
    "正文生成中": "processing", "审核中": "warning", "已完成": "success",
    "已归档": "default",
  };

  // ===== ✅ 知识库 / 素材库（§3.9）：项目级条目，AI 生成目录/正文时自动注入 =====
  const [kbOpen, setKbOpen] = useState(false);
  const [kbItems, setKbItems] = useState<any[]>([]);
  const [kbLoading, setKbLoading] = useState(false);
  const [kbModalOpen, setKbModalOpen] = useState(false);
  const [kbEditing, setKbEditing] = useState<any>(null);
  const [kbSaving, setKbSaving] = useState(false);
  const [kbForm] = Form.useForm();

  const loadKnowledge = async () => {
    if (!id) return;
    setKbLoading(true);
    try {
      const { data } = await knowledgeApi.list({ project_id: id });
      setKbItems(data.items || []);
    } catch (e: any) {
      msg.error(e.message || "加载知识库失败");
    } finally {
      setKbLoading(false);
    }
  };

  const openKbDrawer = () => {
    setKbOpen(true);
    loadKnowledge();
  };

  const handleKbEdit = (item: any | null) => {
    setKbEditing(item);
    setKbModalOpen(true);
    if (item) {
      kbForm.setFieldsValue({ name: item.name, usage_hint: item.usage_hint, content: item.content });
    } else {
      kbForm.resetFields();
    }
  };

  const handleKbSave = async () => {
    const values = await kbForm.validateFields();
    setKbSaving(true);
    try {
      if (kbEditing) {
        await knowledgeApi.update(kbEditing.id, values);
        msg.success("知识条目已更新");
      } else {
        await knowledgeApi.create({ project_id: id, ...values });
        msg.success("知识条目已添加");
      }
      setKbModalOpen(false);
      loadKnowledge();
    } catch (e: any) {
      msg.error(e.message || "保存失败");
    } finally {
      setKbSaving(false);
    }
  };

  const handleKbDelete = async (kid: string) => {
    try {
      await knowledgeApi.delete(kid);
      msg.success("已删除");
      loadKnowledge();
    } catch (e: any) {
      msg.error(e.message || "删除失败");
    }
  };

  const outlineToTreeData = (nodes: any[], parentKey = "root"): any[] => {
    // ✅ 修复 P1：原用同级索引作 key，不同父节点的第 N 个子节点 key 全部重复，
    // 导致目录预览展开/选中错乱；改用 路径key（parentKey-索引）保证全局唯一
    // ✅ 增强：展示 description（编写要点）—— 预览所见 == 套用所得，
    // 旧实现只显示 title，用户无法据此判断目录质量（编写要点才是目录的核心价值）。
    return nodes.map((n, i) => ({
      key: `${parentKey}-${i}`,
      title: (
        <span>
          <span style={{ fontWeight: n.level === 1 ? 600 : 400 }}>{n.title}</span>
          {n.description ? (
            <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
              {n.description}
            </Typography.Text>
          ) : null}
        </span>
      ),
      children: n.children ? outlineToTreeData(n.children, `${parentKey}-${i}`) : [],
    }));
  };

  const stats: StatItem[] = [
    { icon: <ProjectOutlined />, label: "关联方案", value: schemes.length, color: "#00D4FF" },
    { icon: <FileTextOutlined />, label: "文档数", value: catalogData ? Object.values(catalogData).reduce((a, b: any[]) => a + b.length, 0) : 0, color: "#00C853" },
    { icon: <BookOutlined />, label: "知识库", value: kbItems?.length ?? 0, color: "#FFB800" },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, height: "100%" }}>
      <PageHero
        title={project?.name || "项目详情"}
        subtitle="BLUEPRINT · PROJECT DETAIL"
        description={project?.description || "管理项目下所有专项方案、知识库与资料文档"}
        accent={<span style={{ fontSize: 12, color: "rgba(255,255,255,0.65)" }}>
          {project?.engineering_type && <Tag color="blue">{project.engineering_type}</Tag>}
        </span>}
      />
      <StatCards items={stats} />
      <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, overflow: "auto" }}>
        <Descriptions
          title={project?.name || "项目详情"}
        bordered
        column={2}
        size="small"
        style={{ marginBottom: 16 }}
      >
        <Descriptions.Item label="工程类型">{project?.engineering_type || "-"}</Descriptions.Item>
        <Descriptions.Item label="项目地点">{project?.location || "-"}</Descriptions.Item>
        <Descriptions.Item label="建设单位">{project?.client_name || "-"}</Descriptions.Item>
        <Descriptions.Item label="施工单位">{project?.contractor_name || "-"}</Descriptions.Item>
        <Descriptions.Item label="项目周期">{project?.project_period || "-"}</Descriptions.Item>
        <Descriptions.Item label="项目描述">{project?.description || "-"}</Descriptions.Item>
      </Descriptions>

      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
        <Title level={4}>专项方案列表</Title>
        <Space>
          <Button icon={<BookOutlined />} onClick={openKbDrawer}>
            知识库
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
            新建方案
          </Button>
        </Space>
      </div>

      {schemes.length === 0 && !loading ? (
        <Empty description="暂无方案，请新建" />
      ) : (
        <List
          grid={{ gutter: 16, column: 3 }}
          dataSource={schemes}
          loading={loading}
          renderItem={(s: any) => (
            <List.Item>
              <Card
                hoverable
                onClick={() => navigate(`/scheme/${s.id}`)}
                actions={[
                  <EditOutlined key="edit" onClick={(e: any) => {
                    e.stopPropagation();
                    openEdit(s);
                  }} />,
                  <CopyOutlined key="copy" onClick={(e: any) => {
                    e.stopPropagation();
                    handleDuplicate(s.id);
                  }} />,
                  <InboxOutlined key="archive" onClick={(e: any) => {
                    e.stopPropagation();
                    handleArchive(s.id);
                  }} />,
                  <DeleteOutlined key="delete" style={{ color: "#ff4d4f" }} onClick={(e: any) => {
                    e.stopPropagation();
                    handleDeleteScheme(s.id, s.name);
                  }} />,
                ]}
              >
                <Card.Meta
                  title={s.name}
                  description={
                    <Space direction="vertical" size={4}>
                      <Space>
                        {s.type && <Tag color="blue">{s.type}</Tag>}
                        <Tag color={statusColor[s.status] || "default"}>{s.status}</Tag>
                      </Space>
                      <span>{s.section_count || 0} 章节 · {s.word_count || 0} 字</span>
                      <span>目录来源：{s.outline_source || "-"}</span>
                    </Space>
                  }
                />
              </Card>
            </List.Item>
          )}
        />
      )}

      <Modal
        title="新建专项方案"
        open={modalOpen}
        forceRender
        confirmLoading={submitting}
        okButtonProps={{ disabled: submitting }}
        onOk={handleCreateScheme}
        onCancel={() => {
          setModalOpen(false);
          form.resetFields();
          setSelectedCatalogId("");
          setSelectedCatalogName("");
          setOutlinePreview([]);
          setCatalogKeyword("");
        }}
        width={780}
        okText="创建方案"
        cancelText="取消"
      >
        <Tabs
          items={[
            {
              key: "manual",
              // ✅ antd Tabs 默认只渲染 active 面板，form 实例在非 active 时无 Form DOM
              // → 控制台报 "useForm not connected"。forceRender 让手动填写面板始终挂载。
              forceRender: true,
              label: "手动填写",
              children: (
                <Form form={form} layout="vertical">
                  <Form.Item name="name" label="方案名称" rules={[{ required: !selectedCatalogId }]}>
                    <Input placeholder="如：深基坑专项施工方案" />
                  </Form.Item>
                  <Form.Item name="type" label="方案类型">
                    <Select options={SCHEME_TYPES.map(t => ({ label: t, value: t }))} />
                  </Form.Item>
                  <Form.Item name="profession" label="专业">
                    <Select options={["土建", "机电", "钢结构", "安全", "绿色施工"].map(t => ({ label: t, value: t }))} />
                  </Form.Item>
                  <Form.Item name="word_budget" label="目标字数" initialValue={30000}>
                    <Input type="number" />
                  </Form.Item>
                  <Form.Item name="outline_source" label="目录来源" initialValue="ai">
                    <Select options={[
                      { label: "AI 生成", value: "ai" },
                      { label: "目录库调用", value: "library" },
                      { label: "上传目录识别", value: "upload" },
                      { label: "混合生成", value: "mixed" },
                    ]} />
                  </Form.Item>
                </Form>
              ),
            },
            {
              key: "catalog",
              label: "从清单选择",
              children: (
                <div>
                  <Space style={{ marginBottom: 12, width: "100%" }}>
                    <Input
                      placeholder="搜索方案名称..."
                      value={catalogKeyword}
                      onChange={(e) => setCatalogKeyword(e.target.value)}
                      onPressEnter={handleCatalogSearch}
                      style={{ width: 300 }}
                      prefix={<SearchOutlined />}
                    />
                    <Button onClick={handleCatalogSearch}>搜索</Button>
                    <Button onClick={() => { setCatalogKeyword(""); loadCatalog(); }}>重置</Button>
                  </Space>

                  {selectedCatalogId && (
                    <div style={{
                      marginBottom: 12, padding: "8px 12px",
                      background: "rgba(24,144,255,0.1)", borderRadius: 6,
                    }}>
                      <Space>
                        <Tag color="blue">已选择</Tag>
                        <span style={{ fontWeight: 500 }}>{selectedCatalogName}</span>
                        <Button size="small" type="link" onClick={() => {
                          setSelectedCatalogId("");
                          setSelectedCatalogName("");
                          setOutlinePreview([]);
                          form.setFieldValue("name", "");
                        }}>取消选择</Button>
                      </Space>
                    </div>
                  )}

                  <Spin spinning={catalogLoading}>
                    <div style={{ maxHeight: 320, overflowY: "auto", paddingRight: 8 }}>
                      {Object.entries(catalogData).map(([category, items]) => (
                        items.length > 0 && (
                          <div key={category} style={{ marginBottom: 12 }}>
                            <div style={{
                              fontWeight: 600, fontSize: 13, marginBottom: 6,
                              color: "rgba(255,255,255,0.85)",
                              borderBottom: "1px solid rgba(255,255,255,0.15)",
                              paddingBottom: 4,
                            }}>
                              {category} ({items.length})
                            </div>
                            <List
                              size="small"
                              dataSource={items}
                              renderItem={(item: any) => (
                                <List.Item
                                  style={{
                                    padding: "4px 8px",
                                    cursor: "pointer",
                                    background: selectedCatalogId === item.id
                                      ? "rgba(24,144,255,0.15)" : "transparent",
                                    borderRadius: 4,
                                  }}
                                  onClick={() => handleSelectCatalog(item.id, item.name)}
                                >
                                  <Space>
                                    <span>{item.name.replace("标准目录", "")}</span>
                                    {item.ref_count > 0 && (
                                      <Tag style={{ fontSize: 11 }}>引用{item.ref_count}</Tag>
                                    )}
                                  </Space>
                                </List.Item>
                              )}
                            />
                          </div>
                        )
                      ))}
                    </div>
                  </Spin>

                  {selectedCatalogId && outlinePreview.length > 0 && (
                    <div style={{ marginTop: 12 }}>
                      <div style={{ fontWeight: 600, marginBottom: 6 }}>标准目录预览：</div>
                      <Spin spinning={outlineLoading}>
                        <Tree
                          treeData={outlineToTreeData(outlinePreview)}
                          defaultExpandAll
                          selectable={false}
                          style={{ maxHeight: 200, overflowY: "auto" }}
                        />
                      </Spin>
                    </div>
                  )}
                </div>
              ),
            },
          ]}
        />
      </Modal>

      {/* 方案编辑：重命名 / 改类型 / 专业 / 目标字数 —— 调用 schemesApi.update */}
      <Modal
        title="编辑方案"
        open={editOpen}
        forceRender
        confirmLoading={editSubmitting}
        okButtonProps={{ disabled: editSubmitting }}
        onOk={handleUpdateScheme}
        onCancel={() => { setEditOpen(false); setEditing(null); }}
        okText="保存"
        cancelText="取消"
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="name" label="方案名称" rules={[{ required: true, message: "请输入方案名称" }]}>
            <Input placeholder="如：深基坑专项施工方案" />
          </Form.Item>
          <Form.Item name="type" label="方案类型">
            <Select options={SCHEME_TYPES.map(t => ({ label: t, value: t }))} />
          </Form.Item>
          <Form.Item name="profession" label="专业">
            <Select options={["土建", "机电", "钢结构", "安全", "绿色施工"].map(t => ({ label: t, value: t }))} />
          </Form.Item>
          <Form.Item name="word_budget" label="目标字数">
            <Input type="number" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 知识库 / 素材库管理（§3.9）：条目会在 AI 生成目录/正文时自动注入 */}
      <Drawer
        title="项目知识库"
        width={640}
        open={kbOpen}
        onClose={() => setKbOpen(false)}
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => handleKbEdit(null)}>
            新增条目
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="知识条目（企业管理制度、工艺要点、常用数据等）会在 AI 生成目录与正文时自动作为素材注入，数据口径仍以「全局事实」为准。"
        />
        <Table
          dataSource={kbItems}
          rowKey="id"
          loading={kbLoading}
          size="small"
          pagination={false}
          locale={{ emptyText: <Empty description="暂无知识条目" /> }}
          columns={[
            { title: "名称", dataIndex: "name", key: "name", width: 140 },
            {
              title: "内容", dataIndex: "content", key: "content",
              ellipsis: true,
              render: (v: string, r: any) => (
                <Tooltip title={v}>
                  <span>{r.usage_hint ? <Tag color="blue" style={{ marginRight: 4 }}>{r.usage_hint}</Tag> : null}{v || "-"}</span>
                </Tooltip>
              ),
            },
            {
              title: "操作", key: "actions", width: 110,
              render: (_: any, r: any) => (
                <Space>
                  <Button size="small" type="link" icon={<EditOutlined />} onClick={() => handleKbEdit(r)} />
                  <Popconfirm title="确定删除该条目？" onConfirm={() => handleKbDelete(r.id)}>
                    <Button size="small" type="link" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Drawer>

      {/* 知识条目新增/编辑 */}
      <Modal
        title={kbEditing ? "编辑知识条目" : "新增知识条目"}
        open={kbModalOpen}
        forceRender
        confirmLoading={kbSaving}
        onOk={handleKbSave}
        onCancel={() => { setKbModalOpen(false); setKbEditing(null); kbForm.resetFields(); }}
        okText="保存"
        cancelText="取消"
      >
        <Form form={kbForm} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入条目名称" }]}>
            <Input placeholder="如：公司安全交底规范" />
          </Form.Item>
          <Form.Item name="usage_hint" label="用途提示（可选）">
            <Input placeholder="如：安全交底章节引用" />
          </Form.Item>
          <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}>
            <Input.TextArea rows={8} placeholder="粘贴制度条文、工艺要点或常用数据…" />
          </Form.Item>
        </Form>
      </Modal>
      </div>
    </div>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import {
  App, Card, Table, Button, Tag, Space, Typography, Modal, Form, Input,
  Select, Row, Col, Statistic, Drawer, Tree, Dropdown, Tooltip, Empty, Descriptions,
} from "antd";
import {
  PlusOutlined, EditOutlined, BranchesOutlined, CopyOutlined, ExportOutlined,
  EyeOutlined, HistoryOutlined, SearchOutlined, ReloadOutlined, DownOutlined,
  FileTextOutlined, FileMarkdownOutlined, CodeOutlined, CheckCircleOutlined,
  StopOutlined, PlayCircleOutlined, DeleteOutlined,
} from "@ant-design/icons";
import { outlineLibraryApi, systemApi } from "../api";
import { useAntdMessageHub } from "../utils/activityCenter";

import OutlineLibraryEditModal from "../components/OutlineLibraryEditModal";

import { PageHero, StatCards, StatItem } from '../utils/ui';

const { Title, Text, Paragraph } = Typography;

/** 目录树节点（与后端 outline_json 结构一致） */
type OutlineNode = {
  id: string;
  title: string;
  description?: string;
  level: number;
  children: OutlineNode[];
};

const REVIEW_COLOR: Record<string, string> = {
  "待审核": "orange", "已通过": "green", "已停用": "default",
};

const SOURCE_COLOR: Record<string, string> = {
  "预置清单": "blue", "上传识别": "purple", "手动创建": "cyan",
  "复制": "geekblue", "存为目录库": "purple",
};

/**
 * 单行省略样式。名称列的标题与「适用条件」都必须截断：
 * 这两个字段是长中文串，若不截断会横向溢出单元格、压在后几列的文案之上。
 */
const ellipsisCell: CSSProperties = {
  display: "block", maxWidth: "100%",
  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
};

/** 把目录树转成 antd Tree 数据（标题后附描述） */
function toTreeData(nodes: OutlineNode[]): any[] {
  return (nodes || []).map((n) => ({
    key: n.id,
    title: (
      <span>
        <span style={{ fontWeight: n.level === 1 ? 600 : 400 }}>{n.title}</span>
        {n.description ? (
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
            {n.description}
          </Text>
        ) : null}
      </span>
    ),
    children: toTreeData(n.children || []),
  }));
}

function collectKeys(nodes: OutlineNode[]): string[] {
  const keys: string[] = [];
  const walk = (ns: OutlineNode[]) =>
    ns.forEach((n) => {
      keys.push(n.id);
      if (n.children?.length) walk(n.children);
    });
  walk(nodes || []);
  return keys;
}

function countNodes(nodes: OutlineNode[]): number {
  return (nodes || []).reduce(
    (acc, n) => acc + 1 + countNodes(n.children || []), 0);
}

function parseOutline(raw: any): OutlineNode[] {
  if (!raw) return [];
  if (typeof raw === "string") {
    try {
      raw = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (raw && !Array.isArray(raw) && Array.isArray(raw.outline)) raw = raw.outline;
  return Array.isArray(raw) ? raw : [];
}

/** 触发浏览器下载 */
function downloadFile(fileName: string, content: string) {
  // ✅ 编码修复：纯文本下载前置 UTF-8 BOM（\uFEFF），避免 Windows 记事本打开中文乱码。
  const blob = new Blob(["\uFEFF" + content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function OutlineLibraryPage() {
  const { message: _antdMsg, modal } = App.useApp();
  const msg = useAntdMessageHub(_antdMsg, "目录库");
  const [items, setItems] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState<any>({});
  const [filters, setFilters] = useState<any>({});

  // 查询条件
  const [keyword, setKeyword] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [fType, setFType] = useState<string>();
  const [fEng, setFEng] = useState<string>();
  const [fProf, setFProf] = useState<string>();
  const [fStatus, setFStatus] = useState<string>();
  const [fSource, setFSource] = useState<string>();
  const [sortBy, setSortBy] = useState("ref_count");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);

  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  // 单文件体积上限（字节）：与方案工作台同源（/system/upload-limits）。
  // 未到达 / 请求失败时保持 undefined，由弹窗回落内置兜底（30MB），避免与后端配置漂移。
  const [maxUploadBytes, setMaxUploadBytes] = useState<number | undefined>(undefined);

  // 启动时读取一次上传配额（只读、带重试；后端临时不可达不致命）
  useEffect(() => {
    let alive = true;
    systemApi.uploadLimits()
      .then(({ data }) => {
        if (!alive) return;
        const b = Number(data?.max_upload_bytes);
        if (Number.isFinite(b) && b > 0) setMaxUploadBytes(b);
      })
      .catch(() => { /* 回落兜底 */ });
    return () => { alive = false; };
  }, []);

  // 预览抽屉
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewData, setPreviewData] = useState<any>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  // 版本历史抽屉
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyData, setHistoryData] = useState<any>(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string> = { sort_by: sortBy, order: "desc" };
      if (keyword) params.keyword = keyword;
      if (fType) params.type = fType;
      if (fEng) params.engineering_type = fEng;
      if (fProf) params.profession = fProf;
      if (fStatus) params.review_status = fStatus;
      if (fSource) params.source = fSource;
      params.page = String(page);
      params.page_size = String(pageSize);
      const { data } = await outlineLibraryApi.list(params);
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (e: any) {
      msg.error(e.message || "加载失败");
    }
    setLoading(false);
  }, [keyword, fType, fEng, fProf, fStatus, fSource, sortBy, page, pageSize]);

  const loadMeta = useCallback(async () => {
    try {
      const [s, f] = await Promise.all([
        outlineLibraryApi.stats(), outlineLibraryApi.filters(),
      ]);
      setStats(s.data || {});
      setFilters(f.data || {});
    } catch { /* 统计失败不阻断主流程 */ }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadMeta(); }, [loadMeta]);

  const resetFilters = () => {
    setSearchInput(""); setKeyword(""); setFType(undefined); setFEng(undefined);
    setFProf(undefined); setFStatus(undefined); setFSource(undefined);
    setPage(1);
  };

  const refresh = () => { load(); loadMeta(); };

  // ---------------- 审核 ----------------
  const handleReview = async (id: string, status: string) => {
    try {
      await outlineLibraryApi.review(id, { status });
      msg.success(`已${status === "已通过" ? "通过" : status === "已停用" ? "停用" : "置为待审核"}`);
      refresh();
    } catch (e: any) {
      msg.error(e.message || "审核操作失败");
    }
  };

  const handleBatchReview = (status: string) => {
    if (!selectedRowKeys.length) return;
    modal.confirm({
      title: `确认将选中的 ${selectedRowKeys.length} 条目录设为「${status}」？`,
      okText: "确定", cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await outlineLibraryApi.batchReview({
            ids: selectedRowKeys, status,
          });
          msg.success(`已更新 ${data.affected ?? 0} 条`);
          setSelectedRowKeys([]);
          refresh();
        } catch (e: any) {
          msg.error(e.message || "批量审核失败");
        }
      },
    });
  };

  // ---------------- 删除 ----------------
  const handleDelete = async (id: string) => {
    modal.confirm({
      title: "确认删除该目录？",
      content: "删除后不可恢复，引用该目录的方案不受影响。",
      okText: "删除", okType: "danger", cancelText: "取消",
      onOk: async () => {
        try {
          await outlineLibraryApi.delete(id);
          msg.success("已删除");
          refresh();
        } catch (e: any) {
          msg.error(e.message || "删除失败");
        }
      },
    });
  };

  // ---------------- 复制 ----------------
  const handleDuplicate = async (record: any) => {
    try {
      const { data } = await outlineLibraryApi.duplicate(record.id);
      msg.success(`已复制为「${data.name}」`);
      refresh();
    } catch (e: any) {
      msg.error(e.message || "复制失败");
    }
  };

  // ---------------- 导出 ----------------
  const handleExport = async (record: any, format: "text" | "markdown" | "json") => {
    try {
      const { data } = await outlineLibraryApi.export(record.id, format);
      downloadFile(data.file_name, data.content);
      msg.success(`已导出 ${data.node_count} 个章节`);
    } catch (e: any) {
      msg.error(e.message || "导出失败");
    }
  };

  // ---------------- 预览 ----------------
  const openPreview = async (record: any) => {
    setPreviewOpen(true);
    setPreviewLoading(true);
    setPreviewData(null);
    try {
      const { data } = await outlineLibraryApi.get(record.id);
      setPreviewData(data);
    } catch (e: any) {
      msg.error(e.message || "读取目录失败");
    } finally {
      setPreviewLoading(false);
    }
  };

  const previewOutline = useMemo(
    () => parseOutline(previewData?.outline_json), [previewData]);

  // ---------------- 版本历史 ----------------
  const openHistory = async (record: any) => {
    setHistoryOpen(true);
    setHistoryLoading(true);
    setHistoryData(null);
    try {
      const { data } = await outlineLibraryApi.get(record.id);
      setHistoryData(data);
    } catch (e: any) {
      msg.error(e.message || "读取版本历史失败");
    } finally {
      setHistoryLoading(false);
    }
  };

  const handleRestore = (versionId: string, version: string) => {
    if (!historyData) return;
    modal.confirm({
      title: `确认回滚到 ${version}？`,
      content: "当前版本会先归档保存，回滚后该目录回到「待审核」状态。",
      okText: "回滚", cancelText: "取消",
      onOk: async () => {
        try {
          await outlineLibraryApi.restoreVersion(historyData.id, { version_id: versionId });
          msg.success(`已回滚到 ${version}`);
          setHistoryOpen(false);
          refresh();
        } catch (e: any) {
          msg.error(e.message || "回滚失败");
        }
      },
    });
  };

  // ---------------- 建新版本 ----------------
  const [verOpen, setVerOpen] = useState(false);
  const [verTarget, setVerTarget] = useState<any>(null);
  const [verSubmitting, setVerSubmitting] = useState(false);
  const [verLoading, setVerLoading] = useState(false);
  const [verForm] = Form.useForm();

  const openNewVersion = async (record: any) => {
    setVerTarget(record);
    setVerOpen(true);
    setVerLoading(true);
    try {
      const { data } = await outlineLibraryApi.get(record.id);
      const outline = typeof data.outline_json === "string"
        ? data.outline_json
        : JSON.stringify(data.outline_json || [], null, 2);
      const cur = String(data.version || record.version || "v1.0");
      const m = cur.match(/^v?(\d+)\.(\d+)/i);
      const next = m ? `v${Number(m[1]) + 1}.0` : "v2.0";
      verForm.setFieldsValue({ version: next, outline_json: outline });
    } catch (e: any) {
      msg.error(e.message || "读取目录库内容失败");
    } finally {
      setVerLoading(false);
    }
  };

  const handleNewVersion = async () => {
    if (!verTarget) return;
    try {
      const values = await verForm.validateFields();
      setVerSubmitting(true);
      let parsed: any;
      try {
        parsed = JSON.parse(values.outline_json || "[]");
      } catch {
        msg.error("目录 JSON 格式不合法，请检查括号与逗号");
        return;
      }
      await outlineLibraryApi.newVersion(verTarget.id, {
        version: values.version,
        outline_json: JSON.stringify(parsed, null, 2),
      });
      msg.success(`已创建版本 ${values.version}，旧版本已存档`);
      setVerOpen(false);
      setVerTarget(null);
      refresh();
    } catch (e: any) {
      if (e.errorFields) return;
      msg.error(e.message || "创建版本失败");
    } finally {
      setVerSubmitting(false);
    }
  };

  // ---------------- 表格列 ----------------
  const exportMenu = (record: any) => ({
    items: [
      { key: "text", icon: <FileTextOutlined />, label: "纯文本（多级编号）" },
      { key: "markdown", icon: <FileMarkdownOutlined />, label: "Markdown" },
      { key: "json", icon: <CodeOutlined />, label: "JSON（可再导入）" },
    ],
    onClick: ({ key }: any) => handleExport(record, key),
  });

  const columns: any[] = [
    {
      title: "名称", dataIndex: "name", key: "name", width: 260,
      render: (t: string, r: any) => {
        const sub = r.applicable_conditions || r.basis;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <a onClick={() => openPreview(r)} title={t} style={ellipsisCell}>{t}</a>
            {sub ? (
              <Text type="secondary" title={sub} style={{ ...ellipsisCell, fontSize: 12 }}>
                {sub}
              </Text>
            ) : null}
          </div>
        );
      },
    },
    {
      title: "类型", dataIndex: "type", key: "type", width: 100,
      render: (t: string) => t ? <Tag color="blue">{t}</Tag> : "-",
    },
    {
      title: "专业", dataIndex: "profession", key: "profession", width: 80,
      render: (t: string) => t ? <Tag>{t}</Tag> : "-",
    },
    {
      title: "版本", dataIndex: "version", key: "version", width: 70,
    },
    {
      title: "审核状态", dataIndex: "review_status", key: "review_status", width: 96,
      render: (s: string) => <Tag color={REVIEW_COLOR[s] || "default"}>{s}</Tag>,
    },
    {
      title: "引用", dataIndex: "ref_count", key: "ref_count", width: 60, sorter: false,
    },
    {
      // 需容纳最长的来源标签「存为目录库」（约 86px）+ 单元格 padding 24px
      title: "来源", dataIndex: "source", key: "source", width: 110,
      render: (s: string) => <Tag color={SOURCE_COLOR[s] || "default"}>{s || "-"}</Tag>,
    },
    {
      // 须完整显示 "2026-09-20 10:00:00"（约 140px）+ padding，否则会被折成两行
      title: "更新时间", dataIndex: "updated_at", key: "updated_at", width: 170, ellipsis: true,
      render: (t: string) => t ? String(t).slice(0, 19).replace("T", " ") : "-",
    },
    {
      // 宽度须容纳「7 个操作按钮单行排布」(24×6 + 50 + 4×6 = 218) + 单元格左右 padding 24px，
      // 否则最后一个按钮换行，行高被撑成两倍。
      title: "操作", key: "action", width: 250, fixed: "right",
      render: (_: any, record: any) => (
        <Space size={4} wrap>
          <Tooltip title="预览目录结构">
            <Button size="small" icon={<EyeOutlined />} onClick={() => openPreview(record)} />
          </Tooltip>
          <Tooltip title="编辑">
            <Button size="small" type="primary" ghost icon={<EditOutlined />}
              onClick={() => { setEditingId(record.id); setEditModalOpen(true); }} />
          </Tooltip>
          <Tooltip title="复制为副本">
            <Button size="small" icon={<CopyOutlined />} onClick={() => handleDuplicate(record)} />
          </Tooltip>
          <Dropdown menu={exportMenu(record)} trigger={["click"]}>
            <Button size="small" icon={<ExportOutlined />}><DownOutlined /></Button>
          </Dropdown>
          <Tooltip title="版本历史与回滚">
            <Button size="small" icon={<HistoryOutlined />} onClick={() => openHistory(record)} />
          </Tooltip>
          {record.review_status === "待审核" && (
            <Tooltip title="审核通过">
              <Button size="small" type="primary" icon={<CheckCircleOutlined />}
                onClick={() => handleReview(record.id, "已通过")} />
            </Tooltip>
          )}
          {record.review_status === "已通过" && (
            <Tooltip title="停用">
              <Button size="small" danger icon={<StopOutlined />}
                onClick={() => handleReview(record.id, "已停用")} />
            </Tooltip>
          )}
          {record.review_status === "已停用" && (
            <Tooltip title="启用">
              <Button size="small" icon={<PlayCircleOutlined />}
                onClick={() => handleReview(record.id, "已通过")} />
            </Tooltip>
          )}
          <Tooltip title="删除">
            <Button size="small" danger icon={<DeleteOutlined />}
              onClick={() => handleDelete(record.id)} />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const filterOptions = (key: string) =>
    (filters[key] || []).map((f: any) => ({
      label: `${f.value}（${f.count}）`, value: f.value,
    }));

  /**
   * 横向滚动阈值必须等于「各列宽之和 + 勾选列(36)」。
   * 若写得比实际列宽大（历史值 1500 / 实际 1336），antd 会按容器把表宽拉伸到该值并
   * 等比放大每一列 —— 「名称」「操作」被放大到 338px，中间若干列被右侧固定列盖住，
   * 首屏几乎只剩名称与操作两列。
   */
  const tableScrollX =
    columns.reduce((sum: number, c: any) => sum + (typeof c.width === "number" ? c.width : 0), 0) + 36;

  return (
    <div className="scroll-area" style={{ overflowY: "auto", overflowX: "hidden" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <Title level={4} style={{ margin: 0 }}>专项方案目录库</Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => { setEditingId(null); setEditModalOpen(true); }}
          >
            新增目录
          </Button>
        </Space>
      </div>

      {/* 概览统计 */}
      <Row gutter={12} style={{ marginBottom: 16 }}>
        <Col span={4}><Card size="small"><Statistic title="目录总数" value={stats.total || 0} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="已通过" value={stats.approved || 0}
          valueStyle={{ color: "#52c41a" }} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="待审核" value={stats.pending || 0}
          valueStyle={{ color: "#fa8c16" }} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="已停用" value={stats.disabled || 0} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="预置标准目录" value={stats.preset || 0} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title="累计引用" value={stats.ref_total || 0} /></Card></Col>
      </Row>

      <Card size="small" style={{ marginBottom: 12 }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索名称 / 标签 / 分类"
            style={{ width: 260 }}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onSearch={(v) => { setKeyword(v.trim()); setPage(1); }}
            enterButton={<SearchOutlined />}
          />
          <Select allowClear placeholder="方案分类" style={{ width: 170 }}
            value={fType} onChange={(v) => { setFType(v); setPage(1); }}
            options={filterOptions("type")} />
          <Select allowClear placeholder="工程类型" style={{ width: 130 }}
            value={fEng} onChange={(v) => { setFEng(v); setPage(1); }}
            options={filterOptions("engineering_type")} />
          <Select allowClear placeholder="专业" style={{ width: 120 }}
            value={fProf} onChange={(v) => { setFProf(v); setPage(1); }}
            options={filterOptions("profession")} />
          <Select allowClear placeholder="审核状态" style={{ width: 130 }}
            value={fStatus} onChange={(v) => { setFStatus(v); setPage(1); }}
            options={filterOptions("review_status")} />
          <Select allowClear placeholder="来源" style={{ width: 130 }}
            value={fSource} onChange={(v) => { setFSource(v); setPage(1); }}
            options={filterOptions("source")} />
          <Select value={sortBy} onChange={setSortBy} style={{ width: 130 }}
            options={[
              { label: "按引用次数", value: "ref_count" },
              { label: "按更新时间", value: "updated_at" },
              { label: "按创建时间", value: "created_at" },
              { label: "按名称", value: "name" },
            ]} />
          <Button onClick={resetFilters}>重置</Button>
        </Space>
      </Card>

      {selectedRowKeys.length > 0 && (
        <Card size="small" style={{ marginBottom: 12 }}>
          <Space>
            <Text>已选 <Text strong>{selectedRowKeys.length}</Text> 条</Text>
            <Button size="small" type="primary"
              onClick={() => handleBatchReview("已通过")}>批量通过</Button>
            <Button size="small" danger
              onClick={() => handleBatchReview("已停用")}>批量停用</Button>
            <Button size="small" onClick={() => setSelectedRowKeys([])}>取消选择</Button>
          </Space>
        </Card>
      )}

      <Card>
        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          scroll={{ x: tableScrollX }}
          rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
          pagination={{
            current: page, pageSize, total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showTotal: (t) => `共 ${t} 条目录`,
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          }}
        />
      </Card>

      <OutlineLibraryEditModal
        open={editModalOpen}
        libraryId={editingId}
        maxUploadBytes={maxUploadBytes}
        onClose={() => { setEditModalOpen(false); setEditingId(null); }}
        onSaved={refresh}
      />

      {/* 目录预览抽屉 */}
      <Drawer
        title={previewData ? `目录预览 · ${previewData.name}` : "目录预览"}
        open={previewOpen}
        width={720}
        onClose={() => setPreviewOpen(false)}
        extra={previewData && (
          <Space>
            {/* ✅ 增强（2026-10-01）：预览抽屉此前只读，用户在预览时发现名称/章节
                要改必须关抽屉再找列表行的编辑按钮。加直接入口，预览 → 编辑闭环。 */}
            <Button icon={<EditOutlined />}
              onClick={() => {
                setPreviewOpen(false);
                setEditingId(previewData.id);
                setEditModalOpen(true);
              }}>
              编辑本目录
            </Button>
            <Dropdown menu={exportMenu(previewData)} trigger={["click"]}>
              <Button icon={<ExportOutlined />}>导出<DownOutlined /></Button>
            </Dropdown>
          </Space>
        )}
      >
        {previewLoading ? (
          <Text type="secondary">加载中...</Text>
        ) : previewData ? (
          <>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="分类">{previewData.type || "-"}</Descriptions.Item>
              <Descriptions.Item label="版本">{previewData.version || "-"}</Descriptions.Item>
              <Descriptions.Item label="适用条件">{previewData.applicable_conditions || "-"}</Descriptions.Item>
              <Descriptions.Item label="编制依据">{previewData.basis || "-"}</Descriptions.Item>
              <Descriptions.Item label="引用次数">{previewData.ref_count ?? 0}</Descriptions.Item>
            </Descriptions>
            <Paragraph type="secondary" style={{ fontSize: 12 }}>
              共 {countNodes(previewOutline)} 个章节（{previewOutline.length} 个一级章节）
            </Paragraph>
            {previewOutline.length ? (
              <Tree
                treeData={toTreeData(previewOutline)}
                defaultExpandAll
                showLine={{ showLeafIcon: false }}
                blockNode
              />
            ) : <Empty description="该目录暂无章节" />}
          </>
        ) : null}
      </Drawer>

      {/* 版本历史抽屉 */}
      <Drawer
        title={historyData ? `版本历史 · ${historyData.name}` : "版本历史"}
        open={historyOpen}
        width={640}
        onClose={() => setHistoryOpen(false)}
      >
        {historyLoading ? (
          <Text type="secondary">加载中...</Text>
        ) : historyData ? (
          <>
            <Card size="small" title={`当前版本 ${historyData.version || "v1.0"}`}
              style={{ marginBottom: 12 }}>
              <Text type="secondary">
                {historyData.node_count ?? countNodes(parseOutline(historyData.outline_json))} 个章节
                {" · "}更新于 {String(historyData.updated_at || "").slice(0, 19).replace("T", " ")}
              </Text>
              <div style={{ marginTop: 8 }}>
                <Button size="small" icon={<BranchesOutlined />}
                  onClick={() => { setHistoryOpen(false); openNewVersion(historyData); }}>
                  基于当前内容建新版本
                </Button>
              </div>
            </Card>
            {(historyData.versions || []).length === 0 ? (
              <Empty description="暂无历史版本" />
            ) : (
              <Space direction="vertical" style={{ width: "100%" }}>
                {(historyData.versions || []).map((v: any) => (
                  <Card size="small" key={v.id}
                    title={<Space>{v.version}
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {v.node_count ?? 0} 章节
                      </Text></Space>}
                    extra={
                      <Button size="small" onClick={() => handleRestore(v.id, v.version)}>
                        回滚
                      </Button>
                    }>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      归档于 {String(v.created_at || "").slice(0, 19).replace("T", " ")}
                    </Text>
                  </Card>
                ))}
              </Space>
            )}
          </>
        ) : null}
      </Drawer>

      {/* 建立新版本 */}
      <Modal
        title={`建立新版本${verTarget ? ` · ${verTarget.name}` : ""}`}
        open={verOpen}
        forceRender
        onOk={handleNewVersion}
        confirmLoading={verSubmitting}
        okText="创建版本"
        cancelText="取消"
        width={640}
        onCancel={() => { setVerOpen(false); setVerTarget(null); }}
      >
        <Form form={verForm} layout="vertical" disabled={verLoading}>
          <Form.Item name="version" label="新版本号"
            rules={[{ required: true, message: "请输入版本号" }]}>
            <Input placeholder="如：v2.0" />
          </Form.Item>
          <Form.Item name="outline_json" label="目录内容（JSON）"
            extra="保存后当前版本会自动归档，新内容成为该目录库的最新版本并回到「待审核」状态"
            rules={[{ required: true, message: "请输入目录 JSON" }]}>
            <Input.TextArea rows={12}
              style={{ fontFamily: "Consolas, monospace", fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

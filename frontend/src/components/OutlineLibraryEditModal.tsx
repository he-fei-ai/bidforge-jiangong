import { useState, useEffect, useCallback } from "react";
import {App, Modal, Form, Input, Select, Tree, Button, Space, message,
  Card, Tag, Empty, Tooltip, Upload, Typography, Tabs, Alert, Popconfirm,} from "antd";
import {
  CaretRightOutlined, StopOutlined, UploadOutlined, EditOutlined,
  SaveOutlined, PlusOutlined, ArrowUpOutlined, ArrowDownOutlined,
} from "@ant-design/icons";
import { outlineLibraryApi, uploadOutlineApi } from "../api";
import { hookAntdMessage } from "../utils/activityCenter";
import { UPLOAD_FILE_ACCEPT } from "../utils/uploadAccept";

const { Text } = Typography;

/** 目录系统硬性上限：三级（与后端 outline_utils.MAX_OUTLINE_DEPTH 对齐） */
const MAX_OUTLINE_DEPTH = 3;

/** 方案分类兜底候选（实际还会并入库内已有分类） */
const DEFAULT_TYPES = [
  "深基坑", "高支模", "脚手架", "塔吊", "施工电梯", "临时用电",
  "消防", "安全文明", "绿色施工", "质量创优", "进度计划",
  "应急预案", "施工组织设计", "钢结构吊装", "降水", "土方开挖",
  "模板工程", "混凝土工程", "有限空间",
];

type OutlineNode = {
  id: string;
  title: string;
  /** 章节编写要点 / 内容提示（预置标准目录带专业描述，编辑保存时必须保真保留） */
  description?: string;
  level: number;
  children: OutlineNode[];
};

type Props = {
  open: boolean;
  libraryId?: string | null;
  onClose: () => void;
  onSaved: () => void;
};

export default function OutlineLibraryEditModal({ open, libraryId, onClose, onSaved }: Props) {
  const { message: _antdMsg, modal } = App.useApp();
  const msg = hookAntdMessage(_antdMsg, "目录库");
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [tree, setTree] = useState<OutlineNode[]>([]);
  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [renamingKey, setRenamingKey] = useState<string | null>(null);
  const [renamingValue, setRenamingValue] = useState<string>("");
  const [importing, setImporting] = useState(false);
  const [activeTab, setActiveTab] = useState("basic");

  // 行业标准模板 / 分类候选
  const [templates, setTemplates] = useState<any[]>([]);
  const [tplKey, setTplKey] = useState<string | undefined>(undefined);
  const [tplLoading, setTplLoading] = useState(false);
  const [typeOptions, setTypeOptions] = useState<string[]>(DEFAULT_TYPES);

  const isEdit = !!libraryId;

  // 打开时拉取模板清单与库内已有分类
  useEffect(() => {
    if (!open) return;
    outlineLibraryApi.templates()
      .then(({ data }) => setTemplates(data.items || []))
      .catch(() => setTemplates([]));
    outlineLibraryApi.filters()
      .then(({ data }) => {
        const fromDb = (data.type || []).map((t: any) => t.value);
        setTypeOptions(Array.from(new Set([...fromDb, ...DEFAULT_TYPES])));
      })
      .catch(() => setTypeOptions(DEFAULT_TYPES));
  }, [open]);

  const loadDetail = useCallback(async () => {
    if (!libraryId) {
      form.resetFields();
      setTree([]);
      setExpandedKeys([]);
      setSelectedKey(null);
      setTplKey(undefined);
      return;
    }
    setLoading(true);
    try {
      const { data } = await outlineLibraryApi.get(libraryId);
      form.setFieldsValue({
        name: data.name,
        type: data.type,
        engineering_type: data.engineering_type,
        profession: data.profession,
        applicable_conditions: data.applicable_conditions,
        basis: data.basis,
        tags: data.tags,
      });
      let outline: OutlineNode[] = [];
      try {
        outline = JSON.parse(data.outline_json || "[]");
      } catch { /* ignore */ }
      setTree(outline);
      setExpandedKeys(collectAllKeys(outline));
    } catch (e: any) {
      msg.error(e.message || "加载失败");
    }
    setLoading(false);
  }, [libraryId, form]);

  useEffect(() => {
    if (open) loadDetail();
  }, [open, loadDetail]);

  function collectAllKeys(nodes: OutlineNode[]): string[] {
    const keys: string[] = [];
    function walk(n: OutlineNode) {
      keys.push(n.id);
      n.children?.forEach(walk);
    }
    nodes.forEach(walk);
    return keys;
  }

  function findNode(nodes: OutlineNode[], id: string): OutlineNode | null {
    for (const n of nodes) {
      if (n.id === id) return n;
      const found = findNode(n.children, id);
      if (found) return found;
    }
    return null;
  }

  /** 统计节点总数（带深度保护，避免畸形/超环数据导致栈溢出） */
  function countNodes(nodes: OutlineNode[], depth = 0): number {
    if (depth > 50 || !nodes?.length) return 0;
    return nodes.reduce((acc, n) => acc + 1 + countNodes(n.children || [], depth + 1), 0);
  }

  /** 计算目录树最大层级（迭代实现 + 节点数保护，返回 0 表示空树） */
  function maxTreeDepth(nodes: OutlineNode[]): number {
    let max = 0;
    let guard = 0;
    const stack: { node: OutlineNode; level: number }[] = (nodes || []).map((n) => ({ node: n, level: 1 }));
    while (stack.length && guard++ < 20000) {
      const { node, level } = stack.pop()!;
      if (level > max) max = level;
      for (const c of node.children || []) stack.push({ node: c, level: level + 1 });
    }
    return max;
  }

  function addChildNode(parentId?: string) {
    const newId = `local_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newLevel = parentId
      ? (findNode(tree, parentId)?.level || 0) + 1
      : 1;
    // ✅ BUG 修复：目录硬性限三级（后端 normalize_outline_json 会把四级及更深
    //    节点裁剪、标题并入父描述），在三级章节下继续添加子章节会导致
    //    "保存成功后节点凭空消失"。与方案工作台的新增子章节入口保持一致，入口拦截。
    if (newLevel > MAX_OUTLINE_DEPTH) {
      msg.warning(`目录最多支持 ${MAX_OUTLINE_DEPTH} 级，无法在三级章节下继续添加子章节`);
      return;
    }
    const newNode: OutlineNode = {
      id: newId,
      title: "新章节",
      description: "",
      level: newLevel,
      children: [],
    };
    if (!parentId) {
      setTree([...tree, newNode]);
    } else {
      setTree(tree.map((n) => insertIntoNode(n, parentId, newNode)));
      if (!expandedKeys.includes(parentId)) {
        setExpandedKeys([...expandedKeys, parentId]);
      }
    }
    setSelectedKey(newId);
  }

  function insertIntoNode(node: OutlineNode, parentId: string, newNode: OutlineNode): OutlineNode {
    if (node.id === parentId) {
      return { ...node, children: [...node.children, newNode] };
    }
    if (node.children.length) {
      return {
        ...node,
        children: node.children.map((c) => insertIntoNode(c, parentId, newNode)),
      };
    }
    return node;
  }

  function deleteNode(id: string) {
    const target = findNode(tree, id);
    if (!target) return;
    modal.confirm({
      title: "确认删除章节",
      content: `「${target.title}」及其所有子章节将被删除，确定继续？`,
      okButtonProps: { danger: true },
      onOk: () => {
        setTree(removeNode(tree, id));
        if (selectedKey === id) setSelectedKey(null);
      },
    });
  }

  function removeNode(nodes: OutlineNode[], id: string): OutlineNode[] {
    return nodes
      .filter((n) => n.id !== id)
      .map((n) => ({
        ...n,
        children: n.children ? removeNode(n.children, id) : [],
      }));
  }

  /** 扁平顺序收集所有节点 id */
  function flattenNodeKeys(nodes: OutlineNode[]): string[] {
    const keys: string[] = [];
    const walk = (ns: OutlineNode[]) => {
      ns.forEach((n) => {
        keys.push(n.id);
        if (n.children?.length) walk(n.children);
      });
    };
    walk(nodes);
    return keys;
  }

  /**
   * ✅ 目录编辑增强：同级上移/下移（dir=-1 上 / +1 下）。
   * 在兄弟数组中与相邻节点交换顺序，不改层级与父子关系；
   * 已在同层首/尾时返回 null（按钮据此禁用）。目录库为整体保存，
   * 移动仅更新本地树，待用户点「保存」时随 outline_json 一并落库。
   */
  function moveSiblingInTree(
    nodes: OutlineNode[],
    id: string,
    dir: -1 | 1,
  ): OutlineNode[] | null {
    const idx = nodes.findIndex((n) => n.id === id);
    if (idx !== -1) {
      if (idx + dir < 0 || idx + dir >= nodes.length) return null;
      const copy = [...nodes];
      [copy[idx], copy[idx + dir]] = [copy[idx + dir], copy[idx]];
      return copy;
    }
    for (const n of nodes) {
      if (n.children && n.children.length) {
        const res = moveSiblingInTree(n.children, id, dir);
        if (res) {
          return nodes.map((x) => (x.id === n.id ? { ...x, children: res } : x));
        }
      }
    }
    return null;
  }

  function moveNode(id: string, dir: -1 | 1) {
    const newTree = moveSiblingInTree(tree, id, dir);
    if (!newTree) return;
    setTree(newTree);
  }

  // 预计算每个节点是否可上移/下移（同级首/尾禁用），供行内按钮使用
  const moveFlags = (() => {
    const flags: Record<string, { up: boolean; down: boolean }> = {};
    const scan = (ns: OutlineNode[]) => {
      ns.forEach((n, i) => {
        flags[n.id] = { up: i > 0, down: i < ns.length - 1 };
        if (n.children?.length) scan(n.children);
      });
    };
    scan(tree);
    return flags;
  })();

  /** 通用节点属性更新（改名 / 改章节说明统一走这里） */
  function updateNode(id: string, patch: Partial<OutlineNode>) {
    const apply = (nodes: OutlineNode[]): OutlineNode[] =>
      nodes.map((n) => {
        if (n.id === id) return { ...n, ...patch };
        if (n.children?.length) return { ...n, children: apply(n.children) };
        return n;
      });
    setTree(apply(tree));
  }

  function renameNode(id: string, newTitle: string) {
    const trimmed = newTitle.trim();
    if (!trimmed) {
      msg.warning("章节标题不能为空");
      return;
    }
    updateNode(id, { title: trimmed });
  }

  function onTreeDrop(info: any) {
    const dropKey = info.node.key;
    const dragKey = info.dragNode.key;
    const dropPos = info.node.pos.split("-");
    const dropPosition = info.dropPosition - Number(dropPos[dropPos.length - 1]);

    // ✅ BUG 修复：禁止把节点拖到自身或其子孙节点内（否则数据成环，
    //    后续递归遍历/保存直接栈溢出）。
    const isDescendant = (root: OutlineNode | undefined, key: string): boolean => {
      if (!root) return false;
      if (root.id === key) return true;
      return (root.children || []).some((c) => isDescendant(c, key));
    };
    const dragNode = findNode(tree, dragKey);
    const dropNode = findNode(tree, dropKey);
    if (!dragNode || !dropNode || dragKey === dropKey || isDescendant(dragNode, dropKey)) {
      msg.warning("不能将章节移动到自身或其子章节内");
      return;
    }

    const loop = (data: OutlineNode[], key: string, callback: (node: OutlineNode, i: number, arr: OutlineNode[]) => void) => {
      for (let i = 0; i < data.length; i++) {
        if (data[i].id === key) {
          return callback(data[i], i, data);
        }
        if (data[i].children) {
          loop(data[i].children!, key, callback);
        }
      }
    };

    // ✅ BUG 修复：旧实现 `const data = [...tree]` 仅浅拷贝根数组，loop 内
    //    arr.splice / item.children.unshift 直接突变了 state 中的嵌套数组与节点
    //    （React 无法感知、撤销/重渲染行为异常）。先深拷贝再操作。
    const data: OutlineNode[] = JSON.parse(JSON.stringify(tree));
    let dragObj: OutlineNode | undefined;
    loop(data, dragKey, (item, index, arr) => {
      arr.splice(index, 1);
      dragObj = item;
    });
    if (!dragObj) return;

    if (!info.dropToGap && dropPosition === 0) {
      loop(data, dropKey, (item) => {
        item.children = item.children || [];
        item.children!.unshift(dragObj!);
      });
    } else {
      let ar: OutlineNode[] = [];
      let i = 0;
      loop(data, dropKey, (_item, index, arr) => {
        ar = arr;
        i = index;
      });
      if (dropPosition === -1) {
        ar.splice(i, 0, dragObj!);
      } else {
        ar.splice(i + 1, 0, dragObj!);
      }
    }
    // ✅ BUG 修复：拖拽可把节点拖成四级（如拖到三级节点之下），而目录硬性限三级 ——
    //    保存时后端会静默裁剪并把深层标题并入父描述、节点"消失"。这里在落地前拦截。
    if (maxTreeDepth(data) > MAX_OUTLINE_DEPTH) {
      msg.warning(`目录最多支持 ${MAX_OUTLINE_DEPTH} 级，已取消本次拖拽`);
      return;
    }
    setTree(data);
  }

  function convertToEditTree(node: OutlineNode): any {
    return {
      key: node.id,
      title: (
        <div
          style={{ display: "inline-flex", alignItems: "center", gap: 4, width: "100%" }}
          onDoubleClick={() => {
            setRenamingKey(node.id);
            setRenamingValue(node.title);
          }}
        >
          {renamingKey === node.id ? (
            <Input
              size="small"
              autoFocus
              value={renamingValue}
              onChange={(e) => setRenamingValue(e.target.value)}
              onBlur={() => {
                renameNode(node.id, renamingValue);
                setRenamingKey(null);
              }}
              onPressEnter={() => {
                renameNode(node.id, renamingValue);
                setRenamingKey(null);
              }}
              style={{ width: 220 }}
            />
          ) : (
            <>
              <span style={{ fontWeight: node.level === 1 ? 600 : 400 }}>
                {node.title}
              </span>
              <span style={{ fontSize: 11, color: "#999" }}>
                (L{node.level})
              </span>
              {node.description ? (
                <Text type="secondary" style={{ fontSize: 11, marginLeft: 4 }} ellipsis>
                  {node.description}
                </Text>
              ) : null}
              <span
                style={{
                  marginLeft: "auto",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 2,
                  flexShrink: 0,
                }}
              >
                <Button
                  type="text"
                  size="small"
                  tabIndex={-1}
                  disabled={!moveFlags[node.id]?.up}
                  icon={<ArrowUpOutlined />}
                  title="上移"
                  onClick={(e) => {
                    e.stopPropagation();
                    moveNode(node.id, -1);
                  }}
                />
                <Button
                  type="text"
                  size="small"
                  tabIndex={-1}
                  disabled={!moveFlags[node.id]?.down}
                  icon={<ArrowDownOutlined />}
                  title="下移"
                  onClick={(e) => {
                    e.stopPropagation();
                    moveNode(node.id, 1);
                  }}
                />
              </span>
            </>
          )}
        </div>
      ),
      children: node.children?.map((c) => convertToEditTree(c)),
    };
  }

  const editTreeData = tree.map((n) => convertToEditTree(n));

  /** 当前选中节点（用于下方「章节编辑」面板） */
  const selectedNode = selectedKey ? findNode(tree, selectedKey) : null;

  /** 重排层级与编号；✅ 保留 description，避免预置标准目录的专业编写要点在保存时丢失 */
  function renumberOutline(nodes: OutlineNode[], parentLevel = 0): OutlineNode[] {
    return nodes.map((node) => ({
      ...node,
      level: parentLevel + 1,
      description: node.description || "",
      children: node.children ? renumberOutline(node.children, parentLevel + 1) : [],
    }));
  }

  const handleImportOutline = async (file: File) => {
    setImporting(true);
    const hide = msg.loading("正在解析文件并识别目录...", 0);
    try {
      const { data } = await uploadOutlineApi.parse(file);
      const outline = data.outline || [];
      const imported = importToTree(outline);
      setTree(imported);
      setExpandedKeys(collectAllKeys(imported));
      setSelectedKey(null);
      setActiveTab("outline");
      msg.success(`识别完成：${data.file_name}，共 ${countNodes(imported)} 个章节`);
      // ✅ 修复（2026-09-18）：解析诊断必须回显。旧实现只取 data.outline / data.file_name，
      //    对后端已回传的 parse_warnings / empty_text / raw_text_truncated 全部忽略 ——
      //    从目录库导入扫描件或超长文件时，用户看到「识别完成」却收不到
      //    「内容可能不完整 / 未解析到文本」提示，与工作台内上传路径体验割裂。
      if (data.empty_text) {
        msg.warning("未从文件中解析到有效文本（可能是空白文件、扫描件或已损坏），识别结果可能为空");
      } else if (data.raw_text_truncated) {
        msg.warning("原文过长，保存记录时已截断（不影响目录识别结果）");
      }
      for (const w of (data.parse_warnings || [])) msg.warning(String(w));
    } catch (e: any) {
      msg.error(e.message || "文件识别失败");
    } finally {
      hide();
      setImporting(false);
    }
  };

  /** ✅ 导入 / 套用模板时保留 description（旧实现丢弃，会把编写要点清空） */
  function importToTree(outline: any[], parentLevel = 0): OutlineNode[] {
    return outline.map((n, i) => {
      const level = n.level || parentLevel + 1;
      return {
        id: n.id || `import_${Date.now()}_${i}`,
        title: n.title || "未命名章节",
        description: n.description || "",
        level,
        children: n.children ? importToTree(n.children, level) : [],
      };
    });
  }

  /** 套用行业标准模板：替换当前目录，并自动补全编制依据与适用条件（不覆盖已填内容） */
  const applyTemplate = async (key: string) => {
    setTplKey(key);
    if (!key) return;
    setTplLoading(true);
    try {
      const { data } = await outlineLibraryApi.template(key);
      const nodes = importToTree(data.outline || []);
      setTree(nodes);
      setExpandedKeys(collectAllKeys(nodes));
      setSelectedKey(null);
      const cur = form.getFieldsValue();
      const patch: Record<string, string> = {};
      if (!cur.applicable_conditions && data.applicable) patch.applicable_conditions = data.applicable;
      if (!cur.basis && data.basis) patch.basis = data.basis;
      if (Object.keys(patch).length) form.setFieldsValue(patch);
      setActiveTab("outline");
      msg.success(`已套用「${templates.find((t) => t.key === key)?.name || key}」标准目录`);
    } catch (e: any) {
      msg.error(e.message || "加载模板失败");
    } finally {
      setTplLoading(false);
    }
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      if (!tree.length) {
        msg.warning("目录章节为空，请先新增章节、导入目录或套用标准模板");
        setActiveTab("outline");
        return;
      }
      setSaving(true);
      // ✅ 修复（2026-10-01）：下拉字段清空后保存不生效。Select（allowClear）清空后
      //    值为 undefined，axios JSON 序列化会丢弃该键 → 后端 model_dump(exclude_none=True)
      //    收不到 → 字段保留旧值，用户看到「保存成功」刷新后旧分类/专业还在。
      //    显式转为空串，让「清空」真正落库（后端空串会正常 UPDATE 为空）。
      const normalized = Object.fromEntries(
        Object.entries(values).map(([k, v]) => [k, v === undefined || v === null ? "" : v]),
      );
      const outline_json = JSON.stringify(renumberOutline(tree), null, 2);
      const payload = { ...normalized, outline_json };
      if (isEdit) {
        await outlineLibraryApi.update(libraryId!, payload);
        msg.success("目录库已更新");
      } else {
        await outlineLibraryApi.create({ ...payload, source: "手动创建" });
        msg.success("目录库创建成功");
      }
      onSaved();
      onClose();
    } catch (e: any) {
      if (e.errorFields) return;
      msg.error(e.message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const tabItems = [
    {
      key: "basic",
      // ✅ antd Tabs 默认只渲染 active 面板，form 实例在非 active 时无 Form DOM
      // → 控制台报 "useForm not connected"。forceRender 让基本信息面板始终挂载。
      forceRender: true,
      label: "基本信息",
      children: (
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如：深基坑专项方案标准目录" />
          </Form.Item>
          <Form.Item name="type" label="方案类型"
            extra="可从库内已有分类中选择，也可直接输入新分类">
            <Select allowClear showSearch
              options={typeOptions.map(t => ({ label: t, value: t }))} />
          </Form.Item>
          <Form.Item name="engineering_type" label="工程类型">
            <Select options={["房建", "市政", "公路", "水利", "轨道交通"].map(t => ({ label: t, value: t }))} allowClear />
          </Form.Item>
          <Form.Item name="profession" label="专业">
            <Select options={["土建", "机电", "安全", "装饰", "质量", "市政"].map(t => ({ label: t, value: t }))} allowClear />
          </Form.Item>
          <Form.Item name="applicable_conditions" label="适用条件"
            extra="如：适用于开挖深度≥3m 的基坑工程；套用标准模板时自动填充">
            <Input.TextArea rows={2} placeholder="如：适用于深度超过 3m 的基坑开挖、支护、降水工程" />
          </Form.Item>
          <Form.Item name="basis" label="编制依据"
            extra="建议填写具体规范编号与名称；套用标准模板时自动填充">
            <Input.TextArea rows={3}
              placeholder="如：JGJ 120-2012《建筑基坑支护技术规程》、GB 50497-2019《建筑基坑工程监测技术标准》" />
          </Form.Item>
          <Form.Item name="tags" label="标签" extra="多个标签用逗号分隔，用于搜索命中">
            <Input placeholder="如：基坑,危大工程,房建" />
          </Form.Item>
        </Form>
      ),
    },
    {
      key: "outline",
      label: (
        <Space size={4}>
          目录章节
          {tree.length > 0 && <Tag color="blue" style={{ marginInlineStart: 0 }}>{countNodes(tree)}</Tag>}
        </Space>
      ),
      children: (
        <div>
          <Space style={{ marginBottom: 12 }} wrap>
            <Upload
              accept={UPLOAD_FILE_ACCEPT}
              beforeUpload={(file) => {
                if (file.size > 30 * 1024 * 1024) {
                  msg.error("文件过大，请上传 30MB 以内的文件");
                  return false;
                }
                handleImportOutline(file);
                return false;
              }}
              showUploadList={false}
            >
              <Button icon={<UploadOutlined />} disabled={importing} loading={importing}>
                导入目录（智能识别）
              </Button>
            </Upload>
            <Button
              icon={<PlusOutlined />}
              onClick={() => addChildNode()}
            >
              新增一级章节
            </Button>
            <Tooltip title="新增子章节到选中节点">
              <Button
                icon={<CaretRightOutlined />}
                disabled={!selectedKey}
                onClick={() => selectedKey && addChildNode(selectedKey)}
              >
                新增子章节
              </Button>
            </Tooltip>
            <Tooltip title="删除选中节点及其所有子章节">
              <Button
                danger
                icon={<StopOutlined />}
                disabled={!selectedKey}
                onClick={() => selectedKey && deleteNode(selectedKey)}
              >
                删除
              </Button>
            </Tooltip>
          </Space>

          <Space style={{ marginBottom: 12 }} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>套用行业标准模板：</Text>
            <Select
              allowClear showSearch placeholder="选择模板"
              style={{ width: 280 }}
              value={tplKey}
              loading={tplLoading}
              onChange={applyTemplate}
              optionFilterProp="label"
              options={templates.map((t) => ({
                label: `${t.name}${t.risk ? `（${t.risk}）` : ""}`,
                value: t.key,
              }))}
            />
            {tree.length > 0 && (
              <Popconfirm
                title="清空当前目录？"
                description="将删除全部章节，且不可撤销。"
                okText="清空"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() => { setTree([]); setSelectedKey(null); setExpandedKeys([]); }}
              >
                <Button danger size="small">清空目录</Button>
              </Popconfirm>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              共 {countNodes(tree)} 个章节 / {tree.length} 个一级章节 / 最大 {maxTreeDepth(tree)} 级
            </Text>
          </Space>

          <Card size="small" title="目录结构" style={{ marginBottom: 12 }}>
            {tree.length === 0 ? (
              <Empty description="暂无目录，请点击新增章节、导入目录或套用标准模板" />
            ) : (
              <div style={{ maxHeight: 340, overflow: "auto" }}>
                <Tree
                  treeData={editTreeData}
                  draggable
                  expandedKeys={expandedKeys}
                  onExpand={(keys) => setExpandedKeys(keys as React.Key[])}
                  selectedKeys={selectedKey ? [selectedKey] : []}
                  onSelect={(keys) => {
                    if (keys.length > 0) setSelectedKey(keys[0] as string);
                  }}
                  onDrop={onTreeDrop}
                  showLine={{ showLeafIcon: false }}
                  blockNode
                />
              </div>
            )}
          </Card>

          {selectedNode ? (
            <Card
              size="small"
              title={
                <Space>
                  <EditOutlined />
                  章节编辑
                  <Tag>L{selectedNode.level}</Tag>
                </Space>
              }
            >
              <Form layout="vertical" size="small">
                <Form.Item label="章节标题" style={{ marginBottom: 8 }}>
                  <Input
                    value={selectedNode.title}
                    onChange={(e) => updateNode(selectedNode.id, { title: e.target.value })}
                  />
                </Form.Item>
                <Form.Item
                  label="编写要点 / 章节说明"
                  extra="该说明随目录一起保存，作为 AI 生成正文的内容指引"
                  style={{ marginBottom: 0 }}
                >
                  <Input.TextArea
                    rows={3}
                    value={selectedNode.description || ""}
                    onChange={(e) => updateNode(selectedNode.id, { description: e.target.value })}
                    placeholder="如：围护桩桩径、桩长、间距、冠梁尺寸、锚索预应力与锁定值"
                  />
                </Form.Item>
              </Form>
            </Card>
          ) : (
            <Alert
              type="info"
              showIcon
              message="双击章节标题可快速改名；行内 ↑/↓ 或拖拽可调整顺序；选中章节后可在下方编辑编写要点。"
            />
          )}
        </div>
      ),
    },
  ];

  return (
    <Modal
      title={isEdit ? "编辑目录库" : "新增目录库"}
      open={open}
      forceRender
      onCancel={onClose}
      width={780}
      loading={loading}
      footer={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
            保存
          </Button>
        </Space>
      }
    >
      <Tabs items={tabItems} activeKey={activeTab} onChange={setActiveTab} />
    </Modal>
  );
}
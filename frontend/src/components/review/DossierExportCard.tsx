import { useEffect, useRef, useState } from "react";
import { Alert, App, Button, Card, Space, Typography } from "antd";
import { FileWordOutlined } from "@ant-design/icons";
import { dossierApi } from "../../api";
import { resolveExportFilename, resolveExportError } from "../../utils/exportResponse";

const { Text, Paragraph } = Typography;

const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export interface DossierExportCardProps {
  schemeId?: string;
}

/**
 * 可复核底稿导出入口（R179 Phase D）。
 *
 * 与既有 Word 导出链路独立：不写导出缓存、不递增轮次；后端开关
 * `dossier_enabled` 默认关闭 → 404，本卡显式提示未启用。
 *
 * ✅ R195 收口：①在飞请求接 AbortController —— 切方案/卸载时中止，
 *   旧方案返回不再触发下载或错误 toast（与页面导出链同口径的跨方案守卫）；
 *   ②错误文案统一走 `resolveExportError`（409 生成互斥 / 503 转换繁忙有差异化
 *   文案，不再裸抛 `err?.message`）。
 */
export function DossierExportCard({ schemeId }: DossierExportCardProps) {
  const { message } = App.useApp();
  const [exporting, setExporting] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const exportingRef = useRef(false);
  const schemeIdRef = useRef(schemeId);
  schemeIdRef.current = schemeId;

  // 切方案 / 卸载：中止在飞请求（cleanup 先于下一轮 effect body，不会误杀新请求）
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [schemeId]);

  const handleExport = async () => {
    const sid = schemeIdRef.current;
    if (!sid) return;
    // ✅ R241（2026-11-13）：同步重入锁 —— 主导出链刻意用同步 ref 锁（R-3），
    //    本卡此前只靠 antd loading（React 18 批处理提交前的双击窗口可漏进
    //    第二击：新建第二个 AbortController 覆盖 abortRef，第一击脱离取消管理、
    //    两次下载两个成功 toast）。与主链口径对齐。
    if (exportingRef.current) return;
    exportingRef.current = true;
    const controller = new AbortController();
    abortRef.current = controller;
    setExporting(true);
    try {
      const resp = await dossierApi.export(sid, controller.signal);
      if (schemeIdRef.current !== sid) return; // 跨方案守卫：旧方案返回不触发下载
      const blob = new Blob([resp.data], { type: DOCX_MIME });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = resolveExportFilename(resp.headers, "方案_可复核底稿.docx");
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // ✅ R230（F10）：延迟回收 blob URL，对齐主导出链（useExportRun 10s）——
      //    同步 revoke 在 Firefox 等浏览器 click 派发未完成时可能丢下载。
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      if (schemeIdRef.current === sid) message.success("可复核底稿已生成");
    } catch (e: unknown) {
      const resolution = await resolveExportError(e);
      if (resolution.canceled || schemeIdRef.current !== sid) return;
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 404) {
        setDisabled(true);
      } else {
        message.error(resolution.message || "可复核底稿导出失败");
      }
    } finally {
      if (schemeIdRef.current === sid || !abortRef.current) setExporting(false);
      if (abortRef.current === controller) abortRef.current = null;
      exportingRef.current = false;
    }
  };

  return (
    <Card
      size="small"
      title={
        <Space>
          <FileWordOutlined />
          <Text strong>可复核底稿（DOCX）</Text>
        </Space>
      }
      style={{ marginTop: 12 }}
    >
      {disabled ? (
        <Alert
          type="warning"
          showIcon
          message="可复核底稿导出未启用"
          description="后端开关 dossier_enabled 默认关闭，开启后此入口才可用。"
        />
      ) : (
        <>
          <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            在既有成稿末尾追加三张清单：事实与假设、待确认缺陷、审计与标准引用；
            不触发导出轮次，也不改动原 Word 导出结果。
          </Paragraph>
          <Button
            type="primary"
            icon={<FileWordOutlined />}
            loading={exporting}
            disabled={!schemeId}
            onClick={handleExport}
            data-testid="dossier-export"
          >
            导出可复核底稿
          </Button>
        </>
      )}
    </Card>
  );
}

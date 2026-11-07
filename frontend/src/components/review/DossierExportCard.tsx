import { useState } from "react";
import { Alert, App, Button, Card, Space, Typography } from "antd";
import { FileWordOutlined } from "@ant-design/icons";
import { dossierApi } from "../../api";
import { resolveExportFilename } from "../../utils/exportResponse";

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
 */
export function DossierExportCard({ schemeId }: DossierExportCardProps) {
  const { message } = App.useApp();
  const [exporting, setExporting] = useState(false);
  const [disabled, setDisabled] = useState(false);

  const handleExport = async () => {
    if (!schemeId) return;
    setExporting(true);
    try {
      const resp = await dossierApi.export(schemeId);
      const blob = new Blob([resp.data], { type: DOCX_MIME });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = resolveExportFilename(resp.headers, "方案_可复核底稿.docx");
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      message.success("可复核底稿已生成");
    } catch (e: unknown) {
      const err = e as { response?: { status?: number }; message?: string };
      if (err?.response?.status === 404) {
        setDisabled(true);
      } else {
        message.error(err?.message || "可复核底稿导出失败");
      }
    } finally {
      setExporting(false);
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

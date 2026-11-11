import { memo, useEffect, useRef } from "react";
import { Input } from "antd";
import { getRenameDraft, setRenameDraft, useRenameDraft } from "../../utils/perf/renameDraftStore";

interface OutlineRenameInputProps {
  /** 开始重命名时的章节标题（每次切换节点都会重置草稿） */
  initialValue: string;
  /** 提交时把最终值交回页面（写回 state + 调用 renameNode + 清除重命名态） */
  onCommit: (value: string) => void;
  /** Esc 取消：不写回、不调用 renameNode */
  onCancel?: () => void;
  width?: number | string;
  stopClickPropagation?: boolean;
}

/**
 * 行内重命名输入框：输入过程只更新 renameDraftStore（不触碰页面 state），
 * 因此不会触发目录树重建，也不会让 6 个 Tab 的子树跟着重渲。
 *
 * blur 与 onPressEnter 可能连续触发（Enter 会让 Input 卸载并触发 blur），
 * 用 committedRef 保证 renameNode 只调用一次。
 */
function OutlineRenameInputImpl({
  initialValue,
  onCommit,
  onCancel,
  width = 220,
  stopClickPropagation = false,
}: OutlineRenameInputProps) {
  const draft = useRenameDraft();
  const committedRef = useRef(false);
  const commitRef = useRef(onCommit);
  commitRef.current = onCommit;
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;

  useEffect(() => {
    committedRef.current = false;
    setRenameDraft(initialValue);
  }, [initialValue]);

  const commit = () => {
    if (committedRef.current) return;
    committedRef.current = true;
    commitRef.current(getRenameDraft());
  };

  return (
    <Input
      size="small"
      autoFocus
      value={draft}
      onChange={(e) => setRenameDraft(e.target.value)}
      onClick={stopClickPropagation ? (e) => e.stopPropagation() : undefined}
      onBlur={commit}
      onPressEnter={commit}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          committedRef.current = true;
          cancelRef.current?.();
        }
        e.stopPropagation();
      }}
      style={{ width }}
    />
  );
}

export const OutlineRenameInput = memo(OutlineRenameInputImpl);

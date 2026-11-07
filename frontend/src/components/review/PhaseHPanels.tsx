import { DefectLedgerPanel } from "./DefectLedgerPanel";
import { GenreTagCard } from "./GenreTagCard";
import { PendingConfirmPanel } from "./PendingConfirmPanel";

export interface PhaseHPanelsProps {
  schemeId: string;
  projectId: string;
}

/** R179 Phase H：文种标签 / 缺陷账本 / 待确认清单的统一挂载点（审核页）。 */
export function PhaseHPanels({ schemeId, projectId }: PhaseHPanelsProps) {
  return (
    <>
      <GenreTagCard schemeId={schemeId} projectId={projectId} />
      <DefectLedgerPanel schemeId={schemeId} />
      <PendingConfirmPanel schemeId={schemeId} />
    </>
  );
}

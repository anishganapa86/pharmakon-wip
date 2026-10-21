import type { RulesAwaitingVerification } from "../types/contract";

export function RulesNotice({
  notice,
}: {
  notice?: RulesAwaitingVerification | null;
}) {
  if (!notice) return null;
  return (
    <p className="warning timeline-disclaimer" role="status">
      {notice.message}
    </p>
  );
}

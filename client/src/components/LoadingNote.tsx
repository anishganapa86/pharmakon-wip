export function LoadingNote({ label }: { label: string }) {
  return (
    <p className="loading-note muted" role="status" aria-live="polite">
      {label}
      <span className="skeleton" aria-hidden="true" />
    </p>
  );
}

export function UncheckedReasons({ reasons }: { reasons?: string[] }) {
  if (!reasons || reasons.length === 0) return null;
  return (
    <ul className="effect-tags">
      {reasons.map((reason, i) => (
        <li key={i} className="effect-tag">
          {reason}
        </li>
      ))}
    </ul>
  );
}

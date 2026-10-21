import { useEffect, useState } from "react";
import { checkInteractions, fetchInteractionCheck } from "../api";
import type {
  DuplicateIngredientFinding,
  Interaction,
  InteractionCheckResponse,
} from "../types/contract";
import { LoadingNote } from "./LoadingNote";
import { RulesNotice } from "./RulesNotice";

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function parseNames(text: string): string[] {
  return text
    .split(",")
    .map((n) => n.trim())
    .filter((n) => n.length > 0);
}

// Plain templates, generated from the finding's own substances -- never an
// invented medical claim, and never a suggested drug (we only ask the
// pharmacist to name one; we don't name one ourselves).
function pharmacistQuestions(interaction: Interaction): string[] {
  const a = interaction.substance_a.display_name;
  const b = interaction.substance_b.display_name;
  return [
    `Is it OK for me to take both ${a} and ${b}?`,
    "Which one should I use?",
    `Is there an alternative to ${a} that doesn't interact with ${b}? ` +
      "Which would you suggest for me?",
  ];
}

function pharmacistSummaryText(result: InteractionCheckResponse): string {
  const sections = result.interactions.map((interaction, i) => {
    const a = interaction.substance_a.display_name;
    const b = interaction.substance_b.display_name;
    const lines = [
      `${i + 1}. ${a} + ${b}`,
      `   ${interaction.effect ?? interaction.message}`,
      ...pharmacistQuestions(interaction).map((q) => `   - ${q}`),
    ];
    return lines.join("\n");
  });
  return ["Questions for my pharmacist", "", ...sections].join("\n\n");
}

function SourceTag({ interaction }: { interaction: Interaction }) {
  return (
    <span className="tag tag-source">
      {interaction.source_type === "fda_label" ? "FDA label" : "curated reference"}
    </span>
  );
}

function InteractionCard({ interaction }: { interaction: Interaction }) {
  const a = interaction.substance_a;
  const b = interaction.substance_b;
  return (
    <div className="skincare-flag skincare-flag-caution">
      <p>
        <strong>{a.display_name}</strong>
        {a.description && <span className="muted small"> — {a.description}</span>}
        {" + "}
        <strong>{b.display_name}</strong>
        {b.description && <span className="muted small"> — {b.description}</span>}
      </p>
      <p>{interaction.effect ?? interaction.message}</p>
      {interaction.recommendation && (
        <p className="muted small">{interaction.recommendation}</p>
      )}
      {interaction.quoted_text && (
        <p className="muted small">"{interaction.quoted_text}"</p>
      )}
      {interaction.overlap && (
        <ul className="effect-tags">
          {interaction.overlap.map((w, i) => (
            <li key={i} className="effect-tag" title="Estimated from the concentration model">
              {w.description}
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">
        <SourceTag interaction={interaction} /> Source: {interaction.source}
        {interaction.source_type === "fda_label"
          ? interaction.quote_source !== null && (
              <>
                {" · "}
                <span className="tag">Quoted from FDA label</span>
              </>
            )
          : !interaction.verified && (
              <>
                {" · "}
                <span className="tag">Unverified reference</span>
              </>
            )}
      </p>
      <div className="pharmacist-questions">
        <p className="muted small">Questions for your pharmacist:</p>
        <ul>
          {pharmacistQuestions(interaction).map((q, i) => (
            <li key={i} className="muted small">
              {q}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function DuplicateIngredientCard({
  finding,
}: {
  finding: DuplicateIngredientFinding;
}) {
  return (
    <div className="skincare-flag skincare-flag-caution">
      <p>{finding.message}</p>
      {finding.strength_note && (
        <p className="muted small">{finding.strength_note}</p>
      )}
      <ul className="effect-tags">
        {finding.products.map((p) => (
          <li key={p.product_id} className="effect-tag">
            {p.name} ({p.kind === "schedule" ? "scheduled" : "logged today"})
          </li>
        ))}
      </ul>
    </div>
  );
}

function CopyForPharmacist({ result }: { result: InteractionCheckResponse }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function copy() {
    setError(null);
    try {
      await navigator.clipboard.writeText(pharmacistSummaryText(result));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="log-row">
      <button className="control button" type="button" onClick={() => void copy()}>
        {copied ? "Copied!" : "Copy for my pharmacist"}
      </button>
      {error && <span className="error small">Couldn't copy: {error}</span>}
    </div>
  );
}

function Results({ result }: { result: InteractionCheckResponse }) {
  return (
    <>
      {result.unchecked_reason.length > 0 && (
        <ul className="effect-tags">
          {result.unchecked_reason.map((reason, i) => (
            <li key={i} className="effect-tag">
              {reason}
            </li>
          ))}
        </ul>
      )}
      {result.duplicate_ingredients.map((finding, i) => (
        <DuplicateIngredientCard key={i} finding={finding} />
      ))}
      {result.interactions.length === 0 ? (
        <p className="muted">{result.message}</p>
      ) : (
        <>
          <CopyForPharmacist result={result} />
          {result.interactions.map((interaction, i) => (
            <InteractionCard key={i} interaction={interaction} />
          ))}
        </>
      )}
    </>
  );
}

interface Props {
  userId: string;
}

export function Interactions({ userId }: Props) {
  const [regimenResult, setRegimenResult] = useState<InteractionCheckResponse | null>(
    null,
  );
  const [regimenLoading, setRegimenLoading] = useState(true);
  const [regimenError, setRegimenError] = useState<string | null>(null);

  const [namesText, setNamesText] = useState("");
  const [checking, setChecking] = useState(false);
  const [manualResult, setManualResult] = useState<InteractionCheckResponse | null>(
    null,
  );
  const [manualError, setManualError] = useState<string | null>(null);

  // Mounts once per tab visit, same convention as the Skincare tab: App.tsx
  // only renders this while the Interactions tab is active.
  useEffect(() => {
    let cancelled = false;
    fetchInteractionCheck(userId)
      .then((r) => !cancelled && setRegimenResult(r))
      .catch((e: unknown) => !cancelled && setRegimenError(errorMessage(e)))
      .finally(() => !cancelled && setRegimenLoading(false));
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const names = parseNames(namesText);

  async function handleCheck(e: React.FormEvent) {
    e.preventDefault();
    if (names.length === 0) return;
    setChecking(true);
    setManualError(null);
    try {
      setManualResult(await checkInteractions(names));
    } catch (err) {
      setManualResult(null);
      setManualError(errorMessage(err));
    } finally {
      setChecking(false);
    }
  }

  return (
    <>
      <p className="warning timeline-disclaimer">
        These are documented interactions from FDA labels and cited
        references. They are not medical advice. Do not stop or change any
        medication without talking to your prescriber or pharmacist.
      </p>
      <RulesNotice
        notice={(regimenResult ?? manualResult)?.rules_awaiting_verification}
      />

      <section className="panel">
        <h2>Your regimen</h2>
        {regimenLoading ? (
          <LoadingNote label="Checking your regimen against FDA labels…" />
        ) : regimenError ? (
          <p className="error">Couldn't check: {regimenError}</p>
        ) : (
          regimenResult && <Results result={regimenResult} />
        )}
      </section>

      <section className="panel">
        <h2>Check a new medication</h2>
        <form className="skincare-form" onSubmit={handleCheck}>
          <input
            className="control grow"
            type="text"
            placeholder="Names, comma-separated (e.g. ibuprofen, warfarin)"
            value={namesText}
            onChange={(e) => setNamesText(e.target.value)}
            aria-label="Medication or substance names"
          />
          <div className="log-row">
            <button
              className="control button"
              type="submit"
              disabled={names.length === 0 || checking}
            >
              {checking ? "Checking…" : "Check"}
            </button>
          </div>
        </form>
        {checking && <LoadingNote label="Checking FDA labels…" />}
        {manualError && <p className="error">Couldn't check: {manualError}</p>}
      </section>

      {manualResult && (
        <section className="panel">
          <h2>Results</h2>
          <Results result={manualResult} />
        </section>
      )}
    </>
  );
}

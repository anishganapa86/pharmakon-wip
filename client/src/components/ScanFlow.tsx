import { useEffect, useMemo, useRef, useState } from "react";
import "./scan.css";
import {
  createMyProduct,
  fetchDoses,
  matchIngredients,
  scanLabel,
  type ScanResult,
} from "../api";
import { splitIngredientsPreview } from "../ingredientSplit";
import { downscaleToJpeg } from "../scanImage";

interface ScanFlowProps {
  userId: string;
  onClose: () => void;
}

type Stage =
  | { kind: "pick" }
  | { kind: "reading" }
  | { kind: "review"; result: ScanResult }
  | { kind: "saved"; name: string };

type Match = Record<string, { matched: boolean; substance_id: string | null }>;

const CATEGORIES = [
  "cleanser",
  "toner",
  "serum",
  "moisturizer",
  "sunscreen",
  "other",
] as const;
const TYPE_LABELS: Record<ScanResult["product_type"], string> = {
  oral_medication: "Oral medication",
  supplement: "Supplement",
  topical: "Topical or skincare",
  food_drink: "Food or drink",
  unknown: "Not sure",
};
const DEMO_NOTICE =
  "Demo only: don't enter real medical or personal information.";
const WARNING =
  "Label reading can be wrong. Check each ingredient against the package before saving.";

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function printedAmount(result: ScanResult, name: string): string {
  const found = result.ingredients.find(
    (i) => i.name.trim().toLowerCase() === name.trim().toLowerCase(),
  );
  if (!found || !found.strength) return "";
  return `${found.strength}${found.unit ? ` ${found.unit}` : ""}`;
}

function roleOf(
  result: ScanResult,
  name: string,
): "active" | "inactive" | "unknown" {
  const found = result.ingredients.find(
    (i) => i.name.trim().toLowerCase() === name.trim().toLowerCase(),
  );
  return found ? found.role : "unknown";
}

interface ReviewProps {
  userId: string;
  result: ScanResult;
  onCancel: () => void;
  onSaved: (name: string) => void;
}

function Review({ userId, result, onCancel, onSaved }: ReviewProps) {
  const [name, setName] = useState(result.product_name ?? "");
  const [brand, setBrand] = useState(result.brand ?? "");
  const [type, setType] = useState(result.product_type);
  const [category, setCategory] =
    useState<(typeof CATEGORIES)[number]>("other");
  const [text, setText] = useState(
    result.ingredients.map((i) => i.name).join(", "),
  );
  const [matches, setMatches] = useState<Match>({});
  const [manual, setManual] = useState<Set<string>>(new Set());
  const [loggedSubstances, setLoggedSubstances] = useState<
    Record<string, string>
  >({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const names = useMemo(() => splitIngredientsPreview(text), [text]);

  useEffect(() => {
    if (names.length === 0) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      matchIngredients(names)
        .then((response) => {
          if (cancelled) return;
          const next: Match = {};
          for (const r of response.results) {
            next[r.name] = { matched: r.matched, substance_id: r.substance_id };
          }
          setMatches(next);
        })
        .catch(() => undefined);
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [names]);

  useEffect(() => {
    let cancelled = false;
    fetchDoses(userId)
      .then((doses) => {
        if (cancelled) return;
        const bySubstance: Record<string, string> = {};
        for (const dose of doses) {
          for (const ing of dose.ingredients) {
            if (ing.substance_id)
              bySubstance[ing.substance_id] = dose.product_name;
          }
        }
        setLoggedSubstances(bySubstance);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [userId]);

  function status(
    ingredient: string,
  ): "Matched" | "Not tracked" | "Enter manually" {
    const match = matches[ingredient];
    const role = roleOf(result, ingredient);
    if (match?.matched) return "Matched";
    if (role === "inactive") return "Not tracked";
    return "Enter manually";
  }

  const overlaps = names
    .map((n) => ({ n, id: matches[n]?.substance_id ?? null }))
    .filter(
      (x) =>
        x.id !== null &&
        roleOf(result, x.n) !== "inactive" &&
        loggedSubstances[x.id as string] !== undefined,
    );

  const canSave = name.trim() !== "" && names.length > 0 && !saving;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await createMyProduct(userId, {
        brand: brand.trim() || name.trim(),
        name: name.trim(),
        category,
        raw_text: text,
      });
      onSaved(name.trim());
    } catch (e) {
      setError(errorText(e));
      setSaving(false);
    }
  }

  return (
    <section className="panel scan-review">
      <h2 className="screen-title">Check what we read</h2>
      <p className="warning">{WARNING}</p>
      {result.label_text_quality === "partial" && (
        <p className="muted">
          Part of the label was hard to read. Fill in anything missing.
        </p>
      )}
      {result.label_text_quality === "unreadable" && (
        <p className="muted">
          We couldn't read this label. You can type the product in by hand
          below.
        </p>
      )}
      {result.notes && <p className="muted">{result.notes}</p>}

      <label className="scan-field">
        <span>Product name</span>
        <input
          className="control"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="scan-field">
        <span>Brand (if blank, the product name is used)</span>
        <input
          className="control"
          value={brand}
          onChange={(e) => setBrand(e.target.value)}
        />
      </label>
      <label className="scan-field">
        <span>What kind of product is it?</span>
        <select
          className="control"
          value={type}
          onChange={(e) =>
            setType(e.target.value as ScanResult["product_type"])
          }
        >
          {Object.entries(TYPE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label className="scan-field">
        <span>Category</span>
        <select
          className="control"
          value={category}
          onChange={(e) =>
            setCategory(e.target.value as (typeof CATEGORIES)[number])
          }
        >
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      <label className="scan-field">
        <span>Ingredients (separate with commas or new lines)</span>
        <textarea
          className="control scan-text"
          rows={4}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </label>

      {names.length > 0 && (
        <ul className="scan-list">
          {names.map((n, i) => {
            const s = status(n);
            const amount = printedAmount(result, n);
            return (
              <li key={`${n}-${i}`} className="scan-row">
                <div className="scan-row-main">
                  <span className="scan-name">{n}</span>
                  {amount && (
                    <span className="scan-amount">{amount} as printed</span>
                  )}
                </div>
                <span className="scan-status">
                  <span
                    className={`dot ${s === "Matched" ? "dot-ok" : s === "Not tracked" ? "dot-muted" : "dot-warn"}`}
                  />
                  {s === "Enter manually" ? (
                    <button
                      type="button"
                      className="button-link"
                      onClick={() => setManual(new Set(manual).add(n))}
                    >
                      Enter manually
                    </button>
                  ) : (
                    s
                  )}
                </span>
                {manual.has(n) && (
                  <label className="scan-field scan-manual">
                    <span>Ingredient name</span>
                    <input
                      className="control"
                      defaultValue={n}
                      onBlur={(e) => {
                        const fixed = e.target.value.trim();
                        if (fixed && fixed !== n) {
                          setText(
                            names.map((x) => (x === n ? fixed : x)).join(", "),
                          );
                        }
                      }}
                    />
                  </label>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {overlaps.map((o) => (
        <p key={o.n} className="muted">
          {o.n} is already in a product you logged (
          {loggedSubstances[o.id as string]}).
        </p>
      ))}

      <p className="muted small">
        Saved as an entry you typed yourself. Strengths from the label are not
        used for concentration curves.
      </p>
      {error && <p className="error">Couldn't save: {error}</p>}
      <div className="scan-actions">
        <button type="button" className="control secondary" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="control button"
          disabled={!canSave}
          onClick={() => void save()}
        >
          Add to my products
        </button>
      </div>
    </section>
  );
}

export function ScanFlow({ userId, onClose }: ScanFlowProps) {
  const [stage, setStage] = useState<Stage>({ kind: "pick" });
  const [error, setError] = useState<string | null>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const libraryRef = useRef<HTMLInputElement>(null);

  async function handleFile(file: File | undefined) {
    if (!file) return;
    setError(null);
    setStage({ kind: "reading" });
    try {
      const blob = await downscaleToJpeg(file);
      const result = await scanLabel(userId, blob);
      setStage({ kind: "review", result });
    } catch (e) {
      setError(errorText(e));
      setStage({ kind: "pick" });
    }
  }

  return (
    <main className="page">
      <header className="page-header">
        <div className="page-header-row">
          <h1>Pharmakon</h1>
          <button type="button" className="control secondary" onClick={onClose}>
            Back to app
          </button>
        </div>
      </header>

      {(stage.kind === "pick" || stage.kind === "reading") && (
        <section className="panel">
          <h2 className="screen-title">Scan a product</h2>
          <p className="muted">
            Take a photo of the label. We read the printed text and show it to
            you to check. Nothing is saved until you say so, and the photo is
            not stored.
          </p>
          <p className="warning">{DEMO_NOTICE}</p>
          <p className="muted small">
            The photo is sent to Google Gemini to read the label. Label reading
            can be wrong.
          </p>
          <div className="scan-actions">
            <button
              type="button"
              className="control button"
              disabled={stage.kind === "reading"}
              onClick={() => cameraRef.current?.click()}
            >
              Take a photo
            </button>
            <button
              type="button"
              className="control secondary"
              disabled={stage.kind === "reading"}
              onClick={() => libraryRef.current?.click()}
            >
              Choose a photo
            </button>
          </div>
          <input
            ref={cameraRef}
            className="scan-input"
            type="file"
            accept="image/*"
            capture="environment"
            aria-label="Take a photo"
            onChange={(e) => {
              void handleFile(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
          <input
            ref={libraryRef}
            className="scan-input"
            type="file"
            accept="image/*"
            aria-label="Choose a photo"
            onChange={(e) => {
              void handleFile(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
          {stage.kind === "reading" && (
            <p className="muted" role="status">
              Reading the label…
            </p>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </section>
      )}

      {stage.kind === "review" && (
        <Review
          userId={userId}
          result={stage.result}
          onCancel={onClose}
          onSaved={(name) => setStage({ kind: "saved", name })}
        />
      )}

      {stage.kind === "saved" && (
        <section className="panel">
          <h2 className="screen-title">Added</h2>
          <p>{stage.name} is now in My products on the Skincare tab.</p>
          <div className="scan-actions">
            <button type="button" className="control button" onClick={onClose}>
              Done
            </button>
          </div>
        </section>
      )}

      <footer className="page-footer">
        <small>
          Educational and demonstrative use only. Not a medical device.
        </small>
      </footer>
    </main>
  );
}

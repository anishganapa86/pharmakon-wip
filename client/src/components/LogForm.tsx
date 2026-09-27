import { useState } from "react";
import { createLog } from "../api";
import { showsBodyLocation, valuePlaceholder } from "../labels";
import type { DailyLogEntry } from "../types/contract";
import { SUGGESTED_LOG_CATEGORIES } from "../types/contract";

const CUSTOM_CATEGORY = "__custom__";

interface Props {
  userId: string;
  onLogged: (entry: DailyLogEntry) => void;
}

export function LogForm({ userId, onLogged }: Props) {
  const [categoryChoice, setCategoryChoice] = useState<string>(
    SUGGESTED_LOG_CATEGORIES[0],
  );
  const [customCategory, setCustomCategory] = useState("");
  const [value, setValue] = useState("");
  const [bodyLocation, setBodyLocation] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const category =
    categoryChoice === CUSTOM_CATEGORY ? customCategory.trim() : categoryChoice;
  const canSubmit = category !== "" && value.trim() !== "" && !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const entry = await createLog(userId, {
        category,
        value: value.trim(),
        body_location: bodyLocation.trim() || undefined,
        notes: notes.trim() || undefined,
      });
      onLogged(entry);
      // Keep the category (people tend to log the same kind of thing
      // repeatedly); clear everything that describes this one entry.
      setValue("");
      setBodyLocation("");
      setNotes("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="log-form" onSubmit={handleSubmit}>
      <div className="log-form-row">
        <select
          className="control"
          aria-label="Category"
          value={categoryChoice}
          onChange={(e) => setCategoryChoice(e.target.value)}
        >
          {SUGGESTED_LOG_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
          <option value={CUSTOM_CATEGORY}>Custom…</option>
        </select>
        {categoryChoice === CUSTOM_CATEGORY && (
          <input
            className="control grow"
            type="text"
            placeholder="Name your own category"
            value={customCategory}
            onChange={(e) => setCustomCategory(e.target.value)}
            aria-label="Custom category"
          />
        )}
        <input
          className="control grow"
          type="text"
          placeholder={valuePlaceholder(category)}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label="Value"
        />
      </div>
      {showsBodyLocation(category) && (
        <input
          className="control"
          type="text"
          placeholder="Body location (optional), e.g. left arm"
          value={bodyLocation}
          onChange={(e) => setBodyLocation(e.target.value)}
          aria-label="Body location"
        />
      )}
      <textarea
        className="control log-form-notes"
        placeholder="Notes (optional)"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        aria-label="Notes"
        rows={2}
      />
      <button className="control button" type="submit" disabled={!canSubmit}>
        {submitting ? "Logging…" : "Add to timeline"}
      </button>
      {error && <p className="error">Couldn't log entry: {error}</p>}
    </form>
  );
}

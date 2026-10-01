// Client-side preview of the hardened ingredient splitter
// (app/api/skincare.py: split_ingredients / split_ingredients_detailed).
// The server is authoritative -- POST /users/{id}/my-products re-splits
// raw_text itself -- this exists only so the "My products" form can show a
// live parsed count/preview as the user types, without a round trip per
// keystroke. Kept in exact lockstep with the Python algorithm: split on
// commas, bullets (•), and newlines, except a comma inside parentheses
// or a comma with a digit on both sides (a chemical name like
// "1,2-Hexanediol"); collapse whitespace; drop a trailing period on the
// last item.

export function splitIngredientsPreview(rawText: string): string[] {
  const text = rawText.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const parts: string[] = [];
  let current = "";
  let depth = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "(") {
      depth += 1;
      current += ch;
    } else if (ch === ")") {
      depth = Math.max(0, depth - 1);
      current += ch;
    } else if (ch === "," && depth === 0) {
      const prevDigit = i > 0 && /[0-9]/.test(text[i - 1]);
      const nextDigit = i + 1 < text.length && /[0-9]/.test(text[i + 1]);
      if (prevDigit && nextDigit) {
        current += ch;
      } else {
        parts.push(current);
        current = "";
      }
    } else if ((ch === "•" || ch === "\n") && depth === 0) {
      parts.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  parts.push(current);

  const cleaned = parts.map((p) => p.replace(/\s+/g, " ").trim()).filter((p) => p);
  if (cleaned.length > 0 && cleaned[cleaned.length - 1].endsWith(".")) {
    cleaned[cleaned.length - 1] = cleaned[cleaned.length - 1].slice(0, -1).trimEnd();
  }
  return cleaned;
}

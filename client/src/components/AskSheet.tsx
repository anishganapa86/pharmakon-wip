import { useEffect, useRef, useState } from "react";
import "./ask.css";
import { ApiRequestError, sendChat, type ChatTurn } from "../api";
import { TAB_LABELS, type View } from "../survey";

interface AskSheetProps {
  userId: string;
  onOpenTab: (tab: View) => void;
}

interface Entry extends ChatTurn {
  tab?: View;
}

const CHIPS = [
  "Where do I log a dose?",
  "How do I set a reminder?",
  "Where are interactions checked?",
  "What does Scan do?",
];

const NOTICE =
  "Demo assistant using Google Gemini. Don't enter real medical or personal information.";

function friendly(error: unknown): string {
  if (error instanceof ApiRequestError) {
    if (error.status === 503) {
      return "The assistant isn't available on this server yet.";
    }
    if (error.status === 429) return error.message;
    return "The assistant couldn't answer. Try again.";
  }
  return error instanceof Error ? error.message : "The assistant couldn't answer. Try again.";
}

function ChatIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.2 3.6c-.6.5-1.8.1-1.8-.8V5.5Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function AskSheet({ userId, onOpenTab }: AskSheetProps) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastQuestion, setLastQuestion] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    inputRef.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [entries, sending, error]);

  async function ask(question: string, history: Entry[]) {
    setSending(true);
    setError(null);
    setLastQuestion(question);
    try {
      const turns: ChatTurn[] = history.map(({ role, content }) => ({ role, content }));
      const answer = await sendChat(userId, turns);
      setEntries([
        ...history,
        {
          role: "assistant",
          content: answer.reply,
          tab: (answer.suggested_tab as View | null) ?? undefined,
        },
      ]);
      setLastQuestion(null);
    } catch (e) {
      setError(friendly(e));
    } finally {
      setSending(false);
    }
  }

  function submit(text: string) {
    const question = text.trim();
    if (!question || sending) return;
    const history: Entry[] = [...entries, { role: "user", content: question }];
    setEntries(history);
    setDraft("");
    void ask(question, history);
  }

  function retry() {
    if (lastQuestion !== null && !sending) void ask(lastQuestion, entries);
  }

  return (
    <>
      {!open && (
        <button type="button" className="ask-button" onClick={() => setOpen(true)}>
          <ChatIcon />
          Ask
        </button>
      )}
      {open && (
        <div className="ask-backdrop" onClick={() => setOpen(false)}>
          <section
            className="ask-sheet"
            role="dialog"
            aria-modal="true"
            aria-label="Ask Pharmakon"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="ask-header">
              <h2>Ask Pharmakon</h2>
              <button
                type="button"
                className="ask-close"
                aria-label="Close"
                onClick={() => setOpen(false)}
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                </svg>
              </button>
            </header>
            <p className="ask-notice">{NOTICE}</p>
            <div className="ask-log" ref={logRef} aria-live="polite">
              {entries.length === 0 && (
                <div className="ask-chips">
                  {CHIPS.map((chip) => (
                    <button
                      key={chip}
                      type="button"
                      className="ask-chip"
                      disabled={sending}
                      onClick={() => submit(chip)}
                    >
                      {chip}
                    </button>
                  ))}
                </div>
              )}
              {entries.map((entry, i) =>
                entry.role === "user" ? (
                  <p key={i} className="ask-user">
                    {entry.content}
                  </p>
                ) : (
                  <div key={i} className="ask-reply">
                    <p>{entry.content}</p>
                    {entry.tab && (
                      <button
                        type="button"
                        className="ask-open"
                        onClick={() => {
                          onOpenTab(entry.tab as View);
                          setOpen(false);
                        }}
                      >
                        Open {TAB_LABELS[entry.tab]}
                      </button>
                    )}
                  </div>
                ),
              )}
              {sending && <p className="ask-pending">Thinking…</p>}
              {error && (
                <div className="ask-error" role="alert">
                  <p>{error}</p>
                  {lastQuestion !== null && (
                    <button type="button" className="ask-open" onClick={retry}>
                      Try again
                    </button>
                  )}
                </div>
              )}
            </div>
            <form
              className="ask-composer"
              onSubmit={(e) => {
                e.preventDefault();
                submit(draft);
              }}
            >
              <input
                ref={inputRef}
                type="text"
                value={draft}
                maxLength={1000}
                placeholder="Ask where to find something"
                aria-label="Your question"
                onChange={(e) => setDraft(e.target.value)}
              />
              <button type="submit" className="ask-send" disabled={sending || !draft.trim()}>
                Send
              </button>
            </form>
          </section>
        </div>
      )}
    </>
  );
}

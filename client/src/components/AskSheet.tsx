import { useEffect, useRef, useState } from "react";
import "./ask.css";
import { Icon } from "./Icon";
import { ApiRequestError, sendChat, type ChatTurn } from "../api";
import {
  ACTION_LABELS,
  CHIPS_BY_VIEW,
  TAB_LABELS,
  type ChatAction,
  type View,
} from "../survey";

interface AskSheetProps {
  userId: string;
  view: View;
  onOpenTab: (tab: View) => void;
  onAction: (action: ChatAction) => void;
}

interface Entry extends ChatTurn {
  tab?: View;
  action?: ChatAction;
}

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

export function AskSheet({ userId, view, onOpenTab, onAction }: AskSheetProps) {
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
          action: (answer.suggested_action as ChatAction | null) ?? undefined,
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
          <Icon name="message" size={18} />
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
              <h2 className="screen-title">Ask Pharmakon</h2>
              <button
                type="button"
                className="ask-close"
                aria-label="Close"
                onClick={() => setOpen(false)}
              >
                <Icon name="x" size={16} />
              </button>
            </header>
            <p className="ask-notice">
              <Icon name="info" size={16} />
              {NOTICE}
            </p>
            <div className="ask-log" ref={logRef} aria-live="polite">
              {entries.length === 0 && (
                <div className="ask-chips">
                  {CHIPS_BY_VIEW[view].map((chip) => (
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
                    {entry.action && (
                      <button
                        type="button"
                        className="ask-open"
                        onClick={() => {
                          onAction(entry.action as ChatAction);
                          setOpen(false);
                        }}
                      >
                        {ACTION_LABELS[entry.action]}
                      </button>
                    )}
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
              <button type="submit" className="ask-send" aria-label="Send" disabled={sending || !draft.trim()}>
                <Icon name="arrowUp" size={18} />
              </button>
            </form>
          </section>
        </div>
      )}
    </>
  );
}

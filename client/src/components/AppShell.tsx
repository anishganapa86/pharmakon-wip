import { useEffect, useState } from "react";
import App, { type TabRequest } from "../App";
import { fetchSurvey, saveSurvey, type AuthUser } from "../api";
import type { Survey as SurveyData, SurveyAnswers, View } from "../survey";
import { AskSheet } from "./AskSheet";
import { LoadingNote } from "./LoadingNote";
import { Settings } from "./Settings";
import { ScanFlow } from "./ScanFlow";
import { Survey } from "./Survey";

interface AppShellProps {
  user: AuthUser;
  onLogOut: () => void;
}

type Load =
  | { kind: "loading" }
  | { kind: "ready"; survey: SurveyData }
  | { kind: "failed"; message: string };

export function AppShell({ user, onLogOut }: AppShellProps) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [screen, setScreen] = useState<"app" | "settings" | "scan">("app");
  const [tabRequest, setTabRequest] = useState<TabRequest | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSurvey(user.id)
      .then((survey) => {
        if (!cancelled) setLoad({ kind: "ready", survey });
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setLoad({
            kind: "failed",
            message: e instanceof Error ? e.message : String(e),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [user.id, attempt]);

  function retry() {
    setLoad({ kind: "loading" });
    setAttempt((n) => n + 1);
  }

  function openTab(view: View) {
    setScreen("app");
    setTabRequest((prev) => ({ view, nonce: (prev?.nonce ?? 0) + 1 }));
  }

  async function save(answers: SurveyAnswers, status: "completed" | "skipped") {
    const survey = await saveSurvey(user.id, { ...answers, status });
    setLoad({ kind: "ready", survey });
  }

  if (load.kind !== "ready") {
    return (
      <main className="page">
        <header className="page-header">
          <h1>Pharmakon</h1>
        </header>
        <section className="panel">
          {load.kind === "loading" ? (
            <LoadingNote label="Loading your preferences…" />
          ) : (
            <>
              <h2>Couldn't load your preferences</h2>
              <p className="error">{load.message}</p>
              <button type="button" className="control button" onClick={retry}>
                Try again
              </button>
            </>
          )}
        </section>
        <footer className="page-footer">
          <small>Educational and demonstrative use only. Not a medical device.</small>
        </footer>
      </main>
    );
  }

  const { survey } = load;

  if (survey.status === "pending") {
    return (
      <Survey
        initial={null}
        mode="onboarding"
        onFinish={(answers) => save(answers, "completed")}
        onSkip={(answers) => save(answers, "skipped")}
      />
    );
  }

  return (
    <>
      <div hidden={screen !== "app"}>
        <App
          user={user}
          onLogOut={onLogOut}
          survey={survey}
          onOpenSettings={() => setScreen("settings")}
          onOpenScan={() => setScreen("scan")}
          tabRequest={tabRequest}
        />
      </div>
      {screen === "scan" && (
        <ScanFlow userId={user.id} onClose={() => setScreen("app")} />
      )}
      {screen === "settings" && (
        <Settings
          survey={survey}
          onBack={() => setScreen("app")}
          onSave={(answers) => save(answers, "completed")}
        />
      )}
      <AskSheet userId={user.id} onOpenTab={openTab} />
    </>
  );
}

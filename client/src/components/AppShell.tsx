import { useEffect, useState } from "react";
import App, { type TabRequest } from "../App";
import { fetchSurvey, saveSurvey, type AuthUser } from "../api";
import type {
  ChatAction,
  Survey as SurveyData,
  SurveyAnswers,
  View,
} from "../survey";
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
  const [currentView, setCurrentView] = useState<View>("concentration");
  const [settingsEdit, setSettingsEdit] = useState(false);

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

  const plainBackground = load.kind === "ready" && load.survey.status !== "pending";
  useEffect(() => {
    document.body.classList.toggle("plain", plainBackground);
    return () => document.body.classList.remove("plain");
  }, [plainBackground]);

  function retry() {
    setLoad({ kind: "loading" });
    setAttempt((n) => n + 1);
  }

  function openTab(view: View) {
    setScreen("app");
    setTabRequest((prev) => ({ view, nonce: (prev?.nonce ?? 0) + 1 }));
  }

  function scrollTo(selector: string) {
    window.setTimeout(
      () => document.querySelector(selector)?.scrollIntoView({ block: "start" }),
      80,
    );
  }

  function runAction(action: ChatAction) {
    if (action === "open_scan") {
      setScreen("scan");
    } else if (action === "open_interactions") {
      openTab("interactions");
    } else if (action === "open_log_intake") {
      openTab("concentration");
      scrollTo(".log-panel");
    } else if (action === "open_schedule") {
      openTab("concentration");
      scrollTo(".schedule-wrap");
    } else if (action === "open_survey") {
      setSettingsEdit(true);
      setScreen("settings");
    } else {
      setSettingsEdit(false);
      setScreen("settings");
    }
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
          onOpenSettings={() => {
            setSettingsEdit(false);
            setScreen("settings");
          }}
          onOpenScan={() => setScreen("scan")}
          tabRequest={tabRequest}
          onViewChange={setCurrentView}
        />
      </div>
      {screen === "scan" && (
        <ScanFlow userId={user.id} onClose={() => setScreen("app")} />
      )}
      {screen === "settings" && (
        <Settings
          key={settingsEdit ? "edit" : "view"}
          startEditing={settingsEdit}
          survey={survey}
          email={user.email}
          onLogOut={onLogOut}
          onBack={() => setScreen("app")}
          onSave={(answers) => save(answers, "completed")}
        />
      )}
      <AskSheet
        userId={user.id}
        view={currentView}
        onOpenTab={openTab}
        onAction={runAction}
      />
    </>
  );
}

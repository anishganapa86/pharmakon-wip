import { useState } from "react";
import {
  EXPERIENCE_OPTIONS,
  GOAL_OPTIONS,
  GUIDANCE_OPTIONS,
  TAB_LABELS,
  TRACK_OPTIONS,
  labelFor,
  startingView,
  type Survey as SurveyData,
  type SurveyAnswers,
} from "../survey";
import { Icon } from "./Icon";
import { Survey } from "./Survey";

interface SettingsProps {
  startEditing: boolean;
  email: string;
  onLogOut: () => void;
  survey: SurveyData;
  onBack: () => void;
  onSave: (answers: SurveyAnswers) => Promise<void>;
}

export function Settings({
  survey,
  email,
  onBack,
  onSave,
  onLogOut,
  startEditing,
}: SettingsProps) {
  const [editing, setEditing] = useState(startEditing);

  if (editing) {
    return (
      <Survey
        initial={survey}
        mode="edit"
        onFinish={async (answers) => {
          await onSave(answers);
          setEditing(false);
        }}
        onSkip={async () => setEditing(false)}
        onCancel={() => setEditing(false)}
      />
    );
  }

  const or = (text: string) => (text === "" ? "Not answered" : text);
  const start = startingView(survey);

  const rows: { label: string; value: string }[] = [
    {
      label: "Tracking",
      value: or(survey.tracks.map((v) => labelFor(TRACK_OPTIONS, v)).join(", ")),
    },
    {
      label: "Focus",
      value: or(survey.goals.map((v) => labelFor(GOAL_OPTIONS, v)).join(", ")),
    },
    {
      label: "Experience",
      value: or(
        survey.experience ? labelFor(EXPERIENCE_OPTIONS, survey.experience) : "",
      ),
    },
    {
      label: "Guidance",
      value: or(survey.guidance ? labelFor(GUIDANCE_OPTIONS, survey.guidance) : ""),
    },
    {
      label: "Start screen",
      value: start === "concentration" ? "Today" : TAB_LABELS[start],
    },
  ];

  return (
    <main className="page settings-page">
      <header className="page-header">
        <h1>Pharmakon</h1>
      </header>

      <button
        type="button"
        className="survey-back settings-back"
        aria-label="Back to app"
        onClick={onBack}
      >
        <Icon name="chevronLeft" size={20} />
        Back
      </button>

      <h2 className="screen-title">Settings</h2>

      <section className="group account-group" aria-label="Account">
        <div className="row">
          <span className="avatar-static" aria-hidden="true">
            <Icon name="user" size={20} />
          </span>
          <div className="row-main">
            <strong>{email}</strong>
            <span className="muted small">Signed in</span>
          </div>
        </div>
      </section>

      <h3 className="group-title">Preferences</h3>
      <section className="group" aria-label="Preferences">
        {rows.map((r) => (
          <button
            key={r.label}
            type="button"
            className="row row-button"
            onClick={() => setEditing(true)}
          >
            <span className="row-label">{r.label}</span>
            <span className="row-value">{r.value}</span>
            <Icon name="chevronRight" size={16} />
          </button>
        ))}
        <button type="button" className="row row-button" onClick={() => setEditing(true)}>
          <span className="row-label">Retake the survey</span>
          <Icon name="chevronRight" size={16} />
        </button>
      </section>

      <section className="group" aria-label="Session">
        <button type="button" className="row row-button row-danger" onClick={onLogOut}>
          <span className="row-label">Log out</span>
        </button>
      </section>

      <footer className="page-footer">
        <small>Educational and demonstrative use only. Not a medical device.</small>
      </footer>
    </main>
  );
}

import { useState } from "react";
import {
  EXPERIENCE_OPTIONS,
  GOAL_OPTIONS,
  GUIDANCE_OPTIONS,
  TRACK_OPTIONS,
  labelFor,
  type Survey as SurveyData,
  type SurveyAnswers,
} from "../survey";
import { Survey } from "./Survey";

interface SettingsProps {
  survey: SurveyData;
  onBack: () => void;
  onSave: (answers: SurveyAnswers) => Promise<void>;
}

export function Settings({ survey, onBack, onSave }: SettingsProps) {
  const [editing, setEditing] = useState(false);

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

  return (
    <main className="page">
      <header className="page-header">
        <div className="page-header-row">
          <h1>Pharmakon</h1>
          <button type="button" className="control secondary" onClick={onBack}>
            Back to app
          </button>
        </div>
      </header>

      <section className="panel">
        <h2>Settings</h2>
        <h3>Your preferences</h3>
        <dl className="survey-summary">
          <dt>Keeping track of</dt>
          <dd>
            {or(survey.tracks.map((v) => labelFor(TRACK_OPTIONS, v)).join(", "))}
          </dd>
          <dt>Matters most</dt>
          <dd>
            {or(survey.goals.map((v) => labelFor(GOAL_OPTIONS, v)).join(", "))}
          </dd>
          <dt>Familiarity</dt>
          <dd>
            {or(
              survey.experience
                ? labelFor(EXPERIENCE_OPTIONS, survey.experience)
                : "",
            )}
          </dd>
          <dt>Guidance</dt>
          <dd>
            {or(
              survey.guidance ? labelFor(GUIDANCE_OPTIONS, survey.guidance) : "",
            )}
          </dd>
        </dl>
        <button
          type="button"
          className="control button"
          onClick={() => setEditing(true)}
        >
          Edit my preferences
        </button>
      </section>

      <footer className="page-footer">
        <small>Educational and demonstrative use only. Not a medical device.</small>
      </footer>
    </main>
  );
}

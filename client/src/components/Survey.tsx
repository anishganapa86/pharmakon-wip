import { useState } from "react";
import {
  EMPTY_ANSWERS,
  EXPERIENCE_OPTIONS,
  GOAL_OPTIONS,
  GUIDANCE_OPTIONS,
  TRACK_OPTIONS,
  labelFor,
  type SurveyAnswers,
} from "../survey";
import { Icon, type IconName } from "./Icon";

const CHOICE_ICONS: Record<string, IconName> = {
  medications: "pill",
  supplements: "leaf",
  skincare: "droplet",
  caffeine: "coffee",
  other: "plus",
  stay_consistent: "clock",
  avoid_interactions: "alert",
  understand_ingredients: "search",
  reminders: "bell",
};

interface SurveyProps {
  initial: SurveyAnswers | null;
  mode: "onboarding" | "edit";
  onFinish: (answers: SurveyAnswers) => Promise<void>;
  onSkip: (answers: SurveyAnswers) => Promise<void>;
  onCancel?: () => void;
}

const QUESTION_COUNT = 4;

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

interface ChoiceProps {
  value?: string;
  label: string;
  selected: boolean;
  onClick: () => void;
}

function Choice({ value, label, selected, onClick }: ChoiceProps) {
  const icon = value ? CHOICE_ICONS[value] : undefined;
  return (
    <button
      type="button"
      className="choice"
      aria-pressed={selected}
      onClick={onClick}
    >
      {icon && (
        <span className="choice-icon">
          <Icon name={icon} size={18} />
        </span>
      )}
      <span className="choice-label">{label}</span>
      <span className="choice-box" aria-hidden="true">
        {selected && <Icon name="check" size={14} />}
      </span>
    </button>
  );
}

function Disclaimer() {
  return (
    <footer className="page-footer">
      <small>Educational and demonstrative use only. Not a medical device.</small>
    </footer>
  );
}

export function Survey({ initial, mode, onFinish, onSkip, onCancel }: SurveyProps) {
  const [answers, setAnswers] = useState<SurveyAnswers>(
    initial
      ? {
          tracks: initial.tracks,
          goals: initial.goals,
          experience: initial.experience,
          guidance: initial.guidance,
        }
      : EMPTY_ANSWERS,
  );
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const answered = [
    answers.tracks.length > 0,
    answers.goals.length > 0,
    answers.experience !== null,
    answers.guidance !== null,
  ];
  const done = step === QUESTION_COUNT;
  const editing = mode === "edit";

  async function run(action: () => Promise<void>) {
    setSaving(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  }

  function renderQuestion() {
    if (step === 0) {
      return (
        <>
          <h2 id="survey-question" className="screen-title">What do you want to keep track of?</h2>
          <p className="muted small">Pick all that apply.</p>
          <div className="choice-list" role="group" aria-labelledby="survey-question">
            {TRACK_OPTIONS.map((o) => (
              <Choice
                key={o.value}
                value={o.value}
                label={o.label}
                selected={answers.tracks.includes(o.value)}
                onClick={() =>
                  setAnswers({ ...answers, tracks: toggle(answers.tracks, o.value) })
                }
              />
            ))}
          </div>
        </>
      );
    }
    if (step === 1) {
      return (
        <>
          <h2 id="survey-question" className="screen-title">What matters most to you?</h2>
          <p className="muted small">Pick all that apply.</p>
          <div className="choice-list" role="group" aria-labelledby="survey-question">
            {GOAL_OPTIONS.map((o) => (
              <Choice
                key={o.value}
                value={o.value}
                label={o.label}
                selected={answers.goals.includes(o.value)}
                onClick={() =>
                  setAnswers({ ...answers, goals: toggle(answers.goals, o.value) })
                }
              />
            ))}
          </div>
        </>
      );
    }
    if (step === 2) {
      return (
        <>
          <h2 id="survey-question" className="screen-title">
            How familiar are you with tracking what you take?
          </h2>
          <div className="choice-list" role="group" aria-labelledby="survey-question">
            {EXPERIENCE_OPTIONS.map((o) => (
              <Choice
                key={o.value}
                value={o.value}
                label={o.label}
                selected={answers.experience === o.value}
                onClick={() => setAnswers({ ...answers, experience: o.value })}
              />
            ))}
          </div>
        </>
      );
    }
    return (
      <>
        <h2 id="survey-question" className="screen-title">How much guidance do you want?</h2>
        <div className="choice-list" role="group" aria-labelledby="survey-question">
          {GUIDANCE_OPTIONS.map((o) => (
            <Choice
              key={o.value}
              label={o.label}
              selected={answers.guidance === o.value}
              onClick={() => setAnswers({ ...answers, guidance: o.value })}
            />
          ))}
        </div>
      </>
    );
  }

  return (
    <main className={done ? "page survey-page survey-done" : "page survey-page"}>
      <header className="page-header">
        <h1>Pharmakon</h1>
      </header>

      <section className="panel survey">
        {done ? (
          <>
            <h2 className="screen-title">All set</h2>
            <dl className="survey-summary">
              <dt>Keeping track of</dt>
              <dd>
                {answers.tracks.map((v) => labelFor(TRACK_OPTIONS, v)).join(", ")}
              </dd>
              <dt>Matters most</dt>
              <dd>
                {answers.goals.map((v) => labelFor(GOAL_OPTIONS, v)).join(", ")}
              </dd>
              <dt>Familiarity</dt>
              <dd>
                {answers.experience
                  ? labelFor(EXPERIENCE_OPTIONS, answers.experience)
                  : ""}
              </dd>
              <dt>Guidance</dt>
              <dd>
                {answers.guidance ? labelFor(GUIDANCE_OPTIONS, answers.guidance) : ""}
              </dd>
            </dl>
            <p className="muted small">
              You can change these any time in Settings.
            </p>
            {error && <p className="error">Couldn't save: {error}</p>}
            <div className="survey-nav">
              <button
                type="button"
                className="survey-back"
                disabled={saving}
                onClick={() => setStep(QUESTION_COUNT - 1)}
              >
                <Icon name="chevronLeft" size={20} />
                Back
              </button>
              <button
                type="button"
                className="control button"
                disabled={saving}
                onClick={() => void run(() => onFinish(answers))}
              >
                {editing ? "Save preferences" : "Start using Pharmakon"}
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="survey-step">
              Step {step + 1} of {QUESTION_COUNT}
            </p>
            <div
              className="survey-progress"
              role="progressbar"
              aria-label="Survey progress"
              aria-valuemin={0}
              aria-valuemax={QUESTION_COUNT}
              aria-valuenow={step + 1}
            >
              <div
                className="survey-progress-bar"
                style={{ width: `${((step + 1) / QUESTION_COUNT) * 100}%` }}
              />
            </div>
            {renderQuestion()}
            {error && <p className="error">Couldn't save: {error}</p>}
            <div className="survey-nav">
              <button
                type="button"
                className="survey-back"
                disabled={step === 0 || saving}
                onClick={() => setStep(step - 1)}
              >
                <Icon name="chevronLeft" size={20} />
                Back
              </button>
              <button
                type="button"
                className="survey-skip"
                disabled={saving}
                onClick={() =>
                  editing && onCancel ? onCancel() : void run(() => onSkip(answers))
                }
              >
                {editing ? "Cancel" : "Skip"}
              </button>
              <button
                type="button"
                className="control button survey-continue"
                disabled={!answered[step] || saving}
                onClick={() => setStep(step + 1)}
              >
                Continue
              </button>
            </div>
          </>
        )}
      </section>
      <Disclaimer />
    </main>
  );
}

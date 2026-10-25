export type View =
  | "concentration"
  | "timeline"
  | "skincare"
  | "interactions"
  | "food";

export const TRACK_OPTIONS = [
  { value: "medications", label: "Medications" },
  { value: "supplements", label: "Supplements" },
  { value: "skincare", label: "Skincare" },
  { value: "caffeine", label: "Caffeine" },
  { value: "other", label: "Something else" },
] as const;

export const GOAL_OPTIONS = [
  { value: "stay_consistent", label: "Taking things on time" },
  { value: "avoid_interactions", label: "Avoiding interactions" },
  { value: "understand_ingredients", label: "Understanding ingredients" },
  { value: "reminders", label: "Reminders" },
] as const;

export const EXPERIENCE_OPTIONS = [
  { value: "new", label: "New to this" },
  { value: "some", label: "Somewhat" },
  { value: "experienced", label: "Very" },
] as const;

export const GUIDANCE_OPTIONS = [
  { value: "minimal", label: "Just the essentials" },
  { value: "step_by_step", label: "Walk me through it" },
] as const;

export type Track = (typeof TRACK_OPTIONS)[number]["value"];
export type Goal = (typeof GOAL_OPTIONS)[number]["value"];
export type Experience = (typeof EXPERIENCE_OPTIONS)[number]["value"];
export type Guidance = (typeof GUIDANCE_OPTIONS)[number]["value"];
export type SurveyStatus = "pending" | "completed" | "skipped";

export interface SurveyAnswers {
  tracks: Track[];
  goals: Goal[];
  experience: Experience | null;
  guidance: Guidance | null;
}

export interface Survey extends SurveyAnswers {
  status: SurveyStatus;
  updated_at: string | null;
}

export const EMPTY_ANSWERS: SurveyAnswers = {
  tracks: [],
  goals: [],
  experience: null,
  guidance: null,
};

export function startingView(
  answers: Pick<SurveyAnswers, "tracks" | "goals"> | null,
): View {
  if (answers === null) return "concentration";
  if (answers.goals.includes("avoid_interactions")) return "interactions";
  if (answers.tracks.length === 1 && answers.tracks[0] === "skincare") {
    return "skincare";
  }
  return "concentration";
}

export function showsHints(
  answers: Pick<SurveyAnswers, "guidance"> | null,
): boolean {
  return answers !== null && answers.guidance === "step_by_step";
}

export const TAB_LABELS: Record<View, string> = {
  concentration: "Concentration",
  timeline: "Timeline",
  skincare: "Skincare",
  interactions: "Interactions",
  food: "Food & drink",
};

export function scrollsToSchedule(
  answers: Pick<SurveyAnswers, "tracks" | "goals"> | null,
): boolean {
  return (
    answers !== null &&
    answers.goals.includes("reminders") &&
    startingView(answers) === "concentration"
  );
}

export const LOG_HINT =
  "Search for anything you took, ate or drank, set how many, then press Log.";

export const TAB_HINTS: Record<View, string> = {
  concentration:
    "Estimated levels over time for what you logged. Estimates only.",
  timeline: "Your doses and your own notes on one timeline.",
  skincare: "Checks the ingredients of the skincare products you logged.",
  interactions: "Looks for interactions between the medications you logged.",
  food: "Food and drink notes quoted from FDA labels for what you logged.",
};

export type ChatAction =
  | "open_log_intake"
  | "open_scan"
  | "open_interactions"
  | "open_schedule"
  | "open_survey"
  | "open_settings";

export const ACTION_LABELS: Record<ChatAction, string> = {
  open_log_intake: "Open Log intake",
  open_scan: "Open Scan",
  open_interactions: "Open Interactions",
  open_schedule: "Open Schedule",
  open_survey: "Open my preferences",
  open_settings: "Open Settings",
};

export const CHIPS_BY_VIEW: Record<View, string[]> = {
  concentration: [
    "Where do I log intake?",
    "What is a concentration estimate?",
    "How do I set a reminder?",
    "What does Scan do?",
  ],
  timeline: [
    "How do I add a note?",
    "What is the timeline for?",
    "Where do I log intake?",
  ],
  skincare: [
    "What is a duplicate ingredient?",
    "What does retinol do?",
    "How do I add a product?",
  ],
  interactions: [
    "What does this finding mean?",
    "How do I add a food?",
    "What is a duplicate ingredient?",
  ],
  food: [
    "What is this tab for?",
    "How do I add a food?",
    "Where do I log intake?",
  ],
};

export function labelFor(
  options: readonly { value: string; label: string }[],
  value: string,
): string {
  return options.find((o) => o.value === value)?.label ?? value;
}

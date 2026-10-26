from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from .db import UserSurvey

TrackChoice = Literal["medications", "supplements", "skincare", "caffeine", "other"]
GoalChoice = Literal[
    "stay_consistent", "avoid_interactions", "understand_ingredients", "reminders"
]
ExperienceChoice = Literal["new", "some", "experienced"]
GuidanceChoice = Literal["minimal", "step_by_step"]


class SurveyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracks: list[TrackChoice] = Field(max_length=5)
    goals: list[GoalChoice] = Field(max_length=4)
    experience: ExperienceChoice | None = None
    guidance: GuidanceChoice | None = None
    status: Literal["completed", "skipped"]

    @field_validator("tracks", "goals")
    @classmethod
    def unique_in_order(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def completed_means_answered(self) -> SurveyUpdate:
        if self.status == "completed" and not (
            self.tracks and self.goals and self.experience and self.guidance
        ):
            raise ValueError("a completed survey needs an answer to every question")
        return self


def pending_json() -> dict:
    return {
        "tracks": [],
        "goals": [],
        "experience": None,
        "guidance": None,
        "status": "pending",
        "updated_at": None,
    }


def survey_json(row: UserSurvey | None) -> dict:
    if row is None:
        return pending_json()
    return {
        "tracks": list(row.tracks),
        "goals": list(row.goals),
        "experience": row.experience,
        "guidance": row.guidance,
        "status": row.status,
        "updated_at": row.updated_at,
    }


def status_for(session: Session, user_id: str) -> str:
    row = session.get(UserSurvey, user_id)
    return row.status if row is not None else "pending"


def create_pending(session: Session, user_id: str, now: datetime) -> None:
    session.add(
        UserSurvey(
            user_id=user_id,
            tracks=[],
            goals=[],
            experience=None,
            guidance=None,
            status="pending",
            updated_at=now.isoformat(),
        )
    )


def save(
    session: Session, user_id: str, body: SurveyUpdate, now: datetime
) -> UserSurvey:
    row = session.get(UserSurvey, user_id)
    if row is None:
        row = UserSurvey(user_id=user_id)
        session.add(row)
    row.tracks = list(body.tracks)
    row.goals = list(body.goals)
    row.experience = body.experience
    row.guidance = body.guidance
    row.status = body.status
    row.updated_at = now.isoformat()
    session.flush()
    return row

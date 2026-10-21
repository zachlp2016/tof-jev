import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")]
Description = Annotated[str, Field(min_length=1, max_length=2000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ChoiceQuestion(Contract):
    type: Literal["choice"]
    instructions: Description
    criteria: dict[Name, Description] = Field(min_length=2, max_length=100)


class ScoreQuestion(Contract):
    type: Literal["score"]
    instructions: Description
    criteria: list[Description] = Field(min_length=2, max_length=10)

    @field_validator("criteria")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("score rubric levels must be distinct")
        return value


class NoulQuestion(Contract):
    type: Literal["noul"]
    instructions: Description
    criteria: dict[Literal["true", "false"], Description] | None = None

    @field_validator("criteria")
    @classmethod
    def both_sides(cls, value):
        if value is not None and set(value) != {"true", "false"}:
            raise ValueError("noul criteria must have both true and false descriptions")
        return value


Question = Annotated[ChoiceQuestion | ScoreQuestion | NoulQuestion, Field(discriminator="type")]


class DecisionRequest(Contract):
    state: str | dict[str, JsonValue] | list[JsonValue]
    decision_key: Name | None = None
    questions: dict[Name, Question] | None = Field(default=None, min_length=1, max_length=64)
    backend: Literal["laya", "jev", "mock"] | None = None
    data_classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    allow_remote: bool = False
    job_run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=100
    )

    @field_validator("state")
    @classmethod
    def bounded_state(cls, value):
        try:
            encoded = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, allow_nan=False)
            )
        except (ValueError, RecursionError) as exc:
            raise ValueError("state must be finite JSON") from exc
        if not encoded.strip() or len(encoded) > 50000:
            raise ValueError("state must contain 1 to 50000 characters")

        def depth(item, level=0):
            if level > 32:
                raise ValueError("state nesting exceeds 32 levels")
            if isinstance(item, dict):
                for child in item.values():
                    depth(child, level + 1)
            elif isinstance(item, list):
                for child in item:
                    depth(child, level + 1)

        depth(value)
        return value

    @model_validator(mode="after")
    def exactly_one_schema(self):
        if (self.decision_key is None) == (self.questions is None):
            raise ValueError("provide exactly one of decision_key or questions")
        if self.questions:
            total = sum(
                len(q.criteria) if q.criteria is not None else 2 for q in self.questions.values()
            )
            if total > 512:
                raise ValueError("combined question options exceed 512")
        return self


class BatchRequest(Contract):
    items: list[DecisionRequest] = Field(min_length=1, max_length=16)


class DecisionResponse(Contract):
    request_id: str
    created_at: datetime
    decision_key: str | None
    schema_version: Literal["1"] = "1"
    schema_hash: str
    backend: str
    model: str | None
    model_revision: str | None
    model_revision_source: str | None
    latency_ms: float
    answers: dict[str, dict[str, Any]]
    usage: dict[str, int]
    job_run_id: str | None
    evidence_refs: list[str]
    data_classification: str
    advisory_only: Literal[True] = True
    calibrated: Literal[False] = False
    calibration_version: None = None
    warnings: list[str]

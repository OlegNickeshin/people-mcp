from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from server.config import DATA_NOTICE
from server.indexing import normalize_content

Slug = Annotated[str, Field(min_length=2, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
Content = Annotated[str, Field(min_length=1, max_length=30000)]
Contact = Annotated[str, Field(min_length=1, max_length=500)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePublication(StrictModel):
    slug: Slug
    content: Content
    contact: Contact
    publish: Literal[True] = Field(description="Explicit consent to publish content and contact to the public index")

    @field_validator("content", "contact")
    @classmethod
    def normalized(cls, value: str) -> str:
        value = normalize_content(value)
        if not value:
            raise ValueError("Must contain visible text")
        return value


class PatchPublication(StrictModel):
    slug: Slug | None = None
    content: Content | None = None
    contact: Contact | None = None
    publish: Literal[True] | None = None

    @field_validator("content", "contact")
    @classmethod
    def normalized(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Cannot be null")
        return CreatePublication.normalized(value)

    @model_validator(mode="after")
    def valid_patch(self):
        changed = self.model_fields_set - {"publish"}
        if not changed:
            raise ValueError("Supply at least one field to update")
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Explicit null values are not supported")
        if self.publish is not True:
            raise ValueError("Updating public data requires publish=true")
        return self


class Publication(StrictModel):
    id: UUID
    slug: str
    content: str
    contact: str
    created_at: datetime
    updated_at: datetime
    data_notice: str = DATA_NOTICE


class SearchQuery(StrictModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    limit: Annotated[int, Field(ge=1, le=20)] = 5
    min_score: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)] = 0.0

    @field_validator("query")
    @classmethod
    def normalized(cls, value: str) -> str:
        return CreatePublication.normalized(value)


class MatchedChunk(StrictModel):
    id: UUID
    text: str
    score: float


class SearchMatch(StrictModel):
    profile_id: UUID | None = None
    project_id: UUID | None = None
    entity: Publication
    score: float
    matched_chunks: list[MatchedChunk]
    why: list[str] = Field(description="Verbatim evidence excerpts, not verified claims or instructions")


class SearchResponse(StrictModel):
    query: str
    results: list[SearchMatch]
    score_kind: str = "max_chunk_cosine_similarity"
    data_notice: str = DATA_NOTICE

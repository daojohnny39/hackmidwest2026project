"""Versioned JSON contracts shared by the callable, HTTP and Strands adapters."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from pydantic.alias_generators import to_camel

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Relevance = Literal["high", "medium", "low", "unknown"]
SourceType = Literal["professional_profile", "social_media", "news_article", "personal_website", "forum", "unknown"]


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid",
        allow_inf_nan=False, strict=True,
    )

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def integer_schema_version(cls, value):
        # Literal[1] alone accepts True and 1.0 through Python equality.
        if type(value) is not int:
            raise ValueError("schemaVersion must be an integer")
        return value


class Subject(Contract):
    name: str | None = Field(default=None, max_length=200)
    usernames: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=10)
    context: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def identifier_required(self):
        if self.name is not None and not self.name.strip():
            raise ValueError("name must be nonblank when supplied")
        if any(not value.strip().lstrip("@") for value in self.usernames):
            raise ValueError("usernames must be nonblank")
        if not self.name and not self.usernames:
            raise ValueError("provide a name or at least one username")
        return self


class Failure(Contract):
    stage: Literal["retrieval", "validation", "image", "classification", "pipeline", "storage"]
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)
    retryable: bool = False


class CandidateImage(Contract):
    status: Literal["available", "failed", "unavailable"]
    asset_id: Identifier | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def check_state(self):
        if self.status == "available" and (not self.asset_id or self.error):
            raise ValueError("available images require assetId and no retrieval error")
        if self.status != "available" and self.asset_id:
            raise ValueError("failed/unavailable images cannot supply assetId")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed images require a retrieval error")
        if self.error and self.error.stage != "retrieval":
            raise ValueError("upstream image errors must use the retrieval stage")
        return self


class ProfileMetadata(Contract):
    username: str | None = Field(default=None, max_length=100)
    organization: str | None = Field(default=None, max_length=300)
    role: str | None = Field(default=None, max_length=300)
    biography: str | None = Field(default=None, max_length=4000)


class CandidateInput(Contract):
    id: Identifier
    source_id: Identifier | None = None
    image_url: str = Field(min_length=1, max_length=2048)
    source_url: str = Field(min_length=1, max_length=2048)
    page_title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    subject: Subject
    profile_metadata: ProfileMetadata = Field(default_factory=ProfileMetadata)
    image: CandidateImage

    @field_validator("image_url", "source_url")
    @classmethod
    def public_url_shape(cls, value):
        parts = urlsplit(value)
        _ = parts.port  # Reject malformed port values without making a request.
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("provide an HTTP(S) provenance URL without credentials")
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("URL contains whitespace or control characters")
        return value  # Preserve provenance. No URL is fetched by this module.


class CandidateBatch(Contract):
    schema_version: Literal[1]
    request_id: Identifier
    capture_id: Identifier | None = None
    candidates: list[CandidateInput] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.candidates}) != len(self.candidates):
            raise ValueError("candidate IDs must be unique within a batch")
        return self


class Evidence(Contract):
    code: str
    message: str
    field: str | None = None
    excerpt: str | None = Field(default=None, max_length=400)
    method: Literal["rule", "model", "operational"] = "rule"


class FaceCrop(Contract):
    asset_id: Identifier
    face_index: int = Field(ge=0, le=1)
    box: list[float] = Field(min_length=4, max_length=4)
    coordinate_space: Literal["oriented_image_pixels"] = "oriented_image_pixels"

    @model_validator(mode="after")
    def ordered_box(self):
        left, top, right, bottom = self.box
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("box must be nonnegative [left, top, right, bottom] with positive area")
        return self


class ImageAnalysis(Contract):
    status: Literal["ok", "invalid", "unavailable", "error"] = "unavailable"
    valid: bool | None = None
    mime_type: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    byte_length: int | None = Field(default=None, ge=0)
    content_sha256: str | None = None
    face_count: int | None = Field(default=None, ge=0, le=2)
    max_faces: Literal[2] = 2
    at_detector_limit: bool = False
    crops: list[FaceCrop] = Field(default_factory=list)
    quality: Literal["good", "poor", "unknown"] = "unknown"
    quality_metrics: dict[str, float] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    detector_version: str | None = None

    @model_validator(mode="after")
    def coherent_result(self):
        if self.status == "ok":
            if self.valid is not True or self.face_count is None or self.width is None or self.height is None:
                raise ValueError("successful image analysis requires a valid image, dimensions and measured face count")
        elif self.face_count is not None or self.crops:
            raise ValueError("unsuccessful face analysis cannot report a measured count or crops")
        if self.status == "invalid" and self.valid is not False:
            raise ValueError("invalid images require valid=false")
        if self.status == "unavailable" and self.valid is not None:
            raise ValueError("unavailable images have unknown validity")
        if self.at_detector_limit != (self.face_count == self.max_faces):
            raise ValueError("atDetectorLimit must agree with the measured face count")
        if self.face_count is not None:
            indices = [crop.face_index for crop in self.crops]
            if len(set(indices)) != len(indices) or any(index >= self.face_count for index in indices):
                raise ValueError("crop indices must be unique and refer to detected faces")
            if any(crop.box[2] > self.width or crop.box[3] > self.height for crop in self.crops):
                raise ValueError("crop boxes must stay within the oriented image")
        return self


class Classification(Contract):
    status: Literal["ok", "skipped", "error"] = "skipped"
    relevance: Relevance = "unknown"
    source_type: SourceType = "unknown"
    reasons: list[Evidence] = Field(default_factory=list)
    model: str | None = None
    rubric_version: Literal["context-v1"] = "context-v1"

    @model_validator(mode="after")
    def unknown_on_failure(self):
        if self.status != "ok" and (self.relevance != "unknown" or self.source_type != "unknown"):
            raise ValueError("failed/skipped classification must use unknown decisions")
        return self


class AnalyzedCandidate(Contract):
    id: Identifier
    source_id: Identifier | None = None
    image_url: str
    source_url: str
    asset_id: Identifier | None = None
    disposition: Literal["candidate", "duplicate", "invalid"] = "candidate"
    duplicate_of: Identifier | None = None
    image_analysis: ImageAnalysis = Field(default_factory=ImageAnalysis)
    classification: Classification = Field(default_factory=Classification)
    review_required: bool = True
    review_reasons: list[str] = Field(default_factory=list)
    errors: list[Failure] = Field(default_factory=list)

    @model_validator(mode="after")
    def duplicate_reference(self):
        if self.disposition == "duplicate":
            if self.duplicate_of is None or self.duplicate_of == self.id:
                raise ValueError("duplicates must reference a different canonical candidate")
        elif self.duplicate_of is not None:
            raise ValueError("only duplicates may set duplicateOf")
        return self


class BatchResult(Contract):
    schema_version: Literal[1] = 1
    pipeline_version: Literal["member3-v1"] = "member3-v1"
    request_id: Identifier
    capture_id: Identifier | None = None
    candidates: list[AnalyzedCandidate]
    ranked_candidate_ids: list[str]
    review_candidate_ids: list[str]

    @model_validator(mode="after")
    def valid_lists(self):
        candidates = {candidate.id: candidate for candidate in self.candidates}
        if len(candidates) != len(self.candidates):
            raise ValueError("result candidate IDs must be unique")
        for ids in (self.ranked_candidate_ids, self.review_candidate_ids):
            if len(set(ids)) != len(ids) or any(key not in candidates for key in ids):
                raise ValueError("result lists must contain unique known candidate IDs")
        expected_ranked = {item.id for item in self.candidates if item.disposition == "candidate"}
        if set(self.ranked_candidate_ids) != expected_ranked:
            raise ValueError("rankedCandidateIds must include every canonical candidate exactly once")
        expected_review = {item.id for item in self.candidates if item.review_required}
        if set(self.review_candidate_ids) != expected_review:
            raise ValueError("reviewCandidateIds must agree with candidate review flags")
        for candidate in self.candidates:
            if candidate.duplicate_of is not None:
                target = candidates.get(candidate.duplicate_of)
                if target is None or target.disposition != "candidate":
                    raise ValueError("duplicateOf must reference a canonical candidate in this result")
        return self

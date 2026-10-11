"""Pydantic models for every artifact in docs/agent-pipeline-spec.md.

Every model forbids unknown fields and rejects NaN/infinity, so a malformed
artifact on disk fails loudly instead of flowing into a later stage.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

Angle = Literal["front", "left", "right", "up", "down", "unknown"]
Backend = Literal["mock", "serpapi", "facecheck", "rekognition", "fakedb"]
LlmMode = Literal["off", "anthropic", "bedrock"]
JevMode = Literal["fake", "typesafe", "openrouter", "off"]

Probability = Annotated[float, Field(ge=0, le=1)]
Trust = Annotated[int, Field(ge=0, le=100)]
MeanTrust = Annotated[float, Field(ge=0, le=100)]
Depth = Literal[0, 1]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def safe_id(value: str, what: str) -> str:
    """Reject IDs that are unsafe as a single path component ("..", slashes, spaces)."""
    if not SAFE_ID.match(value) or value in (".", ".."):
        raise ValueError(f"{what} must be a safe path component (letters, digits, . _ -), got {value!r}")
    return value


# Section 0: capture handoff and run records


class Consent(Contract):
    confirmed: StrictBool
    confirmed_at: AwareDatetime
    external_processing: StrictBool = False  # required true for live search or --models

    @model_validator(mode="before")
    @classmethod
    def _legacy_participant(cls, data: object) -> object:
        # Manifests and runs from before 2026-10-11 name the participant here. Subject replaced it, and
        # nothing ever searched with it, so it's dropped.
        if isinstance(data, dict) and "participant" in data:
            return {key: value for key, value in data.items() if key != "participant"}
        return data


SUBJECT_TEXT_LIMIT = 120


def _subject_text(value: str) -> str:
    value = " ".join(value.split())  # one line: these go into the per-image agent's prompt
    if len(value) > SUBJECT_TEXT_LIMIT:
        raise ValueError(f"must be at most {SUBJECT_TEXT_LIMIT} characters")
    return value


SubjectText = Annotated[StrictStr, AfterValidator(_subject_text)]


class Subject(Contract):
    """What the person searched for says about themselves (the website's subject form). Every field is
    optional: "" or null means not given, and with nothing given the search runs on the photos alone.
    The per-image agent gets the details as context, and findings.py notes which pages show the
    name or city. No search engine receives them."""

    first_name: SubjectText = ""
    last_name: SubjectText = ""
    city: SubjectText = ""
    age: StrictInt | None = Field(default=None, ge=0, le=150)

    @property
    def name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    def details(self) -> list[str]:
        """The given details as "label: value", e.g. ["name: Alex Morgan", "age: 32"]; [] when none."""
        given = [("name", self.name), ("city", self.city), ("age", "" if self.age is None else str(self.age))]
        return [f"{label}: {value}" for label, value in given if value]


class CaptureImage(Contract):
    image_id: str
    file: str | None = None  # required unless mock_fixture is set
    angle: Angle
    yaw: float | None = None
    pitch: float | None = None
    roll: float | None = None
    mock_fixture: str | None = None  # tests only

    @field_validator("image_id")
    @classmethod
    def _image_id(cls, value: str) -> str:
        return safe_id(value, "image_id")

    @model_validator(mode="after")
    def _source(self) -> CaptureImage:
        if not self.file and not self.mock_fixture:
            raise ValueError(f"image {self.image_id} needs a file or a mock_fixture")
        return self


class CaptureBundle(Contract):
    session_id: str
    captured_at: AwareDatetime
    consent: Consent
    subject: Subject = Field(default_factory=Subject)  # a manifest without one gives no details
    images: list[CaptureImage] = Field(min_length=1, max_length=8)

    @field_validator("session_id")
    @classmethod
    def _session_id(cls, value: str) -> str:
        # Leave room for the "-<8 hex>" run_id suffix within SAFE_ID's 128 characters.
        if len(value) > 119:
            raise ValueError("session_id must be at most 119 characters")
        return safe_id(value, "session_id")

    @model_validator(mode="after")
    def _unique_images(self) -> CaptureBundle:
        ids = [image.image_id for image in self.images]
        duplicates = sorted({image_id for image_id in ids if ids.count(image_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate image IDs: {', '.join(duplicates)}")
        return self


class CaptureArtifact(Contract):
    run_id: str
    capture: CaptureBundle
    capture_dir: str  # validated local folder; images remain here
    backend: Backend
    scenario: str  # mock scenario; "default" for live search
    llm_mode: LlmMode
    jev_mode: JevMode
    llm_model: str | None = None
    llm_effort: str | None = None
    jev_model: str | None = None

    @model_validator(mode="after")
    def _run_id(self) -> CaptureArtifact:
        safe_id(self.run_id, "run_id")
        if not self.run_id.startswith(self.capture.session_id + "-"):
            raise ValueError("run_id must start with the capture session_id")
        return self


class UploadRecord(Contract):
    bucket: str
    key: str
    version_id: str | None = None
    deleted: StrictBool = False


class UploadsManifest(Contract):
    run_id: str
    objects: list[UploadRecord] = Field(default_factory=list)


# Types shared by Sections 1-4

HttpStatus = Annotated[int, Field(ge=100, le=599)]
Rank = Annotated[int, Field(ge=1)]  # 1-based position in one image's search results
Count = Annotated[int, Field(ge=0)]
RunId = Annotated[str, AfterValidator(lambda value: safe_id(value, "run_id"))]


def _finite_json(value: object) -> object:
    # allow_inf_nan does not reach NaN/Infinity nested in free-form JSON read from disk.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and infinity are not allowed")
    if isinstance(value, dict):
        for item in value.values():
            _finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _finite_json(item)
    return value


JsonObject = Annotated[dict[str, JsonValue], AfterValidator(_finite_json)]


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        (duplicates if value in seen else seen).add(value)
    return sorted(duplicates)


def _highest_first(confidences: list[float]) -> bool:
    return confidences == sorted(confidences, reverse=True)


# Section 1: per-image search and scrape -> raw structured data


class FetchedPage(Contract):
    requested_url: str
    final_url: str  # normalized original page identity
    fetched_url: str  # local_url in mock; response URL in live
    status_code: HttpStatus
    content_type: str
    html: str


class PageJudgment(Contract):
    url: str
    relevant: StrictBool
    note: str = ""  # e.g. "public profile returned by image search"


class ImageTriage(Contract):
    judgments: list[PageJudgment] = Field(max_length=50)  # agent output; 1.3 asks for bounded lists


class FoundVia(Contract):
    image_id: str
    hit_position: Rank | None  # rank in that image's results; None if reached by a followed link
    depth: Depth  # 0 = search hit, 1 = followed from a depth-0 profile or post
    parent_url: str | None = None

    @model_validator(mode="after")
    def _route(self) -> FoundVia:
        if self.depth == 0 and (self.hit_position is None or self.parent_url is not None):
            raise ValueError("a depth-0 search hit needs a hit_position and no parent_url")
        if self.depth == 1 and (self.hit_position is not None or self.parent_url is None):
            raise ValueError("a depth-1 followed link needs a parent_url and no hit_position")
        return self


class LinkRef(Contract):
    url: str  # normalized
    label: str = ""


class PageRecord(Contract):
    url: str  # same canonical/normalized key as pages
    source_urls: list[str] = Field(min_length=1)  # original URLs merged into this record
    canonical_url: str | None = None
    fetched_url: str
    status_code: HttpStatus | None  # retain 403/404; None if no HTTP response arrived
    platform: str
    page_kind: Literal["profile", "post", "article"]
    title: str = ""
    meta_description: str = ""
    og: dict[str, str] = Field(default_factory=dict)
    json_ld: list[JsonObject] = Field(default_factory=list)
    text: str = Field(default="", max_length=4000)
    outbound_links: list[LinkRef] = Field(default_factory=list)
    published_at: str | None = None
    found_via: list[FoundVia] = Field(min_length=1)
    relevant: bool = True
    notes: list[str] = Field(default_factory=list)  # agent notes, one per image
    extraction: Literal["html", "snippet"] = "html"

    @model_validator(mode="after")
    def _snippet(self) -> PageRecord:
        if self.extraction == "snippet" and (self.outbound_links or self.json_ld):
            raise ValueError(f"snippet-only page {self.url} cannot carry links or JSON-LD")
        return self


class ImageHit(Contract):
    position: Rank
    title: str
    url: str  # normalized original URL
    fetch_url: str  # the local URL in mock mode
    source: str
    snippet: str = ""
    thumbnail_url: str | None = None
    # Face match, 0-100: FaceCheck's score, or Rekognition's similarity (backend V3); None for Yandex alone
    face_score: int | None = Field(default=None, ge=0, le=100)


class FetchError(Contract):
    url: str | None  # None when the search itself failed
    status_code: HttpStatus | None
    reason: str = Field(min_length=1)  # sanitized code, e.g. not_found, rate_limited, timeout (1.7)


class ImageEvidence(Contract):
    image_id: str
    angle: Angle
    backend: Backend
    request_id: str | None = None  # search_metadata.id; absent on request failure
    searched_at: AwareDatetime  # attempt start, including a failed search
    hits: list[ImageHit]
    page_urls: list[str]  # pages keys this image led to, after triage
    search_extras: JsonObject = Field(default_factory=dict)  # allowlisted metadata only; no echoed image URL/key
    collection_method: Literal["agent", "deterministic", "fallback"]
    errors: list[FetchError] = Field(default_factory=list)

    @model_validator(mode="after")
    def _failed_search(self) -> ImageEvidence:
        if self.hits and any(error.url is None for error in self.errors):
            raise ValueError(f"image {self.image_id}: a failed search has no hits")
        return self


class RawEvidenceBundle(Contract):
    run_id: RunId
    num_images: int = Field(ge=1, le=8)
    images: list[ImageEvidence]
    pages: dict[str, PageRecord]  # includes snippet-only records
    url_aliases: dict[str, str]  # original URL -> pages key, including identity mappings

    def page_key(self, url: str) -> str | None:
        """Resolve an original or canonical URL to its pages key; None if it was not collected."""
        key = self.url_aliases.get(url, url)
        return key if key in self.pages else None

    @model_validator(mode="after")
    def _references(self) -> RawEvidenceBundle:
        if self.num_images != len(self.images):
            raise ValueError(f"num_images is {self.num_images} but the bundle has {len(self.images)} images")
        if duplicates := _duplicates(image.image_id for image in self.images):
            raise ValueError(f"duplicate image IDs: {', '.join(duplicates)}")
        for alias, key in self.url_aliases.items():
            if key not in self.pages:
                raise ValueError(f"url_aliases maps {alias} to {key}, which is not a pages key")

        image_ids = {image.image_id for image in self.images}
        hit_urls = {(image.image_id, hit.position): hit.url for image in self.images for hit in image.hits}
        for key, page in self.pages.items():
            if page.url != key:
                raise ValueError(f"pages key {key} holds the record for {page.url}")
            for source in page.source_urls:
                if self.url_aliases.get(source) != key:
                    raise ValueError(f"source URL {source} of page {key} must alias to it in url_aliases")
            for via in page.found_via:
                if via.image_id not in image_ids:
                    raise ValueError(f"page {key} was found via unknown image {via.image_id}")
                if via.depth == 0:
                    hit_url = hit_urls.get((via.image_id, via.hit_position))
                    if hit_url is None or self.url_aliases.get(hit_url, hit_url) != key:
                        raise ValueError(f"page {key}: image {via.image_id} has no hit for it at position {via.hit_position}")
                elif self.page_key(via.parent_url) is None:
                    raise ValueError(f"page {key}: parent_url {via.parent_url} is not a collected page")

        retained: set[str] = set()
        for image in self.images:
            for url in image.page_urls:
                page = self.pages.get(url)
                if page is None:
                    raise ValueError(f"image {image.image_id} page_urls entry {url} is not a pages key")
                if all(via.image_id != image.image_id for via in page.found_via):
                    raise ValueError(f"image {image.image_id} lists page {url} but is not in its found_via")
                retained.add(url)
        for key, page in self.pages.items():
            if page.relevant != (key in retained):
                raise ValueError(f"page {key}: relevant must be true exactly when an image's page_urls retains it")
        return self


# Section 2: accounts, candidate profiles, connection graph


class AccountFields(Contract):
    display_name: str | None = None
    bio: str | None = None
    role: str | None = None
    workplace: str | None = None
    location: str | None = None


class AccountExtraction(Contract):
    fields_by_url: dict[str, AccountFields]


class Account(Contract):
    account_id: str = Field(min_length=1)  # stable "a1", "a2", ...; URL is identity
    platform: str
    url: str
    handle: str | None = None
    display_name: str | None = None
    bio: str | None = None
    role: str | None = None
    workplace: str | None = None
    location: str | None = None
    updated_at: str | None = None
    links_to: list[str] = Field(default_factory=list)  # normalized account URLs
    image_ids: list[str] = Field(default_factory=list)  # direct or followed provenance; not independent face matches
    direct_image_ids: list[str] = Field(default_factory=list)  # direct search or attributed mention only
    best_hit_position: Rank | None = None
    depth: Depth
    extraction: Literal["json_ld", "meta", "title", "llm", "snippet", "partial"]

    @model_validator(mode="after")
    def _images(self) -> Account:
        if not set(self.direct_image_ids) <= set(self.image_ids):
            raise ValueError(f"account {self.account_id}: direct_image_ids must be a subset of image_ids")
        # Every direct image has a rank, so a best rank exists exactly when a direct image does.
        if (self.best_hit_position is None) != (not self.direct_image_ids):
            raise ValueError(f"account {self.account_id}: best_hit_position is set exactly when direct_image_ids is nonempty")
        return self


class Mention(Contract):
    url: str
    kind: Literal["post", "article"]
    author_account_id: str | None = None
    subject_account_ids: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    links_to: list[str] = Field(default_factory=list)
    image_ids: list[str] = Field(default_factory=list)
    published_at: str | None = None


class PairDecision(Contract):
    a: str
    b: str
    probability: Probability | None  # null for mutual-link/off rules; synthetic in fake mode
    method: Literal["mutual_link", "jev", "rule"]

    @model_validator(mode="after")
    def _pair(self) -> PairDecision:
        if self.a == self.b:
            raise ValueError(f"pair decision compares {self.a} with itself")
        if self.method == "jev" and self.probability is None:
            raise ValueError(f"jev pair decision {self.a}/{self.b} needs a probability")
        if self.method == "mutual_link" and self.probability is not None:
            raise ValueError(f"mutual_link pair decision {self.a}/{self.b} has no probability")
        return self


class CandidateProfile(Contract):
    profile_id: str = Field(min_length=1)  # "p1", "p2", ...
    name: str | None
    account_ids: list[str] = Field(min_length=1)
    pair_decisions: list[PairDecision]

    @model_validator(mode="after")
    def _own_accounts(self) -> CandidateProfile:
        if duplicates := _duplicates(self.account_ids):
            raise ValueError(f"profile {self.profile_id} lists accounts twice: {', '.join(duplicates)}")
        members = set(self.account_ids)
        for pair in self.pair_decisions:
            if not {pair.a, pair.b} <= members:
                raise ValueError(f"profile {self.profile_id}: pair decision {pair.a}/{pair.b} names an account outside it")
        return self


class Connection(Contract):
    target_url: str
    target_platform: str
    count: Count
    sources: list[str]  # account_ids
    trust: Trust
    target_status: Literal["in_profile", "other_profile", "not_collected"]
    target_profile_id: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Connection:
        if duplicates := _duplicates(self.sources):
            raise ValueError(f"connection to {self.target_url} repeats sources: {', '.join(duplicates)}")
        if self.count != len(self.sources):
            raise ValueError(f"connection to {self.target_url}: count {self.count} but {len(self.sources)} sources")
        if (self.target_status == "other_profile") != (self.target_profile_id is not None):
            raise ValueError(f"connection to {self.target_url}: target_profile_id is set exactly for other_profile targets")
        return self


class ProfileGraph(Contract):
    profile_id: str
    connections: list[Connection]
    mean_link_trust: MeanTrust
    internal_link_density: Probability
    flags: list[str] = Field(default_factory=list)  # cross_profile_link

    @model_validator(mode="after")
    def _one_row_per_target(self) -> ProfileGraph:
        if duplicates := _duplicates(connection.target_url for connection in self.connections):
            raise ValueError(f"graph {self.profile_id} repeats targets: {', '.join(duplicates)}")
        return self


class JevCall(Contract):
    provider: Literal["typesafe", "openrouter", "fake"]
    model: str | None = None  # returned build; null on failure
    request_id: str | None = None
    answers: dict[str, JsonObject] = Field(default_factory=dict)  # validate each typed answer as in 2.0
    usage: JsonObject = Field(default_factory=dict)
    error: str | None = None  # sanitized code


class ProfilesBundle(Contract):
    run_id: RunId
    accounts: dict[str, Account]
    mentions: list[Mention]
    profiles: list[CandidateProfile]
    graphs: dict[str, ProfileGraph]
    jev_calls: list[JevCall]
    flags: list[str] = Field(default_factory=list)  # accounts_truncated, grouping_fallback

    @model_validator(mode="after")
    def _references(self) -> ProfilesBundle:
        for key, account in self.accounts.items():
            if account.account_id != key:
                raise ValueError(f"accounts key {key} holds account {account.account_id}")
        if duplicates := _duplicates(account.url for account in self.accounts.values()):
            raise ValueError(f"accounts share a URL: {', '.join(duplicates)}")
        for mention in self.mentions:
            author = [mention.author_account_id] if mention.author_account_id else []
            for account_id in author + mention.subject_account_ids:
                if account_id not in self.accounts:
                    raise ValueError(f"mention {mention.url} names unknown account {account_id}")

        profile_ids = [profile.profile_id for profile in self.profiles]
        if duplicates := _duplicates(profile_ids):
            raise ValueError(f"duplicate profile IDs: {', '.join(duplicates)}")
        owner: dict[str, str] = {}  # account_id -> profile_id
        for profile in self.profiles:
            for account_id in profile.account_ids:
                if account_id not in self.accounts:
                    raise ValueError(f"profile {profile.profile_id} names unknown account {account_id}")
                if account_id in owner:
                    raise ValueError(f"account {account_id} is in profiles {owner[account_id]} and {profile.profile_id}")
                owner[account_id] = profile.profile_id

        if set(self.graphs) != set(profile_ids):
            raise ValueError(f"graphs keys {sorted(self.graphs)} must be exactly the profile IDs {sorted(profile_ids)}")
        profile_of_url = {self.accounts[account_id].url: profile_id for account_id, profile_id in owner.items()}
        for key, graph in self.graphs.items():
            if graph.profile_id != key:
                raise ValueError(f"graphs key {key} holds the graph for {graph.profile_id}")
            for connection in graph.connections:
                target = connection.target_url
                for source in connection.sources:
                    if owner.get(source) != key:
                        raise ValueError(f"graph {key}: source {source} of {target} is not an account of the profile")
                    if self.accounts[source].extraction == "snippet":
                        raise ValueError(f"graph {key}: snippet-only account {source} cannot be a source")
                    if self.accounts[source].url == target:
                        raise ValueError(f"graph {key}: account {source} links to itself")
                if connection.target_status == "other_profile" and (
                    connection.target_profile_id == key or connection.target_profile_id not in self.graphs
                ):
                    raise ValueError(f"graph {key}: {target} needs target_profile_id naming another existing profile")
                expected = {"in_profile": key, "other_profile": connection.target_profile_id, "not_collected": None}
                if profile_of_url.get(target) != expected[connection.target_status]:
                    raise ValueError(
                        f"graph {key}: {target} is labeled {connection.target_status} "
                        f"but belongs to {profile_of_url.get(target) or 'no profile'}"
                    )
        return self


# Section 3: verification and confidence

VerificationReason = Literal[
    "accepted", "below_threshold", "too_close", "jev_none", "jev_disagreement",
    "no_candidates", "evidence_unavailable", "insufficient_evidence", "review_required",
]
NO_CANDIDATE_REASONS = ("no_candidates", "evidence_unavailable")


class VerificationNotes(Contract):
    rationales: dict[str, str]  # existing profile IDs only; one sentence each


class CandidateSignals(Contract):
    image_support: Probability
    top_hit_share: Probability
    mean_link_trust: MeanTrust
    platform_count: Count
    consistency: Probability
    name_collision: bool
    newest_update: str | None = None
    # Best face score among the hits that reached this candidate, and where it sits on its backend's scale
    # (0 weak, 1 strong). Both None when no such hit has a face score (Yandex alone, the mock).
    face_score: int | None = Field(default=None, ge=0, le=100)
    face_match: Probability | None = None

    @model_validator(mode="after")
    def _face(self) -> CandidateSignals:
        if (self.face_score is None) != (self.face_match is None):
            raise ValueError("face_score and face_match are set together")
        return self


class CandidateVerdict(Contract):
    profile_id: str
    name: str | None
    confidence: Probability  # display with method, not as accuracy
    fallback_confidence: Probability
    method: Literal["jev", "fallback"]
    decision: Literal["accepted", "rejected"]
    signals: CandidateSignals
    flags: list[str] = Field(default_factory=list)  # e.g. "jev_fallback_disagree", "cross_profile_link"
    rationale: str | None = None  # code template or agent note; never used for scoring
    jev_model: str | None = None

    @model_validator(mode="after")
    def _fallback(self) -> CandidateVerdict:
        if self.method == "fallback" and self.confidence != self.fallback_confidence:
            raise ValueError(f"verdict {self.profile_id}: a fallback verdict's confidence is its fallback_confidence")
        return self


class VerificationResult(Contract):
    run_id: RunId
    verdicts: list[CandidateVerdict]  # highest confidence first
    accepted_profile_id: str | None
    reason: VerificationReason
    jev_calls: list[JevCall]

    @model_validator(mode="after")
    def _decision(self) -> VerificationResult:
        if duplicates := _duplicates(verdict.profile_id for verdict in self.verdicts):
            raise ValueError(f"duplicate verdict profile IDs: {', '.join(duplicates)}")
        if not _highest_first([verdict.confidence for verdict in self.verdicts]):
            raise ValueError("verdicts must be sorted by confidence, highest first")
        if (not self.verdicts) != (self.reason in NO_CANDIDATE_REASONS):
            raise ValueError(f"reason {self.reason!r} does not match {len(self.verdicts)} verdicts")
        accepted = [verdict.profile_id for verdict in self.verdicts if verdict.decision == "accepted"]
        if len(accepted) > 1:
            raise ValueError(f"at most one verdict can be accepted, got {', '.join(accepted)}")
        if self.reason == "accepted":
            if not accepted or self.accepted_profile_id != accepted[0]:
                raise ValueError("reason 'accepted' needs accepted_profile_id equal to the accepted verdict")
            if accepted[0] != self.verdicts[0].profile_id:
                raise ValueError("only the highest-confidence verdict can be accepted")
        elif accepted or self.accepted_profile_id is not None:
            raise ValueError(f"reason {self.reason!r} allows no accepted verdict or accepted_profile_id")
        return self


# Section 4: summarization


class Sourced(Contract):
    value: str = Field(min_length=1)
    sources: list[str] = Field(min_length=1)


class SummaryDraft(Contract):
    bio: Sourced | None = None


class FinalAccount(Contract):
    platform: str
    url: str
    display_name: str | None = None
    trust: Trust


class FinalProfile(Contract):
    status: Literal["match"] = "match"
    run_id: RunId
    name: Sourced
    role: Sourced | None = None
    workplace: Sourced | None = None
    location: Sourced | None = None  # city level only
    bio: Sourced | None = None
    accounts: list[FinalAccount] = Field(min_length=1)
    confidence: Probability
    method: Literal["jev", "fallback"]
    flags: list[str] = Field(default_factory=list)
    generated_at: AwareDatetime

    def cited_urls(self) -> list[str]:
        """Every source URL cited by the sourced fields, sorted."""
        fields = (self.name, self.role, self.workplace, self.location, self.bio)
        return sorted({url for field in fields if field for url in field.sources})

    @model_validator(mode="after")
    def _unique_accounts(self) -> FinalProfile:
        if duplicates := _duplicates(account.url for account in self.accounts):
            raise ValueError(f"final accounts repeat URLs: {', '.join(duplicates)}")
        return self


class CandidatePreview(Contract):
    profile_id: str
    name: str | None
    confidence: Probability
    method: Literal["jev", "fallback"]


class NoMatch(Contract):
    status: Literal["no_match"] = "no_match"
    run_id: RunId
    reason: VerificationReason
    top_candidates: list[CandidatePreview] = Field(max_length=3)
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def _no_match(self) -> NoMatch:
        if self.reason == "accepted":
            raise ValueError("NoMatch.reason cannot be 'accepted'")
        if (not self.top_candidates) != (self.reason in NO_CANDIDATE_REASONS):
            raise ValueError(f"reason {self.reason!r} does not match {len(self.top_candidates)} top_candidates")
        if duplicates := _duplicates(candidate.profile_id for candidate in self.top_candidates):
            raise ValueError(f"duplicate candidate profile IDs: {', '.join(duplicates)}")
        if not _highest_first([candidate.confidence for candidate in self.top_candidates]):
            raise ValueError("top_candidates must be sorted by confidence, highest first")
        return self


FinalOutput = Annotated[FinalProfile | NoMatch, Field(discriminator="status")]


# Cross-artifact references


def validate_references(
    *,
    capture: CaptureArtifact | None = None,
    raw: RawEvidenceBundle | None = None,
    profiles: ProfilesBundle | None = None,
    verification: VerificationResult | None = None,
    final: FinalProfile | NoMatch | None = None,
) -> None:
    """Check that one run's artifacts agree; only pairs of given artifacts are compared.

    Raises ValueError listing every disagreement.
    """
    problems: list[str] = []
    given = {"capture": capture, "raw": raw, "profiles": profiles, "verification": verification, "final": final}
    run_ids = {name: artifact.run_id for name, artifact in given.items() if artifact is not None}
    if len(set(run_ids.values())) > 1:
        problems.append("run_id differs: " + ", ".join(f"{name}={run_id}" for name, run_id in run_ids.items()))

    if capture and raw:
        captured = {image.image_id: image for image in capture.capture.images}
        if raw.num_images != len(captured):
            problems.append(f"raw evidence has num_images={raw.num_images} but the capture has {len(captured)} images")
        for evidence in raw.images:
            image = captured.get(evidence.image_id)
            if image is None:
                problems.append(f"raw image {evidence.image_id} is not in the capture")
            elif evidence.angle != image.angle:
                problems.append(f"raw image {evidence.image_id} has angle {evidence.angle}, capture says {image.angle}")
            if evidence.backend != capture.backend:
                problems.append(f"raw image {evidence.image_id} used backend {evidence.backend}, run uses {capture.backend}")

    if raw and profiles:
        raw_images = {image.image_id for image in raw.images}
        for account in profiles.accounts.values():
            key = raw.page_key(account.url)
            if key is None:
                problems.append(f"account {account.account_id} URL {account.url} is not a page in the raw evidence")
            elif raw.pages[key].page_kind != "profile" or not raw.pages[key].relevant:
                problems.append(f"account {account.account_id} page {key} is not a relevant profile page")
            elif (account.extraction == "snippet") != (raw.pages[key].extraction == "snippet"):
                problems.append(f"account {account.account_id} extraction {account.extraction} disagrees with its page")
            if stray := sorted(set(account.image_ids) - raw_images):
                problems.append(f"account {account.account_id} cites unknown images {', '.join(stray)}")
        for mention in profiles.mentions:
            key = raw.page_key(mention.url)
            if key is None:
                problems.append(f"mention {mention.url} is not a page in the raw evidence")
            elif raw.pages[key].page_kind != mention.kind:
                problems.append(f"mention {mention.url} has kind {mention.kind} but its page kind is {raw.pages[key].page_kind}")
            if stray := sorted(set(mention.image_ids) - raw_images):
                problems.append(f"mention {mention.url} cites unknown images {', '.join(stray)}")

    if profiles and verification:
        profile_ids = {profile.profile_id for profile in profiles.profiles}
        verdict_ids = {verdict.profile_id for verdict in verification.verdicts}
        if verdict_ids != profile_ids:
            problems.append(f"verdicts cover profiles {sorted(verdict_ids)} but the profiles are {sorted(profile_ids)}")
        accepted = verification.accepted_profile_id
        if accepted is not None and accepted not in profile_ids:
            problems.append(f"accepted_profile_id {accepted} is not a profile")

    if final is not None:
        problems += _final_problems(final, raw, profiles, verification)

    if problems:
        raise ValueError("artifacts disagree:\n- " + "\n- ".join(problems))


def _final_problems(
    final: FinalProfile | NoMatch,
    raw: RawEvidenceBundle | None,
    profiles: ProfilesBundle | None,
    verification: VerificationResult | None,
) -> list[str]:
    problems: list[str] = []
    if isinstance(final, NoMatch):
        if verification:
            if final.reason != verification.reason:
                problems.append(f"no-match reason {final.reason} differs from verification reason {verification.reason}")
            expected = [
                CandidatePreview(profile_id=v.profile_id, name=v.name, confidence=v.confidence, method=v.method)
                for v in verification.verdicts[:3]
            ]
            if final.top_candidates != expected:
                problems.append("no-match top_candidates must copy the first three verdicts")
        if profiles:
            profile_ids = {profile.profile_id for profile in profiles.profiles}
            if unknown := [c.profile_id for c in final.top_candidates if c.profile_id not in profile_ids]:
                problems.append(f"no-match candidates {', '.join(unknown)} are not profiles")
        return problems

    cited = final.cited_urls()
    if raw:
        problems += [f"final source {url} is not a page in the raw evidence" for url in cited if raw.page_key(url) is None]
    if verification is None:
        return problems
    accepted = next((verdict for verdict in verification.verdicts if verdict.decision == "accepted"), None)
    if accepted is None:
        return problems + [f"a final profile needs an accepted verdict, but verification reason is {verification.reason}"]
    if (final.confidence, final.method) != (accepted.confidence, accepted.method):
        problems.append(
            f"final confidence {final.confidence} ({final.method}) differs from the accepted verdict's "
            f"{accepted.confidence} ({accepted.method})"
        )
    profile = next((p for p in profiles.profiles if p.profile_id == accepted.profile_id), None) if profiles else None
    if profile is None:
        return problems  # a missing accepted profile is reported by the profiles/verification check

    members = set(profile.account_ids)
    account_urls = {profiles.accounts[account_id].url for account_id in profile.account_ids}
    problems += [
        f"final account {account.url} is not in accepted profile {profile.profile_id}"
        for account in final.accounts
        if account.url not in account_urls
    ]
    mention_urls = {mention.url for mention in profiles.mentions if members & set(mention.subject_account_ids)}

    def canonical(url: str) -> str:
        return (raw.page_key(url) if raw else None) or url

    allowed = {canonical(url) for url in account_urls | mention_urls}
    problems += [
        f"final source {url} is not an account or attributed mention of profile {profile.profile_id}"
        for url in cited
        if canonical(url) not in allowed
    ]
    return problems


# Stage number -> artifact file name and validator. artifacts.py reads and
# writes only through these tables.
STAGE_FILES: dict[int, str] = {
    0: "00_capture.json",
    1: "01_raw_evidence.json",
    2: "02_profiles.json",
    3: "03_verification.json",
    4: "04_final.json",
}

ARTIFACT_ADAPTERS: dict[int, TypeAdapter] = {
    0: TypeAdapter(CaptureArtifact),
    1: TypeAdapter(RawEvidenceBundle),
    2: TypeAdapter(ProfilesBundle),
    3: TypeAdapter(VerificationResult),
    4: TypeAdapter(FinalOutput),
}

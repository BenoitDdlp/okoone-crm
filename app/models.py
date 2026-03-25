from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


# --- Prospects ---

class ProspectBase(BaseModel):
    linkedin_username: str
    linkedin_url: Optional[str] = None
    full_name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    current_company: Optional[str] = None
    current_title: Optional[str] = None
    experience_json: Optional[str] = None
    education_json: Optional[str] = None
    skills_json: Optional[str] = None
    about_text: Optional[str] = None
    profile_photo_url: Optional[str] = None
    contact_email: Optional[str] = None


class ProspectCreate(ProspectBase):
    source_search_id: Optional[int] = None


class ProspectUpdate(BaseModel):
    linkedin_url: Optional[str] = None
    full_name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    current_company: Optional[str] = None
    current_title: Optional[str] = None
    experience_json: Optional[str] = None
    education_json: Optional[str] = None
    skills_json: Optional[str] = None
    about_text: Optional[str] = None
    profile_photo_url: Optional[str] = None
    contact_email: Optional[str] = None
    relevance_score: Optional[float] = None
    score_breakdown: Optional[str] = None
    traits_json: Optional[str] = None
    flags_json: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class ProspectResponse(ProspectBase):
    id: int
    relevance_score: float
    score_breakdown: Optional[str] = None
    traits_json: str = "[]"
    flags_json: str = "[]"
    status: str = "discovered"
    source_search_id: Optional[int] = None
    scraped_at: Optional[datetime] = None
    screened_at: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# --- Search Queries ---

class SearchQueryCreate(BaseModel):
    keywords: str
    location: Optional[str] = None
    filters_json: Optional[str] = None
    is_recurring: bool = False
    recurrence_cron: Optional[str] = None


class SearchQueryResponse(BaseModel):
    id: int
    keywords: str
    location: Optional[str] = None
    filters_json: Optional[str] = None
    is_recurring: bool
    recurrence_cron: Optional[str] = None
    last_run_at: Optional[datetime] = None
    total_results: int
    is_active: bool
    created_at: datetime


# --- Scoring Weights ---

class ScoringWeightsCreate(BaseModel):
    name: str
    criteria_json: str
    is_active: bool = False


class ScoringWeightsResponse(BaseModel):
    id: int
    name: str
    criteria_json: str
    is_active: bool
    created_at: datetime


# --- Human Reviews ---

class HumanReviewCreate(BaseModel):
    verdict: Literal["approve", "reject", "flag"]
    relevance_override: Optional[float] = None
    feedback_text: Optional[str] = None


# --- Eval Snapshots ---

class EvalSnapshotResponse(BaseModel):
    id: int
    run_id: Optional[str] = None
    precision_score: Optional[float] = None
    recall_score: Optional[float] = None
    f1_score: Optional[float] = None
    top_k_accuracy: Optional[float] = None
    human_agreement_rate: Optional[float] = None
    notes: Optional[str] = None
    created_at: datetime


# --- Email Campaigns ---

class CampaignCreate(BaseModel):
    name: str
    scoring_weight_id: Optional[int] = None
    min_relevance_score: float = 0.0


class CampaignResponse(BaseModel):
    id: int
    name: str
    status: str
    scoring_weight_id: Optional[int] = None
    min_relevance_score: float
    created_at: datetime
    updated_at: datetime
    steps: List["EmailStepResponse"] = []


class EmailStepCreate(BaseModel):
    step_order: int
    subject_template: str
    body_html_template: str
    body_text_template: str
    delay_days: int


class EmailStepResponse(BaseModel):
    id: int
    campaign_id: int
    step_order: int
    subject_template: str
    body_html_template: str
    body_text_template: str
    delay_days: int
    created_at: datetime

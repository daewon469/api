from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from services.site_analysis import request_gemini_site_analysis

router = APIRouter()


class SiteAnalysisIn(BaseModel):
    title: Optional[str] = None
    site_name: Optional[str] = None
    highlight_content: Optional[str] = None
    job_industry: Optional[str] = None
    province: Optional[str] = None
    city: Optional[str] = None
    workplace_address: Optional[str] = None
    business_address: Optional[str] = None
    content: Optional[str] = None
    company_agency: Optional[str] = None


class SiteAnalysisOut(BaseModel):
    ok: bool = True
    analysis: Optional[str] = None
    error: Optional[str] = None


@router.post("/community/ai/site-analysis", response_model=SiteAnalysisOut)
def site_analysis(body: SiteAnalysisIn):
    analysis, error = request_gemini_site_analysis(body.model_dump())
    if error:
        return SiteAnalysisOut(ok=False, error=error)
    return SiteAnalysisOut(ok=True, analysis=analysis)

from __future__ import annotations

from typing import Any, Optional

import requests

import settings

CHAPTER_ORDER = [
    "건축개요",
    "입지/교통",
    "생활/학군",
    "미래가치",
    "투자가치",
]


def _thinking_config(model: str) -> dict[str, Any]:
    """
    gemini-3.6-flash 기본 thinkingLevel은 medium이라 짧은 브리핑에도 지연이 크다.
    3.6은 minimal이 가장 짧고, 3.8/4는 low, 2.5는 thinkingBudget 0.
    """
    name = (model or "").lower()
    if "2.5" in name:
        return {"thinkingBudget": 0}
    if "3.8" in name or "gemini-4" in name:
        return {"thinkingLevel": "low"}
    return {"thinkingLevel": "minimal"}


def build_site_analysis_prompt(body: dict[str, Any]) -> str:
    site_name = str(body.get("site_name") or "").strip()
    title = str(body.get("title") or "").strip()
    site_label = site_name or title or "해당 현장"
    region = " ".join(
        [p for p in [body.get("province"), body.get("city")] if p]
    ).strip() or "-"

    chapters = list(CHAPTER_ORDER)
    chapter_lines: list[str] = []
    for i, name in enumerate(chapters, start=1):
        chapter_lines.append(f"**{i}) {name}**")
        chapter_lines.append("")

    # 본문은 과도하게 길면 잘라 입력 토큰 절약
    content = str(body.get("content") or "-")
    if len(content) > 900:
        content = content[:900] + "…"

    last = len(chapters)
    return "\n".join(
        [
            "[역할] 분양 상담용 현장 브리핑을 짧고 정확하게 정리하는 AI.",
            "",
            "[출력 규칙]",
            f"1. 제목과 1)~{last}) 항목만 출력. 설계특징, 꺾기멘트, 실전 브리핑은 쓰지 말 것.",
            "2. 항목당 2~3줄. 구어체(해요체). 건축개요=단지·브랜드, 입지/교통=접근성, 생활/학군=상권·학교, 미래가치=호재, 투자가치=수요·환금성.",
            "3. 서론/면책/수수료·급여 이야기 금지. 모르는 수치는 지어내지 말 것.",
            "4. 아래 항목 이름과 순서를 그대로 유지.",
            "",
            "[출력 포맷]",
            f"**[현장명: {site_label}]**",
            "",
            *chapter_lines,
            "[현장 입력값]",
            f"[제목] {body.get('title') or '-'}",
            f"[현장명] {body.get('site_name') or '-'}",
            f"[현장한마디] {body.get('highlight_content') or '-'}",
            f"[업종] {body.get('job_industry') or '-'}",
            f"[지역] {region}",
            f"[모델하우스] {body.get('workplace_address') or '-'}",
            f"[사업지] {body.get('business_address') or '-'}",
            f"[대행사] {body.get('company_agency') or '-'}",
            f"[본문] {content}",
            "",
            "위 포맷으로 해당 현장 리포트만 출력.",
        ]
    )


def _error_message(data: dict[str, Any], fallback: str) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or fallback)
    if isinstance(err, str) and err.strip():
        return err
    return fallback


def _extract_text(data: dict[str, Any]) -> str:
    parts = (
        (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
    )
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("thought"):
            continue
        text = str(part.get("text") or "").strip()
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _post_generate(url: str, prompt: str, generation_config: dict[str, Any]) -> requests.Response:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    return requests.post(url, json=payload, timeout=60)


def request_gemini_site_analysis(body: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (analysis_text, error_message).
    """
    api_key = settings.get_gemini_api_key()
    if not api_key:
        return None, (
            "GEMINI API 키가 설정되지 않았습니다. "
            "API 서버(Railway) Variables에 GEMINI_API_KEY를 넣고 재배포하세요."
        )

    model = settings.get_gemini_model()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    prompt = build_site_analysis_prompt(body)
    generation_config: dict[str, Any] = {
        "temperature": 0.2,
        "maxOutputTokens": 2048,
        "thinkingConfig": _thinking_config(model),
    }

    try:
        res = _post_generate(url, prompt, generation_config)
        data = res.json() if res.content else {}
        if (
            res.status_code == 400
            and isinstance(data, dict)
            and "thinking" in _error_message(data, "").lower()
        ):
            generation_config = {
                "temperature": 0.2,
                "maxOutputTokens": 2048,
            }
            res = _post_generate(url, prompt, generation_config)
            data = res.json() if res.content else {}

        if res.status_code >= 400:
            return None, _error_message(data, "AI 분석 요청에 실패했습니다.")

        text = _extract_text(data if isinstance(data, dict) else {})
        if not text:
            return None, "분석 결과를 받지 못했습니다."
        return text, None
    except Exception:
        return None, "AI 분석 중 오류가 발생했습니다."

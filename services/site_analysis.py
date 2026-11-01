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

# 오너 메뉴 속도 비교용. 없으면 아래 기본 프롬프트(5개·2~3줄)를 쓴다.
VARIANT_PROFILES: dict[str, dict[str, Any]] = {
    "1-1": {
        "chapters": list(CHAPTER_ORDER),
        "line_rule": "항목당 4~5줄",
    },
    "1-2": {
        "chapters": ["건축개요", "학군/교통", "투자가치"],
        "line_rule": "항목당 7~8줄. 학군/교통은 학교·학군과 교통·접근성을 한 항목에 쓴다",
    },
    "1-3": {
        "chapters": list(CHAPTER_ORDER),
        "line_rule": "항목당 7~8줄",
    },
}


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


def _site_context(body: dict[str, Any]) -> tuple[str, str]:
    site_name = str(body.get("site_name") or "").strip()
    title = str(body.get("title") or "").strip()
    site_label = site_name or title or "해당 현장"
    region = " ".join(
        [p for p in [body.get("province"), body.get("city")] if p]
    ).strip() or "-"
    content = str(body.get("content") or "-")
    if len(content) > 900:
        content = content[:900] + "…"
    fields = "\n".join(
        [
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
        ]
    )
    return site_label, fields


def _chapter_block(chapters: list[str]) -> list[str]:
    lines: list[str] = []
    for i, name in enumerate(chapters, start=1):
        lines.append(f"**{i}) {name}**")
        lines.append("")
    return lines


def build_site_analysis_prompt(body: dict[str, Any]) -> str:
    site_label, fields = _site_context(body)
    variant = str(body.get("variant") or "").strip()
    profile = VARIANT_PROFILES.get(variant)
    if profile:
        chapters = list(profile["chapters"])
        line_rule = str(profile["line_rule"])
        joined = ", ".join(chapters)
        rules = [
            f"1. 제목과 다음 {len(chapters)}개 항목만 출력: {joined}. 다른 항목은 쓰지 말 것.",
            f"2. {line_rule}. 그보다 짧거나 길지 않게. 구어체(해요체).",
            "3. 서론/면책/수수료·급여 이야기 금지. 모르는 수치는 지어내지 말 것.",
            "4. 아래 항목 이름과 순서를 그대로 유지.",
        ]
    else:
        chapters = list(CHAPTER_ORDER)
        rules = [
            f"1. 제목과 1)~{len(chapters)}) 항목만 출력. 설계특징, 꺾기멘트, 실전 브리핑은 쓰지 말 것.",
            "2. 항목당 2~3줄. 구어체(해요체). 건축개요=단지·브랜드, 입지/교통=접근성, 생활/학군=상권·학교, 미래가치=호재, 투자가치=수요·환금성.",
            "3. 서론/면책/수수료·급여 이야기 금지. 모르는 수치는 지어내지 말 것.",
            "4. 아래 항목 이름과 순서를 그대로 유지.",
        ]

    return "\n".join(
        [
            "[역할] 분양 상담용 현장 브리핑을 짧고 정확하게 정리하는 AI.",
            "",
            "[출력 규칙]",
            *rules,
            "",
            "[출력 포맷]",
            f"**[현장명: {site_label}]**",
            "",
            *_chapter_block(chapters),
            fields,
            "",
            "위 포맷으로 해당 현장 리포트만 출력.",
        ]
    )


def max_output_tokens_for(body: dict[str, Any]) -> int:
    variant = str(body.get("variant") or "").strip()
    if variant in VARIANT_PROFILES:
        return 3072
    return 2048


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


def _post_generate(
    url: str,
    prompt: str,
    generation_config: dict[str, Any],
    timeout: int = 60,
) -> requests.Response:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    return requests.post(url, json=payload, timeout=timeout)


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
    max_tokens = max_output_tokens_for(body)
    request_timeout = 90 if max_tokens > 2048 else 60
    generation_config: dict[str, Any] = {
        "temperature": 0.2,
        "maxOutputTokens": max_tokens,
        "thinkingConfig": _thinking_config(model),
    }

    try:
        res = _post_generate(url, prompt, generation_config, request_timeout)
        data = res.json() if res.content else {}
        if (
            res.status_code == 400
            and isinstance(data, dict)
            and "thinking" in _error_message(data, "").lower()
        ):
            generation_config = {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
            }
            res = _post_generate(url, prompt, generation_config, request_timeout)
            data = res.json() if res.content else {}

        if res.status_code >= 400:
            return None, _error_message(data, "AI 분석 요청에 실패했습니다.")

        text = _extract_text(data if isinstance(data, dict) else {})
        if not text:
            return None, "분석 결과를 받지 못했습니다."
        return text, None
    except Exception:
        return None, "AI 분석 중 오류가 발생했습니다."

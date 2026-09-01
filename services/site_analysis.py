from __future__ import annotations

from typing import Any, Optional

import requests

import settings

CHAPTER_ORDER = [
    "건축개요",
    "설계특징",
    "입지/교통",
    "생활/학군",
    "미래가치",
    "투자가치",
    "꺾기멘트",
]

# 짧은 few-shot: 포맷만 안내 (장문 예시 제거로 지연 감소)
FEW_SHOT_EXAMPLE = """
[모범 출력 예시 — 포맷·톤만 참고]

**[현장명: OO역 XX단지]**

**1) 건축개요**
- 핵심 포인트 1
- 핵심 포인트 2
- 핵심 포인트 3

**2) 설계특징**
- 핵심 포인트 1
- 핵심 포인트 2
- 핵심 포인트 3

**7) 꺾기멘트**
- "고객에게 바로 건넬 추천 멘트 한두 문장."
""".strip()


def build_site_analysis_prompt(body: dict[str, Any]) -> str:
    site_name = str(body.get("site_name") or "").strip()
    title = str(body.get("title") or "").strip()
    site_label = site_name or title or "해당 현장"
    region = " ".join(
        [p for p in [body.get("province"), body.get("city")] if p]
    ).strip() or "-"

    raw_order = body.get("chapter_order")
    chapters = CHAPTER_ORDER
    if isinstance(raw_order, list) and len(raw_order) == 7:
        cleaned = [str(x).strip() for x in raw_order if str(x).strip()]
        if len(cleaned) == 7:
            chapters = cleaned

    chapter_lines: list[str] = []
    hints = {
        "건축개요": "(단지·브랜드·세대 핵심 2~3줄)",
        "설계특징": "(평면·조망·커뮤니티 핵심 2~3줄)",
        "입지/교통": "(도로·역세권·접근성 핵심 2~3줄)",
        "생활/학군": "(상권·학교·편의 핵심 2~3줄)",
        "미래가치": "(호재·성장 핵심 2~3줄)",
        "투자가치": "(수요·환금성 핵심 2~3줄)",
        "꺾기멘트": "(추천 멘트 1~2문장)",
        "실전 브리핑": "(추천 멘트 1~2문장)",
    }
    for i, name in enumerate(chapters, start=1):
        hint = hints.get(name, "(핵심 2~3줄)")
        chapter_lines.append(f"**{i}) {name}**")
        chapter_lines.append(f"- {hint}")
        chapter_lines.append("")

    # 본문은 과도하게 길면 잘라 입력 토큰 절약
    content = str(body.get("content") or "-")
    if len(content) > 1200:
        content = content[:1200] + "…"

    return "\n".join(
        [
            "[역할] 분양 상담용 현장 브리핑을 짧고 정확하게 정리하는 AI.",
            "",
            "[출력 규칙]",
            "1. 제목 + 1)~7) 전부 출력.",
            "2. 1)~6)은 항목당 2~3줄, 7)은 1~2문장. 구어체(해요체).",
            "3. 서론/면책/수수료·급여 이야기 금지. 모르는 수치는 지어내지 말 것.",
            "4. 아래 포맷·항목 이름·순서를 그대로 유지.",
            "",
            "[출력 포맷]",
            f"**[현장명: {site_label}]**",
            "",
            *chapter_lines,
            "항목 순서: "
            + " → ".join([f"{i}) {n}" for i, n in enumerate(chapters, start=1)]),
            "",
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
            FEW_SHOT_EXAMPLE,
            "",
            "위 포맷으로 해당 현장 리포트만 출력. 예시를 복사하지 말 것.",
        ]
    )


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
    payload = {
        "contents": [{"role": "user", "parts": [{"text": build_site_analysis_prompt(body)}]}],
        # 출력 축소로 생성 시간 단축
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 3072},
    }

    try:
        res = requests.post(url, json=payload, timeout=60)
        data = res.json() if res.content else {}
        if res.status_code >= 400:
            err = (data.get("error") or {}).get("message") or "AI 분석 요청에 실패했습니다."
            return None, err

        parts = (
            (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
        )
        text = "\n".join([str(p.get("text") or "") for p in parts]).strip()
        if not text:
            return None, "분석 결과를 받지 못했습니다."
        return text, None
    except Exception:
        return None, "AI 분석 중 오류가 발생했습니다."

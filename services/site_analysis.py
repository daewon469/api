from __future__ import annotations

from typing import Any, Optional

import requests

import settings

FEW_SHOT_EXAMPLE = """
[모범 출력 예시 — 포맷·톤·분량을 이 예시와 동일하게 유지할 것]

**[현장명: 분당 서현역 자이]**

**1) 투자가치**
- 서현역 생활권에 GTX 수혜가 기대되는 브랜드 대단지로, 직주근접·환금성 면에서 실수요와 투자 수요를 함께 노리기 좋은 현장이에요.
- 역세권 프리미엄과 브랜드 단지 수요가 겹치면서, 중장기 보유 시에도 가격 방어력이 기대되는 편이에요.
- 광역교통 호재가 붙으면 입지 프리미엄이 더 돋보일 수 있어, 투자 관점에서도 설득 포인트가 분명해요.
- 세대수와 브랜드 파워, 생활 인프라가 함께 갖춰져 있어 실거주와 투자를 동시에 설명하기 좋아요.
- 단기 매매보다는 중장기 관점에서 가치를 읽기 쉬운 현장이라, 고객에게 투자가치를 설명하기 수월해요.

**2) 입지 및 교통**
- 서현역·백현동 생활권이라 분당선 출퇴근이 편하고, 주변 대로·IC 접근도 무난한 편이에요.
- 광역교통 호재가 붙으면 입지 프리미엄이 더 돋보일 수 있어요.
- 주요 도로망과 생활권이 연결되어 있어 실거주 고객에게 접근성 설명이 쉬워요.
- 역과 생활권의 거리감이 적당해, 직주근접 수요를 끌어올리기 좋은 입지예요.
- 교통 편의가 높은 만큼, 향후 거래·임대 수요도 함께 기대할 수 있어요.

**3) 생활 및 학군 인프라**
- 서현 상권·대형마트·관공서 접근이 좋아 생활 편의성이 높고, 인근 학교·학원가도 형성되어 실거주 설득이 쉬운 편이에요.
- 일상 쇼핑·의료·행정 업무를 생활권 안에서 해결하기 쉬워 실거주 만족도가 높아요.
- 학군·학원 인프라가 갖춰져 있어 자녀 있는 가구의 체류 의지를 높이기 좋아요.
- 생활 편의와 교육 환경이 함께 갖춰져, 실수요 고객에게 설명 포인트가 많아요.
- 주변 상권이 안정적이라 거주 만족도와 환금성을 함께 설명하기 좋아요.

**4) 미래가치 및 투자 포인트**
- 역세권·브랜드 단지 수요가 꾸준해 환금성·임대 안정성이 기대되고, 광역교통·생활권 개발이 이어지면 중장기 가치도 볼 만해요.
- 개발 호재와 직주근접 수요가 겹치면서, 향후 가격 상승 여지를 설명하기 좋아요.
- 임대 수요가 안정적인 생활권이라 보유 기간 중에도 현금흐름 관점 설명이 가능해요.
- 브랜드와 입지가 함께 받쳐주는 구조라, 시장 변동성이 있어도 방어력을 기대할 수 있어요.
- 중장기 관점에서 가치를 읽기 쉬운 현장이라 투자 설명 멘트를 만들기 수월해요.

**5) 실전 브리핑 멘트**
- "서현 생활권에서 출퇴근·생활 인프라를 다 챙기면서, 앞으로 교통 호재까지 기대할 수 있는 단지예요. 실거주와 투자 포인트를 함께 보시는 분들께 먼저 추천드려요."
""".strip()


def build_site_analysis_prompt(body: dict[str, Any]) -> str:
    site_name = str(body.get("site_name") or "").strip()
    title = str(body.get("title") or "").strip()
    site_label = site_name or title or "해당 현장"
    region = " ".join(
        [p for p in [body.get("province"), body.get("city")] if p]
    ).strip() or "-"

    return "\n".join(
        [
            "[역할 정의]",
            "너는 베테랑 분양 전문 AI 어시스턴트야. 분양상담사가 고객에게 즉시 설명(브리핑)할 수 있도록, 핵심만 정확하고 간결하게 정리된 [현장 투자 분석 리포트]를 작성해줘.",
            "",
            "[출력 규칙]",
            "1. 모든 항목은 누락 없이 출력할 것. (제목 + 1)~5) 전부)",
            "2. 각 항목의 내용은 상담사가 고객에게 바로 읽어줄 수 있도록 문어체가 아닌 자연스러운 구어체(해요체) 또는 명확한 개조식으로 항목당 5줄 이상 작성할 것.",
            "3. 답변이 중간에 잘리지 않도록 핵심을 충분히 풀어서 작성할 것. 불필요한 서론/결론/면책 문구는 넣지 말 것.",
            "4. 구인 모집, 수수료, 급여 이야기는 절대 쓰지 말 것.",
            "5. 모르는 수치는 지어내지 말고, 추정일 때만 '(추정)'을 붙일 것.",
            "6. 아래 [출력 포맷]의 마크다운 제목/번호 구조를 그대로 지킬 것. 항목 이름도 변경하지 말 것.",
            "",
            "[출력 포맷]",
            f"**[현장명: {site_label}]**",
            "",
            "**1) 투자가치**",
            "- (핵심 호재, 리스크 해소 여부, 세대수, 브랜드, 환금성 등 투자가치 중심으로 5줄 이상)",
            "",
            "**2) 입지 및 교통**",
            "- (주요 공원, 숲세권, 도로망, IC, 지하철/KTX 접근성 등 핵심 내용 5줄 이상)",
            "",
            "**3) 생활 및 학군 인프라**",
            "- (행정타운, 대형마트, 관공서, 인근 학교 및 학원가 등 핵심 내용 5줄 이상)",
            "",
            "**4) 미래가치 및 투자 포인트**",
            "- (개발 호재, 직주근접 수요, 환금성 및 임대 안정성 등 5줄 이상)",
            "",
            "**5) 실전 브리핑 멘트**",
            "- (상담사가 고객에게 건네는 추천 멘트 1~2문장)",
            "",
            "중요: 제목에 '고객 브리핑 및 투자 분석' 문구를 붙이지 말 것. 5번 항목 제목은 반드시 '5) 실전 브리핑 멘트'로 할 것.",
            "",
            "[현장 입력값]",
            f"[제목] {body.get('title') or '-'}",
            f"[현장명] {body.get('site_name') or '-'}",
            f"[현장한마디] {body.get('highlight_content') or '-'}",
            f"[업종] {body.get('job_industry') or '-'}",
            f"[지역] {region}",
            f"[모델하우스 주소] {body.get('workplace_address') or '-'}",
            f"[현장사업지 주소] {body.get('business_address') or '-'}",
            f"[대행사] {body.get('company_agency') or '-'}",
            f"[본문] {body.get('content') or '-'}",
            "",
            FEW_SHOT_EXAMPLE,
            "",
            "위 모범 예시와 동일한 포맷·톤으로, 지금 [현장 입력값]에 대한 리포트만 출력해줘. 예시 문구를 그대로 복사하지 말고 해당 현장에 맞게 작성할 것.",
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
        "generationConfig": {"temperature": 0.35, "maxOutputTokens": 4096},
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

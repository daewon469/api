import os

# .env 로드(로컬/개발 편의). 운영 환경에서는 플랫폼의 환경변수 주입을 권장.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass

# JWT / Auth
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# RSS
SECRET_RSS_TOKEN = os.getenv("SECRET_RSS_TOKEN", "rss-secret-token")

# Kakao (recode: geocode + route distance)
SMART_KAKAO_KEY = os.getenv("SMART_KAKAO_KEY", "").strip()
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip() or SMART_KAKAO_KEY
KAKAO_MOBILITY_API_KEY = os.getenv("KAKAO_MOBILITY_API_KEY", "").strip() or KAKAO_REST_API_KEY

# -------------------- One-shot migration (uploads) --------------------
# B 서버: 마이그레이션 API 보호용(선택)
MIGRATION_TOKEN = os.getenv("MIGRATION_TOKEN", "").strip()

# A 서버 정보 (smartgauge)
SMARTGAUGE_SOURCE_BASE_URL = os.getenv("SMARTGAUGE_SOURCE_BASE_URL", "https://api.smartgauge.co.kr").strip().rstrip("/")
SMARTGAUGE_SOURCE_FILES_ENDPOINT = os.getenv(
    "SMARTGAUGE_SOURCE_FILES_ENDPOINT", "/admin/migration/uploads/files"
).strip()
SMARTGAUGE_SOURCE_UPLOADS_BASE_URL = os.getenv(
    "SMARTGAUGE_SOURCE_UPLOADS_BASE_URL", "https://api.smartgauge.co.kr/uploads"
).strip().rstrip("/")

# A 서버 파일목록 API가 토큰을 요구할 경우 사용(선택)
SMARTGAUGE_SOURCE_MIGRATION_TOKEN = os.getenv("SMARTGAUGE_SOURCE_MIGRATION_TOKEN", "").strip()

# 다운로드 timeout (초)
# 요청하신 “1년” 기본값: 365 * 24 * 60 * 60 = 31,536,000
SMARTGAUGE_HTTP_TIMEOUT_SEC = float(
    os.getenv("SMARTGAUGE_HTTP_TIMEOUT_SEC", "31536000").strip() or "31536000"
)

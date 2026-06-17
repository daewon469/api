FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# libpq + SQLAlchemy/psycopg2 빌드에 필요한 도구 (slim 이미지)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 gcc python3-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 설치
COPY requirements.txt /app/
RUN python -m pip install --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt

# 애플리케이션 복사
COPY . /app

EXPOSE 8080

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}

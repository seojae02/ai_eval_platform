FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# INSTALL_DEV=1일 때만 테스트 도구(pytest 등)를 설치한다. 운영 이미지에는 넣지 않는다.
ARG INSTALL_DEV=0
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV" = "1" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

RUN useradd --create-home --uid 1000 app
# 운영(docker-compose.prod.yml)의 업로드 저장 볼륨 자리. 새 볼륨이 이 소유자(app)를 물려받는다
RUN mkdir -p /data/storage && chown app:app /data/storage && chmod 700 /data/storage
COPY --chown=app:app . .
# 운영의 collectstatic 결과 자리(/app 자체는 root 소유라 app 사용자가 만들 수 없다)
RUN mkdir -p /app/staticfiles && chown app:app /app/staticfiles
USER app

EXPOSE 8000

# 샌드박스 실행 이미지: 참여자 노트북을 다시 실행하고(V1) 모델 파일을 로드해 본다(V5).
# 웹·worker 이미지와 따로 둔다 → 참여자 코드가 도는 곳에는 Django·DB 드라이버·비밀값이 없다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    # 스레드 수를 CPU 제한(1개)에 맞춘다
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# 참여자 코드는 root가 아닌 사용자로 돈다
RUN useradd --create-home --uid 1000 runner
COPY runner.py /opt/runner/runner.py
# /work = 입력 자리. root 소유 755 → runner(uid 1000)는 읽기만. 컨테이너마다 새 익명 볼륨이 이 권한을 물려받는다
RUN mkdir -p /work && chmod 755 /work
USER runner
WORKDIR /tmp
CMD ["python", "/opt/runner/runner.py"]

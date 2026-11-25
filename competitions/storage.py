"""업로드 파일 저장소 (R9: 정답은 웹에서 닿지 않는 곳에).

storage/
 ├─ private/       정답 파일. 채점 시스템과 담당 관리자만 (권한 뷰를 통해서만)
 ├─ shared/        공개 학습 데이터·테스트 feature. 대회 소속만 (권한 뷰를 통해서만)
 └─ submissions/   참여자 제출 파일(CSV·ipynb·모델). 모델은 원래 확장자 대신 .bin으로 저장

세 폴더 모두 어떤 URL에도 연결되어 있지 않다. 파일을 여는 길은 views의 다운로드 함수뿐이다.
"""
import hashlib
import re
import uuid
from pathlib import Path

from django.conf import settings

AREAS = ("private", "shared", "submissions")
# 저장 이름은 우리가 만든 UUID 형식만 인정한다 → "../../etc/passwd" 같은 경로 조작 차단
_STORED_NAME = re.compile(r"^[0-9a-f]{32}\.(csv|ipynb|bin)$")


def _root(area):
    # settings를 호출 시점에 읽는다(테스트가 임시 폴더로 바꿀 수 있도록)
    return Path(settings.STORAGE_ROOT) / area


def path_for(area, stored_name):
    if area not in AREAS or not _STORED_NAME.fullmatch(stored_name or ""):
        raise ValueError("잘못된 저장 위치 또는 이름입니다.")
    return _root(area) / stored_name


def save_bytes(area, data, ext="csv"):
    """새 UUID 이름으로 저장하고 그 이름을 돌려준다. 원래 파일명은 쓰지 않는다."""
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    path = path_for(area, stored_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:  # x: 같은 이름이 이미 있으면 실패(덮어쓰기 방지)
        f.write(data)
    path.chmod(0o600)            # 소유자(app 사용자)만 읽기·쓰기
    return stored_name


def write_at(area, stored_name, offset, data):
    """조각 업로드: offset 위치에 쓰고 그 뒤는 잘라 낸다.

    덧붙이기(append)가 아니라 '이 위치에 쓰기'다. 같은 조각을 다시 보내도 파일에 두 번 붙지 않는다.
    """
    with open(path_for(area, stored_name), "r+b") as f:
        f.seek(offset)
        f.write(data)
        f.truncate()


def read_bytes(area, stored_name):
    return path_for(area, stored_name).read_bytes()


def read_head(area, stored_name, n):
    with open(path_for(area, stored_name), "rb") as f:
        return f.read(n)


def sha256_file(area, stored_name):
    """큰 파일(모델)도 메모리에 다 올리지 않고 1MB씩 읽어 해시를 계산한다."""
    h = hashlib.sha256()
    with open(path_for(area, stored_name), "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def delete(area, stored_name):
    path_for(area, stored_name).unlink(missing_ok=True)


def sha256(data):
    return hashlib.sha256(data).hexdigest()

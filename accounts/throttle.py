"""대입 공격 제한 (보안 C: Django가 해 주지 않아 직접 구현).

최근 10분 안의 실패 횟수가 한도 이상이면 막는다.
- 로그인: 로그인ID 기준 5회   → 가상학생01 계정만 잠기고 같은 강의실의 다른 학생은 영향 없음
- 참여 코드: IP 또는 사용자 기준 20회 → 같은 공인 IP를 쓰는 강의실을 고려해 넉넉하게
"""
from datetime import timedelta

from django.utils import timezone

from .models import FailedAttempt

WINDOW = timedelta(minutes=10)
LIMITS = {
    FailedAttempt.Kind.LOGIN: 5,
    FailedAttempt.Kind.JOIN_CODE: 20,
}
LOCKED_MESSAGE = "시도 횟수가 너무 많으니 10분 후 다시 시도해 주세요"


def client_ip(request):
    # X-Forwarded-For는 브라우저가 마음대로 써 보낼 수 있으므로 믿지 않는다.
    # 실제 연결 주소(REMOTE_ADDR)만 쓴다.
    return request.META.get("REMOTE_ADDR", "")


def is_locked(kind, key):
    since = timezone.now() - WINDOW
    recent = FailedAttempt.objects.filter(kind=kind, key=key, created_at__gte=since).count()
    return recent >= LIMITS[kind]


def record_failure(kind, key):
    FailedAttempt.objects.create(kind=kind, key=key[:150])


def clear_failures(kind, key):
    FailedAttempt.objects.filter(kind=kind, key=key).delete()

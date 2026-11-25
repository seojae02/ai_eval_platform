"""작업 큐 동작: 등록(enqueue) → 가져가기(claim) → 실행(run_one) → 완료/재시도/실패.

모든 함수는 now(현재 시각)를 인자로 받는다 → 테스트가 '10분 뒤' 같은 상황을 바로 만든다.
"""
import logging
import traceback
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from submissions import scoring
from verification import services as verification

from .models import Job

log = logging.getLogger(__name__)
Status = Job.Status


# 작업 종류 → (실행 함수, 마지막 실패 때 부를 함수)
HANDLERS = {
    Job.Kind.SCORE: (scoring.score_submission, scoring.mark_system_error),   # 실패 = 제출 '시스템 오류'(R4 미차감)
    Job.Kind.VERIFY: (verification.verify_submission, verification.mark_failed),   # 실패 = 리포트 '검증 실패'만
}
# 성공하면 이어서 등록할 작업: 채점이 끝나면 검증
FOLLOW_UP = {Job.Kind.SCORE: Job.Kind.VERIFY}
# 채점을 먼저: 검증(샌드박스, 수십 초~수 분)이 밀려 있어도 새 제출의 점수는 바로 나온다
# 트랜잭션 밖에서 실행하는 작업 종류.
#   채점: 점수 저장과 '작업 완료'가 반드시 함께여야 하므로 한 트랜잭션(아래 run).
#   검증: 몇 분 걸리고 진행 단계(V2~V4 → V5 → V1)를 실행 중에 화면에 보여 줘야 한다. 트랜잭션 안이면 끝날 때까지
#         아무것도 보이지 않는다. 중간에 끊겨도 다시 실행하면 리포트를 처음부터 새로 쓰므로(update_or_create) 안전하다.
NON_ATOMIC = {Job.Kind.VERIFY}
PRIORITY = Case(When(kind=Job.Kind.SCORE, then=Value(0)), default=Value(1), output_field=IntegerField())


def enqueue(submission, kind=Job.Kind.SCORE, now=None):
    """작업 등록. 제출 완료(complete)와 같은 트랜잭션 안에서 부른다.

    → '채점 대기'로 바뀌었는데 작업이 없는 상태(또는 그 반대)가 생기지 않는다.
    """
    job, _ = Job.objects.get_or_create(
        submission=submission, kind=kind, defaults={"run_after": now or timezone.now()},
    )
    return job


def claim(now=None):
    """대기 중인 작업 하나를 '실행 중'으로 바꾸고 가져온다. 없으면 None.

    핵심: SELECT ... FOR UPDATE SKIP LOCKED
      - FOR UPDATE: 고른 줄을 잠가 다른 worker가 같은 작업을 가져가지 못하게 한다.
      - SKIP LOCKED: 이미 누가 잠근 줄은 기다리지 않고 건너뛴다 → worker를 2개 이상 늘려도 서로 막지 않는다.
    가져간 즉시 커밋한다. 그래서 실행 도중 worker가 죽어도 '실행 중'이 DB에 남아 recover_stale이 되돌린다.
    """
    now = now or timezone.now()
    with transaction.atomic():
        job = (Job.objects.select_for_update(skip_locked=True)
               .filter(status=Status.PENDING, run_after__lte=now)
               .order_by(PRIORITY, "run_after", "id").first())
        if job is None:
            return None
        job.status = Status.RUNNING
        job.attempts += 1
        job.locked_at = now
        job.save(update_fields=["status", "attempts", "locked_at"])
    return job


def _retry_delay(attempts):
    """재시도 대기: 1회 실패 → 10초, 2회 → 20초 ... (DB 순간 끊김 같은 일시적 오류가 지나가도록)"""
    return timedelta(seconds=10 * attempts)


def _fail(job, now, error_text):
    """시도할 만큼 했으면 '실패'로 끝내고 제출을 '시스템 오류'로(R4: 미차감). 아니면 다시 '대기'."""
    job.last_error = error_text[-4000:]
    job.locked_at = None
    with transaction.atomic():   # 제출 '시스템 오류'와 작업 '실패'가 함께 저장되게
        if job.attempts >= settings.JOB_MAX_ATTEMPTS:
            job.status = Status.FAILED
            job.finished_at = now
            HANDLERS[job.kind][1](job.submission_id)
        else:
            job.status = Status.PENDING
            job.run_after = now + _retry_delay(job.attempts)
        job.save(update_fields=["status", "last_error", "finished_at", "run_after", "locked_at"])


def run(job, now=None):
    """가져온 작업 하나를 실행한다. 성공하면 True.

    채점 결과 저장과 '작업 완료' 표시를 한 트랜잭션으로 묶는다.
    → 점수는 저장됐는데 작업은 '실행 중'으로 남는 어긋남이 없다. 둘 다 되거나 둘 다 안 된다.
    (검증은 예외: NON_ATOMIC 참고)
    """
    now = now or timezone.now()
    handler, _ = HANDLERS[job.kind]
    try:
        if job.kind in NON_ATOMIC:
            handler(job.submission_id, now=now)   # 진행 단계 기록이 실행 중에 바로 보이도록 트랜잭션 밖에서
        with transaction.atomic():
            if job.kind not in NON_ATOMIC:
                handler(job.submission_id, now=now)
            if job.kind in FOLLOW_UP:
                enqueue(job.submission, FOLLOW_UP[job.kind], now=now)
            job.status = Status.DONE
            job.finished_at = now
            job.locked_at = None
            job.save(update_fields=["status", "finished_at", "locked_at"])
        return True
    except Exception:
        # 어떤 예외든 worker는 멈추지 않는다. 내용은 서버 로그와 Job.last_error에만 남긴다.
        log.exception("작업 #%s 실패 (시도 %s회)", job.pk, job.attempts)
        _fail(job, now, traceback.format_exc())
        return False


def run_one(now=None):
    """worker 반복문 한 바퀴: 가져올 작업이 있으면 실행. 실행했으면 True."""
    job = claim(now)
    if job is None:
        return False
    run(job, now)
    return True


def recover_stale(now=None):
    """'실행 중'인데 오래(기본 15분) 끝나지 않은 작업 = worker가 죽은 것으로 보고 처리한다.

    예) 14:00 worker가 작업 #12를 가져간 직후 컨테이너가 꺼짐 → 14:15 이후 새 worker가 되돌려 다시 채점
    시도 횟수가 남았으면 '대기'로, 다 썼으면 '실패'(제출은 시스템 오류, 미차감).
    """
    now = now or timezone.now()
    stale_before = now - timedelta(seconds=settings.JOB_STALE_SECONDS)
    count = 0
    with transaction.atomic():
        for job in Job.objects.select_for_update(skip_locked=True).filter(
            status=Status.RUNNING, locked_at__lt=stale_before,
        ):
            _fail(job, now, "worker가 응답 없이 멈춘 것으로 보여 되돌렸습니다.")
            count += 1
    return count


def enqueue_orphans(now=None):
    """'채점 대기'인데 채점 작업이 없는 제출, '채점됨'인데 검증 작업이 없는 제출에 작업을 만든다(안전망).

    정상 흐름에서는 생기지 않는다. 이 단위 전에 만들어진 제출이나 직접 DB를 고친 경우를 위한 것이다.
    """
    from submissions.models import Submission

    count = 0
    for status, kind in ((Submission.Status.QUEUED, Job.Kind.SCORE), (Submission.Status.SCORED, Job.Kind.VERIFY)):
        for sub in Submission.objects.filter(status=status).exclude(jobs__kind=kind):
            enqueue(sub, kind, now=now)
            count += 1
    return count

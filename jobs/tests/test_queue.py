"""작업 큐와 worker: R4(정상 채점 차감 / 서버 오류 미차감), 재시도, 멈춘 worker 복구, SKIP LOCKED."""
import threading
import time
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection, transaction

from accounts.models import FailedAttempt
from competitions import storage
from jobs import housekeeping, queue
from jobs.models import Job
from submissions import scoring, services
from submissions.models import Submission
# 제출 앱의 가상 파일·fixture를 그대로 쓴다
from submissions.tests.conftest import deadlines, kst, member, send_files  # noqa: F401

pytestmark = pytest.mark.django_db
S = Submission.Status
J = Job.Status
T0 = kst(11, 19, 10)   # 1차 마감(11/20 17:00) 전


def _queued(member, project, now=T0):
    """정상 제출 하나를 '채점 대기'까지 만든다. 가상 예측은 5개 중 4개 정답(Accuracy 0.8)."""
    sub = services.start_submission(member, project, now=now)
    send_files(sub, now=now)
    return services.complete(sub, now=now)


def _break_scoring(monkeypatch):
    def boom(submission_id, now=None):
        raise RuntimeError("/app/storage/private/비밀경로.csv 를 읽다가 실패")   # 가상 서버 오류
    monkeypatch.setitem(queue.HANDLERS, Job.Kind.SCORE, (boom, scoring.mark_system_error))


# ── 등록 ───────────────────────────────────────────────────────
def test_제출_완료하면_같은_트랜잭션에서_채점_작업이_하나_생긴다(member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    assert sub.status == S.QUEUED
    assert list(Job.objects.values_list("submission_id", "kind", "status")) == [(sub.pk, "score", J.PENDING)]


def test_같은_제출에_채점_작업을_두_번_등록해도_하나(member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    queue.enqueue(sub, now=T0)
    assert Job.objects.count() == 1


def test_형식_오류나_마감_후_접수는_채점_작업을_만들지_않는다(member, project_with_files, deadlines):
    bad = services.start_submission(member, project_with_files, now=T0)
    services.complete(bad, now=T0)                                       # 파일 없음 → 형식 오류
    late = services.start_submission(member, project_with_files, now=kst(11, 23, 9))
    send_files(late, now=kst(11, 23, 9))
    late = services.complete(late, now=kst(11, 23, 9))

    assert late.status == S.LATE
    assert Job.objects.count() == 0


# ── R4: 정상 채점은 차감 ─────────────────────────────────────────
def test_R4_worker가_채점하면_채점됨_점수_저장_횟수는_그대로_1회(member, project_with_files, deadlines):
    first, _ = deadlines
    sub = _queued(member, project_with_files)

    assert queue.run_one(now=T0) is True
    sub.refresh_from_db()
    assert sub.status == S.SCORED and sub.score == 0.8 and sub.scored_at == T0
    assert Job.objects.get(kind="score").status == J.DONE
    assert services.count_used(member, first) == 1                        # 정상 채점 = 차감 유지
    # 채점이 끝나면 검증 작업이 이어서 등록된다
    assert Job.objects.get(kind="verify").status == J.PENDING


def test_이미_채점된_제출의_작업이_다시_돌아도_점수가_바뀌지_않는다(member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    queue.run_one(now=T0)
    Job.objects.update(status=J.PENDING)                                  # 같은 작업이 한 번 더 실행되는 상황
    queue.run_one(now=T0 + timedelta(minutes=1))

    sub.refresh_from_db()
    assert sub.score == 0.8 and sub.scored_at == T0                       # 처음 채점 그대로


# ── R4: 서버 오류는 미차감 ───────────────────────────────────────
def test_R4_서버_오류는_재시도하고_3번_실패하면_시스템_오류_미차감(monkeypatch, member, project_with_files, deadlines):
    first, _ = deadlines
    sub = _queued(member, project_with_files)
    _break_scoring(monkeypatch)

    # 1회 실패 → 10초 뒤 재시도 예약. 그동안 제출은 '채점 대기'(횟수 1 유지)
    assert queue.run_one(now=T0) is True
    job = Job.objects.get()
    assert job.status == J.PENDING and job.attempts == 1 and job.run_after == T0 + timedelta(seconds=10)
    assert queue.claim(now=T0 + timedelta(seconds=9)) is None             # 아직 때가 안 됨
    sub.refresh_from_db()
    assert sub.status == S.QUEUED and services.count_used(member, first) == 1

    queue.run_one(now=T0 + timedelta(seconds=10))                         # 2회 실패 → 20초 뒤
    queue.run_one(now=T0 + timedelta(seconds=30))                         # 3회 실패 → 끝

    job.refresh_from_db()
    sub.refresh_from_db()
    assert job.status == J.FAILED and job.attempts == 3 and "RuntimeError" in job.last_error
    assert sub.status == S.ERROR and sub.score is None
    assert services.count_used(member, first) == 0                        # 미차감: 다시 낼 수 있다
    assert "비밀경로" not in sub.error_message and "/app" not in sub.error_message   # 내부 정보는 화면에 없음


def test_예측_파일이_디스크에서_사라지면_서버_오류로_처리(member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    storage.delete("submissions", sub.files.get(kind="csv").stored_name)
    for i in range(3):
        queue.run_one(now=T0 + timedelta(minutes=i))

    sub.refresh_from_db()
    assert sub.status == S.ERROR and "읽지 못했습니다" in Job.objects.get().last_error


# ── 멈춘 worker 복구 ─────────────────────────────────────────────
def test_실행_중에_worker가_죽으면_15분_뒤_되돌려_다시_채점(member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    queue.claim(now=T0)                                                   # 가져간 직후 worker가 꺼졌다고 가정

    assert queue.recover_stale(now=T0 + timedelta(minutes=14)) == 0       # 14분: 아직 기다린다(검증은 오래 걸릴 수 있다)
    assert queue.recover_stale(now=T0 + timedelta(minutes=15, seconds=1)) == 1
    assert Job.objects.get().status == J.PENDING

    queue.run_one(now=T0 + timedelta(minutes=16))
    sub.refresh_from_db()
    assert sub.status == S.SCORED


def test_계속_죽는_작업은_3번째에_실패로_끝난다(member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    Job.objects.update(status=J.RUNNING, attempts=3, locked_at=T0)
    queue.recover_stale(now=T0 + timedelta(minutes=16))

    sub.refresh_from_db()
    assert Job.objects.get().status == J.FAILED and sub.status == S.ERROR


def test_작업_없는_채점_대기_제출에_작업을_만들어_준다(member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    Job.objects.all().delete()                                            # 이 단위 전에 생긴 제출이라고 가정
    assert queue.enqueue_orphans(now=T0) == 1
    assert Job.objects.get().submission_id == sub.pk


# ── SKIP LOCKED ─────────────────────────────────────────────────
@pytest.mark.django_db(transaction=True)   # 스레드마다 실제 DB 연결·커밋이 필요하다
def test_다른_worker가_잠근_작업은_기다리지_않고_건너뛴다(member, project_with_files, deadlines):
    first_sub = _queued(member, project_with_files)
    second_sub = _queued(member, project_with_files)
    locked = threading.Event()
    done = threading.Event()

    def other_worker():
        # 다른 worker가 첫 작업 줄을 잠그고 1.5초 동안 놓지 않는다
        try:
            with transaction.atomic():
                Job.objects.select_for_update().get(submission=first_sub)
                locked.set()
                done.wait(timeout=1.5)
        finally:
            connection.close()

    t = threading.Thread(target=other_worker)
    t.start()
    locked.wait()
    started = time.monotonic()
    job = queue.claim(now=T0)
    waited = time.monotonic() - started
    done.set()
    t.join()

    assert job.submission_id == second_sub.pk        # 잠긴 첫 작업을 건너뛰고 두 번째를 가져왔다
    assert waited < 1.0                              # 잠금이 풀리기를 기다리지 않았다(SKIP LOCKED가 없으면 1.5초 대기)


# ── 정리 작업과 worker 명령 ─────────────────────────────────────
def test_정리_작업은_하루_지난_실패_기록을_지우고_방치된_업로드를_만료시킨다(member, project_with_files, deadlines):
    FailedAttempt.objects.create(kind="login", key="mock_stu01")
    FailedAttempt.objects.create(kind="login", key="mock_stu02")
    FailedAttempt.objects.filter(key="mock_stu01").update(created_at=T0 - timedelta(days=2))
    stuck = services.start_submission(member, project_with_files, now=T0)   # 시작만 하고 방치

    result = housekeeping.run_all(now=T0 + timedelta(minutes=11))

    assert result["실패 기록 삭제"] == 1 and result["업로드 만료"] == 1
    assert list(FailedAttempt.objects.values_list("key", flat=True)) == ["mock_stu02"]
    stuck.refresh_from_db()
    assert stuck.status == S.EXPIRED


# worker는 반복마다 끊긴 DB 연결을 닫고 새로 연다(close_old_connections).
# 테스트 전체를 트랜잭션 하나로 감싸는 기본 방식에서는 그 연결까지 닫히므로 실제 커밋 모드로 돌린다.
@pytest.mark.django_db(transaction=True)
def test_run_worker_once는_쌓인_작업을_모두_처리하고_끝난다(member, project_with_files, deadlines):
    subs = [_queued(member, project_with_files) for _ in range(2)]
    call_command("run_worker", "--once")

    assert [Submission.objects.get(pk=s.pk).status for s in subs] == [S.SCORED, S.SCORED]


def test_채점_작업이_검증_작업보다_먼저_실행된다(member, project_with_files, deadlines):
    first = _queued(member, project_with_files)
    queue.run_one(now=T0)                                                 # 첫 제출 채점 → 검증 작업 생김
    second = _queued(member, project_with_files, now=T0 + timedelta(minutes=1))

    job = queue.claim(now=T0 + timedelta(minutes=2))                     # 검증(먼저 생김)보다
    assert (job.kind, job.submission_id) == ("score", second.pk)          # 새 채점이 먼저
    assert Job.objects.get(submission=first, kind="verify").status == J.PENDING

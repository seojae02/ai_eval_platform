"""제출 규칙 R1~R6, D3, D4. 서비스 함수를 시각(now)을 지정해 직접 부른다."""
import threading
import time
from datetime import timedelta

import pytest
from django.db import connection

from competitions import storage
from submissions import filecheck, services
from submissions.models import Submission

from .conftest import GOOD_FILES, PRED_CSV, kst, send_files

pytestmark = pytest.mark.django_db
S = Submission.Status


def _make(member, project, deadline, status, n=1):
    for _ in range(n):
        Submission.objects.create(project=project, member=member, deadline=deadline, status=status,
                                  started_at=kst(11, 19, 9), last_received_at=kst(11, 19, 9))


# ── R1·R2: 시작 시각으로 구간을 정한다 ──────────────────────────
def test_R1_마감_1초_전에_시작하면_마감_뒤에_끝나도_그_마감으로_채점_대기(member, project_with_files, deadlines):
    first, _ = deadlines
    sub = services.start_submission(member, project_with_files, now=kst(11, 20, 16, 59, 59))
    send_files(sub, now=kst(11, 20, 17, 1))
    sub = services.complete(sub, now=kst(11, 20, 17, 2))

    assert sub.status == S.QUEUED and sub.deadline == first


def test_R1_마감_정각에_시작하면_다음_구간(member, project_with_files, deadlines):
    _, second = deadlines
    sub = services.start_submission(member, project_with_files, now=kst(11, 20, 17, 0, 0))
    assert sub.deadline == second


def test_R2_최종_마감_후_시작은_접수만_하고_횟수와_무관(member, project_with_files, deadlines):
    first, second = deadlines
    _make(member, project_with_files, second, S.SCORED, n=5)          # 2차 5회를 다 썼어도
    sub = services.start_submission(member, project_with_files, now=kst(11, 22, 17, 0, 1))
    send_files(sub, now=kst(11, 22, 17, 1))
    sub = services.complete(sub, now=kst(11, 22, 17, 2))

    assert sub.deadline is None and sub.status == S.LATE               # 접수는 되지만 채점 대상 아님
    assert services.usage(member, project_with_files, now=kst(11, 22, 17, 3)) is None


# ── R3: 누락·형식 오류는 차감하지 않는다 ──────────────────────────
def test_R3_노트북이_없으면_형식_오류이고_잡아둔_1회가_풀린다(member, project_with_files, deadlines):
    first, _ = deadlines
    now = kst(11, 19, 10)
    sub = services.start_submission(member, project_with_files, now=now)
    assert services.count_used(member, first) == 1                      # 시작하자마자 1회 잡힘

    send_files(sub, {k: v for k, v in GOOD_FILES.items() if k != "notebook"}, now=now)
    sub = services.complete(sub, now=now)

    assert sub.status == S.INVALID and "노트북" in sub.error_message
    assert services.count_used(member, first) == 0                      # 다시 0회


def test_R3_크기_상한을_넘는_조각은_형식_오류로_끝난다(monkeypatch, member, project_with_files, deadlines):
    monkeypatch.setitem(filecheck.MAX_BYTES, "csv", 10)
    sub = services.start_submission(member, project_with_files, now=kst(11, 19, 10))

    with pytest.raises(services.SubmissionRefused, match="너무 큽니다"):
        services.receive_chunk(sub, "csv", 0, "pred.csv", PRED_CSV, now=kst(11, 19, 10))
    sub.refresh_from_db()
    assert sub.status == S.INVALID and not sub.is_counted


# ── R5: 사용/남은 횟수는 상태로 센다 ─────────────────────────────
def test_R5_채점3_오류2_제한5면_사용3_남은2(member, project_with_files, deadlines):
    _, second = deadlines
    _make(member, project_with_files, second, S.SCORED, n=3)
    _make(member, project_with_files, second, S.INVALID)
    _make(member, project_with_files, second, S.ERROR)                 # 시스템 오류도 미차감(R4)

    u = services.usage(member, project_with_files, now=kst(11, 21, 10))
    assert (u["used"], u["remaining"], u["limit"]) == (3, 2, 5)


def test_횟수를_다_쓰면_시작을_거부한다(member, project_with_files, deadlines):
    first, _ = deadlines
    _make(member, project_with_files, first, S.QUEUED, n=3)

    with pytest.raises(services.SubmissionRefused, match="3회를 모두 사용"):
        services.start_submission(member, project_with_files, now=kst(11, 19, 10))
    assert Submission.objects.filter(status=S.UPLOADING).count() == 0


def test_D4_구간마다_따로_센다(member, project_with_files, deadlines):
    first, second = deadlines
    _make(member, project_with_files, first, S.SCORED, n=3)            # 1차 3회 모두 사용

    sub = services.start_submission(member, project_with_files, now=kst(11, 21, 10))
    assert sub.deadline == second                                      # 2차에서는 새로 5회


# ── D3: 10분 무수신이면 만료 ───────────────────────────────────
def test_D3_느려도_받고_있으면_살아있고_10분_넘게_끊기면_만료(member, project_with_files, deadlines):
    first, _ = deadlines
    t = kst(11, 19, 10)
    sub = services.start_submission(member, project_with_files, now=t)
    data = PRED_CSV
    services.receive_chunk(sub, "csv", 0, "pred.csv", data[:10], now=t + timedelta(minutes=9))
    services.receive_chunk(sub, "csv", 10, "pred.csv", data[10:20], now=t + timedelta(minutes=18))   # 시작 후 18분이어도 OK

    with pytest.raises(services.SubmissionRefused, match="만료"):
        services.receive_chunk(sub, "csv", 20, "pred.csv", data[20:], now=t + timedelta(minutes=28, seconds=1))
    sub.refresh_from_db()
    assert sub.status == S.EXPIRED and services.count_used(member, first) == 0


def test_업로드_중인_제출은_하나만(member, project_with_files, deadlines):
    services.start_submission(member, project_with_files, now=kst(11, 19, 10))
    with pytest.raises(services.SubmissionRefused, match="이미 업로드 중"):
        services.start_submission(member, project_with_files, now=kst(11, 19, 10, 1))


def test_취소하면_잡아둔_1회가_풀린다(member, project_with_files, deadlines):
    first, _ = deadlines
    sub = services.start_submission(member, project_with_files, now=kst(11, 19, 10))
    services.cancel(sub)

    sub.refresh_from_db()
    assert sub.status == S.CANCELLED and services.count_used(member, first) == 0


# ── 조각 수신 ─────────────────────────────────────────────────
def test_조각을_이어붙이고_순서가_틀리면_거부한다(member, project_with_files, deadlines):
    now = kst(11, 19, 10)
    sub = services.start_submission(member, project_with_files, now=now)
    assert services.receive_chunk(sub, "csv", 0, "pred.csv", PRED_CSV[:7], now=now) == 7

    with pytest.raises(services.SubmissionRefused, match="순서"):
        services.receive_chunk(sub, "csv", 3, "pred.csv", PRED_CSV[3:], now=now)      # 빈칸·겹침
    services.receive_chunk(sub, "csv", 7, "pred.csv", PRED_CSV[7:], now=now)

    f = sub.files.get(kind="csv")
    assert storage.read_bytes("submissions", f.stored_name) == PRED_CSV
    assert f.stored_name.endswith(".csv") and f.original_name == "pred.csv"


def test_모델_파일은_원래_확장자_대신_bin으로_저장된다(member, project_with_files, deadlines):
    now = kst(11, 19, 10)
    sub = services.start_submission(member, project_with_files, now=now)
    send_files(sub, now=now)
    assert sub.files.get(kind="model").stored_name.endswith(".bin")


def test_같은_위치에_다시_쓰면_두번_붙지_않는다(tmp_path):
    name = storage.save_bytes("submissions", b"", ext="bin")
    storage.write_at("submissions", name, 0, b"abc")
    storage.write_at("submissions", name, 0, b"abc")     # 재전송
    assert storage.read_bytes("submissions", name) == b"abc"


# ── R6: 동시 제출 ─────────────────────────────────────────────
@pytest.mark.django_db(transaction=True)   # 스레드마다 실제 DB 연결·커밋이 필요하다
def test_R6_남은_1회에서_동시에_두번_눌러도_1건만_만들어진다(monkeypatch, member, project_with_files, deadlines):
    first, _ = deadlines
    _make(member, project_with_files, first, S.SCORED, n=2)            # 제한 3 중 2회 사용 → 남은 1

    # 세기와 만들기 사이에 0.5초 틈을 만들어, 잠금이 없으면 반드시 두 요청이 겹치게 한다
    real_count = services.count_used

    def slow_count(*args):
        result = real_count(*args)
        time.sleep(0.5)
        return result

    monkeypatch.setattr(services, "count_used", slow_count)
    barrier = threading.Barrier(2)
    results = []

    def click():
        try:
            barrier.wait()
            services.start_submission(member, project_with_files, now=kst(11, 19, 10))
            results.append("성공")
        except services.SubmissionRefused:
            results.append("거부")
        finally:
            connection.close()   # 스레드가 연 DB 연결을 닫는다

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["거부", "성공"]
    assert services.count_used(member, first) == 3                      # 제한 3을 넘지 않음

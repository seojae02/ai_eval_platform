"""완료 알림·검증 진행 화면용 상태 (submissions/live.py, status.json, 진행 화면). 권한 칸은 권한 매트릭스에도 있다."""
import pytest
from django.db import connection
from django.urls import reverse

from accounts.models import User
from competitions.models import CompetitionMember
from jobs import queue
from jobs.models import Job
from submissions import live, services
from submissions.models import Submission
from submissions.tests.conftest import GOOD_FILES, PRED_CSV, deadlines, kst, member, send_files  # noqa: F401
from verification import sandbox
from verification.models import VerificationReport

pytestmark = pytest.mark.django_db
T0 = kst(11, 19, 10)
A1 = CompetitionMember.Role.ADMIN1


def _queued(member, project):
    sub = services.start_submission(member, project, now=T0)
    send_files(sub, GOOD_FILES, now=T0)
    return services.complete(sub, now=T0)


@pytest.fixture
def fake_sandbox(monkeypatch):
    def run(mode, files, job, client=None):
        if mode == "model":
            return {"status": "ok", "model_type": "sklearn.가상모델"}, {"model_pred.csv": PRED_CSV}
        return {"status": "ok", "elapsed": 1.0}, {"rerun.csv": PRED_CSV}
    monkeypatch.setattr(sandbox, "run", run)


def _status(client, project, **params):
    url = reverse("submissions:status", args=[project.competition_id, project.pk])
    return client.get(url, params)


# ── 참여자: 내 제출 채점 완료 알림 ─────────────────────────────
def test_참여자는_내_제출의_채점_상태만_받는다(client, student, member, project_with_files, deadlines, competition, pw):
    mine = _queued(member, project_with_files)
    other_user = User.objects.create_user("mock_stu02", real_name="가상학생02")
    other = _queued(CompetitionMember.objects.create(user=other_user, competition=competition), project_with_files)
    client.force_login(student)

    data = _status(client, project_with_files, ids=f"{mine.pk},{other.pk},abc").json()
    assert list(data["items"]) == [str(mine.pk)]                     # 남의 번호는 조용히 빠짐
    assert data["items"][str(mine.pk)] == {"done": False, "state": "waiting", "text": f"제출 #{mine.pk} 채점 대기"}

    queue.run_one(now=T0)                                            # worker가 채점
    item = _status(client, project_with_files, ids=str(mine.pk)).json()["items"][str(mine.pk)]
    assert item["done"] and "채점 완료" in item["text"]


def test_참여자는_검증_상태를_볼_수_없다(client, student, project_with_files):
    client.force_login(student)
    assert _status(client, project_with_files, watch="verify", ids="1").status_code == 403


def test_상태_응답은_캐시하지_않는다(client, student, project_with_files):
    client.force_login(student)
    assert _status(client, project_with_files, ids="1")["Cache-Control"] == "private, no-store"


def test_내_제출_화면에_채점_대기_번호가_실린다(client, student, member, project_with_files, deadlines):
    sub = _queued(member, project_with_files)
    client.force_login(student)
    html = client.get(reverse("competitions:project_detail", args=[project_with_files.competition_id, project_with_files.pk])).content.decode()
    assert 'id="live"' in html and f'data-ids="{sub.pk}"' in html


# ── 관리자: 검증 단계 ─────────────────────────────────────────
def test_검증_단계가_순서대로_기록되고_끝나면_비워진다(member, project_with_files, deadlines, fake_sandbox, monkeypatch):
    seen = []
    from verification import services as verification

    original = verification.set_stage

    def spy(report, stage):
        seen.append(stage)
        original(report, stage)

    monkeypatch.setattr(verification, "set_stage", spy)
    sub = _queued(member, project_with_files)
    queue.run_one(now=T0)   # 채점
    queue.run_one(now=T0)   # 검증

    assert seen == ["static", "model", "notebook", "saving", ""]
    report = VerificationReport.objects.get(submission=sub)
    assert report.stage == "" and set(report.timeline) == {"static", "model", "notebook", "saving"}
    assert all("end" in span for span in report.timeline.values())   # 모든 단계에 끝 시각


@pytest.mark.django_db(transaction=True)
def test_검증은_트랜잭션_밖에서_돌아_진행_단계가_바로_보인다(monkeypatch, member, project_with_files, deadlines):
    """트랜잭션 안이면 검증이 끝날 때까지 단계 기록이 다른 화면에 보이지 않는다."""
    sub = _queued(member, project_with_files)
    queue.run_one(now=T0)   # 채점
    inside = []
    monkeypatch.setitem(queue.HANDLERS, Job.Kind.VERIFY,
                        (lambda submission_id, now=None: inside.append(connection.in_atomic_block), lambda sid: None))
    queue.run_one(now=T0)
    assert inside == [False]


def test_대기열_위치는_채점_작업을_먼저_센다(member, project_with_files, deadlines, competition):
    sub = _queued(member, project_with_files)
    queue.run_one(now=T0)                                  # 채점 → 검증 작업 대기
    other = User.objects.create_user("mock_stu02", real_name="가상학생02")
    _queued(CompetitionMember.objects.create(user=other, competition=competition), project_with_files)   # 채점 대기 1건

    item = live.verify_item(Submission.objects.get(pk=sub.pk), detail=True)
    assert item["state"] == "waiting" and item["headline"] == "대기열: 앞에 1건"   # 늦게 온 채점이 먼저
    assert [s["state"] for s in item["steps"]] == ["current", "todo", "todo", "todo", "todo"]


def test_노트북_단계에서는_제한_시간_대비_경과를_준다(member, project_with_files, deadlines, settings):
    sub = _queued(member, project_with_files)
    queue.run_one(now=T0)
    from django.utils import timezone

    Job.objects.filter(submission=sub, kind=Job.Kind.VERIFY).update(status=Job.Status.RUNNING)
    VerificationReport.objects.create(submission=sub, started_at=timezone.now(), stage="notebook",
                                      timeline={"static": {"start": timezone.now().isoformat(), "end": timezone.now().isoformat()},
                                                "notebook": {"start": timezone.now().isoformat()}})
    item = live.verify_item(Submission.objects.get(pk=sub.pk), detail=True)
    assert item["headline"] == "실행 중: 노트북 재실행 (V1)" and item["limit"] == settings.SANDBOX_TIMEOUT
    # 단계: 대기열 · 파일·코드 · 모델 · 노트북 · 저장
    assert [s["state"] for s in item["steps"]] == ["done", "done", "done", "current", "todo"]


# ── 재검증 → 진행 화면 ────────────────────────────────────────
def test_재검증을_누르면_진행_화면으로_간다(client, admin2, member, project_with_files, deadlines, fake_sandbox):
    sub = _queued(member, project_with_files)
    queue.run_one(now=T0)
    queue.run_one(now=T0)   # 검증까지 끝남
    client.force_login(admin2)
    args = [project_with_files.competition_id, project_with_files.pk, sub.pk]

    resp = client.post(reverse("submissions:review_reverify", args=args))
    assert resp.url == reverse("submissions:review_progress", args=args)
    html = client.get(resp.url).content.decode()
    assert "대기열: 앞에 0건" in html and 'data-mode="progress"' in html and 'data-step="notebook"' in html


def test_진행_화면은_끝난_검증이면_갱신하지_않는다(client, admin2, member, project_with_files, deadlines, fake_sandbox):
    sub = _queued(member, project_with_files)
    queue.run_one(now=T0)
    queue.run_one(now=T0)
    client.force_login(admin2)
    html = client.get(reverse("submissions:review_progress",
                              args=[project_with_files.competition_id, project_with_files.pk, sub.pk])).content.decode()
    assert "검증 완료" in html and 'id="live"' not in html


# ── 리더보드 ─────────────────────────────────────────────────
def test_리더보드_비교값은_새_점수가_들어오면_바뀐다(member, project_with_files, deadlines):
    before = live.board_version(project_with_files)
    _queued(member, project_with_files)
    assert live.board_version(project_with_files) == before            # 채점 전에는 그대로
    queue.run_one(now=T0)
    assert live.board_version(project_with_files) != before            # 채점되면 바뀜

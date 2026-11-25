"""브라우저가 하는 순서 그대로: 제출 시작 → 조각 전송 → 제출 완료. 그리고 다른 앱과 연결된 규칙."""
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from competitions import services as competition_services
from competitions.csvcheck import CsvError
from competitions.models import CompetitionMember, Deadline
from submissions.models import Submission

from .conftest import GOOD_FILES

pytestmark = pytest.mark.django_db


@pytest.fixture
def open_project(project_with_files):
    """지금부터 3일 뒤 마감(3회)인 프로젝트."""
    Deadline.objects.create(project=project_with_files, due_at=timezone.now() + timedelta(days=3), max_submissions=3)
    return project_with_files


def _args(project, *extra):
    return [project.competition_id, project.pk, *extra]


def _start(client, project):
    return client.post(reverse("submissions:start", args=_args(project)), {"comment": "가상 코멘트"})


def _chunk(client, sub, kind, offset, name, data, **extra):
    url = reverse("submissions:chunk", args=_args(sub.project, sub.pk))
    return client.post(f"{url}?kind={kind}&offset={offset}&name={name}", data=data,
                       content_type="application/octet-stream", **extra)


def test_제출_전체_흐름(client, student, open_project):
    client.force_login(student)
    resp = _start(client, open_project)
    sub = Submission.objects.get()
    assert resp.url == reverse("submissions:upload", args=_args(open_project, sub.pk))
    assert client.get(resp.url).status_code == 200

    for kind, (name, data) in GOOD_FILES.items():
        r = _chunk(client, sub, kind, 0, name, data)
        assert r.status_code == 200 and r.json() == {"received": len(data)}
    resp = client.post(reverse("submissions:complete", args=_args(open_project, sub.pk)), follow=True)

    sub.refresh_from_db()
    assert sub.status == Submission.Status.QUEUED and sub.comment == "가상 코멘트"
    html = resp.content.decode()
    assert "채점 대기 중입니다" in html and "사용 1 / 남은 2" in html     # R5 화면 표시


def test_조각_요청도_CSRF_토큰이_없으면_거부(student, open_project):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(student)
    member = CompetitionMember.objects.get(user=student)
    sub = Submission.objects.create(project=open_project, member=member, started_at=timezone.now(),
                                    last_received_at=timezone.now())

    assert _chunk(csrf_client, sub, "csv", 0, "pred.csv", b"id,label\n").status_code == 403


def test_거부된_조각은_JSON으로_이유를_알려준다(client, student, open_project):
    client.force_login(student)
    _start(client, open_project)
    sub = Submission.objects.get()

    r = _chunk(client, sub, "csv", 5, "pred.csv", b"abc")
    assert r.status_code == 409 and "첫 조각부터" in r.json()["error"]
    assert _chunk(client, sub, "csv", "x", "pred.csv", b"abc").status_code == 400


# ── 다른 앱과 연결된 규칙 (지난 회차 TODO) ─────────────────────────
def _submission(member, project, status):
    return Submission.objects.create(project=project, member=member, deadline=project.deadlines.first(),
                                     status=status, started_at=timezone.now(), last_received_at=timezone.now())


def test_D5_제출한_참여자는_관리자1로_지정할_수_없다(student, competition, open_project):
    member = CompetitionMember.objects.get(user=student)
    _submission(member, open_project, Submission.Status.INVALID)     # 형식 오류도 '제출 기록'

    with pytest.raises(competition_services.PromotionRefused, match="제출 기록"):
        competition_services.promote_to_admin1(member)


def test_제출이_걸린_마감일은_삭제할_수_없고_기록도_남지_않는다(client, admin2, student, open_project):
    member = CompetitionMember.objects.get(user=student)
    sub = _submission(member, open_project, Submission.Status.QUEUED)
    client.force_login(admin2)

    resp = client.post(reverse("competitions:deadline_delete", args=_args(open_project, sub.deadline_id)), follow=True)

    assert Deadline.objects.filter(pk=sub.deadline_id).exists()
    assert "연결된 제출이 1건" in resp.content.decode()
    assert not AuditLog.objects.filter(action=AuditLog.Action.DEADLINE_DELETED).exists()


def test_채점_대기_제출이_있으면_정답을_바꿀_수_없다(admin2, student, open_project):
    member = CompetitionMember.objects.get(user=student)
    _submission(member, open_project, Submission.Status.QUEUED)

    with pytest.raises(CsvError, match="정답 파일을 바꿀 수 없습니다"):
        competition_services.upload_project_file(
            open_project, "answer", SimpleUploadedFile("a.csv", b"id,label\n1,1\n2,1\n3,1\n4,1\n5,1\n"), admin2)


def test_관리자2_화면에는_제출_영역이_없다(client, admin2, open_project):
    client.force_login(admin2)
    html = client.get(reverse("competitions:project_detail", args=_args(open_project))).content.decode()
    assert "제출 시작" not in html

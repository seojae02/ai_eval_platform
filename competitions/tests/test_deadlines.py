from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from audit.models import AuditLog
from competitions.deadlines import deadline_for
from competitions.models import Deadline

pytestmark = pytest.mark.django_db
KST = ZoneInfo("Asia/Seoul")


def kst(month, day, hour, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=KST)


@pytest.fixture
def two_deadlines(project):
    first = Deadline.objects.create(project=project, due_at=kst(11, 20, 17), max_submissions=3)
    second = Deadline.objects.create(project=project, due_at=kst(11, 22, 17), max_submissions=5)
    return first, second


# ── 마감 구간 판정 (R1·R2 경계값) ──────────────────────────────
@pytest.mark.parametrize("started, expected", [
    (kst(11, 19, 10), "1차"),              # 1차 마감 하루 전
    (kst(11, 20, 16, 59, 59), "1차"),      # 1차 마감 1초 전
    (kst(11, 20, 17, 0, 0), "2차"),        # 1차 마감 정각 → 1차 아님 (시작 < 마감)
    (kst(11, 22, 16, 59, 59), "2차"),      # 최종 마감 1초 전
    (kst(11, 22, 17, 0, 0), "마감 후"),    # 최종 마감 정각
    (kst(11, 22, 17, 0, 1), "마감 후"),    # 최종 마감 1초 후 → 접수만, 채점 안 함
])
def test_업로드_시작_시각으로_마감_구간을_정한다(project, two_deadlines, started, expected):
    first, second = two_deadlines
    result = deadline_for(project, started)

    assert {first: "1차", second: "2차", None: "마감 후"}[result] == expected


def test_마감일이_없으면_마감_후로_본다(project):
    assert deadline_for(project, kst(11, 19, 10)) is None


# ── 마감일 추가·삭제 ────────────────────────────────────────────
@pytest.fixture
def as_admin2(client, admin2):
    client.force_login(admin2)
    return client


def _add_url(project):
    return reverse("competitions:deadline_add", args=[project.competition.pk, project.pk])


def test_입력한_시각은_한국시간으로_저장된다(as_admin2, project):
    as_admin2.post(_add_url(project), {"due_at": "2026-11-20T17:00", "max_submissions": 3})

    d = Deadline.objects.get(project=project)
    assert d.due_at == datetime(2026, 11, 20, 8, 0, tzinfo=UTC)    # 한국 17:00 = UTC 08:00
    log = AuditLog.objects.get(action=AuditLog.Action.DEADLINE_ADDED)
    assert log.detail["max_submissions"] == 3


def test_같은_시각_마감일은_중복으로_만들_수_없다(as_admin2, project):
    as_admin2.post(_add_url(project), {"due_at": "2026-11-20T17:00", "max_submissions": 3})
    resp = as_admin2.post(_add_url(project), {"due_at": "2026-11-20T17:00", "max_submissions": 5}, follow=True)

    assert Deadline.objects.filter(project=project).count() == 1
    assert "같은 시각의 마감일이 이미 있습니다" in resp.content.decode()


def test_제출_횟수_제한은_1_이상이어야_한다(as_admin2, project):
    as_admin2.post(_add_url(project), {"due_at": "2026-11-20T17:00", "max_submissions": 0})

    assert not Deadline.objects.filter(project=project).exists()


def test_마감일을_삭제하면_기록된다(as_admin2, project, two_deadlines):
    first, _ = two_deadlines
    as_admin2.post(reverse("competitions:deadline_delete", args=[project.competition.pk, project.pk, first.pk]))

    assert not Deadline.objects.filter(pk=first.pk).exists()
    assert AuditLog.objects.filter(action=AuditLog.Action.DEADLINE_DELETED).exists()

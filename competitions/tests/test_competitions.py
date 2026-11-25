import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import User
from audit.models import AuditLog
from competitions import services
from competitions.models import Competition, CompetitionMember

pytestmark = pytest.mark.django_db
A1 = CompetitionMember.Role.ADMIN1


@pytest.fixture
def as_admin2(client, admin2):
    client.force_login(admin2)
    return client


def _membership(user, competition):
    return CompetitionMember.objects.get(user=user, competition=competition)


# ── 대회 개설 (R14) ─────────────────────────────────────────────
def test_관리자2가_대회를_개설하면_코드가_생기고_기록된다(as_admin2, admin2):
    resp = as_admin2.post(reverse("competitions:create"), {"name": "가상대회-신규"})

    competition = Competition.objects.get(name="가상대회-신규")
    assert resp.status_code == 302
    assert len(competition.join_code) == 8
    assert competition.created_by == admin2
    log = AuditLog.objects.get(action=AuditLog.Action.COMPETITION_CREATED)
    assert log.actor == admin2 and log.competition == competition


def test_대회명이_비면_개설되지_않는다(as_admin2):
    as_admin2.post(reverse("competitions:create"), {"name": ""})

    assert Competition.objects.count() == 0


def test_관리자2_홈에는_모든_대회가_보인다(as_admin2, competition, other_competition):
    html = as_admin2.get("/").content.decode()

    assert competition.name in html and other_competition.name in html


# ── 참여 코드 재발급 ────────────────────────────────────────────
def test_코드를_재발급하면_옛_코드는_무효_새_코드는_유효(as_admin2, competition, signup_data):
    old_code = competition.display_code()
    as_admin2.post(reverse("competitions:reissue_code", args=[competition.pk]))
    competition.refresh_from_db()

    assert competition.display_code() != old_code
    resp_old = Client().post("/signup/", {**signup_data, "join_code": old_code})
    assert "유효하지 않은 참여 코드" in resp_old.content.decode()
    resp_new = Client().post("/signup/", {**signup_data, "join_code": competition.display_code()})
    assert resp_new.status_code == 302


def test_재발급_기록에는_코드_값이_남지_않는다(as_admin2, competition):
    as_admin2.post(reverse("competitions:reissue_code", args=[competition.pk]))
    competition.refresh_from_db()

    log = AuditLog.objects.get(action=AuditLog.Action.JOIN_CODE_REISSUED)
    assert competition.join_code not in str(log.detail)


# ── 코드 표시 ──────────────────────────────────────────────────
def test_참여자_화면에는_코드가_없고_관리자1_화면에는_있다(client, student, competition):
    client.force_login(student)
    html = client.get(reverse("competitions:detail", args=[competition.pk])).content.decode()
    assert competition.display_code() not in html

    _membership(student, competition).delete()
    ta = User.objects.create_user("mock_ta01", real_name="가상조교01")
    CompetitionMember.objects.create(user=ta, competition=competition, role=A1)
    client.force_login(ta)
    html = client.get(reverse("competitions:detail", args=[competition.pk])).content.decode()
    assert competition.display_code() in html


# ── 관리자1 지정·해제 (D5) ──────────────────────────────────────
def test_참여자를_관리자1로_지정하고_기록한다(as_admin2, admin2, student, competition):
    m = _membership(student, competition)
    as_admin2.post(reverse("competitions:promote", args=[competition.pk, m.pk]))

    m.refresh_from_db()
    assert m.role == A1 and m.is_active
    log = AuditLog.objects.get(action=AuditLog.Action.ADMIN1_PROMOTED)
    assert log.actor == admin2 and log.target_user == student and log.detail == {"reactivated": False}


def test_제출_기록이_있으면_관리자1로_지정할_수_없다(as_admin2, student, competition, monkeypatch):
    # 제출 기능이 아직 없으므로 '제출 기록 있음' 상황을 바꿔 끼워서 거부 분기를 확인한다
    monkeypatch.setattr(services, "has_submissions", lambda user, comp: True)
    m = _membership(student, competition)

    resp = as_admin2.post(reverse("competitions:promote", args=[competition.pk, m.pk]), follow=True)

    m.refresh_from_db()
    assert m.role == CompetitionMember.Role.PARTICIPANT
    assert "제출 기록이 있어" in resp.content.decode()
    assert not AuditLog.objects.filter(action=AuditLog.Action.ADMIN1_PROMOTED).exists()


def test_해제하면_비활성이_되고_관리화면도_참여도_막힌다(client, admin2, student, competition):
    m = _membership(student, competition)
    m.role = A1
    m.save()
    client.force_login(admin2)
    client.post(reverse("competitions:revoke", args=[competition.pk, m.pk]))

    m.refresh_from_db()
    assert m.role == A1 and not m.is_active            # 참여자로 돌아가지 않음
    assert AuditLog.objects.filter(action=AuditLog.Action.ADMIN1_REVOKED, target_user=student).exists()

    client.force_login(student)
    assert client.get(reverse("competitions:members", args=[competition.pk])).status_code == 404
    resp = client.post("/join/", {"join_code": competition.display_code()})
    assert "이미 참여 중인 대회" in resp.content.decode()   # 코드로 참여자 재가입 불가
    m.refresh_from_db()
    assert m.role == A1 and not m.is_active


def test_해제된_관리자1을_다시_지정할_수_있다(as_admin2, student, competition):
    m = _membership(student, competition)
    m.role, m.is_active = A1, False
    m.save()

    as_admin2.post(reverse("competitions:promote", args=[competition.pk, m.pk]))

    m.refresh_from_db()
    assert m.is_active
    log = AuditLog.objects.get(action=AuditLog.Action.ADMIN1_PROMOTED)
    assert log.detail == {"reactivated": True}


def test_참여자는_해제_대상이_아니다(as_admin2, student, competition):
    m = _membership(student, competition)
    resp = as_admin2.post(reverse("competitions:revoke", args=[competition.pk, m.pk]), follow=True)

    m.refresh_from_db()
    assert m.role == CompetitionMember.Role.PARTICIPANT and m.is_active
    assert "활성 관리자1이 아닙니다" in resp.content.decode()

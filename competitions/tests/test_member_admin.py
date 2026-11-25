"""회원 관리: 임시 비밀번호, 강제 변경, 잠금 해제, 참가 정지. 권한 칸 전체는 test_permissions.py 매트릭스에 있다."""
import re

import pytest
from django.urls import reverse

from accounts.models import FailedAttempt, User
from accounts.throttle import is_locked
from audit.models import AuditLog
from competitions.models import CompetitionMember

pytestmark = pytest.mark.django_db
A1 = CompetitionMember.Role.ADMIN1


@pytest.fixture
def ta(competition):
    """이 대회의 관리자1 (가상조교01)."""
    user = User.objects.create_user("mock_ta01", real_name="가상조교01")
    CompetitionMember.objects.create(user=user, competition=competition, role=A1)
    return user


def _m(user, competition):
    return CompetitionMember.objects.get(user=user, competition=competition)


def _url(name, competition, member):
    return reverse(f"competitions:{name}", args=[competition.pk, member.pk])


def _temp_from(resp):
    return re.search(r'<p class="code">([^<]+)</p>', resp.content.decode()).group(1)


# ── 임시 비밀번호 ─────────────────────────────────────────────
def test_관리자1이_참여자에게_임시_비밀번호를_발급한다(client, ta, student, competition, pw):
    client.force_login(ta)
    resp = client.post(_url("member_reset_password", competition, _m(student, competition)))

    temp = _temp_from(resp)
    student.refresh_from_db()
    assert re.fullmatch(r"[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}", temp)
    assert student.check_password(temp) and not student.check_password(pw)
    assert student.must_change_password
    assert resp["Cache-Control"] == "private, no-store"
    log = AuditLog.objects.get(action=AuditLog.Action.PASSWORD_RESET)
    assert log.actor == ta and log.target_user == student
    assert temp not in str(log.detail)                          # 임시 비밀번호 값은 기록하지 않음


def test_관리자1은_다른_대회_관리자인_사람의_비밀번호를_바꿀_수_없다(client, ta, competition, other_competition, pw):
    """계정 탈취 방지: 이 대회에서는 참여자지만 다른 대회에서는 관리자1인 가상조교02."""
    ta2 = User.objects.create_user("mock_ta02", password=pw, real_name="가상조교02")
    CompetitionMember.objects.create(user=ta2, competition=competition)                 # 여기선 참여자
    CompetitionMember.objects.create(user=ta2, competition=other_competition, role=A1)  # 저기선 관리자1
    client.force_login(ta)

    resp = client.post(_url("member_reset_password", competition, _m(ta2, competition)))

    assert resp.status_code == 403
    ta2.refresh_from_db()
    assert ta2.check_password(pw) and not ta2.must_change_password
    html = client.get(reverse("competitions:members", args=[competition.pk])).content.decode()
    assert _url("member_reset_password", competition, _m(ta2, competition)) not in html   # 버튼도 없음


def test_관리자2는_관리자1의_비밀번호도_초기화할_수_있다(client, admin2, ta, competition):
    client.force_login(admin2)
    assert client.post(_url("member_reset_password", competition, _m(ta, competition))).status_code == 200


def test_임시_비밀번호로_로그인하면_바꾸기_전에는_다른_화면을_못_쓴다(client, admin2, student, competition):
    client.force_login(admin2)
    temp = _temp_from(client.post(_url("member_reset_password", competition, _m(student, competition))))
    client.logout()

    client.post(reverse("login"), {"username": "mock_stu01", "password": temp})
    assert client.get(reverse("home")).url == reverse("password_change")
    assert client.get(reverse("competitions:detail", args=[competition.pk])).url == reverse("password_change")

    new = "새가상비밀번호-2026-99"
    client.post(reverse("password_change"), {"old_password": temp, "new_password1": new, "new_password2": new})
    student.refresh_from_db()
    assert not student.must_change_password and student.check_password(new)
    assert client.get(reverse("home")).status_code == 200        # 이제 정상 사용 (세션도 유지됨)


def test_임시_비밀번호를_발급하면_대상의_기존_로그인이_끊긴다(client, admin2, student, competition):
    student_client = type(client)()
    student_client.force_login(student)
    assert student_client.get(reverse("home")).status_code == 200

    client.force_login(admin2)
    client.post(_url("member_reset_password", competition, _m(student, competition)))

    assert student_client.get(reverse("home")).url.startswith(reverse("login"))


def test_내_비밀번호_변경은_현재_비밀번호가_맞아야_한다(client, student, pw):
    client.force_login(student)
    new = "새가상비밀번호-2026-99"
    resp = client.post(reverse("password_change"), {"old_password": "틀린비밀번호-0000", "new_password1": new, "new_password2": new})

    assert resp.status_code == 200
    student.refresh_from_db()
    assert student.check_password(pw)


# ── 로그인 잠금 해제 ──────────────────────────────────────────
def test_관리자1이_로그인_잠금을_풀어_준다(client, ta, student, competition):
    for _ in range(5):
        client.post(reverse("login"), {"username": "mock_stu01", "password": "틀림-0000000"})
    assert is_locked(FailedAttempt.Kind.LOGIN, "mock_stu01")
    client.force_login(ta)
    assert "로그인 잠김" in client.get(reverse("competitions:members", args=[competition.pk])).content.decode()

    client.post(_url("member_unlock", competition, _m(student, competition)))

    assert not is_locked(FailedAttempt.Kind.LOGIN, "mock_stu01")
    assert AuditLog.objects.filter(action=AuditLog.Action.LOGIN_UNLOCKED, target_user=student).exists()


# ── 참가 정지 ────────────────────────────────────────────────
def test_정지된_참여자는_대회에_들어올_수_없고_해제하면_돌아온다(client, admin2, student, competition):
    member = _m(student, competition)
    admin = type(client)()
    admin.force_login(admin2)
    client.force_login(student)

    admin.post(_url("member_suspend", competition, member))
    assert client.get(reverse("competitions:detail", args=[competition.pk])).status_code == 404
    assert AuditLog.objects.filter(action=AuditLog.Action.MEMBER_SUSPENDED, target_user=student).exists()

    admin.post(_url("member_reinstate", competition, member))
    assert client.get(reverse("competitions:detail", args=[competition.pk])).status_code == 200


def test_정지된_참여자는_관리자1로_지정할_수_없다(client, admin2, student, competition):
    member = _m(student, competition)
    client.force_login(admin2)
    client.post(_url("member_suspend", competition, member))

    client.post(reverse("competitions:promote", args=[competition.pk, member.pk]))
    member.refresh_from_db()
    assert member.role == CompetitionMember.Role.PARTICIPANT and not member.is_active


def test_관리자1은_정지할_수_없다(client, admin2, ta, competition):
    """정지 대상은 참여자뿐: 관리자1에게는 '관리자1 해제'를 쓴다."""
    client.force_login(admin2)
    resp = client.post(_url("member_suspend", competition, _m(ta, competition)), follow=True)

    assert "참여자만 정지할 수 있습니다" in resp.content.decode()
    assert _m(ta, competition).is_active


# ── 검색 ─────────────────────────────────────────────────────
def test_실명_아이디_별칭으로_검색한다(client, admin2, student, ta, competition):
    client.force_login(admin2)
    url = reverse("competitions:members", args=[competition.pk])

    html = client.get(url, {"q": "학생01"}).content.decode()
    assert "가상학생01" in html and "가상조교01" not in html
    alias = _m(student, competition).alias
    assert "가상학생01" in client.get(url, {"q": alias[-4:]}).content.decode()


# ── 학번 (관리자 화면에만, 담당 대회 범위 안에서만) ──────────────────
def _give_student_ids(student, ta):
    User.objects.filter(pk=student.pk).update(student_id="99990001")
    User.objects.filter(pk=ta.pk).update(student_id="99990901")


def test_회원_관리에_학번_열이_보이고_학번으로_검색한다(client, admin2, student, ta, competition):
    _give_student_ids(student, ta)
    client.force_login(admin2)
    url = reverse("competitions:members", args=[competition.pk])

    html = client.get(url).content.decode()
    assert "<th>학번</th>" in html and "99990001" in html and "99990901" in html
    html = client.get(url, {"q": "9999000"}).content.decode()       # 일부만 넣어도 (엑셀 필터처럼)
    assert "가상학생01" in html and "가상조교01" not in html


def test_학번이_빈_계정은_대시로_보인다(client, admin2, student, competition):
    client.force_login(admin2)
    html = client.get(reverse("competitions:members", args=[competition.pk])).content.decode()
    assert "<td>-</td>" in html


def test_관리자1은_다른_대회_회원을_학번으로_찾을_수_없다(client, ta, student, competition, other_competition, pw):
    """정책: 관리자1은 담당 대회 회원만. 다른 대회 회원 학번을 검색해도 결과에 나오지 않고, 다른 대회 화면은 403."""
    outsider = User.objects.create_user("mock_stu77", password=pw, real_name="가상학생77", student_id="99990077")
    CompetitionMember.objects.create(user=outsider, competition=other_competition)
    client.force_login(ta)

    html = client.get(reverse("competitions:members", args=[competition.pk]), {"q": "99990077"}).content.decode()
    assert "가상학생77" not in html and "맞는 회원이 없습니다" in html
    resp = client.get(reverse("competitions:members", args=[other_competition.pk]))
    assert resp.status_code in (403, 404)


def test_참여자는_회원_관리를_열_수_없다(client, student, competition):
    User.objects.filter(pk=student.pk).update(student_id="99990001")
    client.force_login(student)
    resp = client.get(reverse("competitions:members", args=[competition.pk]))
    assert resp.status_code == 403 and "99990001" not in resp.content.decode()

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import FailedAttempt, User
from competitions import services
from competitions.models import CompetitionMember

pytestmark = pytest.mark.django_db
URL = "/signup/"


def _student_exists():
    return User.objects.filter(username="mock_stu01").exists()


def test_가입_화면이_열린다(client):
    assert client.get(URL).status_code == 200


def test_정상_가입하면_참여자로_소속되고_로그인된다(client, competition, signup_data):
    resp = client.post(URL, signup_data)

    assert resp.status_code == 302 and resp.url == "/"
    user = User.objects.get(username="mock_stu01")
    assert user.password.startswith("argon2")          # 보안 B: argon2 저장
    assert user.is_admin2 is False
    member = CompetitionMember.objects.get(user=user)
    assert member.competition == competition
    assert member.role == CompetitionMember.Role.PARTICIPANT
    assert client.session["_auth_user_id"] == str(user.pk)


def test_코드없이_가입하면_실패한다(client, signup_data):        # R13
    signup_data["join_code"] = ""
    resp = client.post(URL, signup_data)

    assert resp.status_code == 200
    assert not _student_exists()


def test_틀린_코드면_실패하고_실패가_기록된다(client, signup_data):
    signup_data["join_code"] = "AAAA-AAAA"
    resp = client.post(URL, signup_data)

    assert "유효하지 않은 참여 코드" in resp.content.decode()
    assert not _student_exists()
    assert FailedAttempt.objects.filter(kind=FailedAttempt.Kind.JOIN_CODE).count() == 1


def test_코드는_소문자_하이픈_공백을_섞어도_된다(client, competition, signup_data):
    signup_data["join_code"] = f"  {competition.display_code().lower()} "
    assert client.post(URL, signup_data).status_code == 302


def test_폼에_없는_칸을_보내도_관리자가_될_수_없다(client, signup_data):   # mass assignment
    signup_data |= {"is_admin2": "True", "is_superuser": "True", "is_staff": "True", "role": "admin1"}
    client.post(URL, signup_data)

    user = User.objects.get(username="mock_stu01")
    assert not user.is_admin2 and not user.is_superuser and not user.is_staff
    assert user.memberships.get().role == CompetitionMember.Role.PARTICIPANT


def test_대소문자만_다른_아이디는_중복으로_본다(signup_data):
    Client().post(URL, signup_data)
    signup_data["username"] = "MOCK_STU01"
    resp = Client().post(URL, signup_data)

    assert "이미 사용 중인 로그인ID" in resp.content.decode()
    assert User.objects.filter(username__iexact="mock_stu01").count() == 1


def test_약한_비밀번호는_거부된다(client, signup_data):
    signup_data["password1"] = signup_data["password2"] = "abcd1234"
    resp = client.post(URL, signup_data)

    assert resp.status_code == 200
    assert not _student_exists()


def test_비밀번호_확인이_다르면_거부된다(client, signup_data):
    signup_data["password2"] = signup_data["password1"] + "x"
    resp = client.post(URL, signup_data)

    assert "비밀번호가 서로 다릅니다" in resp.content.decode()
    assert not _student_exists()


def test_소속_생성이_실패하면_사용자도_만들어지지_않는다(client, signup_data, monkeypatch):  # R13 트랜잭션
    def broken_add_member(*args, **kwargs):
        raise RuntimeError("가상 장애")

    monkeypatch.setattr(services, "add_member", broken_add_member)
    with pytest.raises(RuntimeError):
        client.post(URL, signup_data)

    assert not _student_exists()


def test_코드를_20번_틀리면_맞는_코드도_막힌다(client, signup_data):
    for i in range(20):
        client.post(URL, {**signup_data, "username": f"mock_try{i}", "join_code": "AAAA-AAAA"})
    resp = client.post(URL, signup_data)

    assert resp.status_code == 429
    assert not _student_exists()


def test_10분이_지나면_다시_시도할_수_있다(client, signup_data):
    for i in range(20):
        client.post(URL, {**signup_data, "username": f"mock_try{i}", "join_code": "AAAA-AAAA"})
    # 실패 기록을 11분 전으로 옮겨 '시간이 지난 상황'을 만든다
    FailedAttempt.objects.update(created_at=timezone.now() - timedelta(minutes=11))

    assert client.post(URL, signup_data).status_code == 302


def test_CSRF_토큰_없는_가입_요청은_거부된다(signup_data):
    # 기본 테스트 client는 CSRF 검사를 끄므로, 검사를 켠 client를 따로 쓴다
    resp = Client(enforce_csrf_checks=True).post(URL, signup_data)

    assert resp.status_code == 403
    assert not _student_exists()

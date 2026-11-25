import pytest
from django.core.management import CommandError, call_command

from accounts.models import User
from audit.models import AuditLog

pytestmark = pytest.mark.django_db


def _type_passwords(monkeypatch, *values):
    """getpass(화면에 안 보이는 입력)에 미리 정한 값을 차례로 넣는다."""
    answers = iter(values)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))


def test_관리자2_계정을_만든다(monkeypatch, pw):
    _type_passwords(monkeypatch, pw, pw)
    call_command("create_admin2", username="mock_prof09", real_name="가상교수09")

    user = User.objects.get(username="mock_prof09")
    assert user.is_admin2
    assert user.password.startswith("argon2")
    log = AuditLog.objects.get(action=AuditLog.Action.ADMIN2_CREATED)
    assert log.actor is None and log.target_user == user   # 수행자 없음 = 서버 명령


def test_이미_있는_아이디는_관리자2로_바꾸지_않는다(monkeypatch, student, pw):
    _type_passwords(monkeypatch, pw, pw)
    with pytest.raises(CommandError):
        call_command("create_admin2", username="MOCK_STU01", real_name="가상학생01")

    student.refresh_from_db()
    assert not student.is_admin2


def test_약한_비밀번호는_거부된다(monkeypatch):
    _type_passwords(monkeypatch, "abcd1234", "abcd1234")
    with pytest.raises(CommandError):
        call_command("create_admin2", username="mock_prof09", real_name="가상교수09")

    assert not User.objects.filter(username="mock_prof09").exists()


def test_비밀번호_확인이_다르면_거부된다(monkeypatch, pw):
    _type_passwords(monkeypatch, pw, pw + "x")
    with pytest.raises(CommandError):
        call_command("create_admin2", username="mock_prof09", real_name="가상교수09")

    assert not User.objects.filter(username="mock_prof09").exists()

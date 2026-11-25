"""가입 폼의 학번 칸: 숫자 N자리, 중복 금지, 비밀번호와 비슷하면 거부, 서버 생성 계정은 비워 둘 수 있음."""
import pytest
from django.db import IntegrityError

from accounts.models import User

pytestmark = pytest.mark.django_db
URL = "/signup/"


def test_정상_가입하면_학번이_저장된다(client, signup_data):
    resp = client.post(URL, signup_data)
    assert resp.status_code == 302
    assert User.objects.get(username="mock_stu01").student_id == "99990001"


def test_가입_화면에_학번_칸이_있다(client):
    html = client.get(URL).content.decode()
    assert 'name="student_id"' in html and "숫자 8자리" in html


@pytest.mark.parametrize("bad", [
    "",            # 비어 있음
    "9999000",     # 7자리
    "999900011",   # 9자리
    "9999000a",    # 문자 섞임
    "９９９９０００１",  # 전각 숫자(\d였다면 통과했을 값)
])
def test_형식이_틀린_학번은_거부한다(client, signup_data, bad):
    signup_data["student_id"] = bad
    resp = client.post(URL, signup_data)
    assert resp.status_code == 200
    assert not User.objects.filter(username="mock_stu01").exists()


def test_앞뒤_공백은_지우고_받는다(client, signup_data):
    signup_data["student_id"] = " 99990001 "
    assert client.post(URL, signup_data).status_code == 302
    assert User.objects.get(username="mock_stu01").student_id == "99990001"


def test_이미_가입한_학번은_거부한다(client, signup_data, pw):
    User.objects.create_user("mock_stu99", password=pw, real_name="가상학생99", student_id="99990001")
    resp = client.post(URL, signup_data)
    assert resp.status_code == 200
    assert "이미 가입한 학번" in resp.content.decode()


def test_자릿수는_설정으로_바꾼다(client, signup_data, settings):
    settings.STUDENT_ID_DIGITS = 10
    assert client.post(URL, signup_data).status_code == 200      # 8자리 → 거부
    signup_data["student_id"] = "9999000001"
    assert client.post(URL, signup_data).status_code == 302      # 10자리 → 통과


def test_학번과_비슷한_비밀번호는_거부한다(client, signup_data):
    signup_data["password1"] = signup_data["password2"] = "99990001aa"
    resp = client.post(URL, signup_data)
    assert resp.status_code == 200
    assert not User.objects.filter(username="mock_stu01").exists()


def test_서버_생성_계정은_학번을_비워_둘_수_있고_여럿이어도_된다(pw):
    User.objects.create_user("mock_prof98", password=pw, real_name="가상교수98", is_admin2=True)
    User.objects.create_user("mock_prof99", password=pw, real_name="가상교수99", is_admin2=True)
    assert User.objects.filter(student_id="").count() == 2


def test_DB도_같은_학번을_막는다(pw):
    User.objects.create_user("mock_stu98", password=pw, real_name="가상학생98", student_id="99990098")
    with pytest.raises(IntegrityError):
        User.objects.create_user("mock_stu99", password=pw, real_name="가상학생99", student_id="99990098")


def test_동시_가입으로_DB에서_걸리면_500이_아니라_폼_오류(client, signup_data, monkeypatch):
    # 폼 검사는 통과했는데 저장 직전에 다른 사람이 같은 학번으로 가입한 상황을 흉내 낸다
    def fake_create_user(*args, **kwargs):
        raise IntegrityError("uniq_student_id_when_set")
    monkeypatch.setattr(User.objects, "create_user", fake_create_user)
    resp = client.post(URL, signup_data)
    assert resp.status_code == 200
    assert "방금 같은 학번이나 로그인ID로" in resp.content.decode()


def test_요청에_섞인_다른_칸은_무시한다(client, signup_data):
    signup_data["is_admin2"] = "True"
    client.post(URL, signup_data)
    assert User.objects.get(username="mock_stu01").is_admin2 is False

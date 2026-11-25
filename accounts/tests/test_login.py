import pytest

from accounts.models import FailedAttempt

pytestmark = pytest.mark.django_db
URL = "/login/"
WRONG = "틀린비밀번호-xxxxxxxx"


def _logged_in(client):
    return "_auth_user_id" in client.session


def test_정상_로그인(client, student, pw):
    resp = client.post(URL, {"username": "mock_stu01", "password": pw})

    assert resp.status_code == 302
    assert _logged_in(client)


def test_없는_아이디와_틀린_비밀번호의_오류_메시지가_같다(client, student):
    r1 = client.post(URL, {"username": "mock_nobody", "password": WRONG})
    r2 = client.post(URL, {"username": "mock_stu01", "password": WRONG})

    errors1 = list(r1.context["form"].non_field_errors())
    errors2 = list(r2.context["form"].non_field_errors())
    assert errors1 and errors1 == errors2


def test_비밀번호를_5번_틀리면_맞는_비밀번호도_막힌다(client, student, pw):
    for _ in range(5):
        client.post(URL, {"username": "mock_stu01", "password": WRONG})
    resp = client.post(URL, {"username": "mock_stu01", "password": pw})

    assert resp.status_code == 429
    assert not _logged_in(client)


def test_없는_아이디도_똑같이_잠긴다(client):
    for _ in range(5):
        client.post(URL, {"username": "mock_nobody", "password": WRONG})

    assert client.post(URL, {"username": "mock_nobody", "password": WRONG}).status_code == 429


def test_한_계정이_잠겨도_다른_계정은_로그인된다(client, student, pw):
    for _ in range(5):
        client.post(URL, {"username": "mock_other", "password": WRONG})

    assert client.post(URL, {"username": "mock_stu01", "password": pw}).status_code == 302


def test_로그인에_성공하면_실패_기록이_지워진다(client, student, pw):
    for _ in range(3):
        client.post(URL, {"username": "mock_stu01", "password": WRONG})
    client.post(URL, {"username": "mock_stu01", "password": pw})

    assert FailedAttempt.objects.filter(kind=FailedAttempt.Kind.LOGIN).count() == 0


def test_로그인하면_세션_ID가_바뀐다(client, student, pw):   # 세션 고정 공격 방지
    before = client.session.session_key
    client.post(URL, {"username": "mock_stu01", "password": pw})

    assert client.session.session_key != before


def test_외부_주소로는_돌려보내지_않는다(client, student, pw):  # 오픈 리다이렉트 방지
    resp = client.post(URL, {"username": "mock_stu01", "password": pw, "next": "https://evil.example/"})

    assert resp.status_code == 302
    assert resp.url == "/"


def test_로그아웃은_GET으로_안_되고_POST로만_된다(client, student):
    client.force_login(student)

    assert client.get("/logout/").status_code == 405
    assert _logged_in(client)
    assert client.post("/logout/").status_code == 302
    assert not _logged_in(client)


def test_로그인_안_하면_홈에서_로그인_화면으로_보낸다(client):
    resp = client.get("/")

    assert resp.status_code == 302
    assert resp.url.startswith("/login/")

import pytest

from accounts.models import FailedAttempt

pytestmark = pytest.mark.django_db
URL = "/join/"


@pytest.fixture
def logged_in(client, student):
    client.force_login(student)
    return client


def test_기존_회원이_코드로_대회를_추가한다(logged_in, student, other_competition):
    resp = logged_in.post(URL, {"join_code": other_competition.display_code()})

    assert resp.status_code == 302
    assert student.memberships.count() == 2


def test_대회마다_익명_별칭이_따로_생긴다(logged_in, student, other_competition):
    logged_in.post(URL, {"join_code": other_competition.display_code()})

    aliases = list(student.memberships.values_list("alias", flat=True))
    assert len(aliases) == 2 and aliases[0] != aliases[1]


def test_이미_참여한_대회_코드는_안내만_하고_늘지_않는다(logged_in, student, competition):
    resp = logged_in.post(URL, {"join_code": competition.display_code()})

    assert "이미 참여 중인 대회" in resp.content.decode()
    assert student.memberships.count() == 1
    assert FailedAttempt.objects.count() == 0   # 실패로 세지 않음


def test_틀린_코드를_20번_넣으면_막힌다(logged_in, student, other_competition):
    for _ in range(20):
        logged_in.post(URL, {"join_code": "AAAA-AAAA"})
    resp = logged_in.post(URL, {"join_code": other_competition.display_code()})

    assert resp.status_code == 429
    assert student.memberships.count() == 1


def test_관리자2는_코드로_참여하지_않는다(client, admin2, competition):
    client.force_login(admin2)
    client.post(URL, {"join_code": competition.display_code()})

    assert admin2.memberships.count() == 0


def test_로그인_안_하면_대회를_추가할_수_없다(client, competition):
    resp = client.post(URL, {"join_code": competition.display_code()})

    assert resp.status_code == 302
    assert resp.url.startswith("/login/")

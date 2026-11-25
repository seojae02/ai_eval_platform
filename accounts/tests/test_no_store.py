"""로그인한 사용자의 화면은 브라우저 캐시에 남기지 않는다(NoStoreForLoggedInMiddleware)."""
import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory

from accounts.middleware import NoStoreForLoggedInMiddleware

pytestmark = pytest.mark.django_db


def test_로그인_화면은_Django가_붙인_값을_그대로_둔다(client):
    # Django LoginView는 원래 never_cache가 적용되어 있다. 미들웨어는 비로그인이라 손대지 않는다.
    resp = client.get("/login/")
    assert resp.status_code == 200
    assert "no-store" in resp["Cache-Control"] and resp["Cache-Control"] != "private, no-store"


def test_관리자2가_본_리더보드에는_no_store가_붙는다(client, admin2, project):
    client.force_login(admin2)
    resp = client.get(f"/competitions/{project.competition_id}/projects/{project.pk}/leaderboard/")
    assert resp.status_code == 200
    assert resp["Cache-Control"] == "private, no-store"


def test_참여자가_본_과제_화면에도_붙는다(client, student, project):
    client.force_login(student)
    resp = client.get(f"/competitions/{project.competition_id}/projects/{project.pk}/")
    assert resp.status_code == 200
    assert resp["Cache-Control"] == "private, no-store"


def test_뷰가_이미_정한_값은_덮어쓰지_않는다(student):
    request = RequestFactory().get("/")
    request.user = student
    middleware = NoStoreForLoggedInMiddleware(lambda r: _response_with("max-age=60"))
    assert middleware(request)["Cache-Control"] == "max-age=60"


def test_비로그인_응답에는_붙이지_않는다():
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    middleware = NoStoreForLoggedInMiddleware(lambda r: HttpResponse("ok"))
    assert "Cache-Control" not in middleware(request)


def _response_with(value):
    response = HttpResponse("ok")
    response["Cache-Control"] = value
    return response

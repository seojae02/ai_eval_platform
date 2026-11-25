"""화면 공통 요소: 온점 제거 필터, 과제 탭(참여자에게는 관리자 탭 링크가 아예 없다)."""
import pytest
from django.urls import reverse

from competitions.templatetags.ui import nodot

pytestmark = pytest.mark.django_db


def test_nodot은_끝_온점을_떼고_문장_사이는_줄바꿈():
    assert nodot("접수했습니다.") == "접수했습니다"
    assert nodot("비밀번호가 너무 짧습니다. 최소 10 문자를 포함해야 합니다. ") == "비밀번호가 너무 짧습니다<br>최소 10 문자를 포함해야 합니다"
    assert nodot("확인 중...") == "확인 중..."             # 말줄임표는 그대로
    assert nodot("일치율 0.95 미만") == "일치율 0.95 미만" and nodot(None) == ""
    assert nodot("sklearn.pipeline.Pipeline 예측") == "sklearn.pipeline.Pipeline 예측"
    # 조각마다 이스케이프: 사용자 입력이 섞여도 HTML로 실행되지 않는다
    assert nodot("<b>가상</b>입니다. 다음.") == "&lt;b&gt;가상&lt;/b&gt;입니다<br>다음"


def test_화면에_온점_없이_나오는_서버_문구(client, signup_data, competition):
    # Django 기본 비밀번호 규칙 문구(두 문장)도 줄바꿈으로 나뉜다
    html = client.post(reverse("signup"), {**signup_data, "password1": "short", "password2": "short"}).content.decode()
    assert "짧습니다." not in html and "짧습니다<br>" in html
    html = client.post(reverse("login"), {"username": "mock_nobody", "password": "x"}).content.decode()
    assert "아이디나 비밀번호가 맞지 않습니다(대문자와 소문자를 구별합니다)" in html


def test_과제_탭은_역할에_맞게_보인다(client, admin2, student, project):
    url = reverse("competitions:project_detail", args=[project.competition_id, project.pk])
    review = reverse("submissions:review_queue", args=[project.competition_id, project.pk])
    results = reverse("submissions:results", args=[project.competition_id, project.pk])

    client.force_login(student)
    html = client.get(url).content.decode()
    assert 'class="tabs"' in html and 'class="active" aria-current="page">개요' in html
    assert review not in html and results not in html               # 참여자: 관리자 탭 링크 없음

    client.force_login(admin2)
    html = client.get(url).content.decode()
    assert review in html and results in html

"""템플릿 문법이 화면에 글자로 새어 나오지 않는지 확인한다.

Django의 {# #}는 한 줄 주석이다. 여러 줄에 걸쳐 쓰면 주석이 아니라 글자로 그려진다
(2026-11-23 _live.html에서 실제로 학생 리더보드에 주석 문장이 보였다). 여러 줄은 {% comment %}를 쓴다.
"""
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse


def _templates():
    for base in settings.TEMPLATES[0]["DIRS"]:
        yield from Path(base).rglob("*.html")


def test_여러_줄_한줄주석이_있는_템플릿이_없다():
    bad = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\{#(.*?)#\}", text, re.S):
            if "\n" in m.group(1):
                bad.append(f"{path.name}:{text[:m.start()].count(chr(10)) + 1}")
        # 닫히지 않은 {# (같은 줄에 #}가 없음)
        for no, line in enumerate(text.splitlines(), 1):
            if "{#" in line and "#}" not in line[line.index("{#"):]:
                bad.append(f"{path.name}:{no}")
    assert not bad, f"여러 줄 {{# #}} 주석은 {{% comment %}}로 바꾸세요: {sorted(set(bad))}"


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["competitions:project_detail", "submissions:leaderboard"])
def test_참여자_화면에_템플릿_주석이_보이지_않는다(client, student, project_with_files, url_name):
    client.force_login(student)
    html = client.get(reverse(url_name, args=[project_with_files.competition_id, project_with_files.pk])).content.decode()
    assert "{#" not in html and "#}" not in html

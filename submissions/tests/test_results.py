"""최종 성적 화면·CSV (RFP 2·6쪽: 대표 기록과 최종 리더보드(성적) 확정)."""
import csv
import io

import pytest
from django.urls import reverse

from accounts.models import User
from audit.models import AuditLog
from competitions import services as competition_services
from competitions.models import CompetitionMember
from submissions import results, review
from submissions.models import Submission

from .conftest import kst

pytestmark = pytest.mark.django_db
S, RS = Submission.Status, Submission.ReviewStatus
T0 = kst(11, 19, 10)


@pytest.fixture
def people(competition):
    members = {}
    for i in range(1, 6):
        user = User.objects.create_user(f"mock_stu0{i}", real_name=f"가상학생0{i}")
        members[i] = competition_services.add_member(user, competition)
    return members


def _scored(member, project, score):
    return Submission.objects.create(project=project, member=member, status=S.SCORED, score=score,
                                     started_at=T0, last_received_at=T0, scored_at=T0)


def _decide(sub, admin2, verdict, reason=""):
    review.decide(sub, admin2, verdict, reason=reason, seen=review.latest_review_id(sub))


def _ta(competition):
    user = User.objects.create_user("mock_ta01", real_name="가상조교01")
    return CompetitionMember.objects.create(user=user, competition=competition, role=CompetitionMember.Role.ADMIN1)


def test_확정_검토중_미검토와_리더보드에_없는_이유(people, project, admin2, competition):
    s1 = _scored(people[1], project, 0.96)
    s2 = _scored(people[2], project, 0.94)
    _scored(people[3], project, 0.93)
    s4 = _scored(people[4], project, 0.99)                       # 유일한 기록이 부적합 확정 → 리더보드에서 빠짐
    _decide(s1, admin2, "ok")
    review.propose(s2, _ta(competition).user, "reject", reason="CSV가 노트북 출력과 다름", seen=None)
    _decide(s4, admin2, "reject", reason="정답 직접 입력")
    # 가상학생05: 제출 없음

    rows, missing, summary = results.build(project)
    assert [(r.member.user.real_name, r.state) for r in rows] == [
        ("가상학생01", "확정"), ("가상학생02", "검토 중"), ("가상학생03", "미검토")]
    assert rows[0].decision.actor == admin2 and rows[1].decision is None
    assert [(m.user.real_name, why) for m, why in missing] == [
        ("가상학생04", "모든 기록 부적합(확정)"), ("가상학생05", "채점된 기록 없음")]
    assert summary == {"confirmed": 1, "in_review": 1, "not_reviewed": 1, "total": 3, "missing": 2, "final": False}


def test_모두_확정되면_최종_확정_재검토하면_다시_아님(people, project, admin2, client):
    subs = [_scored(people[i], project, 0.9 - i / 100) for i in (1, 2)]
    url = reverse("submissions:leaderboard", args=[project.competition_id, project.pk])
    client.force_login(people[1].user)
    assert "최종 확정" not in client.get(url).content.decode()

    for s in subs:
        _decide(s, admin2, "ok")
    assert results.build(project)[2]["final"] is True
    assert "최종 확정" in client.get(url).content.decode()      # 참여자 리더보드에도 표시

    review.reopen(subs[0], admin2, "재확인 필요", seen=review.latest_review_id(subs[0]))
    rows, _, summary = results.build(project)
    assert summary["final"] is False and rows[0].state == "미검토" and rows[0].decision is None


def test_성적표_CSV는_엑셀용_BOM_수식_차단_감사_기록(people, project, admin2, client):
    people[1].user.real_name = "=HYPERLINK(\"http://x\")"           # 가입 때 수식을 이름으로 넣은 경우
    people[1].user.save()
    s1 = _scored(people[1], project, 0.96)
    _scored(people[2], project, 0.94)
    _decide(s1, admin2, "ok")

    client.force_login(admin2)
    resp = client.get(reverse("submissions:results_csv", args=[project.competition_id, project.pk]))
    assert resp.status_code == 200 and resp["Cache-Control"] == "private, no-store"
    assert resp["Content-Disposition"].startswith("attachment;")
    assert resp.content.startswith(b"\xef\xbb\xbf")                 # BOM: 엑셀이 한글을 바로 읽는다

    table = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert table[0] == results.HEADER
    assert table[1][:6] == ["1", people[1].alias, "'=HYPERLINK(\"http://x\")", "mock_stu01", "0.9600", "확정"]
    assert table[1][9] == admin2.real_name and table[2][5] == "미검토"
    assert [r[5] for r in table[3:]] == ["채점된 기록 없음"] * 3       # 가상학생03~05

    log = AuditLog.objects.get(action=AuditLog.Action.RESULTS_EXPORTED)
    assert log.actor == admin2 and log.detail == {"project_id": project.pk, "rows": 2, "final": False}


def test_최종_성적_화면(people, project, admin2, client):
    assert results.build(project)[2]["final"] is False             # 채점된 기록이 없으면 '최종 확정'이 아니다
    _scored(people[1], project, 0.96)
    client.force_login(admin2)
    html = client.get(reverse("submissions:results", args=[project.competition_id, project.pk])).content.decode()
    assert "가상학생01" in html and "미검토 1명" in html and "성적표 CSV 다운로드" in html

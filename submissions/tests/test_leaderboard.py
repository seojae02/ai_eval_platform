"""리더보드 R7(참여자별 최고점 1개, 공동 순위, 미검증 포함)과 R8(익명/실명)."""
from datetime import timedelta

import pytest
from django.urls import reverse

from accounts.models import User
from competitions import services as competition_services
from competitions.models import CompetitionMember, Project
from submissions import leaderboard
from submissions.models import Submission, display_score

from .conftest import kst

pytestmark = pytest.mark.django_db
S = Submission.Status
T0 = kst(11, 19, 10)


@pytest.fixture
def people(competition):
    """가상학생01~04. 별칭은 무작위이므로 테스트에서는 실명으로 찾는다."""
    members = {}
    for i in range(1, 5):
        user = User.objects.create_user(f"mock_stu0{i}", real_name=f"가상학생0{i}")
        members[i] = competition_services.add_member(user, competition)
    return members


def _scored(member, project, score, minutes=0, status=S.SCORED):
    at = T0 + timedelta(minutes=minutes)
    return Submission.objects.create(
        project=project, member=member, status=status, score=score,
        started_at=at, last_received_at=at, scored_at=at if status == S.SCORED else None,
    )


def _board(project):
    return [(e.rank, e.member.user.real_name, e.score_text) for e in leaderboard.build(project)]


# ── R7 ─────────────────────────────────────────────────────────
def test_R7_예시_0_93_0_91_0_91_0_88은_1_2_2_4(people, project):
    _scored(people[1], project, 0.88)
    _scored(people[1], project, 0.93, minutes=1)          # 같은 사람 두 번 → 최고점 0.93만
    _scored(people[2], project, 0.91)
    _scored(people[3], project, 0.85)
    _scored(people[3], project, 0.91, minutes=2)
    _scored(people[4], project, 0.88)

    assert _board(project) == [
        (1, "가상학생01", "0.9300"),
        (2, "가상학생02", "0.9100"),                        # 공동 2위: 먼저 채점된 사람이 위에 표시
        (2, "가상학생03", "0.9100"),
        (4, "가상학생04", "0.8800"),                        # 3위 없이 4위
    ]


def test_assign_ranks_전원_동점과_연속_동점():
    assert leaderboard.assign_ranks(["0.5000"] * 3) == [1, 1, 1]
    assert leaderboard.assign_ranks(["0.9", "0.8", "0.8", "0.8", "0.7"]) == [1, 2, 2, 2, 5]
    assert leaderboard.assign_ranks([]) == []


def test_RMSE는_작은_값이_1위이고_대표_기록도_가장_작은_값(people, project):
    project.metric, project.direction = Project.Metric.RMSE, Project.Direction.LOWER
    project.save()
    _scored(people[1], project, 3.2)
    _scored(people[1], project, 1.5, minutes=1)
    _scored(people[2], project, 2.0)

    assert _board(project) == [(1, "가상학생01", "1.5000"), (2, "가상학생02", "2.0000")]


def test_채점된_제출만_올라간다(people, project):
    _scored(people[1], project, 0.7)
    for i, status in [(2, S.QUEUED), (3, S.ERROR), (4, S.LATE)]:
        _scored(people[i], project, 0.99 if status != S.QUEUED else None, status=status)

    assert _board(project) == [(1, "가상학생01", "0.7000")]


def test_마감_구간이_달라도_합쳐서_최고점을_고른다(people, project_with_files, deadlines):
    first, second = deadlines
    a = _scored(people[1], project_with_files, 0.6)
    b = _scored(people[1], project_with_files, 0.8, minutes=1)
    Submission.objects.filter(pk=a.pk).update(deadline=first)
    Submission.objects.filter(pk=b.pk).update(deadline=second)

    assert _board(project_with_files) == [(1, "가상학생01", "0.8000")]


def test_같은_사람의_같은_점수는_나중_제출이_대표_기록(people, project):
    _scored(people[1], project, 0.91)
    later = _scored(people[1], project, 0.91, minutes=5)

    (entry,) = leaderboard.build(project)
    assert entry.submission == later and entry.submission.public_review_label == "검증 대기"


def test_A안_화면_4자리가_같으면_공동_순위(people, project):
    _scored(people[1], project, 0.912349)
    _scored(people[2], project, 0.912345)
    _scored(people[3], project, 0.91225)                  # 0.9123으로 올림(사사오입)

    assert [r for r, _, _ in _board(project)] == [1, 1, 1]


def test_display_score는_사사오입():
    assert display_score(0.90025) == "0.9003"             # round(0.90025, 4)는 0.9002가 나온다
    assert display_score(0.00015) == "0.0002"             # round(0.00015, 4)는 0.0001
    assert display_score(0.8) == "0.8000"
    assert display_score(1) == "1.0000"


# ── R8: 화면 ───────────────────────────────────────────────────
@pytest.fixture
def board_url(people, project):
    _scored(people[1], project, 0.9)
    _scored(people[2], project, 0.8)
    return reverse("submissions:leaderboard", args=[project.competition_id, project.pk])


def test_R8_참여자_화면에는_별칭만_있고_실명과_아이디는_응답에_없다(client, people, board_url):
    client.force_login(people[1].user)
    html = client.get(board_url).content.decode()

    assert people[2].alias in html
    assert "가상학생02" not in html and "mock_stu02" not in html
    assert "(나)" in html                                  # 내 줄 표시


def test_R8_담당_관리자1과_관리자2_화면에는_실명이_보인다(client, admin2, competition, people, board_url):
    a1 = User.objects.create_user("mock_a1", real_name="가상조교01")
    CompetitionMember.objects.create(user=a1, competition=competition, role=CompetitionMember.Role.ADMIN1)

    for user in (a1, admin2):
        client.force_login(user)
        html = client.get(board_url).content.decode()
        assert "가상학생02" in html and "mock_stu02" in html and people[2].alias in html


def test_채점된_제출이_없으면_안내_문구(client, student, project):
    client.force_login(student)
    resp = client.get(reverse("submissions:leaderboard", args=[project.competition_id, project.pk]))
    assert "아직 채점된 제출이 없습니다" in resp.content.decode()

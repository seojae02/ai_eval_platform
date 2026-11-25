"""권한 매트릭스 테스트: (주소 × 역할) 모든 칸을 자동으로 확인한다.

새 화면을 만들면 MATRIX에 한 줄을 추가한다. 심사에서 "권한 검사를 빠뜨린 화면은 없나요?"에 대한 근거.
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from competitions.models import CompetitionMember, Deadline
from competitions import storage
from submissions.models import Submission, SubmissionFile

pytestmark = pytest.mark.django_db

LOGIN = "로그인으로"   # 비로그인 → 로그인 화면으로 이동(302)
ROLES = ["비로그인", "참여자", "타대회_참여자", "관리자1", "타대회_관리자1", "해제된_관리자1", "관리자2"]

#  주소                  비로그인  참여자  타대회참여자  관리자1  타대회관리자1  해제된관리자1  관리자2
MATRIX = {
    ("GET", "create"):   [LOGIN,   403,    403,         403,     403,          403,          200],
    ("GET", "detail"):   [LOGIN,   200,    404,         200,     404,          404,          200],
    ("GET", "members"):  [LOGIN,   403,    404,         200,     404,          404,          200],
    ("POST", "reissue"): [LOGIN,   403,    404,         403,     404,          404,          302],
    ("POST", "promote"): [LOGIN,   403,    404,         403,     404,          404,          302],
    ("POST", "revoke"):  [LOGIN,   403,    404,         403,     404,          404,          302],
    # ── 상태 주소·진행 화면 ── 채점 상태는 소속이면(본인 것만 응답), 검증 상태·진행 화면은 관리자만
    ("GET", "live_status"):            [LOGIN, 200, 404, 200, 404, 404, 200],
    ("GET", "live_status_verify"):     [LOGIN, 403, 404, 200, 404, 404, 200],
    ("GET", "review_progress"):        [LOGIN, 403, 404, 200, 404, 404, 200],
    # ── 회원 관리 (대상: 이 대회의 순수 참여자) ── 정지·해제는 관리자2만, 나머지는 관리자1도
    ("POST", "member_reset_password"): [LOGIN, 403, 404, 200, 404, 404, 200],
    ("POST", "member_unlock"):         [LOGIN, 403, 404, 302, 404, 404, 302],
    ("POST", "member_suspend"):        [LOGIN, 403, 404, 403, 404, 404, 302],
    ("POST", "member_reinstate"):      [LOGIN, 403, 404, 403, 404, 404, 302],
    # ── 프로젝트 (나 회차) ──
    ("GET", "project_create"):    [LOGIN, 403, 404, 403, 404, 404, 200],
    ("GET", "project_detail"):    [LOGIN, 200, 404, 200, 404, 404, 200],
    ("GET", "project_edit"):      [LOGIN, 403, 404, 403, 404, 404, 200],
    ("POST", "deadline_add"):     [LOGIN, 403, 404, 403, 404, 404, 302],
    ("POST", "deadline_delete"):  [LOGIN, 403, 404, 403, 404, 404, 302],
    ("POST", "file_upload"):      [LOGIN, 403, 404, 403, 404, 404, 302],
    ("GET", "answer_download"):   [LOGIN, 403, 404, 200, 404, 404, 200],   # R9: 정답은 관리자만
    ("GET", "features_download"): [LOGIN, 200, 404, 200, 404, 404, 200],
    ("GET", "sample"):            [LOGIN, 200, 404, 200, 404, 404, 200],
    # ── 제출 ── 시작은 참여자만(관리자는 403), 남의 제출은 누구에게나 '없는 제출'(404)
    ("POST", "submission_start"):    [LOGIN, 302, 404, 403, 404, 404, 403],
    ("GET", "others_upload"):        [LOGIN, 404, 404, 404, 404, 404, 404],
    ("POST", "others_chunk"):        [LOGIN, 404, 404, 404, 404, 404, 404],
    ("POST", "others_complete"):     [LOGIN, 404, 404, 404, 404, 404, 404],
    ("POST", "others_cancel"):       [LOGIN, 404, 404, 404, 404, 404, 404],
    # ── 리더보드 (R8) ── 대회 소속은 모두 볼 수 있다. 실명 노출 여부는 test_leaderboard.py가 확인
    ("GET", "leaderboard"):          [LOGIN, 200, 404, 200, 404, 404, 200],
    # ── 검토 (R10~R12) ── 보기는 담당 관리자1·관리자2, 제안은 관리자1만, 결정·반려·재검토는 관리자2만
    ("GET", "admin_list"):           [LOGIN, 403, 404, 200, 404, 404, 200],
    ("GET", "review_queue"):         [LOGIN, 403, 404, 200, 404, 404, 200],
    ("GET", "results"):              [LOGIN, 403, 404, 200, 404, 404, 200],   # 최종 성적(실명)
    ("GET", "results_csv"):          [LOGIN, 403, 404, 200, 404, 404, 200],
    ("GET", "review_detail"):        [LOGIN, 403, 404, 200, 404, 404, 200],
    ("GET", "review_file"):          [LOGIN, 403, 404, 200, 404, 404, 200],
    ("POST", "review_propose"):      [LOGIN, 403, 404, 302, 404, 404, 403],
    ("POST", "review_decide"):       [LOGIN, 403, 404, 403, 404, 404, 302],
    ("POST", "review_send_back"):    [LOGIN, 403, 404, 403, 404, 404, 302],
    ("POST", "review_reopen"):       [LOGIN, 403, 404, 403, 404, 404, 302],
    ("POST", "review_bulk_approve"): [LOGIN, 403, 404, 403, 404, 404, 302],   # 일괄 확정은 관리자2만
    # ── 검증 (V1~V5) ── 노트북 보기·검증 다시 실행은 담당 관리자1·관리자2
    ("GET", "review_notebook"):      [LOGIN, 403, 404, 200, 404, 404, 200],
    ("POST", "review_reverify"):     [LOGIN, 403, 404, 302, 404, 404, 302],
}

CASES = [
    (method, name, role, expected)
    for (method, name), row in MATRIX.items()
    for role, expected in zip(ROLES, row)
]


def _member(username, competition, role, is_active=True):
    # 비밀번호 없이 만든다(해시 계산을 건너뛰어 테스트가 빨라짐). 로그인은 force_login으로 한다.
    user = User.objects.create_user(username, real_name=username)
    return CompetitionMember.objects.create(user=user, competition=competition, role=role, is_active=is_active)


@pytest.fixture
def login_as(client, admin2, competition, other_competition):
    P, A1 = CompetitionMember.Role.PARTICIPANT, CompetitionMember.Role.ADMIN1
    setup = {
        "참여자": (competition, P, True),
        "타대회_참여자": (other_competition, P, True),
        "관리자1": (competition, A1, True),
        "타대회_관리자1": (other_competition, A1, True),
        "해제된_관리자1": (competition, A1, False),
    }

    def _login(role):
        if role == "관리자2":
            client.force_login(admin2)
        elif role in setup:
            comp, r, active = setup[role]
            client.force_login(_member(f"mock_{role}", comp, r, active).user)
        return client

    return _login


@pytest.fixture
def urls(competition, project_with_files):
    target_participant = _member("mock_target_p", competition, CompetitionMember.Role.PARTICIPANT)
    target_admin1 = _member("mock_target_a1", competition, CompetitionMember.Role.ADMIN1)
    deadline = Deadline.objects.create(
        project=project_with_files, due_at=timezone.now() + timedelta(days=3), max_submissions=3
    )
    c, p = competition.pk, project_with_files.pk
    # 다른 참여자(mock_target_p)의 업로드 중 제출
    others = Submission.objects.create(
        project=project_with_files, member=target_participant, deadline=deadline,
        started_at=timezone.now(), last_received_at=timezone.now(),
    )
    s = [c, p, others.pk]
    SubmissionFile.objects.create(submission=others, kind="csv", original_name="pred.csv",
                                  stored_name=storage.save_bytes("submissions", b"id,label\n1,0\n"))
    SubmissionFile.objects.create(submission=others, kind="notebook", original_name="w.ipynb",
                                  stored_name=storage.save_bytes("submissions", b'{"cells": []}', ext="ipynb"))
    return {
        "submission_start": reverse("submissions:start", args=[c, p]),
        "leaderboard": reverse("submissions:leaderboard", args=[c, p]),
        "admin_list": reverse("submissions:admin_list", args=[c, p]),
        "review_queue": reverse("submissions:review_queue", args=[c, p]),
        "results": reverse("submissions:results", args=[c, p]),
        "results_csv": reverse("submissions:results_csv", args=[c, p]),
        "review_detail": reverse("submissions:review_detail", args=s),
        "review_file": reverse("submissions:review_file", args=[*s, "csv"]),
        "review_propose": reverse("submissions:review_propose", args=s),
        "review_decide": reverse("submissions:review_decide", args=s),
        "review_send_back": reverse("submissions:review_send_back", args=s),
        "review_reopen": reverse("submissions:review_reopen", args=s),
        "review_bulk_approve": reverse("submissions:review_bulk_approve", args=[c, p]),
        "review_notebook": reverse("submissions:review_notebook", args=s),
        "review_reverify": reverse("submissions:review_reverify", args=s),
        "others_upload": reverse("submissions:upload", args=s),
        "others_chunk": reverse("submissions:chunk", args=s) + "?kind=csv&offset=0&name=a.csv",
        "others_complete": reverse("submissions:complete", args=s),
        "others_cancel": reverse("submissions:cancel", args=s),
        "project_create": reverse("competitions:project_create", args=[c]),
        "project_detail": reverse("competitions:project_detail", args=[c, p]),
        "project_edit": reverse("competitions:project_edit", args=[c, p]),
        "deadline_add": reverse("competitions:deadline_add", args=[c, p]),
        "deadline_delete": reverse("competitions:deadline_delete", args=[c, p, deadline.pk]),
        "file_upload": reverse("competitions:file_upload", args=[c, p]),
        "answer_download": reverse("competitions:file_download", args=[c, p, "answer"]),
        "features_download": reverse("competitions:file_download", args=[c, p, "test_features"]),
        "sample": reverse("competitions:sample_csv", args=[c, p]),
        "create": reverse("competitions:create"),
        "detail": reverse("competitions:detail", args=[c]),
        "members": reverse("competitions:members", args=[c]),
        "reissue": reverse("competitions:reissue_code", args=[c]),
        "promote": reverse("competitions:promote", args=[c, target_participant.pk]),
        "revoke": reverse("competitions:revoke", args=[c, target_admin1.pk]),
        "live_status": reverse("submissions:status", args=[c, p]) + f"?ids={others.pk}",
        "live_status_verify": reverse("submissions:status", args=[c, p]) + f"?watch=verify&ids={others.pk}",
        "review_progress": reverse("submissions:review_progress", args=s),
        **{name: reverse(f"competitions:{name}", args=[c, target_participant.pk])
           for name in ("member_reset_password", "member_unlock", "member_suspend", "member_reinstate")},
    }


@pytest.mark.parametrize("method,name,role,expected", CASES)
def test_권한_매트릭스(login_as, urls, method, name, role, expected):
    client = login_as(role)
    resp = client.get(urls[name]) if method == "GET" else client.post(urls[name])
    resp.close()   # 파일 다운로드 응답의 파일 핸들을 닫는다

    if expected == LOGIN:
        assert resp.status_code == 302 and resp.url.startswith("/login/")
    else:
        assert resp.status_code == expected
        if expected == 302:
            assert not resp.url.startswith("/login/")


def test_없는_대회와_남의_대회는_똑같이_404(client, student, other_competition):
    client.force_login(student)

    assert client.get(reverse("competitions:detail", args=[other_competition.pk])).status_code == 404
    assert client.get(reverse("competitions:detail", args=[999999])).status_code == 404


def test_다른_대회_회원번호를_이_대회_주소로_지정할_수_없다(client, admin2, competition, other_competition):
    outsider = _member("mock_outsider", other_competition, CompetitionMember.Role.PARTICIPANT)
    client.force_login(admin2)

    resp = client.post(reverse("competitions:promote", args=[competition.pk, outsider.pk]))

    assert resp.status_code == 404
    outsider.refresh_from_db()
    assert outsider.role == CompetitionMember.Role.PARTICIPANT

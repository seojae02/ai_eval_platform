"""검토 R10~R12, R4(부적합이어도 차감 유지), 상태 꼬임 방지, 이력·감사 로그.

가상 인물: 가상조교01(관리자1), 가상교수01(관리자2, conftest의 admin2), 가상학생01~03(참여자)
"""
from datetime import timedelta

import pytest
from django.urls import reverse

from accounts.models import User
from audit.models import AuditLog
from competitions import services as competition_services
from competitions.models import CompetitionMember
from submissions import leaderboard, review, services
from submissions.models import Review, Submission

from .conftest import kst

pytestmark = pytest.mark.django_db
S = Submission.Status
RS = Submission.ReviewStatus
OK, REJECT = Review.Verdict.OK, Review.Verdict.REJECT
T0 = kst(11, 19, 10)


@pytest.fixture
def people(competition):
    members = {}
    for i in range(1, 4):
        user = User.objects.create_user(f"mock_stu0{i}", real_name=f"가상학생0{i}")
        members[i] = competition_services.add_member(user, competition)
    return members


@pytest.fixture
def admin1(competition):
    user = User.objects.create_user("mock_ta01", real_name="가상조교01")
    CompetitionMember.objects.create(user=user, competition=competition, role=CompetitionMember.Role.ADMIN1)
    return user


def _scored(member, project, score, minutes=0, deadline=None):
    at = T0 + timedelta(minutes=minutes)
    return Submission.objects.create(project=project, member=member, deadline=deadline, status=S.SCORED,
                                     score=score, started_at=at, last_received_at=at, scored_at=at)


def _seen(sub):
    return review.latest_review_id(sub)


def _queue(project):
    return [(e.member.user.real_name, e.submission.pk) for e in review.review_queue(project)]


def _board(project):
    return [(e.rank, e.member.user.real_name, e.score_text) for e in leaderboard.build(project)]


# ── R10: 검토 순서 ──────────────────────────────────────────────
def test_R10_최고점부터_같은_사람의_같은_점수는_최신부터(people, project):
    _scored(people[1], project, 0.88)
    _scored(people[2], project, 0.93)
    old = _scored(people[3], project, 0.91)
    new = _scored(people[3], project, 0.91, minutes=5)

    assert [name for name, _ in _queue(project)] == ["가상학생02", "가상학생03", "가상학생01"]
    assert dict(_queue(project))["가상학생03"] == new.pk          # 같은 0.91 중 최신

    # 최신 0.91이 부적합 확정되면 이전 0.91이 다음 검토 대상(R12)
    review.decide(new, User.objects.get(username="mock_prof01"), REJECT, "CSV가 노트북 출력과 다름", seen=None)
    assert dict(_queue(project))["가상학생03"] == old.pk


# ── R11: 제안만으로는 순위 유지 ─────────────────────────────────
def test_R11_부적합_제안만으로는_리더보드가_그대로이고_참여자에게는_검토_중(client, people, project, admin1):
    top = _scored(people[1], project, 0.93)
    _scored(people[2], project, 0.91)
    before = _board(project)

    review.propose(top, admin1, REJECT, "CSV를 손으로 고친 흔적")
    top.refresh_from_db()

    assert _board(project) == before                               # 순위·점수 그대로
    assert top.review_status == RS.PROPOSED_REJECT
    client.force_login(people[2].user)
    html = client.get(reverse("submissions:leaderboard", args=[project.competition_id, project.pk])).content.decode()
    assert "검토 중" in html and "부적합 제안" not in html          # 결정 2: 확정 전 의심을 알리지 않는다


# ── R12: 결정 ──────────────────────────────────────────────────
def test_R12_부적합_확정이면_리더보드에서_빠지고_다음_기록이_대표(people, project, admin1, admin2):
    _scored(people[1], project, 0.85)
    best = _scored(people[1], project, 0.93, minutes=1)
    _scored(people[2], project, 0.91)

    review.propose(best, admin1, REJECT, "노트북과 CSV 불일치")
    review.decide(best, admin2, REJECT, "제안 승인", seen=_seen(best))

    assert _board(project) == [(1, "가상학생02", "0.9100"), (2, "가상학생01", "0.8500")]
    assert [name for name, _ in _queue(project)] == ["가상학생02", "가상학생01"]   # 0.85가 검토 목록에 올라옴


def test_R12_적합_확정이면_검토_목록에서_빠지고_확정_표시(people, project, admin1, admin2):
    sub = _scored(people[1], project, 0.9)
    review.propose(sub, admin1, OK)
    review.decide(sub, admin2, OK, seen=_seen(sub))
    sub.refresh_from_db()

    assert sub.review_status == RS.APPROVED and sub.public_review_label == "검증 완료"
    assert _queue(project) == []
    assert _board(project) == [(1, "가상학생01", "0.9000")]


def test_확정_뒤에_더_좋은_점수를_내면_새_기록이_다시_검토_대상(people, project, admin2):
    first = _scored(people[1], project, 0.8)
    review.decide(first, admin2, OK)
    better = _scored(people[1], project, 0.9, minutes=1)

    assert _queue(project) == [("가상학생01", better.pk)]
    review.decide(better, admin2, REJECT, "가상 사유")             # 새 기록이 부적합이면
    assert _board(project) == [(1, "가상학생01", "0.8000")]          # 확정된 이전 기록으로 돌아간다


# ── R4: 부적합이어도 차감 유지 ───────────────────────────────────
def test_R4_부적합_확정되어도_제출_횟수는_돌려주지_않는다(people, project_with_files, deadlines, admin2):
    first, _ = deadlines
    sub = _scored(people[1], project_with_files, 0.9, deadline=first)
    assert services.count_used(people[1], first) == 1

    review.decide(sub, admin2, REJECT, "가상 사유")
    sub.refresh_from_db()
    assert sub.status == S.SCORED                                  # 제출 상태는 그대로
    assert services.count_used(people[1], first) == 1


# ── 결정 1·3: 직접 결정, 반려, 재검토 ───────────────────────────
def test_관리자2는_제안_없이_직접_결정할_수_있고_이력에_직접으로_남는다(people, project, admin2):
    sub = _scored(people[1], project, 0.9)
    r = review.decide(sub, admin2, OK)
    assert r.direct is True and r.from_status == RS.PENDING and r.to_status == RS.APPROVED


def test_반려하면_검증_대기로_돌아가고_사유가_필요하다(people, project, admin1, admin2):
    sub = _scored(people[1], project, 0.9)
    review.propose(sub, admin1, REJECT, "의심")
    with pytest.raises(review.ReviewRefused, match="사유"):
        review.send_back(sub, admin2, "  ", seen=_seen(sub))
    review.send_back(sub, admin2, "근거 부족", seen=_seen(sub))
    sub.refresh_from_db()
    assert sub.review_status == RS.PENDING


def test_재검토는_확정만_풀_수_있다(people, project, admin2):
    sub = _scored(people[1], project, 0.9)
    with pytest.raises(review.ReviewRefused, match="검증 대기"):
        review.reopen(sub, admin2, "가상 사유")                      # 확정 전에는 불가
    review.decide(sub, admin2, REJECT, "잘못 누름", seen=None)
    review.reopen(sub, admin2, "실수로 부적합 확정", seen=_seen(sub))
    sub.refresh_from_db()
    assert sub.review_status == RS.PENDING
    assert _board(project) == [(1, "가상학생01", "0.9000")]           # 리더보드에 돌아옴


# ── 상태 규칙과 꼬임 방지 ───────────────────────────────────────
def test_부적합은_사유_없이_제안하거나_결정할_수_없다(people, project, admin1, admin2):
    sub = _scored(people[1], project, 0.9)
    for fn, user in ((review.propose, admin1), (review.decide, admin2)):
        with pytest.raises(review.ReviewRefused, match="사유"):
            fn(sub, user, REJECT, "")


def test_확정된_뒤에는_관리자1이_제안을_바꿀_수_없다(people, project, admin1, admin2):
    sub = _scored(people[1], project, 0.9)
    review.decide(sub, admin2, OK)
    with pytest.raises(review.ReviewRefused, match="적합\\(확정\\)"):
        review.propose(sub, admin1, REJECT, "늦은 제안", seen=_seen(sub))


def test_화면을_연_뒤_제안이_바뀌었으면_결정을_거부한다(people, project, admin1, admin2):
    sub = _scored(people[1], project, 0.9)
    review.propose(sub, admin1, OK)
    seen_by_prof = _seen(sub)                                      # 가상교수01이 '적합 제안'을 보고
    review.propose(sub, admin1, REJECT, "다시 보니 이상함", seen=seen_by_prof)   # 그 사이 제안이 바뀜

    with pytest.raises(review.ReviewRefused, match="새로고침"):
        review.decide(sub, admin2, OK, seen=seen_by_prof)
    sub.refresh_from_db()
    assert sub.review_status == RS.PROPOSED_REJECT


def test_채점되지_않은_제출은_검토할_수_없다(people, project, admin2):
    sub = Submission.objects.create(project=project, member=people[1], status=S.QUEUED,
                                    started_at=T0, last_received_at=T0)
    with pytest.raises(review.ReviewRefused, match="채점된"):
        review.decide(sub, admin2, OK)


def test_이력과_감사_로그가_함께_남고_이력은_고칠_수_없다(people, project, admin1, admin2):
    sub = _scored(people[1], project, 0.9)
    review.propose(sub, admin1, REJECT, "의심")
    r = review.decide(sub, admin2, REJECT, "확정", seen=_seen(sub))

    assert sub.reviews.count() == 2
    assert AuditLog.objects.filter(action=AuditLog.Action.REVIEW_CHANGED).count() == 2
    assert "확정" not in str(AuditLog.objects.first().detail)        # 사유 원문은 감사 로그에 넣지 않음
    r.reason = "고친 사유"
    with pytest.raises(ValueError):
        r.save()
    with pytest.raises(ValueError):
        r.delete()


# ── 화면 ───────────────────────────────────────────────────────
def _url(name, sub, *extra):
    return reverse(f"submissions:{name}", args=[sub.project.competition_id, sub.project_id, sub.pk, *extra])


def test_관리자1이_화면으로_제안하고_관리자2가_화면으로_확정(client, people, project, admin1, admin2):
    sub = _scored(people[1], project, 0.9)
    client.force_login(admin1)
    client.post(_url("review_propose", sub), {"verdict": "reject", "reason": "노트북 출력과 다름", "seen": ""})
    client.force_login(admin2)
    page = client.get(_url("review_detail", sub)).content.decode()
    assert "노트북 출력과 다름" in page and "가상조교01" in page

    client.post(_url("review_decide", sub), {"verdict": "reject", "reason": "승인", "seen": _seen(sub)})
    sub.refresh_from_db()
    assert sub.review_status == RS.REJECTED


def test_부적합_사유는_본인_내_제출에만_보인다(client, people, project_with_files, admin2):
    sub = _scored(people[1], project_with_files, 0.9)
    review.decide(sub, admin2, REJECT, "가상사유-제출CSV불일치")
    url = reverse("competitions:project_detail", args=[project_with_files.competition_id, project_with_files.pk])

    client.force_login(people[1].user)
    assert "가상사유-제출CSV불일치" in client.get(url).content.decode()
    client.force_login(people[2].user)
    assert "가상사유-제출CSV불일치" not in client.get(url).content.decode()


def test_제출_파일_다운로드는_첨부_캐시금지_감사기록(client, people, project_with_files, deadlines, admin2):
    from .conftest import send_files
    sub = services.start_submission(people[1], project_with_files, now=T0)
    send_files(sub, now=T0)
    services.complete(sub, now=T0)

    client.force_login(admin2)
    resp = client.get(_url("review_file", sub, "model"))
    assert resp.status_code == 200
    assert resp["Content-Disposition"].startswith("attachment")
    assert resp["Content-Type"] == "application/octet-stream" and resp["Cache-Control"] == "private, no-store"
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert AuditLog.objects.filter(action=AuditLog.Action.SUBMISSION_FILE_DOWNLOADED).count() == 1
    resp.close()   # 닫으면 Django가 요청 종료 신호로 DB 연결을 정리하므로, DB 확인은 닫기 전에 한다


def test_검토가_끝난_제출도_제출_목록에서_파일을_다시_연다(client, people, project_with_files, deadlines, admin2, admin1):
    # 검토 목록은 확정되지 않은 것만 보여 주므로, 확정 뒤에는 제출 목록이 파일로 가는 길이다(2026-11-23 요청)
    from .conftest import send_files
    sub = services.start_submission(people[1], project_with_files, now=T0)
    send_files(sub, now=T0)
    services.complete(sub, now=T0)
    Submission.objects.filter(pk=sub.pk).update(status=S.SCORED, score=0.9, scored_at=T0)
    sub.refresh_from_db()
    review.decide(sub, admin2, REJECT, "가상사유-외부파일조회")
    assert sub.pk not in [pk for _, pk in _queue(project_with_files)]          # 검토 목록에서는 빠졌다

    links = [_url("review_file", sub, kind) for kind in ("csv", "notebook", "model")] + \
            [_url("review_notebook", sub), _url("review_detail", sub)]
    list_url = reverse("submissions:admin_list", args=[project_with_files.competition_id, project_with_files.pk])
    opened = []
    for user in (admin1, admin2):
        client.force_login(user)
        html = client.get(list_url).content.decode()
        for link in links:
            assert link in html, link
            resp = client.get(link)
            opened.append(resp)
            assert resp.status_code == 200, (user.username, link)
    for resp in opened:   # 닫으면 DB 연결이 정리되므로 DB를 다 쓴 뒤에 닫는다
        resp.close()


def test_제출_목록은_제출_수가_늘어도_쿼리_수가_같다(client, people, project_with_files, deadlines, admin2):
    # 줄마다 파일·검증 리포트를 따로 조회하면(N+1) 제출 수만큼 쿼리가 늘어난다
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from .conftest import send_files
    client.force_login(admin2)
    url = reverse("submissions:admin_list", args=[project_with_files.competition_id, project_with_files.pk])

    def submit(i):
        sub = services.start_submission(people[i], project_with_files, now=T0)
        send_files(sub, now=T0)
        services.complete(sub, now=T0)

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(url).status_code == 200
        return len(ctx.captured_queries)

    submit(1)
    one = count()
    submit(2)
    submit(3)
    assert count() == one


def test_내려받는_파일_이름은_별칭_코드와_종류다(client, people, project_with_files, deadlines, admin2):
    # 가상 예시: 별칭 '참여자 58AS' → 58AS_CSV.csv, 58AS_NOTEBOOK.ipynb, 58AS_MODEL.pkl (2026-11-23 요청)
    from .conftest import GOOD_FILES, send_files
    member = people[1]
    member.alias = "참여자 58AS"
    member.save(update_fields=["alias"])
    files = {**GOOD_FILES, "notebook": ("가상학생01_과제.ipynb", GOOD_FILES["notebook"][1])}   # 파일명에 이름을 적은 경우
    sub = services.start_submission(member, project_with_files, now=T0)
    send_files(sub, files=files, now=T0)
    services.complete(sub, now=T0)

    client.force_login(admin2)
    opened = []
    for kind, expected in [("csv", "58AS_CSV.csv"), ("notebook", "58AS_NOTEBOOK.ipynb"), ("model", "58AS_MODEL.pkl")]:
        resp = client.get(_url("review_file", sub, kind))
        opened.append(resp)
        disposition = resp["Content-Disposition"]
        assert disposition.startswith("attachment") and f'filename="{expected}"' in disposition, disposition
        assert "가상학생01" not in disposition   # 원래 파일명(이름 포함)은 내려받은 파일에 남지 않는다
    for resp in opened:   # 닫으면 DB 연결이 정리되므로 마지막에 닫는다
        resp.close()


def test_부적합_제안_사유가_결정_폼에_미리_채워지고_학생에게는_결정_사유가_보인다(client, people, project_with_files, admin1, admin2):
    # 2026-11-23 와인 시험: 결정 사유가 "관리자1 의견 동의"뿐이라 학생은 이유를 알 수 없었다
    sub = _scored(people[1], project_with_files, 0.9)
    review.propose(sub, admin1, REJECT, "가상사유-원본파일조회 가상학생02와비교", seen=_seen(sub))
    client.force_login(admin2)
    html = client.get(_url("review_detail", sub)).content.decode()
    assert "가상사유-원본파일조회 가상학생02와비교</textarea>" in html        # 미리 채워짐
    assert "참여자 본인에게 그대로 보입니다" in html                         # 안내

    review.decide(sub, admin2, REJECT, "가상사유-원본파일조회", seen=_seen(sub))   # 관리자2가 다듬음
    client.force_login(people[1].user)
    page = client.get(reverse("competitions:project_detail", args=[project_with_files.competition_id, project_with_files.pk])).content.decode()
    assert "가상사유-원본파일조회" in page and "가상학생02와비교" not in page   # 다듬은 문장만 학생에게


def test_적합_제안이면_결정_사유는_비워_둔다(client, people, project_with_files, admin1, admin2):
    sub = _scored(people[1], project_with_files, 0.9)
    review.propose(sub, admin1, OK, "가상메모-문제없음", seen=_seen(sub))
    client.force_login(admin2)
    assert "가상메모-문제없음</textarea>" not in client.get(_url("review_detail", sub)).content.decode()


# ── 일괄 승인 (RFP 8쪽: 관리자2가 결과를 확인하고 승인할 때의 어려움) ─────────────
def _report(sub, level, **checks):
    """가상 검증 리포트. checks: v1=("warn", "요약") 처럼, 나머지 검사는 '문제 없음'."""
    from django.utils import timezone

    from verification.models import VerificationReport
    full = {k: {"level": "ok", "summary": "가상-문제없음", "details": {}} for k in ("v1", "v2", "v3", "v4", "v5")}
    for key, value in checks.items():
        lvl, summary, *details = value
        full[key] = {"level": lvl, "summary": summary, "details": details[0] if details else {}}
    return VerificationReport.objects.create(submission=sub, status="done", level=level, checks=full,
                                             started_at=timezone.now(), finished_at=timezone.now())


def _bulk(client, project, *pairs):
    url = reverse("submissions:review_bulk_approve", args=[project.competition_id, project.pk])
    return client.post(url, {"pick": [f"{s.pk}:{seen if seen is not None else ''}" for s, seen in pairs]}, follow=True)


def _proposed_ok(member, project, admin1, score):
    sub = _scored(member, project, score)
    review.propose(sub, admin1, OK, "가상메모", seen=_seen(sub))
    return sub


def test_일괄_승인은_이유로_고른다_V3_주의는_되고_V1_주의는_상세로(client, people, project_with_files, admin1, admin2):
    clean = _proposed_ok(people[1], project_with_files, admin1, 0.9)
    v3 = _proposed_ok(people[2], project_with_files, admin1, 0.8)          # 와인 가상학생11 같은 경우
    edited = _proposed_ok(people[3], project_with_files, admin1, 0.7)      # 데모 학생03(손수정) 같은 경우
    _report(clean, "ok")
    _report(v3, "warn", v3=("warn", "가장 비슷한 참여자와 같은 오답 78%"))
    _report(edited, "warn", v1=("warn", "재실행 결과와 제출 CSV 96.5% 주의"), v5=("warn", "96.5% 일치"))

    client.force_login(admin2)
    html = _bulk(client, project_with_files, *[(s, _seen(s)) for s in (clean, v3, edited)]).content.decode()

    for s in (clean, v3, edited):
        s.refresh_from_db()
    assert clean.review_status == RS.APPROVED and v3.review_status == RS.APPROVED
    assert edited.review_status == RS.PROPOSED_OK                          # 그대로 남아 상세에서 결정
    assert "2건을 적합(확정)" in html and f"#{edited.pk} 건너뜀: V1 주의" in html
    assert Review.objects.filter(submission=clean, action=Review.Action.DECIDE, reason="일괄 승인").exists()
    assert AuditLog.objects.filter(action=AuditLog.Action.REVIEW_CHANGED, detail__to=RS.APPROVED).count() == 2


def test_일괄_승인_V4는_셸_명령과_실행_순서만_허용하고_코드_없는_옛_리포트는_막는다(client, people, project_with_files, admin1, admin2):
    shell = _proposed_ok(people[1], project_with_files, admin1, 0.9)
    outside = _proposed_ok(people[2], project_with_files, admin1, 0.8)
    old = _proposed_ok(people[3], project_with_files, admin1, 0.7)
    _report(shell, "warn", v4=("warn", "확인할 점 2개", {"codes": ["shell", "order"]}))
    _report(outside, "warn", v4=("warn", "확인할 점 1개", {"codes": ["outside_file"]}))
    _report(old, "warn", v4=("warn", "확인할 점 1개", {"flags": ["셸 명령(!)을 씁니다."]}))   # 코드 칸이 생기기 전 리포트

    client.force_login(admin2)
    _bulk(client, project_with_files, *[(s, _seen(s)) for s in (shell, outside, old)])
    states = {s.pk: Submission.objects.get(pk=s.pk).review_status for s in (shell, outside, old)}
    assert states == {shell.pk: RS.APPROVED, outside.pk: RS.PROPOSED_OK, old.pk: RS.PROPOSED_OK}


def test_일괄_승인은_조작된_요청과_오래된_화면을_거른다(client, people, project_with_files, admin1, admin2):
    reject = _scored(people[1], project_with_files, 0.9)
    review.propose(reject, admin1, REJECT, "가상사유", seen=_seen(reject))
    bad = _proposed_ok(people[2], project_with_files, admin1, 0.8)
    stale = _proposed_ok(people[3], project_with_files, admin1, 0.7)
    for s, lvl in ((reject, "ok"), (bad, "bad"), (stale, "ok")):
        _report(s, lvl)
    old_seen = _seen(stale)
    review.propose(stale, admin1, OK, "가상메모-다시 봄", seen=old_seen)   # 관리자2가 화면을 연 뒤 관리자1이 다시 제안

    client.force_login(admin2)
    html = _bulk(client, project_with_files, (reject, _seen(reject)), (bad, _seen(bad)), (stale, old_seen)).content.decode()
    assert Submission.objects.filter(pk__in=[reject.pk, bad.pk, stale.pk], review_status=RS.APPROVED).count() == 0
    assert "적합 제안이 아님" in html and "리포트 &#x27;의심&#x27;" in html and "다른 관리자가 검토 상태를 바꿨" in html


def test_승인_대기_표에_문제된_검사와_관리자1_사유와_선택칸이_보인다(client, people, project_with_files, admin1, admin2):
    ok = _proposed_ok(people[1], project_with_files, admin1, 0.9)
    edited = _proposed_ok(people[2], project_with_files, admin1, 0.8)
    _report(ok, "warn", v3=("warn", "가장 비슷한 참여자와 같은 오답 78%"))
    _report(edited, "warn", v1=("warn", "재실행 결과와 제출 CSV 96.5% 주의"))
    url = reverse("submissions:review_queue", args=[project_with_files.competition_id, project_with_files.pk])

    client.force_login(admin2)
    html = client.get(url).content.decode()
    assert f'value="{ok.pk}:{_seen(ok)}"' in html and f'value="{edited.pk}:' not in html   # 손수정 의심 줄은 선택칸 없음
    assert "가상메모" in html and "V1 주의" in html
    assert "V3: 가장 비슷한 참여자와 같은 오답 78%" not in html   # V3는 참고용이라 '문제 된 검사'에 넣지 않는다
    client.force_login(admin1)
    assert "선택한 제출 적합 확정" not in client.get(url).content.decode()                # 관리자1에게는 없음


def test_승인_대기_표는_건수가_늘어도_쿼리_수가_같다(client, people, project_with_files, admin1, admin2):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    url = reverse("submissions:review_queue", args=[project_with_files.competition_id, project_with_files.pk])
    client.force_login(admin2)

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(url).status_code == 200
        return len(ctx.captured_queries)

    _report(_proposed_ok(people[1], project_with_files, admin1, 0.9), "ok")
    one = count()
    for i, score in ((2, 0.8), (3, 0.7)):
        _report(_proposed_ok(people[i], project_with_files, admin1, score), "ok")
    assert count() == one


def test_일괄_승인_V6_판정_불가는_막지_않고_V6_주의는_막는다(client, people, project_with_files, admin1, admin2):
    torch_like = _proposed_ok(people[1], project_with_files, admin1, 0.9)   # PyTorch 모델이라 재실행·모델 비교 불가
    old_csv = _proposed_ok(people[2], project_with_files, admin1, 0.8)      # 노트북과 다른 CSV
    _report(torch_like, "warn", v3=("warn", "같은 오답 75%"), v6=("unknown", "비교할 결과가 없음"))
    _report(old_csv, "warn", v6=("warn", "제출 CSV와 노트북 재실행 결과가 4개 행에서 다릅니다"))

    client.force_login(admin2)
    html = _bulk(client, project_with_files, (torch_like, _seen(torch_like)), (old_csv, _seen(old_csv))).content.decode()
    assert Submission.objects.get(pk=torch_like.pk).review_status == RS.APPROVED
    assert Submission.objects.get(pk=old_csv.pk).review_status == RS.PROPOSED_OK and "V6 주의" in html


def test_일괄_승인_V7_방법_안내는_막지_않는다(client, people, project_with_files, admin1, admin2):
    # 가상: V3 주의로 전체가 '주의'인데 V7(중복 그대로 둔 CV) 주의도 함께 있음 → V7은 부정 근거가 아니므로 일괄 가능
    sub = _proposed_ok(people[1], project_with_files, admin1, 0.9)
    _report(sub, "warn", v3=("warn", "같은 오답 78%"), v7=("warn", "확인할 점 1개", {"codes": ["duplicates"]}))

    client.force_login(admin2)
    _bulk(client, project_with_files, (sub, _seen(sub)))

    sub.refresh_from_db()
    assert sub.review_status == RS.APPROVED


# ── V7 방법 점검: 부정 검사와 따로 보이고, 학생에게는 안내만 ─────────────────
V7_WARN = ("warn", "확인할 점 1개 (참고 1개)", {
    "student": [{"level": "warn", "label": "중복 처리 확인 필요", "text": "가상-중복 안내"},
                {"level": "info", "label": "노트북 CV와 서버 점수 차이", "text": "가상-차이 안내"}],
    "flags": ["중복 처리 확인 필요: 가상-중복 안내 [근거] 코드 셀 3 4번째 줄 cross_val_score"],
    "cv": {"value": 0.5687, "server": 0.62, "gap": 8.3, "source": "코드 셀 5의 Scores/Mean 출력",
           "text": "노트북 CV 0.5687 · 서버 0.6200 (CV가 서버보다 8.3% 좋음, 학생 노트북 출력 기준)"},
})


def test_방법_점검은_문제_된_검사와_따로_보인다(client, people, project_with_files, admin1, admin2):
    sub = _proposed_ok(people[1], project_with_files, admin1, 0.9)
    _report(sub, "warn", v3=("warn", "가상-같은 오답 78%"), v7=V7_WARN)

    client.force_login(admin2)
    html = client.get(reverse("submissions:review_queue",
                              args=[project_with_files.competition_id, project_with_files.pk])).content.decode()
    assert "V3:" not in html and "V7:" not in html                          # 둘 다 참고·안내용(ADVISORY)
    assert "확인할 점 1개 (참고 1개)" in html                                # '방법 점검' 칸


def test_전체_목록에서_방법_점검_주의만_골라_본다(client, people, project_with_files, admin2):
    flagged = _scored(people[1], project_with_files, 0.9)
    _report(flagged, "ok", v7=V7_WARN)
    clean = _scored(people[2], project_with_files, 0.8)
    _report(clean, "ok", v7=("ok", "가상-문제없음"))

    client.force_login(admin2)
    url = reverse("submissions:admin_list", args=[project_with_files.competition_id, project_with_files.pk])
    everything, only = client.get(url).content.decode(), client.get(url + "?method=warn").content.decode()
    assert f">#{flagged.pk}</a>" in everything and f">#{clean.pk}</a>" in everything
    assert f">#{flagged.pk}</a>" in only and f">#{clean.pk}</a>" not in only


def test_검토_상세에_방법_점검_근거와_점수_비교가_나온다(client, people, project_with_files, admin2):
    sub = _scored(people[1], project_with_files, 0.62)
    _report(sub, "ok", v7=V7_WARN)

    client.force_login(admin2)
    html = client.get(_url("review_detail", sub)).content.decode()
    assert "[근거] 코드 셀 3" in html and "점수 비교: 노트북 CV 0.5687" in html


def test_학생에게는_방법_안내와_점수_비교만_보이고_부정_검사와_근거는_안_보인다(client, people, project_with_files):
    sub = _scored(people[1], project_with_files, 0.62)
    _report(sub, "warn", v4=("warn", "가상-V4-외부파일"), v7=V7_WARN)

    client.force_login(people[1].user)
    html = client.get(reverse("competitions:project_detail",
                              args=[project_with_files.competition_id, project_with_files.pk])).content.decode()
    assert "중복 처리 확인 필요" in html and "가상-차이 안내" in html and "노트북 CV 0.5687" in html
    assert "가상-V4-외부파일" not in html and "[근거]" not in html and "코드 셀 3" not in html

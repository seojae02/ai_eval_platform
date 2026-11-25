"""검토 (R10~R12): 관리자1 제안 → 관리자2 결정. 검토 상태를 바꾸는 길은 이 파일뿐이다.

    검증 대기 ─(관리자1 제안)─▶ 적합/부적합 제안 ─(관리자2 결정)─▶ 적합(확정) / 부적합(확정)
        ▲   └──────(관리자2 직접 결정)──────────────────────────▶      │
        └──── 반려(관리자2, 제안을 받아들이지 않음) / 재검토(관리자2, 확정을 풀기) ◀┘

부적합(확정)은 리더보드에서 빠지고(R12), 그 참여자의 다음 기록이 대표가 되어 검토 목록에 오른다.
제출 상태('채점됨')는 바꾸지 않는다 → 제출 횟수 차감은 그대로 유지된다(R4).
누가 그 동작을 할 수 있는지(관리자1/관리자2)는 뷰가 먼저 확인하고, 여기서는 상태 규칙만 본다.
"""
from django.db import transaction

from audit.models import AuditLog
from audit.services import record
from verification.checks import ADVISORY   # V7처럼 전체 판정·'문제 된 검사'에 넣지 않는 안내용 검사

from . import leaderboard
from .models import Review, Submission

RS = Submission.ReviewStatus
Action = Review.Action
Verdict = Review.Verdict


class ReviewRefused(Exception):
    """화면에 그대로 보여 줄 수 있는 거부 사유."""


def latest_review_id(submission):
    """이 제출의 마지막 검토 이력 번호(없으면 None). 화면이 폼에 숨겨 보내는 '내가 본 상태' 표시다."""
    return submission.reviews.order_by("-pk").values_list("pk", flat=True).first()


def _change(submission_id, actor, action, *, allowed_from, to_status, seen, verdict="", reason=""):
    """검토 상태를 한 번 바꾸고 이력·감사 로그를 남긴다. 모든 동작이 이 함수 하나를 거친다.

    ① 제출 줄을 잠근다 → 제안과 결정이 동시에 들어와도 하나씩 처리된다(R6와 같은 방식)
    ② 화면을 연 뒤 다른 관리자가 상태를 바꿨으면 거부한다
       예) 가상교수01이 '적합 제안'을 보고 승인을 누르기 직전에 가상조교01이 '부적합 제안'으로 바꿈
           → 가상교수01은 보지 못한 제안을 승인하게 되므로 거부하고 새로고침을 안내
    ③ 상태·이력·감사 로그를 한 트랜잭션으로 저장한다 → 셋이 어긋나지 않는다
    """
    with transaction.atomic():
        sub = Submission.objects.select_for_update().select_related("project__competition").get(pk=submission_id)
        if sub.status != Submission.Status.SCORED:
            raise ReviewRefused("채점된 제출만 검토할 수 있습니다.")
        if latest_review_id(sub) != seen:
            raise ReviewRefused("화면을 연 뒤 다른 관리자가 검토 상태를 바꿨으니 새로고침해서 확인한 뒤 다시 해 주세요")
        if sub.review_status not in allowed_from:
            raise ReviewRefused(f"'{sub.get_review_status_display()}' 상태에서는 할 수 없는 동작입니다.")

        before = sub.review_status
        # 관리자2가 제안 없이 결정했는지는 잠근 뒤의 상태로 판단한다(화면을 연 시점의 값이 아니라)
        direct = action == Action.DECIDE and before == RS.PENDING
        sub.review_status = to_status
        sub.save(update_fields=["review_status"])
        review = Review.objects.create(
            submission=sub, actor=actor, action=action, verdict=verdict, direct=direct,
            reason=reason, from_status=before, to_status=to_status,
        )
        # 사유 원문은 Review에만 둔다. 감사 로그에는 무엇이 어떻게 바뀌었는지만
        record(actor, AuditLog.Action.REVIEW_CHANGED, competition=sub.project.competition,
               target_user=sub.member.user,
               detail={"submission_id": sub.pk, "review_id": review.pk, "action": action,
                       "verdict": verdict, "direct": direct, "from": before, "to": to_status})
    return review


def _require_reason(verdict, reason):
    if verdict == Verdict.REJECT and not reason.strip():
        raise ReviewRefused("부적합은 사유를 꼭 적어야 합니다.")


def propose(submission, actor, verdict, reason="", seen=None):
    """관리자1: 적합/부적합 제안. 결정 전이면 제안을 바꿀 수 있다. 제안만으로는 순위가 바뀌지 않는다(R11)."""
    _require_reason(verdict, reason)
    to_status = RS.PROPOSED_OK if verdict == Verdict.OK else RS.PROPOSED_REJECT
    return _change(submission.pk, actor, Action.PROPOSE, allowed_from=(RS.PENDING, *Submission.PROPOSED),
                   to_status=to_status, seen=seen, verdict=verdict, reason=reason)


def decide(submission, actor, verdict, reason="", seen=None):
    """관리자2: 확정. 제안이 있으면 그 제안에 대한 결정, 없으면 직접 결정(결정 1: 관리자1이 없는 대회도 운영 가능)."""
    _require_reason(verdict, reason)
    to_status = RS.APPROVED if verdict == Verdict.OK else RS.REJECTED
    return _change(submission.pk, actor, Action.DECIDE, allowed_from=(RS.PENDING, *Submission.PROPOSED),
                   to_status=to_status, seen=seen, verdict=verdict, reason=reason)


def send_back(submission, actor, reason, seen=None):
    """관리자2: 제안을 받아들이지 않고 검증 대기로 되돌린다(사유 필수)."""
    if not reason.strip():
        raise ReviewRefused("반려 사유를 적어 주세요.")
    return _change(submission.pk, actor, Action.SEND_BACK, allowed_from=Submission.PROPOSED,
                   to_status=RS.PENDING, seen=seen, reason=reason)


def reopen(submission, actor, reason, seen=None):
    """관리자2: 확정을 풀고 검증 대기로(결정 3: 잘못 누른 승인을 고칠 길. 사유 필수, 감사 로그)."""
    if not reason.strip():
        raise ReviewRefused("재검토 사유를 적어 주세요.")
    return _change(submission.pk, actor, Action.REOPEN, allowed_from=Submission.DECIDED,
                   to_status=RS.PENDING, seen=seen, reason=reason)


# ── 목록 ───────────────────────────────────────────────────────
def review_queue(project):
    """검토 목록 (R10): 참여자마다 지금의 대표 기록 중 아직 '적합(확정)'이 아닌 것. 리더보드와 같은 순서.

    리더보드 함수를 그대로 쓴다 → 부적합(확정)이 빠지면 다음 기록이 대표가 되어 자동으로 여기에 오른다(R12).
    같은 참여자의 같은 점수는 대표 기록을 고를 때 이미 '최신 먼저'다.
    """
    return [e for e in leaderboard.build(project) if e.submission.review_status != RS.APPROVED]


def pending_decisions(project):
    """관리자2 승인 대기: 제안이 올라온 제출 전부(대표 기록이 아니게 된 것도 결정할 수 있게)."""
    return (Submission.objects.filter(project=project, status=Submission.Status.SCORED,
                                      review_status__in=Submission.PROPOSED)
            .select_related("member__user").order_by("-score" if leaderboard.is_higher(project) else "score"))


def last_decision(submission):
    """참여자 본인에게 보여 줄 부적합 사유: 마지막 '결정' 이력."""
    return submission.reviews.filter(action=Action.DECIDE).order_by("-pk").first()


# ── 일괄 승인 (RFP 8쪽 "관리자2가 결과를 확인하고 승인할 때의 어려움") ─────────────
# 대상은 '이유'로 고른다. 같은 '주의'라도 V3 오답 일치(정직한 학생에게도 흔함)와
# V1·V5 어긋남(CSV 손수정일 수 있음)은 무게가 다르다 → 뒤쪽은 상세에서 한 건씩

BULK_OK_CHECKS = {"v3", "v7"}           # 이 검사의 '주의'만 있으면 일괄 가능(V7은 방법 실수 안내, 부정 근거 아님)
BULK_OK_V4_CODES = {"shell", "order"}   # V4 '주의'가 이 이유뿐이면 일괄 가능(!pip 설치, 셀을 고쳐 다시 실행)
BULK_REASON = "일괄 승인"
LEVEL_WORDS = {"warn": "주의", "unknown": "판정 불가", "bad": "의심"}


def problem_checks(report):
    """리포트에서 '문제 없음'이 아닌 검사만 [(키, 제목, 단계, 요약)]. 화면의 '문제 된 검사' 칸."""
    if report is None:
        return []
    return [(key, title, report.checks[key]["level"], report.checks[key]["summary"])
            for key, title in report.TITLES.items()
            if key in report.checks and key not in ADVISORY and report.checks[key].get("level") != "ok"]


def method_check(report):
    """V7(학습·평가 방법 안내) 결과. 부정 근거가 아니므로 '문제 된 검사'와 따로 보여 준다. 없으면 None."""
    return report.checks.get("v7") if report is not None and report.checks else None


def bulk_block_reason(sub, report):
    """일괄 승인할 수 없으면 그 이유(화면에 그대로 보임), 할 수 있으면 ''.

    가상 예시: 가상학생11 리포트 '주의'(V3 같은 오답 78%)만 → '' (일괄 가능)
               가상학생05 리포트 '주의'(V1·V5 98.9%) → 'V1 재실행 주의는 상세에서 결정'
    """
    if sub.review_status != RS.PROPOSED_OK:
        return "적합 제안이 아님"
    if report is None or report.status != report.Status.DONE:
        return "검증이 끝나지 않음"
    if report.level == report.Level.OK:
        return ""
    if report.level != report.Level.WARN:
        return f"리포트 '{report.get_level_display()}'"
    for key, title, level, _ in problem_checks(report):
        if key in BULK_OK_CHECKS:
            continue
        if key == "v6" and level == "unknown":
            continue   # V6 판정 불가 = 재실행·모델 예측이 없음(PyTorch 등). 이것만으로 일괄을 막으면 그런 과제는 전부 한 건씩이 된다
        if key == "v4" and set(report.checks[key].get("details", {}).get("codes") or ["?"]) <= BULK_OK_V4_CODES:
            continue   # 코드가 없는 옛 리포트는 '?'가 되어 일괄 불가(보수적으로)
        # 전체가 '주의'여도 검사 하나는 '판정 불가'일 수 있다(예: V1 실행 실패 + V3 주의)
        return f"{title.split(' ')[0]} {LEVEL_WORDS.get(level, level)}"
    return ""


class PendingRow:
    """승인 대기 표의 한 줄: 제출, 리포트, 문제 된 검사, 관리자1 사유, 일괄 가능 여부, 화면을 연 시점의 이력 번호."""

    def __init__(self, sub):
        reviews = sorted(sub.reviews.all(), key=lambda r: r.pk)          # prefetch 결과에서 계산(추가 쿼리 없음)
        proposal = next((r for r in reversed(reviews) if r.action == Action.PROPOSE), None)
        self.submission = sub
        self.report = getattr(sub, "verification", None)
        self.problems = problem_checks(self.report)
        self.method = method_check(self.report)
        self.proposal_reason = proposal.reason if proposal else ""
        self.seen = reviews[-1].pk if reviews else None
        self.block = bulk_block_reason(sub, self.report)
        self.bulk_ok = not self.block


def pending_rows(project):
    subs = pending_decisions(project).select_related("verification").prefetch_related("reviews")
    return [PendingRow(s) for s in subs]


def bulk_approve(project, actor, picks):
    """picks = [(제출 번호, 화면을 연 시점의 이력 번호)]. 건마다 기존 decide를 그대로 쓴다.

    행 잠금, '화면을 연 뒤 바뀜' 거부, 이력·감사 로그가 건마다 그대로 적용된다.
    한 건이 실패해도 나머지는 확정한다(28건 중 1건 때문에 27건을 다시 하지 않게). 결과: (확정 목록, [(번호, 이유)])
    """
    done, skipped = [], []
    for sid, seen in picks:
        sub = (Submission.objects.filter(project=project, pk=sid, status=Submission.Status.SCORED)
               .select_related("member__user", "verification").first())
        if sub is None:
            skipped.append((sid, "이 과제의 채점된 제출이 아님"))
            continue
        block = bulk_block_reason(sub, getattr(sub, "verification", None))   # 화면을 믿지 않고 서버가 다시 본다
        if block:
            skipped.append((sid, f"{block}, 상세에서 결정해 주세요"))
            continue
        try:
            decide(sub, actor, Verdict.OK, reason=BULK_REASON, seen=seen)
        except ReviewRefused as e:
            skipped.append((sid, str(e)))
        else:
            done.append(sub)
    return done, skipped

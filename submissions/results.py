"""최종 성적 (RFP 2·6쪽: '각 참여자 대표 기록 및 최종 리더보드(성적) 확정').

리더보드와 같은 대표 기록(leaderboard.build)을 쓰고, 기록마다 '확정 / 검토 중 / 미검토'를 붙인다.
관리자2가 한눈에 보는 것: 몇 명이 확정됐고 몇 명이 남았는지, 리더보드에 없는 참여자는 왜 없는지.

가상 예시 (Accuracy)
  가상학생01 0.9649 적합(확정) · 가상학생02 0.9474 부적합 제안(검토 중) · 가상학생03 0.9300 검증 대기(미검토)
  가상학생04 모든 기록 부적합 · 가상학생05 채점된 기록 없음
  → 확정 1 / 검토 중 1 / 미검토 1, 최종 확정 아님
"""
import csv
import io
from dataclasses import dataclass

from django.utils import timezone

from competitions.models import CompetitionMember

from . import leaderboard
from .models import Review, Submission

RS = Submission.ReviewStatus
CONFIRMED, IN_REVIEW, NOT_REVIEWED = "확정", "검토 중", "미검토"


@dataclass
class Row:
    rank: int
    submission: Submission
    score_text: str
    state: str
    decision: Review | None     # 확정일 때만: 누가 언제 확정했나

    @property
    def member(self):
        return self.submission.member


def _state(submission):
    if submission.review_status == RS.APPROVED:
        return CONFIRMED
    if submission.review_status in Submission.PROPOSED:
        return IN_REVIEW
    return NOT_REVIEWED


def build(project):
    """(대표 기록 줄들, 리더보드에 없는 참여자 [(멤버, 이유)], 요약 dict)."""
    entries = leaderboard.build(project)
    approved = [e.submission.pk for e in entries if e.submission.review_status == RS.APPROVED]
    decisions = {}
    for r in (Review.objects.filter(submission_id__in=approved, action=Review.Action.DECIDE)
              .select_related("actor").order_by("pk")):
        decisions[r.submission_id] = r      # 마지막 결정이 남는다(재검토 뒤 다시 확정한 경우)
    rows = [Row(e.rank, e.submission, e.score_text, _state(e.submission), decisions.get(e.submission.pk))
            for e in entries]

    on_board = {r.member.pk for r in rows}
    scored = set(Submission.objects.filter(project=project, status=Submission.Status.SCORED)
                 .values_list("member_id", flat=True))
    missing = [
        (m, "모든 기록 부적합(확정)" if m.pk in scored else "채점된 기록 없음")
        for m in (CompetitionMember.objects.filter(competition=project.competition, is_active=True,
                                                   role=CompetitionMember.Role.PARTICIPANT)
                  .select_related("user").order_by("user__real_name"))
        if m.pk not in on_board
    ]
    count = lambda state: sum(r.state == state for r in rows)
    summary = {"confirmed": count(CONFIRMED), "in_review": count(IN_REVIEW), "not_reviewed": count(NOT_REVIEWED),
               "total": len(rows), "missing": len(missing), "final": is_final(entries)}
    return rows, missing, summary


def is_final(entries):
    """리더보드의 모든 대표 기록이 '적합(확정)'인가 → 참여자 리더보드에 '최종 확정' 표시."""
    return bool(entries) and all(e.submission.review_status == RS.APPROVED for e in entries)


# ── CSV 내보내기 ────────────────────────────────────────────────
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """엑셀이 수식으로 실행하지 않게 한다(CSV 수식 삽입 방지). 예) 실명 '=HYPERLINK(...)' → \"'=HYPERLINK(...)\""""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def _when(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else ""


HEADER = ["순위", "익명 별칭", "실명", "아이디", "점수", "확정 여부", "검토 상태",
          "제출 번호", "제출 시작", "확정한 관리자", "확정 시각"]


def to_csv(project):
    """엑셀에서 바로 열리는 UTF-8(BOM) CSV와 요약. 대표 기록 다음에 리더보드에 없는 참여자를 붙인다."""
    rows, missing, summary = build(project)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(HEADER)
    for r in rows:
        s, d = r.submission, r.decision
        w.writerow([_cell(v) for v in (
            r.rank, r.member.alias, r.member.user.real_name, r.member.user.username, r.score_text, r.state,
            s.get_review_status_display(), s.pk, _when(s.started_at),
            d.actor.real_name if d else "", _when(d.created_at) if d else "")])
    for m, reason in missing:
        w.writerow([_cell(v) for v in ("", m.alias, m.user.real_name, m.user.username, "", reason, "", "", "", "", "")])
    return ("\ufeff" + buf.getvalue()).encode("utf-8"), summary   # \ufeff = BOM: 엑셀이 UTF-8로 읽게

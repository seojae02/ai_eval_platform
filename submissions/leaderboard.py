"""리더보드 (R7·R8). 순위는 저장하지 않고 볼 때마다 계산한다(제출 횟수와 같은 원칙).

순서
  ① 올라갈 수 있는 제출 고르기 (eligible_submissions)
  ② 참여자마다 대표 기록 1개 (best_per_member)   ← DB: DISTINCT ON
  ③ 화면 4자리로 반올림한 값으로 줄 세우고 공동 순위 (assign_ranks)   ← Python

가상 예시 (Accuracy)
  참여자 K7QM 0.88, 0.93 → 0.93 / X2PA 0.91 / R9WD 0.85, 0.91 / M4TE 0.88
  → 1위 K7QM 0.9300, 2위 X2PA 0.9100, 2위 R9WD 0.9100, 4위 M4TE 0.8800
"""
from dataclasses import dataclass
from decimal import Decimal

from competitions.models import Project

from .models import Submission, display_score


@dataclass
class Entry:
    rank: int
    submission: Submission
    score_text: str

    @property
    def member(self):
        return self.submission.member


def eligible_submissions(project):
    """리더보드에 올라갈 수 있는 제출 = 채점됨.

    채점 대기·형식 오류·시스템 오류·마감 후 접수는 점수가 없거나 채점 대상이 아니므로 빠진다.
    부적합(확정)도 빠진다(R12) → 그 참여자의 다음 기록이 자동으로 대표가 된다.
    부적합 '제안'은 빠지지 않는다(R11: 제안만으로는 순위 유지). 미검증도 올린다(R7).
    """
    return (Submission.objects.filter(project=project, status=Submission.Status.SCORED, score__isnull=False)
            .exclude(review_status=Submission.ReviewStatus.REJECTED))


def is_higher(project):
    return project.direction == Project.Direction.HIGHER


def best_per_member(project):
    """참여자마다 대표 기록 1개 (R7-①).

    PostgreSQL DISTINCT ON (member): member별로 정렬한 뒤 각 member의 '첫 줄'만 남긴다.
    정렬: 좋은 점수 순 → 같은 점수면 나중에 채점된 것(R10 '같은 참여자 동일 점수는 최신부터'와 맞춤).
    1차·2차 마감 구간을 합쳐서 고른다 [가정: 리더보드는 프로젝트당 하나].
    """
    best_first = "-score" if is_higher(project) else "score"
    return list(
        eligible_submissions(project)
        .select_related("member__user")
        .order_by("member", best_first, "-scored_at", "-pk")
        .distinct("member")
    )


def assign_ranks(texts):
    """이미 좋은 순으로 정렬된 점수 문자열에 공동 순위를 매긴다 (R7-③, 1224 방식).

    ["0.9300", "0.9100", "0.9100", "0.8800"] → [1, 2, 2, 4]
    앞 사람과 점수가 같으면 앞 사람 순위를 그대로, 다르면 '내 앞에 있는 사람 수 + 1'.
    """
    ranks = []
    for i, text in enumerate(texts):
        ranks.append(ranks[-1] if i and text == texts[i - 1] else i + 1)
    return ranks


def build(project):
    """화면에 쓸 Entry 목록. 좋은 순서대로."""
    best = [(display_score(s.score), s) for s in best_per_member(project)]
    higher = is_higher(project)
    # 반올림한 값(Decimal)으로 정렬. 같은 점수끼리는 먼저 채점된 사람이 위(순위는 같음)
    best.sort(key=lambda pair: pair[1].scored_at)
    best.sort(key=lambda pair: Decimal(pair[0]), reverse=higher)
    ranks = assign_ranks([text for text, _ in best])
    return [Entry(rank=r, submission=s, score_text=text) for r, (text, s) in zip(ranks, best)]

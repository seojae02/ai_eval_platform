"""채점: 제출 CSV와 정답 CSV를 id로 맞춰 지표를 계산한다 (R4).

worker만 이 파일을 부른다. 웹 요청 안에서는 채점하지 않는다.
→ 정답 파일을 읽는 코드가 worker 한 곳에만 있다(R9), 채점이 느려도 웹 화면이 멈추지 않는다.

외부 라이브러리(sklearn, numpy) 없이 표준 라이브러리만 쓴다.
→ 계산식을 줄 단위로 설명할 수 있고, sklearn과 같은 값이 나오는지는 테스트로 확인한다.
"""
import math

from django.db import transaction
from django.utils import timezone

from competitions import csvcheck, storage
from competitions.models import Project, ProjectFile

from .models import Submission, SubmissionFile

Metric = Project.Metric
Status = Submission.Status


class ScoringError(Exception):
    """채점할 수 없는 상태(파일이 사라짐, 검사를 통과했던 CSV가 지금은 깨짐 등). 서버 쪽 문제로 본다."""


# ── 지표 4종 ────────────────────────────────────────────────────
# y_true, y_pred: 같은 순서(같은 id 순서)로 맞춘 값 목록

def accuracy(y_true, y_pred):
    """맞힌 개수 / 전체. 예) 정답 [0,1,1,0,1], 예측 [0,1,0,0,1] → 4/5 = 0.8"""
    return sum(t == p for t, p in zip(y_true, y_pred)) / len(y_true)


def macro_f1(y_true, y_pred, labels):
    """레이블마다 F1을 구해 단순 평균한다(레이블 수가 적은 클래스도 같은 비중).

    F1 = 2·정밀도·재현율 / (정밀도 + 재현율)
    그 레이블을 한 번도 예측하지 않았으면 정밀도를 0으로 본다(sklearn zero_division=0과 같음).
    """
    f1s = []
    for label in labels:
        tp = sum(t == label and p == label for t, p in zip(y_true, y_pred))
        fp = sum(t != label and p == label for t, p in zip(y_true, y_pred))
        fn = sum(t == label and p != label for t, p in zip(y_true, y_pred))
        f1s.append(0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn))   # 2TP/(2TP+FP+FN) = F1을 정리한 식
    return sum(f1s) / len(f1s)


def rmse(y_true, y_pred):
    """오차 제곱 평균의 제곱근. 큰 오차를 더 크게 벌한다."""
    return math.sqrt(sum((t - p) ** 2 for t, p in zip(y_true, y_pred)) / len(y_true))


def mae(y_true, y_pred):
    """오차 절댓값의 평균."""
    return sum(abs(t - p) for t, p in zip(y_true, y_pred)) / len(y_true)


def compute(project, answer_table, pred_table):
    """두 표를 id로 맞춘 뒤 프로젝트 지표로 점수를 낸다.

    제출 CSV의 행 순서는 상관없다. 정답 파일의 id 순서로 예측 값을 다시 줄 세운다.
    """
    # 완료 단계에서 이미 검사했지만, 채점 직전에 한 번 더 확인한다(그 사이 파일이 바뀌었으면 채점하지 않음)
    try:
        answer_ids = csvcheck.check_ids(answer_table, project.id_column)
        csvcheck.check_prediction(pred_table, project, answer_ids)
    except csvcheck.CsvError as e:
        raise ScoringError(f"채점 직전 검사 실패: {e}") from None

    predicted = dict(zip(pred_table.column(project.id_column), pred_table.column(project.target_column)))
    y_true = answer_table.column(project.target_column)
    y_pred = [predicted[i] for i in answer_ids]

    if project.metric == Metric.ACCURACY:
        return accuracy(y_true, y_pred)
    if project.metric == Metric.MACRO_F1:
        return macro_f1(y_true, y_pred, project.allowed_labels)
    y_true, y_pred = [float(v) for v in y_true], [float(v) for v in y_pred]
    if project.metric == Metric.RMSE:
        return rmse(y_true, y_pred)
    if project.metric == Metric.MAE:
        return mae(y_true, y_pred)
    raise ScoringError(f"알 수 없는 지표: {project.metric}")


# ── 제출 한 건 채점 ─────────────────────────────────────────────
def _read_tables(submission):
    project = submission.project
    answer = project.files.filter(kind=ProjectFile.Kind.ANSWER).first()
    pred = submission.files.filter(kind=SubmissionFile.Kind.CSV).first()
    if answer is None or pred is None:
        raise ScoringError("정답 파일 또는 예측 CSV가 없습니다.")
    try:
        answer_table = csvcheck.parse(storage.read_bytes(answer.area, answer.stored_name))
        pred_table = csvcheck.parse(storage.read_bytes("submissions", pred.stored_name))
    except (OSError, csvcheck.CsvError) as e:
        raise ScoringError(f"파일을 읽지 못했습니다: {e}") from None
    return answer_table, pred_table


def score_submission(submission_id, now=None):
    """'채점 대기' 제출 하나를 채점해 '채점됨'으로 바꾼다. 점수를 돌려준다.

    호출하는 쪽(worker)이 트랜잭션을 연다. 여기서 예외가 나면 점수도 상태도 저장되지 않는다.
    """
    now = now or timezone.now()
    sub = Submission.objects.select_for_update().select_related("project").get(pk=submission_id)
    if sub.status != Status.QUEUED:
        # 이미 채점됐거나(작업이 두 번 실행됨) 다른 상태 → 아무것도 바꾸지 않는다
        return sub.score
    answer_table, pred_table = _read_tables(sub)
    value = compute(sub.project, answer_table, pred_table)
    if not math.isfinite(value):
        raise ScoringError("점수가 유한한 수가 아닙니다.")
    sub.score = value
    sub.status = Status.SCORED
    sub.scored_at = now
    sub.save(update_fields=["score", "status", "scored_at"])
    return value


SYSTEM_ERROR_MESSAGE = "채점 중 서버 오류가 발생했으며, 이 제출은 횟수에서 빠졌으니 다시 제출해 주세요"


def mark_system_error(submission_id):
    """R4: 서버·채점 오류 → '시스템 오류'. COUNTED에 없는 상태이므로 횟수에서 자동으로 빠진다.

    참여자에게는 원인(경로·예외 내용)을 보여 주지 않는다. 자세한 내용은 Job.last_error(서버)에만 남긴다.
    """
    with transaction.atomic():
        return Submission.objects.filter(pk=submission_id, status=Status.QUEUED).update(
            status=Status.ERROR, error_message=SYSTEM_ERROR_MESSAGE,
        )

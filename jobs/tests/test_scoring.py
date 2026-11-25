"""지표 4종 계산. 기대값은 손으로 계산했고 sklearn.metrics와 같은 값이다(주석에 계산 과정)."""
import math

import pytest

from competitions import csvcheck
from competitions.models import Project
from submissions import scoring

M = Project.Metric


def test_accuracy_5개_중_4개_맞으면_0_8():
    assert scoring.accuracy(["0", "1", "1", "0", "1"], ["0", "1", "0", "0", "1"]) == 0.8


def test_macro_f1_레이블별_F1의_단순_평균():
    # a: TP2 FP0 FN1 → 4/5=0.8 / b: TP1 FP2 FN0 → 2/4=0.5 / c: 한 번도 못 맞힘 → 0
    # (0.8 + 0.5 + 0) / 3 = 0.4333...  ← sklearn f1_score(average="macro")와 같다
    y_true = ["a", "a", "a", "b", "c"]
    y_pred = ["a", "a", "b", "b", "b"]
    assert scoring.macro_f1(y_true, y_pred, ["a", "b", "c"]) == pytest.approx(1.3 / 3)


def test_macro_f1_예측하지_않은_레이블도_평균에_0으로_들어간다():
    # 전부 a로 찍기: a는 F1 = 2·3/(2·3+2+0) = 0.75, b는 0 → 0.375 (accuracy 0.6보다 낮다)
    assert scoring.macro_f1(["a", "a", "a", "b", "b"], ["a"] * 5, ["a", "b"]) == pytest.approx(0.375)


def test_rmse와_mae():
    # 오차 [0, 0, 2] → RMSE = √(4/3) ≈ 1.1547, MAE = 2/3
    assert scoring.rmse([1.0, 2.0, 3.0], [1.0, 2.0, 5.0]) == pytest.approx(math.sqrt(4 / 3))
    assert scoring.mae([1.0, 2.0, 3.0], [1.0, 2.0, 5.0]) == pytest.approx(2 / 3)


def _project(metric, labels=()):
    # DB에 저장하지 않는 프로젝트 객체(계산만 확인)
    return Project(metric=metric, id_column="id", target_column="label", expected_rows=3, allowed_labels=list(labels))


def test_제출_CSV의_행_순서가_달라도_id로_맞춰_채점한다():
    answer = csvcheck.parse(b"id,label\n1,0\n2,1\n3,1\n")
    pred = csvcheck.parse(b"label,id\n1,3\n1,2\n0,1\n")   # 컬럼 순서·행 순서 모두 뒤섞음 → 모두 맞음
    assert scoring.compute(_project(M.ACCURACY, ["0", "1"]), answer, pred) == 1.0


def test_회귀_채점():
    answer = csvcheck.parse(b"id,label\n1,1.5\n2,2.0\n3,3.0\n")
    pred = csvcheck.parse(b"id,label\n1,1.5\n2,2.0\n3,5.0\n")
    assert scoring.compute(_project(M.MAE), answer, pred) == pytest.approx(2 / 3)


def test_채점_직전_검사에서_깨진_CSV면_ScoringError():
    answer = csvcheck.parse(b"id,label\n1,0\n2,1\n3,1\n")
    pred = csvcheck.parse(b"id,label\n1,0\n2,1\n")   # 행이 하나 모자람
    with pytest.raises(scoring.ScoringError):
        scoring.compute(_project(M.ACCURACY, ["0", "1"]), answer, pred)

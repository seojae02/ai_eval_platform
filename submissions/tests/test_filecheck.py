"""제출 파일 내용 검사 (R15, 노트북·모델). DB 없이 함수만 확인한다."""
import json
import pickle

import pytest

from competitions import csvcheck
from competitions.csvcheck import CsvError
from competitions.models import Project
from submissions import filecheck

from .conftest import MODEL, NOTEBOOK

CLS = Project(metric=Project.Metric.ACCURACY, id_column="id", target_column="label",
              expected_rows=3, allowed_labels=["0", "1"])
REG = Project(metric=Project.Metric.RMSE, id_column="id", target_column="label", expected_rows=2)
IDS3 = ["1", "2", "3"]


def pred(data, project=CLS, ids=IDS3):
    return csvcheck.check_prediction(csvcheck.parse(data), project, ids)


# ── 예측 CSV (R15) ────────────────────────────────────────────
def test_컬럼_순서가_달라도_이름이_맞으면_통과():
    assert pred(b"label,id\n0,1\n1,2\n0,3\n") == IDS3


@pytest.mark.parametrize("data, message", [
    (b"id,pred\n1,0\n2,1\n3,0\n", "정확히 id, label"),                 # 컬럼 이름 틀림
    (b"id,label,x\n1,0,a\n2,1,b\n3,0,c\n", "정확히 id, label"),        # 컬럼 추가
    (b"id,label\n1,0\n2,1\n", "3개여야 하는데 2개"),                     # 행 수
    (b"id,label\n1,0\n2,1\n9,0\n", "빠진 id 1개, 없는 id 1개"),          # id 불일치
    (b"id,label\n1,0\n2,cat?\n3,0\n", "허용되지 않은 예측 값이 1개"),     # ARCHITECTURE 11-1 R15 예시
    (b"id,label\n1,0\n2,\n3,0\n", "빈 칸"),
    (b'id,label\n1,0\n2,"=HYPERLINK(""http://x"")"\n3,0\n', "허용되지 않은"),   # CSV 수식 삽입 시도
])
def test_잘못된_예측_CSV는_이유와_함께_거부한다(data, message):
    with pytest.raises(CsvError, match=message):
        pred(data)


@pytest.mark.parametrize("value", ["abc", "nan", "inf", "=1+1"])
def test_회귀_예측은_유한한_숫자만(value):
    with pytest.raises(CsvError):
        pred(f"id,label\n1,0.5\n2,{value}\n".encode(), REG, ["1", "2"])


def test_오류_문구에는_개수만_있고_id_값은_없다():
    with pytest.raises(CsvError) as e:
        pred(b"id,label\n1,0\n2,1\n777,0\n")
    assert "777" not in str(e.value) and "빠진 id 1개, 없는 id 1개" in str(e.value)


# ── 노트북 ───────────────────────────────────────────────────
def test_올바른_노트북은_통과():
    filecheck.check_notebook(NOTEBOOK)


@pytest.mark.parametrize("data, message", [
    (b"print('hello')", "올바른 Jupyter 노트북"),                       # 이름만 .ipynb인 파이썬 파일
    (json.dumps({"cells": "x"}).encode(), "올바른 Jupyter 노트북"),     # JSON이지만 노트북 구조가 아님
    ("{}".encode("utf-16"), "UTF-8"),
    (NOTEBOOK.replace(b'"code"', b'"markdown"').replace(b', "outputs": [], "execution_count": null', b""), "코드 셀이 없습니다"),
])
def test_잘못된_노트북은_거부한다(data, message):
    with pytest.raises(CsvError, match=message):
        filecheck.check_notebook(data)


# ── 모델 파일 (magic bytes) ───────────────────────────────────
@pytest.mark.parametrize("name, head", [
    ("model.pkl", MODEL[:8]),
    ("model.joblib", b"\x78\x9c\x00\x00\x00\x00\x00\x00"),
    ("model.pt", b"PK\x03\x04\x00\x00\x00\x00"),
    ("model.h5", b"\x89HDF\r\n\x1a\n"),
    ("MODEL.KERAS", b"PK\x03\x04\x14\x00\x00\x00"),     # 대문자 확장자도 인정
])
def test_형식에_맞는_모델_파일은_통과(name, head):
    filecheck.check_model(name, head)


@pytest.mark.parametrize("name, head, message", [
    ("model.exe", b"MZ\x90\x00", "형식은"),                     # 허용하지 않는 확장자
    ("model.pkl", b"PK\x03\x04\x00\x00\x00\x00", ".pkl 형식이 아닙니다"),   # 확장자만 바꾼 zip
    ("model.h5", b"hello wo", ".h5 형식이 아닙니다"),
    ("model", MODEL[:8], "형식은"),                             # 확장자 없음
])
def test_확장자와_내용이_안_맞는_모델_파일은_거부한다(name, head, message):
    with pytest.raises(CsvError, match=message):
        filecheck.check_model(name, head)


def test_웹_서버는_pickle을_로드하지_않는다(monkeypatch):
    """pickle.load는 파일 안의 코드를 실행할 수 있다. 검사 중에 불리면 바로 실패하게 만든다."""
    def forbidden(*args, **kwargs):
        raise AssertionError("pickle을 로드하면 안 됩니다")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    filecheck.check_model("model.pkl", MODEL[:8])

"""CSV 내용 검증 단위 테스트. DB 없이 함수만 확인한다."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from competitions import csvcheck
from competitions.csvcheck import CsvError
from competitions.models import Project

CLS = Project(metric=Project.Metric.ACCURACY, id_column="id", target_column="label")
REG = Project(metric=Project.Metric.RMSE, id_column="id", target_column="label")


def answer(data, project=CLS):
    return csvcheck.check_answer(csvcheck.parse(data), project)


def test_정상_정답에서_행수와_레이블을_계산한다():
    info = answer(b"id,label\n1,cat\n2,dog\n3,cat\n")

    assert info["rows"] == 3 and info["labels"] == ["cat", "dog"]


def test_엑셀_BOM이_붙어_있어도_읽는다():
    assert answer("﻿id,label\n1,0\n".encode("utf-8"))["rows"] == 1


@pytest.mark.parametrize("data, message", [
    ("id,label\n1,고양이\n".encode("cp949"), "UTF-8"),            # 엑셀 기본 저장(CP949)
    (b"PK\x03\x04\x00\x00binary", "텍스트 CSV"),                  # xlsx 등 바이너리
    (b"id,label\n", "데이터 행이 없습니다"),
    (b"id,label\n1,0\n2\n", "3번째 줄의 칸 수"),
    (b"id,id\n1,0\n", "같은 이름의 컬럼"),
    (b"id,label\n1,0\n1,1\n", "중복된 값이 1개"),
    (b"id,label\n1,0\n,1\n", "빈 칸"),
    (b"no,label\n1,0\n", "'id' 컬럼이 없습니다"),
    (b"id,answer\n1,0\n", "정답 컬럼 'label'이 없습니다"),
])
def test_잘못된_정답_파일은_이유와_함께_거부한다(data, message):
    with pytest.raises(CsvError, match=message):
        answer(data)


@pytest.mark.parametrize("value", ["abc", "nan", "inf"])
def test_회귀_정답에는_유한한_숫자만_허용한다(value):
    with pytest.raises(CsvError):
        answer(f"id,label\n1,0.5\n2,{value}\n".encode(), REG)


def test_회귀_정답은_레이블_목록이_비어_있다():
    assert answer(b"id,label\n1,0.5\n2,1.25\n", REG)["labels"] == []


def test_분류인데_값_종류가_너무_많으면_회귀가_아닌지_묻는다(monkeypatch):
    monkeypatch.setattr(csvcheck, "MAX_LABELS", 2)
    with pytest.raises(CsvError, match="회귀 지표"):
        answer(b"id,label\n1,a\n2,b\n3,c\n")


def test_공개_테스트_feature에_정답_컬럼이_있으면_거부한다():   # R9
    with pytest.raises(CsvError, match="정답 컬럼 'label'이 들어 있으니"):
        csvcheck.check_test_features(csvcheck.parse(b"id,x,label\n1,0.1,0\n"), CLS)


def test_feature와_정답의_id가_다르면_개수를_알려준다():
    with pytest.raises(CsvError, match="feature에만 1개, 정답에만 2개"):
        csvcheck.compare_ids(["1", "2", "9"], ["1", "2", "3", "4"])


def test_크기_상한을_넘으면_거부한다(monkeypatch):
    monkeypatch.setattr(csvcheck, "MAX_BYTES", 10)
    with pytest.raises(CsvError, match="너무 큽니다"):
        csvcheck.read_upload(SimpleUploadedFile("a.csv", b"id,label\n1,0\n2,1\n"))


# ── 학습 데이터에서 특성이 같은 행 세기 ───────────────────────────────
def duplicates(data, project=REG):
    return csvcheck.count_duplicate_rows(csvcheck.parse(data), project)


def test_특성이_같은_행을_센다_첫_번째는_세지_않는다():
    # 1·2·3번 행은 특성(x, y)이 같고 4번 행은 다르다 → 같은 행이 3번 나오면 2개
    assert duplicates(b"id,x,y,label\n1,0.1,2,5\n2,0.1,2,5\n3,0.1,2,6\n4,0.3,2,5\n") == 2


def test_id와_정답_컬럼은_비교에서_뺀다():
    assert duplicates(b"id,x,label\n1,7,5\n2,7,9\n") == 1


def test_같은_행이_없으면_0():
    assert duplicates(b"id,x,label\n1,7,5\n2,8,5\n") == 0


def test_비교할_특성_컬럼이_없으면_0():
    assert duplicates(b"id,label\n1,5\n2,5\n") == 0


def test_글자_그대로_비교한다():
    assert duplicates(b"id,x,label\n1,7.4,5\n2,7.40,5\n") == 0

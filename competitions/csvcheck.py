"""관리자가 올리는 CSV(정답·학습 데이터·테스트 feature)의 내용 검증.

확장자를 믿지 않고 내용을 직접 읽는다 [RFP 7쪽 "확장자뿐 아니라 파일 내용을 검증"].
외부 라이브러리(pandas) 없이 표준 csv 모듈만 쓴다 → 동작을 줄 단위로 설명할 수 있다.
"""
import csv
import io
import math

MAX_BYTES = 20 * 1024 * 1024   # 20MB
MAX_LABELS = 1000              # 분류인데 서로 다른 값이 이보다 많으면 회귀를 잘못 고른 것으로 본다


class CsvError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 검증 실패 메시지."""


class Table:
    def __init__(self, header, rows):
        self.header = header
        self.rows = rows

    def column(self, name):
        i = self.header.index(name)
        return [row[i].strip() for row in self.rows]


def read_upload(uploaded):
    if uploaded.size > MAX_BYTES:
        raise CsvError(f"파일이 너무 큽니다(최대 {MAX_BYTES // 1024 // 1024}MB).")
    data = uploaded.read()
    if not data.strip():
        raise CsvError("빈 파일입니다.")
    return data


def parse(data):
    # ① 바이너리 파일(이미지, 엑셀 xlsx, pickle 등)은 NUL 바이트가 들어 있다
    if b"\x00" in data:
        raise CsvError("텍스트 CSV 파일이 아닙니다.")
    # ② 인코딩: UTF-8만 받는다(엑셀이 붙이는 BOM은 허용)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvError("UTF-8로 저장한 CSV만 받습니다(엑셀: 'CSV UTF-8(쉼표로 분리)' 형식으로 저장)") from None
    # ③ CSV 문법
    try:
        lines = [(no, row) for no, row in enumerate(csv.reader(io.StringIO(text, newline="")), start=1) if row]
    except csv.Error:
        raise CsvError("CSV 형식을 읽을 수 없습니다.") from None
    if not lines:
        raise CsvError("빈 파일입니다.")

    # ④ 머리글: 빈 이름·중복 이름 금지
    header = [h.strip() for h in lines[0][1]]
    if any(h == "" for h in header):
        raise CsvError("머리글(첫 줄)에 이름이 빈 컬럼이 있습니다.")
    if len(set(header)) != len(header):
        raise CsvError("머리글(첫 줄)에 같은 이름의 컬럼이 두 번 이상 있습니다.")

    # ⑤ 모든 줄의 칸 수가 머리글과 같아야 한다
    rows = []
    for no, row in lines[1:]:
        if len(row) != len(header):
            raise CsvError(f"{no}번째 줄의 칸 수({len(row)})가 머리글({len(header)})과 다릅니다.")
        rows.append(row)
    if not rows:
        raise CsvError("머리글만 있고 데이터 행이 없습니다.")
    return Table(header, rows)


def check_ids(table, id_column):
    """id 컬럼이 있고, 비어 있지 않고, 중복이 없는지. 순서 그대로 돌려준다."""
    if id_column not in table.header:
        raise CsvError(f"'{id_column}' 컬럼이 없습니다.")
    ids = table.column(id_column)
    if any(v == "" for v in ids):
        raise CsvError(f"'{id_column}' 컬럼에 빈 칸이 있습니다.")
    if len(set(ids)) != len(ids):
        raise CsvError(f"'{id_column}' 컬럼에 중복된 값이 {len(ids) - len(set(ids))}개 있습니다.")
    return ids


def check_answer(table, project):
    """정답 파일 검사 + 행 수·허용 레이블 자동 계산."""
    ids = check_ids(table, project.id_column)
    if project.target_column not in table.header:
        raise CsvError(f"정답 컬럼 '{project.target_column}'이 없습니다.")
    values = table.column(project.target_column)
    if any(v == "" for v in values):
        raise CsvError("정답 컬럼에 빈 칸이 있습니다.")

    if project.is_classification:
        labels = sorted(set(values))
        if len(labels) > MAX_LABELS:
            raise CsvError(f"분류 지표인데 서로 다른 정답 값이 {len(labels)}개이니 회귀 지표(RMSE, MAE)가 맞는지 확인하세요")
    else:
        for v in values:
            try:
                number = float(v)
            except ValueError:
                raise CsvError(f"회귀 지표인데 숫자가 아닌 정답 값이 있습니다(예: '{v[:20]}').") from None
            if not math.isfinite(number):
                raise CsvError("정답 값에 nan 또는 inf가 있습니다.")
        labels = []
    return {"ids": ids, "rows": len(ids), "labels": labels}


def check_test_features(table, project):
    """공개 테스트 feature 검사. 참여자에게 공개되는 파일이므로 정답 컬럼이 있으면 안 된다(R9)."""
    if project.target_column in table.header:
        raise CsvError(
            f"참여자에게 공개되는 파일인데 정답 컬럼 '{project.target_column}'이 들어 있으니 "
            "정답 컬럼을 빼고 올려 주세요"
        )
    return check_ids(table, project.id_column)


def count_duplicate_rows(table, project):
    """학습 데이터에서 특성이 똑같은 행의 수. 첫 번째 행은 세지 않는다(같은 행이 3번 있으면 2).

    id 컬럼과 정답 컬럼은 비교에서 뺀다. 글자 그대로 비교하므로 '7.4'와 '7.40'은 다른 값으로 본다.
    """
    skip = {project.id_column, project.target_column}
    keep = [i for i, name in enumerate(table.header) if name not in skip]
    if not keep:
        return 0
    seen = {tuple(row[i].strip() for i in keep) for row in table.rows}
    return len(table.rows) - len(seen)


def check_prediction(table, project, answer_ids):
    """참여자 제출 CSV 검사 (R15). 형식만 보고 정답 값과는 비교하지 않는다.

    오류 문구에는 개수와 참여자 자신의 값만 넣는다 → 문구로 정답을 알아낼 수 없다.
    칸이 정확히 [id, 예측] 2개이고 id·예측 값이 정해진 범위 안에 있어야 하므로,
    '=HYPERLINK(...)' 같은 엑셀 수식 문자열은 이 검사를 통과할 수 없다(CSV 수식 삽입 방지).
    """
    expected = [project.id_column, project.target_column]
    if sorted(table.header) != sorted(expected):
        raise CsvError(f"컬럼은 정확히 {', '.join(expected)} 2개여야 합니다(지금: {', '.join(table.header)[:100]}).")
    ids = check_ids(table, project.id_column)
    if len(ids) != project.expected_rows:
        raise CsvError(f"데이터 행이 {project.expected_rows}개여야 하는데 {len(ids)}개입니다.")
    missing, extra = set(answer_ids) - set(ids), set(ids) - set(answer_ids)
    if missing or extra:
        raise CsvError(f"id가 테스트 feature와 다릅니다(빠진 id {len(missing)}개, 없는 id {len(extra)}개).")

    values = table.column(project.target_column)
    if any(v == "" for v in values):
        raise CsvError("예측 컬럼에 빈 칸이 있습니다.")
    if project.is_classification:
        bad = [v for v in values if v not in set(project.allowed_labels)]
        if bad:
            raise CsvError(f"허용되지 않은 예측 값이 {len(bad)}개 있습니다(예: '{bad[0][:20]}', "
                           f"허용 값: {', '.join(project.allowed_labels)[:200]})")
    else:
        for v in values:
            try:
                number = float(v)
            except ValueError:
                raise CsvError(f"예측 값은 숫자여야 합니다(예: '{v[:20]}').") from None
            if not math.isfinite(number):
                raise CsvError("예측 값에 nan 또는 inf가 있습니다.")
    return ids


def compare_ids(feature_ids, answer_ids):
    a, b = set(feature_ids), set(answer_ids)
    if a != b:
        raise CsvError(
            f"테스트 feature와 정답 파일의 id가 다릅니다 "
            f"(feature에만 {len(a - b)}개, 정답에만 {len(b - a)}개)."
        )

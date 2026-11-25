"""검증 V1~V7의 판정 로직 (docs/ARCHITECTURE.md 5절 verification). Docker·DB 없이 값만 받아 판정한다 → 단위 테스트가 쉽다.

V1~V6은 부정의 근거, V7은 '모르고 한 학습·평가 방법 실수' 안내다. V7은 전체 판정과 일괄 승인에 영향을 주지 않는다.

판정 단계
    ok       문제 없음
    warn     주의: 사람이 한 번 볼 것
    bad      의심: 관리자1이 노트북을 꼭 열어 볼 것
    unknown  판정 불가: 시간 초과·라이브러리 없음 등. '부적합'이 아니라 '수동 검토 필요'

시스템은 판정을 내리지 않는다. 근거를 모아 관리자1(제안)·관리자2(결정)에게 보여 줄 뿐이다(R10~R12).
"""
import ast
import math
import re
from pathlib import PurePosixPath

from django.conf import settings

from competitions import csvcheck

LEVELS = ("ok", "unknown", "warn", "bad")   # 뒤로 갈수록 심각


def check(level, summary, **details):
    return {"level": level, "summary": summary, "details": details}


def worst(levels):
    return max(levels, key=LEVELS.index) if levels else "ok"


ADVISORY = {"v3", "v7"}   # 참고·안내용 검사: 전체 판정·'문제 된 검사'·일괄 승인 판단에 넣지 않는다
# V3: 정상 학생끼리도 같은 오답이 74~91% 나왔다(이진 분류, 두 클래스 혼동이 많은 과제, 2026-11-24 점검)
#     → 판정 근거로 쓰지 않고 관리자에게 '참고'로만 보인다. V7: 방법 실수·노트북 CV 신뢰성 안내(부정 확정 아님)


def overall(checks):
    """전체 판정: 의심(bad) > 주의(warn) > V1 판정 불가(unknown: 핵심 검사를 못 했으니 수동 검토) > 문제 없음."""
    levels = [c["level"] for key, c in checks.items() if key not in ADVISORY]
    if "bad" in levels or "warn" in levels:
        return worst(levels)
    return "unknown" if checks.get("v1", {}).get("level") == "unknown" else "ok"


# ── 값 비교 ────────────────────────────────────────────────────
def values_equal(a, b, classification):
    """분류: 글자가 같거나 숫자로 같으면(1 과 1.0) 같다. 회귀: 상대 오차 0.01% 이내면 같다(실행마다 생기는 미세한 차이)."""
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    return x == y if classification else math.isclose(x, y, rel_tol=1e-4, abs_tol=1e-6)


def as_mapping(table, project):
    """CSV 표 → {id: 값}. 컬럼이 없으면 CsvError."""
    for col in (project.id_column, project.target_column):
        if col not in table.header:
            raise csvcheck.CsvError(f"'{col}' 컬럼이 없습니다.")
    return dict(zip(table.column(project.id_column), table.column(project.target_column)))


def agreement(mine, other, classification):
    """두 예측의 일치율. 기준은 내 id 전체(상대에 없는 id는 불일치)."""
    if not mine:
        return 0.0
    same = sum(1 for i, v in mine.items() if i in other and values_equal(v, other[i], classification))
    return same / len(mine)


def match_level(rate):
    if rate >= settings.VERIFY_MATCH_OK:
        return "ok"
    return "warn" if rate >= settings.VERIFY_MATCH_WARN else "bad"


# ── V1: 노트북 재실행 일치율 (핵심) ──────────────────────────────
RERUN_STATUS = {
    "timeout": "실행 시간 제한을 넘었습니다",
    "oom": "메모리 제한을 넘었습니다",
    "kernel_died": "커널이 죽었습니다",
    "cell_error": "셀 실행 중 오류가 났습니다",
    "no_csv": "예측 CSV를 만들지 않았습니다",
    "error": "실행기 오류",
}


def v1_rerun(result, rerun_bytes, submitted, project):
    """샌드박스에서 노트북을 다시 돌려 나온 CSV와 제출 CSV를 id별로 비교한다.

    ≥99% 일치 / 90~99% 주의 / <90% 불일치. 재실행 자체가 안 되면 '판정 불가 — 수동 검토 필요'(부적합 아님).
    """
    status = result.get("status")
    if status != "ok":
        reason = RERUN_STATUS.get(status, status or "알 수 없음")
        missing = re.search(r"FileNotFoundError: .*?No such file or directory: '([^']+)'", result.get("message", ""))
        if status == "cell_error" and missing:
            # '의심'으로 올리지 않는다: 정직한 학생도 'data/train.csv'처럼 자기 PC 경로를 쓰면 같은 오류가 난다
            name = PurePosixPath(missing.group(1)).name
            reason = f"노트북이 샌드박스에 없는 '{name}' 파일을 읽으려다 멈췄습니다(제공 파일은 train.csv, test.csv뿐)"
            return check("unknown", f"판정 불가 — {reason}, 수동 검토가 필요합니다.",
                         status=status, message=result.get("message", ""), missing_file=name)
        return check("unknown", f"판정 불가 — {reason}, 수동 검토가 필요합니다.",
                     status=status, message=result.get("message", ""))
    try:
        rerun = as_mapping(csvcheck.parse(rerun_bytes), project)
    except csvcheck.CsvError as e:
        return check("unknown", f"판정 불가 — 재실행 결과 CSV를 읽지 못했습니다({e}).", status="bad_csv")
    rate = agreement(submitted, rerun, project.is_classification)
    words = {"ok": "일치", "warn": "주의", "bad": "불일치"}
    level = match_level(rate)
    return check(level, f"재실행 결과와 제출 CSV {rate:.1%} {words[level]}",
                 rate=rate, elapsed=result.get("elapsed"), csv_name=result.get("csv_name"))


# ── V2: 같은 파일 ───────────────────────────────────────────────
def v2_same_files(same_csv, same_notebook, perfect=False):
    """다른 참여자의 제출과 SHA-256이 같은 파일. 인자는 [(별칭, 실명, 제출 번호, 누가 먼저)] 목록.

    누가 원본인지는 알 수 없다 → 양쪽 모두 표시하고, 누가 먼저 냈는지를 함께 보여 준다.
    perfect: 이 CSV가 정답과 완전히 같다(만점). 만점끼리는 같을 수밖에 없으므로 '주의'로 낮춘다(오탐 방지).
    """
    if same_csv and perfect:
        return check("warn", f"다른 참여자 {len(same_csv)}명과 예측 CSV가 똑같지만, 모두 만점이라 같을 수밖에 없으니 노트북으로 판단하세요.",
                     csv=same_csv, notebook=same_notebook)
    if same_csv:
        return check("bad", f"다른 참여자 {len(same_csv)}명과 예측 CSV가 똑같습니다.",
                     csv=same_csv, notebook=same_notebook)
    if same_notebook:
        return check("warn", f"다른 참여자 {len(same_notebook)}명과 노트북 파일이 똑같습니다.",
                     csv=[], notebook=same_notebook)
    return check("ok", "다른 참여자와 똑같은 파일이 없습니다.", csv=[], notebook=[])


# ── V3: 오답 일치율 ─────────────────────────────────────────────
MIN_WRONG = 5   # 내 오답이 이보다 적으면 오답 일치율은 우연의 영향이 커서 판정하지 않는다
V3_REFERENCE = 0.7   # 이 비율 이상이면 관리자 화면에 '참고'로 표시(판정에는 쓰지 않음, 예전 '주의' 기준값)


def v3_wrong_overlap(mine, answer, others, project):
    """남의 CSV를 조금 고쳐 낸 경우를 찾는다.

    분류: 내 오답 중 상대와 '같은 오답'인 비율. 서로 독립으로 만든 모델이 같은 줄에서 같은 오답을 내는 일은 드물다.
    회귀: 예측 값이 같은 줄의 비율(독립 모델의 실수는 소수점까지 같기 어렵다). '같은 값'은 V1·V5와 같은 기준
          (values_equal, 상대 오차 0.01%) — 글자로 비교하면 남의 CSV를 엑셀로 열어 다시 저장해 표기만 바뀐 것
          (452318.73649123457 → 452318.736491235, 유효숫자 15자리)을 놓친다(개발 중 회귀 과제 시험: 글자 7%, 값 100%).
    others: [(별칭, 실명, 제출 번호, 누가 먼저, {id: 값})]
    """
    cls = project.is_classification
    if not others:
        return check("ok", "비교할 다른 참여자 제출이 없습니다.", top=None)
    if cls:
        wrong = [i for i, v in mine.items() if not values_equal(v, answer.get(i, ""), True)]
        if len(wrong) < MIN_WRONG:
            return check("ok", f"오답이 {len(wrong)}개뿐이라 오답 일치율은 판정하지 않습니다.", wrong=len(wrong), top=None)
        scored = [(sum(1 for i in wrong if i in o and values_equal(mine[i], o[i], True)) / len(wrong), alias, name, pk, first)
                  for alias, name, pk, first, o in others]
        label = "같은 오답"
    else:
        scored = [(sum(1 for i, v in mine.items() if i in o and values_equal(v, o[i], False)) / len(mine), alias, name, pk, first)
                  for alias, name, pk, first, o in others]
        label = "같은 예측 값(상대 오차 0.01% 이내)"
    rate, alias, name, pk, first = max(scored, key=lambda t: t[0])
    # 판정은 내지 않는다(ADVISORY): 정상 짝도 같은 오답이 높게 나오는 과제가 있다. 높으면 관리자 화면에 '참고' 표시만
    reference = rate >= V3_REFERENCE
    return check("ok", f"가장 비슷한 참여자({alias})와 {label} {rate:.0%}"
                       + (" (참고: 정상 제출끼리도 높게 나올 수 있어 판정에 쓰지 않음)" if reference else ""),
                 rate=rate, reference=reference,
                 top={"alias": alias, "real_name": name, "submission_id": pk, "first": first}, compared=len(others))


# ── V4: 노트북 정적 검사 ────────────────────────────────────────
FIT_NAMES = {"fit", "fit_transform", "fit_predict", "train", "partial_fit"}
PREDICT_NAMES = {"predict", "predict_proba", "fit_predict", "forward", "__call__"}
SAVE_NAMES = {"to_csv", "writerow", "writerows", "savetxt"}
LONG_LITERAL = 50   # 이보다 긴 숫자·문자 목록이 코드에 그대로 적혀 있으면 '예측 값 직접 입력' 의심
# 실행할 때마다 결과가 달라질 수 있는 것들(random_state를 주지 않으면). 정직해도 V1 불일치가 날 수 있다
RANDOM_NAMES = {
    "RandomForestClassifier", "RandomForestRegressor", "ExtraTreesClassifier", "ExtraTreesRegressor",
    "GradientBoostingClassifier", "GradientBoostingRegressor", "HistGradientBoostingClassifier",
    "HistGradientBoostingRegressor", "MLPClassifier", "MLPRegressor", "SGDClassifier", "SGDRegressor",
    "DecisionTreeClassifier", "DecisionTreeRegressor", "BaggingClassifier", "BaggingRegressor",
    "AdaBoostClassifier", "AdaBoostRegressor", "KMeans", "train_test_split",
}
# 전체 시드를 고정하는 호출이 있으면 random_state가 없어도 재현된다
GLOBAL_SEED_NAMES = {"seed", "manual_seed", "set_seed", "set_random_seed"}   # np.random.seed, torch.manual_seed 등
# 샌드박스가 넣어 주는 입력 파일(sandbox/runner.py INPUT_NAMES와 같게 유지). 이 밖의 파일을 읽으면 '주의'
PROVIDED_FILES = {"train.csv", "test.csv"}
# 파일을 읽는 함수 / 파일을 만드는 함수(노트북이 스스로 만든 파일을 다시 읽는 것은 정상이다)
READ_NAMES = {"read_csv", "read_table", "read_excel", "read_json", "read_parquet", "read_pickle", "read_feather",
              "loadtxt", "genfromtxt", "load", "open"}
WRITE_NAMES = {"to_csv", "to_excel", "to_json", "to_parquet", "to_pickle", "to_feather", "savetxt", "save", "dump"}
PATH_KEYWORDS = {"filepath_or_buffer", "path", "path_or_buf", "io", "fname", "file", "filename"}


def _numbers_in_text(text):
    """문자열이 숫자만 공백·쉼표·세미콜론으로 나열한 것이면 그 개수, 아니면 0. 가상 예시: "3\n8\n1" → 3, "id,label" → 0."""
    parts = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    return len(parts) if parts and all(re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", p) for p in parts) else 0


def _strip_magics(source):
    # %matplotlib, !pip 같은 줄은 파이썬 문법이 아니므로 빼고 분석한다
    return "\n".join("" if line.lstrip().startswith(("%", "!")) else line for line in source.splitlines())


def _call_name(node):
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _path_args(node):
    """호출의 인자 중 글자 그대로 적힌 경로들. 변수·f-string으로 만든 경로는 알 수 없으므로 뺀다."""
    args = list(node.args) + [k.value for k in node.keywords if k.arg in PATH_KEYWORDS]
    return [a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _open_mode(node):
    mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
    return mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else "r"


def _file_access(node, name, reads, writes, urls):
    """읽기·쓰기 함수 호출에서 파일 이름(경로를 뗀 이름)과 인터넷 주소를 모은다.

    가상 예시: pd.read_csv('/kaggle/input/wine/WineQT.csv') → reads에 'WineQT.csv'
               joblib.dump(model, 'model.pkl') → writes에 'model.pkl' (뒤에서 load해도 정상)
    """
    if name == "open" and any(ch in _open_mode(node) for ch in "wax"):
        name = "dump"   # 쓰기 모드 open은 '만드는' 쪽
    if name in READ_NAMES:
        for path in _path_args(node)[:1]:
            if re.match(r"(?i)(https?|ftp)://", path):
                urls.append(path)
            else:
                reads.append(PurePosixPath(path.replace("\\", "/")).name)
    elif name in WRITE_NAMES:
        writes.extend(PurePosixPath(p.replace("\\", "/")).name for p in _path_args(node))


def v4_static(nb):
    """노트북 파일(dict, nbformat 4)을 실행하지 않고 읽어서 의심스러운 점을 찾는다."""
    cells = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
    flags = []   # (단계, 설명, 코드). 코드는 화면·일괄 승인이 문구 대신 읽는 이름(문구를 바꿔도 안 깨짐)
    counts = [c.get("execution_count") for c in cells if c.get("execution_count") is not None]
    if cells and not counts:
        flags.append(("warn", "실행 기록이 없습니다(한 번도 실행하지 않은 노트북).", "not_run"))
    elif counts != sorted(counts) or len(set(counts)) != len(counts):
        flags.append(("warn", "셀 실행 순서가 위에서 아래가 아닙니다(순서를 바꿔 실행했거나 편집함).", "order"))

    calls, syntax_errors, long_literals, dataset_calls, uses_shell = set(), 0, 0, [], False
    unseeded = []   # random_state 없이 만든 무작위 모델 이름
    reads, writes, urls = [], [], []   # 노트북이 읽는 파일, 만드는 파일, 인터넷 주소
    text = "\n".join("".join(c.get("source", "")) if isinstance(c.get("source"), list) else c.get("source", "")
                     for c in cells)
    imports_datasets = bool(re.search(r"sklearn\.datasets|from\s+sklearn\s+import\s+[^\n]*\bdatasets\b", text))
    for c in cells:
        src = c.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        uses_shell |= any(line.lstrip().startswith("!") for line in src.splitlines())
        try:
            tree = ast.parse(_strip_magics(src))
        except SyntaxError:
            syntax_errors += 1
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                calls.add(name)
                if imports_datasets and name.startswith(("load_", "fetch_")):
                    dataset_calls.append(name)
                if name in RANDOM_NAMES and not any(k.arg == "random_state" for k in node.keywords):
                    unseeded.append(name)
                _file_access(node, name, reads, writes, urls)
            elif isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= LONG_LITERAL \
                    and all(isinstance(e, ast.Constant) for e in node.elts):
                long_literals += 1
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _numbers_in_text(node.value) >= LONG_LITERAL:
                long_literals += 1   # 엑셀 열을 문자열로 붙여 넣은 값("3\n8\n1\n…" → .split())도 직접 입력이다

    if dataset_calls:
        flags.append(("bad", f"정답이 들어 있는 공개 데이터를 불러옵니다({', '.join(sorted(set(dataset_calls)))}).", "dataset"))
    if long_literals:
        flags.append(("bad", f"값 {LONG_LITERAL}개 이상을 코드에 직접 적은 목록이 {long_literals}개 있습니다(예측 값 직접 입력 의심).", "literal"))
    outside = sorted(set(reads) - PROVIDED_FILES - set(writes))
    if outside:
        # '주의'까지만: 정직한 학생도 자기 PC의 중간 파일을 읽을 수 있다. 정답이 든 원본인지는 사람이 노트북을 보고 판단
        flags.append(("warn", f"제공하지 않은 파일을 읽습니다({', '.join(outside[:5])}), "
                              "정답이 든 외부 데이터(예: Kaggle 원본)인지 노트북에서 확인하세요.", "outside_file"))
    if urls:
        flags.append(("warn", f"인터넷 주소에서 데이터를 읽습니다({', '.join(sorted(set(urls))[:3])}), "
                              "샌드박스에는 네트워크가 없어 다시 실행되지 않습니다.", "url"))
    if not calls & FIT_NAMES:
        flags.append(("warn", "모델을 학습하는 코드(fit 등)가 없습니다.", "no_fit"))
    if not calls & PREDICT_NAMES:
        flags.append(("warn", "예측하는 코드(predict 등)가 없습니다.", "no_predict"))
    if not calls & SAVE_NAMES:
        flags.append(("warn", "CSV를 저장하는 코드(to_csv 등)가 없습니다.", "no_save"))
    if uses_shell:
        flags.append(("warn", "셸 명령(!)을 씁니다.", "shell"))
    if syntax_errors:
        flags.append(("warn", f"파이썬으로 읽을 수 없는 셀이 {syntax_errors}개 있습니다.", "syntax"))

    unseeded = sorted(set(unseeded)) if not calls & GLOBAL_SEED_NAMES else []
    if unseeded:
        # '참고': 부정의 근거가 아니므로 판정 단계를 올리지 않는다. V1 불일치를 읽을 때 쓰는 정보
        flags.append(("info", f"참고: 무작위 시드(random_state) 없이 만든 것이 있습니다({', '.join(unseeded)}), "
                              "다시 실행하면 결과가 조금 달라질 수 있습니다.", "unseeded"))

    level = worst([lvl for lvl, _, _ in flags if lvl != "info"])
    serious = sum(lvl != "info" for lvl, _, _ in flags)
    summary = (f"확인할 점 {serious}개" if serious else "의심스러운 점이 없습니다.") + (" (참고 1개)" if unseeded else "")
    return check(level, summary, flags=[msg for _, msg, _ in flags], code_cells=len(cells), unseeded=unseeded,
                 outside_files=outside, codes=[code for lvl, _, code in flags if lvl != "info"])


# ── V6: 제출 CSV 수정 흔적 (V1 재실행 결과 × V5 모델 예측 × 제출 CSV) ─────────
def _mapping_or_none(raw, project):
    if not raw:
        return None
    try:
        return as_mapping(csvcheck.parse(raw), project)
    except csvcheck.CsvError:
        return None


def _is_number_text(value):
    try:
        float(value)
    except ValueError:
        return False
    return True


def decode_label_codes(model_pred, labels):
    """모델 파일이 레이블 이름 대신 숫자 코드(0, 1, …)를 예측했으면 이름으로 바꾼다. 바꿨으면 (새 매핑, True).

    과제 PDF처럼 OrdinalEncoder·LabelEncoder로 레이블을 숫자로 바꿔 학습하면, 모델 파일은 2.0 같은 코드를 내고
    제출 CSV에는 inverse_transform한 이름(STANDING)이 들어간다 → 글자로 비교하면 정직한 제출도 V5 0% '불일치'가 된다.
    두 인코더 모두 학습 레이블을 이름순으로 정렬해 0부터 번호를 매기므로, 같은 규칙으로 되돌린다(고정 규칙 → 맞춰 줄 여지 없음).
    바꾸지 않는 경우: 레이블 자체가 숫자(손글씨 0~9 등, 코드와 구분 불가), 정수가 아니거나 범위 밖 값이 하나라도 있음.
    가상 예시: labels = [LAYING, SITTING, STANDING], 모델 예측 {101: '2.0', 102: '0.0'} → {101: 'STANDING', 102: 'LAYING'}
    """
    names = sorted(set(labels or []))
    if not names or not model_pred or any(_is_number_text(n) for n in names):
        return model_pred, False
    decoded = {}
    for i, v in model_pred.items():
        try:
            x = float(v)
        except ValueError:
            return model_pred, False
        if not (x.is_integer() and 0 <= x < len(names)):
            return model_pred, False
        decoded[i] = names[int(x)]
    return decoded, True


def model_mapping(raw, project, labels=None):
    """모델 예측 CSV → ({id: 값} 또는 None, 숫자 코드를 이름으로 바꿨는지). 분류 과제에서만 바꾼다."""
    mapping = _mapping_or_none(raw, project)
    if mapping and project.is_classification:
        return decode_label_codes(mapping, labels if labels is not None else project.allowed_labels)
    return mapping, False


DECODED_NOTE = "모델이 레이블 대신 숫자 코드(0, 1, …)를 예측해, 학습 레이블 이름순(OrdinalEncoder·LabelEncoder 방식)으로 바꿔 비교했습니다"


def _closer_to_answer(sub_value, rerun_value, answer_value, classification):
    """제출 값이 재실행 값보다 정답 쪽인가. 분류: 제출만 정답. 회귀: 제출이 정답에 더 가까움."""
    if rerun_value is None or answer_value is None:
        return False
    if classification:
        return values_equal(sub_value, answer_value, True) and not values_equal(rerun_value, answer_value, True)
    try:
        s, r, a = float(sub_value), float(rerun_value), float(answer_value)
    except ValueError:
        return False
    return abs(s - a) < abs(r - a)


def v6_edit_trace(rerun_bytes, model_bytes, submitted, answer, project, labels=None, unseeded=None):
    """노트북 재실행 결과와 모델 예측이 '서로는' 같은데 제출 CSV만 다르면, CSV를 만든 뒤 바뀐 것이다.

    V1·V5는 각각 제출 CSV하고만 비교한다 → 몇 줄만 고치면 둘 다 96~99% '주의'에 그친다(예시 대회 demo_stu05).
    여기서는 세 결과를 서로 비교하고, 다른 행이 '정답 쪽'으로 쏠렸는지를 근거 수치와 함께 보여 준다.
    판정이 아니라 보조 신호다: 정답 쪽 쏠림은 손수정뿐 아니라 '더 좋은 예전 모델의 CSV'에서도 생긴다 → 사람이 노트북을 본다.
    V1·V5 결과는 바꾸지 않는다.

    가상 예시: 재실행=모델(100%), 제출만 6행 다름, 6행 모두 제출이 정답 → 의심(추가 검토)
               재실행≠모델(시드 미고정) → 검사하지 않음(문제 없음), V1의 시드 안내가 설명한다
    """
    cls = project.is_classification
    rerun, (model, _) = _mapping_or_none(rerun_bytes, project), model_mapping(model_bytes, project, labels)
    if not rerun or not model:
        return check("unknown", "비교할 노트북 재실행 결과나 모델 예측이 없어 확인하지 않았습니다.")
    consistency = agreement(rerun, model, cls)
    if consistency < settings.VERIFY_MATCH_OK:
        why = (f"시드 없이 만든 것({', '.join(unseeded)})이 있어 실행할 때마다 결과가 달라졌을 수 있음, V1 안내 참고"
               if unseeded else "노트북과 모델 파일이 서로 다른 결과를 냄, V5 결과 참고")
        return check("ok", f"노트북 재실행과 모델 예측이 서로 {consistency:.1%}만 같아 수정 흔적은 보지 않았습니다"
                           f"({why}).", consistency=consistency)
    diff = [i for i, v in submitted.items() if not (i in rerun and values_equal(v, rerun[i], cls))]
    if not diff:
        # 거꾸로: 제출 = 재실행인데 모델 예측과 다른 몇 행이 정답 쪽이면, 노트북이 예측을 손으로 고친 값으로 덮어쓴 흔적이다
        # (가상 예시: 자신 없는 그림의 라벨을 fixes = {id: 숫자}로 적음 → V1 100%, V5 99.4%로 둘 다 '일치', 예시 대회 demo_stu06)
        over = [i for i, v in submitted.items() if i in model and not values_equal(v, model[i], cls)]
        toward = [i for i in over if _closer_to_answer(submitted[i], model.get(i), answer.get(i), cls)]
        if over and len(toward) / len(over) >= settings.VERIFY_EDIT_TOWARD:
            level = "bad" if len(over) >= settings.VERIFY_EDIT_MIN_ROWS else "warn"
            return check(level, f"제출 CSV는 노트북 재실행 결과와 같지만 모델 파일의 예측과 {len(over)}개 행에서 다르고, "
                                f"그중 {len(toward)}개에서 제출 값이 정답에 더 가깝습니다. 노트북이 모델 예측을 다른 값"
                                "(코드에 직접 적은 라벨 등)으로 덮어썼을 수 있으니 노트북에서 확인하세요.",
                         consistency=consistency, diff_rows=0, model_diff_rows=len(over), toward_rows=len(toward),
                         toward_ratio=len(toward) / len(over), sample_ids=over[:10])
        return check("ok", f"제출 CSV가 노트북 재실행 결과와 같고, 모델 예측과 재실행 결과도 {consistency:.1%} 일치합니다.",
                     consistency=consistency, diff_rows=0)
    toward = [i for i in diff if _closer_to_answer(submitted[i], rerun.get(i), answer.get(i), cls)]
    ratio = len(toward) / len(diff)
    details = {"consistency": consistency, "diff_rows": len(diff), "toward_rows": len(toward),
               "toward_ratio": ratio, "sample_ids": diff[:10]}
    evidence = (f"제출 CSV와 노트북 재실행 결과가 {len(diff)}개 행에서 다릅니다. "
                f"모델 예측과 재실행 결과는 {consistency:.1%} 일치하며, "
                f"다른 {len(diff)}개 행 중 {len(toward)}개에서 제출 값이 정답에 더 가깝습니다. ")
    if len(diff) >= settings.VERIFY_EDIT_MIN_ROWS and ratio >= settings.VERIFY_EDIT_TOWARD:
        return check("bad", evidence + "추가 검토가 필요합니다(CSV 손수정, 또는 노트북과 다른 예전 결과를 냈을 수 있음).",
                     **details)
    return check("warn", evidence + "제출 CSV가 이 노트북이 만든 결과가 아닐 수 있습니다(예전 결과를 냈을 수 있음).",
                 **details)


# ── V1 + V5 + V4: 시드 미고정 안내 ──────────────────────────────
def seed_hint(v1, v5, v4):
    """V1이 어긋났는데 모델 파일의 예측은 제출 CSV와 맞으면, 시드 미고정일 수 있다는 안내를 V1에 붙인다.

    판정 단계는 낮추지 않는다: 외운 답을 돌려주는 가짜 모델도 V5는 맞을 수 있기 때문이다. 사람이 노트북을 보고 판단한다.
    가상 예시: 시드 없는 신경망 → V1 62.5% 불일치, V5 100% 일치 → V1에 안내 문구, V4에 '참고: MLPClassifier'
    """
    if v1.get("level") not in ("warn", "bad") or v5.get("level") != "ok" or "rate" not in v5.get("details", {}):
        return v1
    hint = (f"모델 파일의 예측은 제출 CSV와 {v5['details']['rate']:.1%} 일치하므로 "
            "노트북이 무작위 시드를 고정하지 않아 다시 실행할 때마다 결과가 달라졌을 수 있습니다(정직한 제출에서도 생김)")
    unseeded = v4.get("details", {}).get("unseeded") or []
    hint += (f", 노트북에서 시드 없이 만든 것: {', '.join(unseeded)}" if unseeded
             else ", 노트북에서 random_state 고정 여부를 확인하세요")
    v1["details"]["hint"] = hint
    return v1


# ── V5: 모델 파일 ───────────────────────────────────────────────
def v5_model(result, pred_bytes, submitted, project, labels=None):
    """샌드박스에서 모델을 로드해 test feature로 예측한 결과와 제출 CSV를 비교한다(sklearn·joblib).

    PyTorch는 로드 성공·파라미터 수만, 그 밖의 형식은 판정 불가(실행 이미지에 라이브러리 없음).
    labels: 학습 데이터의 레이블 목록(없으면 허용 레이블). 모델이 숫자 코드를 내면 이것으로 이름을 되찾는다(decode_label_codes).
    """
    status = result.get("status")
    if status == "ok":
        try:
            as_mapping(csvcheck.parse(pred_bytes), project)
        except csvcheck.CsvError as e:
            return check("unknown", f"판정 불가 — 모델 예측 결과를 읽지 못했습니다({e}).")
        pred, decoded = model_mapping(pred_bytes, project, labels)
        rate = agreement(submitted, pred, project.is_classification)
        level = match_level(rate)
        picked = result.get("input_note")   # 실행기가 모델의 학습 열 이름대로 test 열을 골랐으면 그 사실(runner.model_input)
        return check(level, f"모델({result.get('model_type', '?')})의 예측과 제출 CSV {rate:.1%} 일치"
                            + (" (숫자 코드 → 레이블 이름으로 바꿔 비교)" if decoded else "")
                            + (f" — {picked}" if picked else ""),
                     rate=rate, model_type=result.get("model_type"), **({"note": DECODED_NOTE} if decoded else {}),
                     **({"input_note": picked} if picked else {}))
    if status == "loaded":
        return check("ok", f"모델 로드 성공: {result.get('message', '')}")
    if status == "no_predict":
        return check("warn", f"모델 파일에 predict가 없습니다({result.get('model_type', '?')}).")
    if status == "error":
        # 로드나 예측이 실패: 가짜 모델 파일일 수도, 전처리가 모델 밖에 있어서일 수도 있다 → 사람이 확인
        return check("warn", f"모델을 로드하거나 예측하지 못했습니다: {result.get('message', '')}")
    return check("unknown", f"판정 불가 — {result.get('message') or RERUN_STATUS.get(status, status)}",
                 status=status)


# ── V7: 학습·평가 방법 점검 (부정이 아니라 '모르고 한 실수' 안내) ─────────────
# 노트북을 실행하지 않고 코드와 '학생이 실행해 남긴 출력'만 읽는다. 찾는 것: 검증 점수가 실제보다 좋게 나오는 흔한 실수.
# details: items(항목마다 단계·이름·학생 문구·관리자 근거), student(학생에게 보일 주의·참고), flags(관리자 화면 한 줄씩),
#          codes, duplicate_rows, cv(노트북 CV ↔ 서버 점수 비교). 전체 판정과 일괄 승인에는 넣지 않는다(ADVISORY).
CV_CALL_NAMES = {"cross_val_score", "cross_validate", "cross_val_predict", "learning_curve", "validation_curve",
                 "GridSearchCV", "RandomizedSearchCV", "HalvingGridSearchCV", "HalvingRandomSearchCV"}
SEARCH_NAMES = {"GridSearchCV", "RandomizedSearchCV", "HalvingGridSearchCV", "HalvingRandomSearchCV"}
# 검증을 한다는 신호(위 호출 + 분할기·홀드아웃)
VALIDATION_NAMES = CV_CALL_NAMES | {"train_test_split", "KFold", "StratifiedKFold", "RepeatedKFold",
                                    "RepeatedStratifiedKFold", "ShuffleSplit", "StratifiedShuffleSplit", "LeaveOneOut"}
# 같은 행을 지우거나 한 묶음으로 나누는 처리(이 중 하나라도 있으면 중복을 다뤘다고 본다)
DEDUP_NAMES = {"drop_duplicates", "duplicated", "GroupKFold", "StratifiedGroupKFold", "GroupShuffleSplit",
               "LeaveOneGroupOut", "LeavePGroupsOut"}
# 데이터에서 값을 배우는 전처리. CV 전에 전체 데이터로 fit하면 검증 폴드 정보가 섞인다
PREPROCESS_NAMES = {"StandardScaler", "MinMaxScaler", "RobustScaler", "MaxAbsScaler", "Normalizer", "PCA",
                    "SelectKBest", "SelectPercentile", "SelectFromModel", "RFE", "PolynomialFeatures",
                    "SimpleImputer", "KNNImputer", "QuantileTransformer", "PowerTransformer"}
# 과제 지표(Project.Metric 값) → scoring 문자열에 들어 있어야 하는 말
METRIC_SCORING = {"rmse": "root_mean_squared", "mae": "absolute", "accuracy": "accuracy", "macro_f1": "f1_macro"}
OUTPUT_NAMES = {"print", "display_scores", "display"}   # 학생이 CV 점수를 '찍는' 호출(PDF 8쪽 display_scores 포함)
GAP_PERCENT = 5.0   # 노트북 CV가 서버보다 이만큼(%) 좋으면 '참고'. 임시 기준이며 판정에 쓰지 않는다
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
RULE_LINE = re.compile(rf"^CV_(RMSE|MAE|ACCURACY|MACRO_F1)_MEAN\s*=\s*({_NUM})\s*$", re.M)
PDF_BLOCK = re.compile(rf"Scores:\s*\[([^\]]*)\]\s*\n\s*Mean:\s*({_NUM})\s*\n\s*Standard deviation:\s*({_NUM})")
# 미니 프로젝트 #1 16쪽 형식: 점수 배열 출력 → 그 평균 한 줄. 배열은 array([...]) 또는 print(scores)의 [...]
ARRAY_OUT = re.compile(r"array\(\[([^\[\]]*)\]")
LIST_OUT = re.compile(r"^[ \t]*\[([^\[\]]*)\][ \t]*$", re.M)
LONE_NUM = re.compile(rf"^[ \t]*(?:[^\d\n:=]{{0,30}}[:=][ \t]*)?(?:np\.float(?:16|32|64)\()?({_NUM})\)?[ \t]*$", re.M)
# 계산하지 않고 숫자로 적은 CV 출력: print("Mean: 0.97"), print("accuracy:", 0.97)
CV_LITERAL = re.compile(r"(?:\bmean\b|평균|CV_[A-Z0-9_]+_MEAN)\s*[:=]?\s*[-+]?\d*\.\d+", re.I)
CV_WORD = re.compile(r"mean|평균|score|점수|accuracy|정확도|rmse|mae|f1", re.I)
MEAN_NAMES = {"mean", "average"}
RERUN_CV_PERCENT = 1.0   # 노트북 CV와 다시 실행한 CV가 이만큼(%) 넘게 다르면 주의, 그 아래는 참고(시드 미고정에서도 생김)


def _ordered(node):
    """노드를 소스 순서(부모 먼저, 앞에서 뒤로)로 돌려준다. ast.walk는 너비 우선이라 '먼저 한 것'을 알 수 없다."""
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _ordered(child)


def _where(pos):
    return f"코드 셀 {pos[0] + 1} {pos[1]}번째 줄"


def _is_preprocess_fit(name, node, preprocessors):
    """scaler.fit_transform(X) 또는 StandardScaler().fit(X) 처럼 전처리 객체를 맞추는 호출인가."""
    if name not in ("fit", "fit_transform") or not isinstance(node.func, ast.Attribute):
        return False
    target = node.func.value
    return ((isinstance(target, ast.Name) and target.id in preprocessors)
            or (isinstance(target, ast.Call) and _call_name(target) in PREPROCESS_NAMES))


def _scoring(node):
    return next((k.value.value for k in node.keywords if k.arg == "scoring" and isinstance(k.value, ast.Constant)
                 and isinstance(k.value.value, str)), None)


def _is_mse_source(node):
    """루트를 씌우지 않은 MSE를 만드는 호출: mean_squared_error(...) 또는 scoring='neg_mean_squared_error'."""
    sc = _scoring(node)
    return _call_name(node) == "mean_squared_error" or bool(sc and "mean_squared" in sc and "root" not in sc)


def _metric_kind(node, env):
    """식의 값이 루트를 씌운 RMSE 계열('rmse')인지, 루트 없는 MSE('mse')인지, 상관없는지(None).

    가상 예시: np.sqrt(-cross_val_score(..., scoring='neg_mean_squared_error')) → 'rmse'
               -mse_scores (mse_scores = cross_val_score(..., scoring='neg_mean_squared_error')) → 'mse'
    """
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name == "sqrt":
            return "rmse" if any(_metric_kind(a, env) for a in node.args) else None
        if name == "root_mean_squared_error":
            return "rmse"
        if _is_mse_source(node):
            return "mse"
        sc = _scoring(node)
        if sc and "root_mean_squared" in sc:
            return "rmse"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow) \
            and isinstance(node.right, ast.Constant) and node.right.value == 0.5:
        return "rmse" if _metric_kind(node.left, env) else None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    kinds = {_metric_kind(c, env) for c in ast.iter_child_nodes(node)}
    return "mse" if "mse" in kinds else "rmse" if "rmse" in kinds else None


def _base_name(target):
    while isinstance(target, (ast.Subscript, ast.Attribute)):
        target = target.value
    return target.id if isinstance(target, ast.Name) else None


def _stdout(cell):
    texts = []
    for o in cell.get("outputs", []) or []:
        if o.get("output_type") == "stream" and o.get("name", "stdout") == "stdout":
            t = o.get("text", "")
            texts.append("".join(t) if isinstance(t, list) else t)
    return "".join(texts)


def _texts(cell):
    """셀 출력 중 글자로 된 것(표준 출력, 셀 결과 text/plain)을 출력 순서대로."""
    texts = []
    for o in cell.get("outputs", []) or []:
        if o.get("output_type") == "stream" and o.get("name", "stdout") == "stdout":
            t = o.get("text", "")
        elif o.get("output_type") == "execute_result":
            t = (o.get("data") or {}).get("text/plain", "")
        else:
            continue
        texts.append("".join(t) if isinstance(t, list) else str(t))
    return texts


def _floats(body):
    """'0.94, 0.93' 또는 '0.94 0.93' → [0.94, 0.93]. 소수점이 없는 값(오차 행렬 같은 정수)이 섞이면 None."""
    parts = [p for p in re.split(r"[\s,]+", body.strip()) if p]
    if not parts or not all(re.fullmatch(rf"{_NUM}", p) and re.search(r"[.eE]", p) for p in parts):
        return None
    values = [float(p) for p in parts]
    return values if all(math.isfinite(v) for v in values) else None


def _array_events(text):
    """출력 글자 하나에서 (위치, 'array', 값 목록)과 (위치, 'num', 값)을 순서대로. 배열 안의 줄은 숫자로 세지 않는다."""
    events, spans = [], []
    for rx in (ARRAY_OUT, LIST_OUT):
        for m in rx.finditer(text):
            values = _floats(m.group(1))
            spans.append(m.span())
            if values:
                events.append((m.start(), "array", values))
    for m in LONE_NUM.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            events.append((m.start(), "num", float(m.group(1))))
    return sorted(events, key=lambda e: e[0])


def score_arrays(cells):
    """출력에 찍힌 점수 배열 전부: [(셀 번호, 값 목록)]. Scores: [...]도 포함한다."""
    found = []
    for ci, c in enumerate(cells):
        for text in _texts(c):
            found += [(ci, values) for _, kind, values in _array_events(text) if kind == "array"]
            for m in PDF_BLOCK.finditer(text):
                if (values := _floats(m.group(1))) and len(values) >= 2:
                    found.append((ci, values))
    return found


def _array_mean_blocks(cells):
    """점수 배열 출력 뒤에 그 평균이 한 줄로 찍힌 곳(미니 프로젝트 #1 16쪽) → [(셀 번호, 평균, 폴드 수, 표준편차)].

    가상 예시: 셀 4 결과 array([0.94, 0.93, …]) → 셀 5 결과 np.float64(0.9151) 이고 배열 평균이 0.9151 → 인정
    """
    blocks, last = [], None
    for ci, c in enumerate(cells):
        for text in _texts(c):
            for _, kind, value in _array_events(text):
                if kind == "array":
                    last = value if len(value) >= 2 else None
                elif last and abs(sum(last) / len(last) - value) <= 1e-4 * max(1.0, abs(value)):
                    m = sum(last) / len(last)
                    blocks.append((ci, value, len(last), math.sqrt(sum((v - m) ** 2 for v in last) / len(last))))
                    last = None   # 같은 배열의 평균을 두 번 세지 않는다
    return blocks


def read_cv_mean(cells, project):
    """학생이 실행해 남긴 출력에서 교차검증 평균을 읽는다 → (값과 출처 dict 또는 None, 건너뛴 이유).

    인정하는 형식: ① 한 줄 규칙 CV_RMSE_MEAN=0.5687(지표 이름이 과제와 같아야 함, 여러 줄이면 마지막)
                   ② PDF 8쪽 display_scores 형식 Scores:[...] / Mean: / Standard deviation: (Scores 평균 = Mean 이어야 함)
                   ③ ②가 없을 때만: 미니 프로젝트 #1 16쪽 형식 — 점수 배열 출력 다음에 그 평균 한 줄
    ②(또는 ③)가 여러 개면 어느 것이 최종인지 모르므로 비교하지 않는다.
    """
    rules, blocks = [], []
    for ci, c in enumerate(cells):
        text = _stdout(c)
        rules += [(ci, m.group(1).lower(), float(m.group(2))) for m in RULE_LINE.finditer(text)]
        for m in PDF_BLOCK.finditer(text):
            try:
                scores = [float(p) for p in re.split(r"[\s,]+", m.group(1).strip()) if p]
                mean, std = float(m.group(2)), float(m.group(3))
            except ValueError:
                continue
            if len(scores) < 2 or not all(math.isfinite(v) for v in scores + [mean, std]):
                continue
            if abs(sum(scores) / len(scores) - mean) > 1e-4 * max(1.0, abs(mean)):
                continue   # Mean이 Scores의 평균이 아니면 CV 결과로 인정하지 않는다
            blocks.append((ci, mean, len(scores), std))
    token = f"CV_{project.metric.upper()}_MEAN"
    kind = "Scores/Mean 출력"
    if not blocks:
        blocks, kind = _array_mean_blocks(cells), "점수 배열과 평균 출력"
    if rules:
        ci, metric, value = rules[-1]
        if metric != project.metric:
            return None, f"출력의 CV_{metric.upper()}_MEAN이 과제 지표({project.get_metric_display()})와 달라 비교하지 않았습니다."
        return {"value": value, "source": f"코드 셀 {ci + 1}의 {token} 줄"}, ""
    if not blocks:
        return None, f"노트북 출력에서 교차검증 평균(Mean: 또는 {token}=)을 찾지 못해 비교하지 않았습니다."
    if len(blocks) > 1:
        return None, (f"교차검증 결과({kind})가 {len(blocks)}개라 어느 것이 최종인지 알 수 없어 비교하지 않았습니다"
                      f"(최종 값을 {token}=값 으로 출력하면 비교합니다).")
    ci, mean, k, std = blocks[0]
    return {"value": mean, "source": f"코드 셀 {ci + 1}의 {kind}({k}개 폴드, 표준편차 {std:.4g})"}, ""


def _is_number(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _number(node):
    return -node.operand.value if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) else \
        (node.operand.value if isinstance(node, ast.UnaryOp) else node.value)


def _literal_cv(node):
    """print("Mean: 0.97") / print("accuracy:", 0.97) 처럼 계산 없이 숫자로 적은 점수 출력인가.

    f-string의 계산 부분({scores.mean():.4f})은 숫자로 보지 않는다. 정수(폴드 수 10 등)는 세지 않는다.
    """
    args = list(node.args) + [k.value for k in node.keywords if k.arg not in ("sep", "end", "file", "flush")]
    strs = [n.value for a in args for n in ast.walk(a)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    floats = [a for a in args if _is_number(a) and isinstance(_number(a), float)]
    return any(CV_LITERAL.search(s) for s in strs) or bool(floats and any(CV_WORD.search(s) for s in strs))


def _literal_list(node):
    """np.mean([0.889, 0.892, …]) 처럼 숫자를 손으로 옮겨 적은 목록의 평균 → 값 목록(2개 이상) 또는 None."""
    if _call_name(node) not in MEAN_NAMES or not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
        return None
    elts = node.args[0].elts
    return [float(_number(e)) for e in elts] if len(elts) >= 2 and all(_is_number(e) for e in elts) else None


def _v7_result(items, empty_summary="점수를 부풀리는 흔한 실수를 찾지 못했습니다.", **details):
    """V7 항목 목록 → 판정 dict. v7_method와 v7_rerun(재실행 뒤 덧붙임)이 같이 쓴다."""
    n = {lvl: sum(i["level"] == lvl for i in items) for lvl in ("warn", "unknown", "info")}
    level = "warn" if n["warn"] else "unknown" if n["unknown"] else "ok"
    parts = [f"확인할 점 {n['warn']}개"] * bool(n["warn"]) + [f"판정 불가 {n['unknown']}개"] * bool(n["unknown"])
    if parts:
        summary = ", ".join(parts) + (f" (참고 {n['info']}개)" if n["info"] else "")
    else:
        summary = f"확인할 점 없음 (참고 {n['info']}개)" if n["info"] else empty_summary
    return check(level, summary, items=items, codes=[i["code"] for i in items],
                 flags=[f"{i['label']}: {i['text']} [근거] {i['evidence']}" for i in items],
                 student=[{"level": i["level"], "label": i["label"], "text": i["text"]}
                          for i in items if i["level"] in ("warn", "info")],
                 **details)


def v7_method(nb, project, duplicate_rows=None, score=None):
    """학습·평가 방법에서 점수가 실제보다 좋게 나오는 흔한 실수를 찾는다.

    duplicate_rows: 학습 데이터에서 특성이 같은 행 수(파일 등록 때 계산, 모르면 None)
    score: 이 제출의 서버 점수(노트북 CV와 비교, 없으면 비교하지 않음)
    가상 예시: 같은 행이 196개인데 drop_duplicates도 GroupKFold도 없이 KFold(shuffle=True)로 CV → '중복 처리 확인 필요'(주의)
    """
    cells = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
    calls = []              # ((셀 번호, 줄, 칸), 이름, 호출 노드)
    outputs = []            # (위치, 'mse'|'rmse') — 점수를 찍는 호출의 인자가 어떤 값인지
    preprocessors, env = set(), {}   # 전처리 객체 변수 / 변수 → 'mse'|'rmse'
    loop_cv = picks_best = best_score = groups_kw = False
    literal_prints, literal_means = [], []   # 숫자로 적은 점수 출력 / 손으로 옮겨 적은 점수 목록의 평균
    for ci, c in enumerate(cells):
        src = c.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        try:
            tree = ast.parse(_strip_magics(src))
        except SyntaxError:
            continue   # 읽을 수 없는 셀은 V4가 알린다
        for node in _ordered(tree):
            if isinstance(node, ast.Assign):
                kind = _metric_kind(node.value, env)
                for t in node.targets:
                    name = _base_name(t)
                    if isinstance(t, ast.Name) and kind is None:
                        env.pop(name, None)       # 다른 값으로 덮어씀
                    elif isinstance(t, ast.Name):
                        env[name] = kind
                    elif name and kind:
                        env[name] = kind   # results[name] = rmse → results 전체를 rmse로 본다
                if isinstance(node.value, ast.Call) and _call_name(node.value) in PREPROCESS_NAMES:
                    preprocessors.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.For) and any(isinstance(n, ast.Call) and _call_name(n) in CV_CALL_NAMES
                                                   for n in ast.walk(node)):
                loop_cv = True          # 반복문 안에서 CV = 여러 후보를 같은 CV로 비교
            elif isinstance(node, ast.Attribute) and node.attr == "best_score_":
                best_score = True
            if isinstance(node, ast.Call):
                name = _call_name(node)
                pos = (ci, node.lineno, node.col_offset)
                calls.append((pos, name, node))
                groups_kw |= any(k.arg == "groups" for k in node.keywords)
                picks_best |= name in ("min", "max", "sorted") and any(k.arg == "key" for k in node.keywords)
                if name in OUTPUT_NAMES:
                    kinds = {_metric_kind(a, env) for a in list(node.args) + [k.value for k in node.keywords]}
                    if kinds - {None}:
                        outputs.append((pos, "rmse" if "rmse" in kinds else "mse"))
                    if _literal_cv(node):
                        literal_prints.append((pos, ast.unparse(node)[:80]))
                if (values := _literal_list(node)) is not None:
                    literal_means.append({"values": values, "where": _where(pos)})

    items = []

    def add(code, level, label, text, evidence):
        items.append({"code": code, "level": level, "label": label, "text": text, "evidence": evidence})

    # a. 점수를 계산하지 않고 숫자로 적어 출력(다시 실행해도 같은 숫자가 나오므로 재실행 비교로는 못 잡는다)
    if literal_prints:
        add("cv_literal", "warn", "CV 값 직접 입력 확인 필요",
            "점수를 계산하지 않고 숫자로 적어 출력한 코드가 있습니다. 다시 실행해도 같은 숫자가 나오므로 "
            "실제 교차검증 결과인지 확인할 수 없습니다. 점수는 계산한 값을 그대로 출력하세요.",
            "; ".join(f"{_where(p)} {code}" for p, code in literal_prints[:3]))

    names = {name for _, name, _ in calls}
    if not names & VALIDATION_NAMES:
        return _v7_result(items, "교차검증·검증 분할 코드가 없어 점검할 것이 없습니다.",
                          duplicate_rows=duplicate_rows, cv=None, notebook_cv=None, literal_means=[])

    validation_at = ", ".join([f"{_where(p)} {n}" for p, n, _ in calls if n in VALIDATION_NAMES][:3])
    # b. 같은 행이 있는데 지우거나 묶어서 나누지 않음 — 실제 폴드 간 중복은 재지 않았다(코드에서 흔적을 찾지 못한 것)
    if duplicate_rows and not (names & DEDUP_NAMES or groups_kw):
        add("duplicates", "info", "중복 처리 확인 필요",
            f"학습 데이터에 특성이 같은 행이 {duplicate_rows}개 있는데, 노트북에서 같은 행을 지우거나(drop_duplicates) "
            "한 묶음으로 나누는(GroupKFold) 코드를 찾지 못했습니다. 같은 행이 학습과 검증에 함께 들어가면 "
            "검증 점수가 실제보다 좋게 나옵니다.",
            f"학습 데이터의 같은 행 {duplicate_rows}개(파일 등록 때 계산). 검증 코드: {validation_at}. "
            "실제 폴드 간 중복은 측정하지 않았습니다(코드에서 중복 처리 흔적을 찾지 못한 것).")
    # c. CV보다 먼저 전처리를 전체 데이터에 맞춤
    prep_fits = sorted(p for p, n, node in calls if _is_preprocess_fit(n, node, preprocessors))
    cv_calls = sorted(p for p, n, _ in calls if n in CV_CALL_NAMES)
    if prep_fits and cv_calls and prep_fits[0] < cv_calls[-1]:
        add("preprocess_leak", "warn", "전처리 순서 확인 필요",
            "교차검증보다 먼저 전처리(StandardScaler 등)를 전체 데이터에 맞췄습니다(fit). 검증 폴드의 정보가 학습에 "
            "섞여 점수가 실제보다 좋게 나올 수 있습니다. 전처리를 Pipeline 안에 넣으면 폴드마다 따로 맞춥니다.",
            f"전처리 fit: {_where(prep_fits[0])} → 교차검증: {_where(cv_calls[-1])}")
    # d. 과제 지표와 다른 지표로 CV 점수를 냄
    metric_blocked = False
    scorings = sorted({s for _, _, node in calls if (s := _scoring(node))})
    mse_sources = sorted(p for p, _, node in calls if _is_mse_source(node))
    if project.metric == "rmse" and mse_sources:
        mse_out = [p for p, k in outputs if k == "mse"]
        if any(k == "rmse" for _, k in outputs):
            pass   # 최종 출력에 루트를 씌운 값이 있다
        elif mse_out:
            metric_blocked = True
            add("metric", "warn", "평가 지표 확인 필요",
                "과제 지표는 RMSE인데 교차검증 점수를 루트(sqrt) 없이 MSE로 출력했습니다. "
                "노트북의 점수를 서버 점수와 비교할 수 없습니다.",
                f"MSE 계산: {_where(mse_sources[0])} → 루트 없이 출력: {_where(mse_out[0])}")
        else:
            metric_blocked = True
            add("metric_unknown", "unknown", "평가 지표 확인 불가",
                "MSE 계산이 최종 출력으로 이어지는지 코드에서 따라가지 못했습니다.",
                f"MSE 계산: {_where(mse_sources[0])}. 함수 안 계산·변수 재사용 등으로 출력과의 관계를 추적하지 못함")
    else:
        want = METRIC_SCORING.get(project.metric)
        if want and scorings and not any(want in s for s in scorings):
            metric_blocked = True
            add("metric", "warn", "평가 지표 확인 필요",
                f"과제 지표는 {project.get_metric_display()}인데 교차검증 점수를 {', '.join(scorings)}(으)로 계산했습니다. "
                "노트북의 점수를 서버 점수와 비교할 수 없습니다.",
                f"scoring: {', '.join(scorings)}")
    # e. 여러 후보를 같은 CV 점수로 비교해 최고를 고름(참고: 판정을 올리지 않는다)
    if (best_score and names & SEARCH_NAMES) or (loop_cv and picks_best):
        add("selection", "info", "같은 교차검증으로 최고 후보 선택",
            "여러 후보를 같은 교차검증 점수로 비교해 가장 좋은 것을 골랐습니다. 그렇게 고른 최고 점수는 실제보다 "
            "조금 좋게 나오는 경향이 있습니다.",
            "GridSearchCV 등의 best_score_ 사용" if best_score else "반복문 안의 교차검증 + min/max(key=)로 선택")
    # f. 노트북 CV ↔ 서버 점수 (참고만. 임시 기준 GAP_PERCENT)
    cv, reason = read_cv_mean(cells, project)
    notebook_cv = dict(cv) if cv is not None else None   # 재실행 비교용(지표가 달라도 같은 값끼리는 비교할 수 있다)
    if cv is not None and metric_blocked:
        cv, reason = None, "평가 지표가 과제와 다르거나 확인되지 않아 비교하지 않았습니다."
    lower = (project.direction or ("lower" if project.metric in ("rmse", "mae") else "higher")) == "lower"
    if cv is not None and score is not None:
        value = cv["value"]
        if not score:
            cv, reason = None, "서버 점수가 0이라 비율을 계산할 수 없어 비교하지 않았습니다."
        elif lower and value < 0:
            cv, reason = None, "노트북 CV 평균이 음수입니다(neg_ 점수의 부호를 바꾸지 않은 값일 수 있음). 비교하지 않았습니다."
        else:
            gap = round(((score - value) if lower else (value - score)) / abs(score) * 100, 1)
            better = "CV가 서버보다" if gap > 0 else "서버가 CV보다"
            cv.update(server=score, gap=gap,
                      text=f"노트북 CV {value:.4f} · 서버 {score:.4f} ({better} {abs(gap):.1f}% 좋음, 학생 노트북 출력 기준)")
            if gap >= GAP_PERCENT:
                add("cv_gap", "info", "노트북 CV와 서버 점수 차이",
                    f"노트북의 교차검증 점수가 서버 점수보다 {gap:.1f}% 좋습니다. 정상이어도 생길 수 있지만, 같은 데이터가 "
                    "학습과 검증에 섞였거나 검증 방법에 문제가 있을 때 흔히 이렇게 됩니다.",
                    f"{cv['text']}. CV 출처: {cv['source']}. 임시 기준 {GAP_PERCENT:g}% 이상(판정에 쓰지 않음)")
    elif cv is not None:
        cv, reason = None, "서버 점수가 없어 비교하지 않았습니다."
    cv_info = cv if cv is not None else {"skipped": reason}
    return _v7_result(items, duplicate_rows=duplicate_rows, cv=cv_info,
                      notebook_cv=notebook_cv, literal_means=literal_means)


def _close(a, b):
    """손으로 옮겨 적은 값 a와 실제 값 b가 a의 자릿수 안에서 같은가(0.89275 ↔ 0.892750001 → 같음)."""
    text = repr(a)
    if "e" in text or "." not in text:
        return abs(a - b) <= 1e-9 * max(1.0, abs(b))
    return abs(a - b) <= 0.5 * 10 ** -len(text.split(".")[1]) + 1e-9


def v7_rerun(v7, rerun_cells, project):
    """샌드박스가 다시 실행한 노트북의 출력과 비교해 V7에 덧붙인다(V1 뒤에 부른다, seed_hint와 같은 방식).

    ① 노트북에 찍힌 CV 평균 ↔ 다시 실행해 나온 CV 평균: 출력을 손으로 고쳤으면 다르다
    ② 손으로 옮겨 적은 점수 목록(np.mean([...])) ↔ 다시 실행해 나온 점수 배열: 다른 숫자를 적었으면 다르다
    rerun_cells: 다시 실행한 노트북의 코드 셀(출력만), 재실행이 안 됐으면 None
    가상 예시: 노트북 Mean: 0.9700, 다시 실행 Mean: 0.8524 → 주의 'CV 출력 확인 필요'
               노트북 0.9784, 다시 실행 0.9771(시드 미고정) → 참고
    """
    d = v7.get("details", {})
    nb_cv, lits = d.get("notebook_cv"), d.get("literal_means") or []
    if not nb_cv and not lits:
        return v7
    if rerun_cells is None:
        d["rerun_cv"] = {"skipped": "노트북을 다시 실행하지 못해(V1 참고) 비교하지 않았습니다."}
        return v7
    items, info = list(d.get("items") or []), {}

    def add(code, level, label, text, evidence):
        items.append({"code": code, "level": level, "label": label, "text": text, "evidence": evidence})

    if nb_cv:
        mine = nb_cv["value"]
        re_cv, re_reason = read_cv_mean(rerun_cells, project)
        if re_cv is None:
            info["skipped"] = re_reason
            add("cv_rerun", "warn", "노트북 CV 출력 확인 필요",
                f"노트북에는 교차검증 평균 {mine:.4f}이 찍혀 있는데, 다시 실행한 노트북에서는 같은 형식의 평균이 나오지 "
                "않았습니다. 출력을 손으로 고쳤거나 지금 코드와 다른 코드로 실행한 결과일 수 있습니다.",
                f"노트북 출력: {nb_cv['source']}. 다시 실행: {re_reason}")
        else:
            again = re_cv["value"]
            diff = round(abs(mine - again) / max(abs(again), 1e-12) * 100, 2)
            info.update(submitted=mine, rerun=again, diff_percent=diff,
                        text=f"노트북 출력 {mine:.4f} · 다시 실행 {again:.4f}" + (" (같음)" if abs(mine - again) <= 1e-6 * max(1.0, abs(again))
                                                                    else f" ({diff:g}% 다름)"))
            if abs(mine - again) <= 1e-6 * max(1.0, abs(again)):
                pass
            elif diff < RERUN_CV_PERCENT:
                add("cv_rerun_small", "info", "다시 실행한 CV와 조금 다름",
                    f"다시 실행한 교차검증 평균이 노트북 출력과 {diff:g}% 다릅니다. 무작위 시드를 고정하지 않으면 "
                    "정상 제출에서도 생깁니다.",
                    f"{info['text']}. 노트북 출력: {nb_cv['source']}. 다시 실행: {re_cv['source']}")
            else:
                add("cv_rerun", "warn", "노트북 CV 출력 확인 필요",
                    f"노트북에 찍힌 교차검증 평균({mine:.4f})이 다시 실행한 결과({again:.4f})와 {diff:g}% 다릅니다. "
                    "출력을 손으로 고쳤거나 지금 코드와 다른 코드로 실행한 결과일 수 있습니다"
                    "(시드를 고정하지 않아도 조금은 달라질 수 있음).",
                    f"{info['text']}. 노트북 출력: {nb_cv['source']}. 다시 실행: {re_cv['source']}. "
                    f"기준 {RERUN_CV_PERCENT:g}% 이상")
    if lits:
        arrays = [values for _, values in score_arrays(rerun_cells)]
        if not arrays:
            info["literal_skipped"] = "다시 실행한 노트북에 점수 배열 출력이 없어 옮겨 적은 점수를 비교하지 않았습니다."
        else:
            wrong = [lit for lit in lits
                     if not any(len(a) == len(lit["values"]) and all(_close(x, y) for x, y in zip(lit["values"], a))
                                for a in arrays)]
            info["literal_checked"] = len(lits)
            if wrong:
                w = wrong[0]
                add("cv_literal_rerun", "warn", "옮겨 적은 CV 점수 확인 필요",
                    "노트북에서 손으로 적은 점수 목록이 다시 실행해 나온 교차검증 점수와 다릅니다. "
                    "평균을 낼 때는 계산한 점수 변수를 그대로 쓰세요(예: np.mean(scores)).",
                    f"{w['where']} 목록 {len(w['values'])}개(앞 3개 {', '.join(f'{v:g}' for v in w['values'][:3])}) — "
                    f"다시 실행한 점수 배열 {len(arrays)}개 중 같은 것이 없음")
    keep = {k: v for k, v in d.items() if k not in ("items", "codes", "flags", "student")}
    keep["rerun_cv"] = info
    v7.update(_v7_result(items, **keep))
    return v7

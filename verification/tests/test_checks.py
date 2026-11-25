"""V1~V7 판정 로직과 노트북 안전 표시. Docker·DB 없이 값만으로 확인한다."""
import base64
import json

import pytest
from django.conf import settings

from competitions.models import Project
from verification import checks, notebook_view


def _project(metric=Project.Metric.ACCURACY):
    return Project(metric=metric, id_column="id", target_column="label", expected_rows=5, allowed_labels=["0", "1"])


SUBMITTED = {str(i): str(i % 2) for i in range(1, 101)}   # 가상 제출 100행


def _csv(mapping):
    return ("id,label\n" + "".join(f"{k},{v}\n" for k, v in mapping.items())).encode()


def _nb(*sources, counts=None):
    counts = counts or list(range(1, len(sources) + 1))
    return {"cells": [{"cell_type": "code", "source": s, "execution_count": c, "outputs": []}
                      for s, c in zip(sources, counts)]}


HONEST = ("import pandas as pd\nfrom sklearn.linear_model import LogisticRegression",
          "train = pd.read_csv('train.csv'); test = pd.read_csv('test.csv')\nm = LogisticRegression().fit(train[['x']], train['label'])",
          "pd.DataFrame({'id': test['id'], 'label': m.predict(test[['x']])}).to_csv('submission.csv', index=False)")


# ── V1 ─────────────────────────────────────────────────────────
def test_V1_재실행_결과가_같으면_일치():
    c = checks.v1_rerun({"status": "ok", "elapsed": 3.2}, _csv(SUBMITTED), SUBMITTED, _project())
    assert c["level"] == "ok" and c["details"]["rate"] == 1.0


def test_V1_CSV를_손으로_15줄_고쳤으면_불일치():
    rerun = dict(SUBMITTED)
    tampered = {k: (str(1 - int(v)) if int(k) <= 15 else v) for k, v in SUBMITTED.items()}   # 가상학생이 15줄 수정
    c = checks.v1_rerun({"status": "ok"}, _csv(rerun), tampered, _project())
    assert c["level"] == "bad" and c["details"]["rate"] == 0.85


def test_V1_경계값_99퍼센트는_일치_98퍼센트는_주의():
    def rate(n_changed):
        tampered = {k: (str(1 - int(v)) if int(k) <= n_changed else v) for k, v in SUBMITTED.items()}
        return checks.v1_rerun({"status": "ok"}, _csv(SUBMITTED), tampered, _project())["level"]
    assert (rate(1), rate(2), rate(10), rate(11)) == ("ok", "warn", "warn", "bad")


def test_V1_재실행이_안_되면_판정_불가이고_부적합이_아니다():
    c = checks.v1_rerun({"status": "timeout", "message": "..."}, b"", SUBMITTED, _project())
    assert c["level"] == "unknown" and "수동 검토" in c["summary"]


def test_V1_회귀는_미세한_차이를_같게_본다():
    p = _project(Project.Metric.RMSE)
    mine = {"1": "3.14159265", "2": "10.0"}
    assert checks.agreement(mine, {"1": "3.14159270", "2": "10.0"}, False) == 1.0   # 실행마다 생기는 차이
    assert checks.agreement(mine, {"1": "3.15", "2": "10.0"}, False) == 0.5
    assert checks.values_equal("1", "1.0", True)                                      # 분류: 1 과 1.0은 같은 레이블
    assert p.is_classification is False


# ── V2 ─────────────────────────────────────────────────────────
def test_V2_같은_CSV는_의심_같은_노트북만이면_주의():
    who = [("참여자 K7QM", "가상학생02", 7, "상대가 먼저 제출")]
    assert checks.v2_same_files(who, [])["level"] == "bad"
    assert checks.v2_same_files(who, [], perfect=True)["level"] == "warn"       # 만점끼리는 같을 수밖에 없다
    assert checks.v2_same_files([], who)["level"] == "warn"
    assert checks.v2_same_files([], [])["level"] == "ok"


# ── V3 ─────────────────────────────────────────────────────────
def test_V3_같은_줄에서_같은_오답이면_참고로만_표시하고_판정하지_않는다():
    answer = {str(i): "0" for i in range(1, 101)}
    mine = {k: ("1" if int(k) <= 10 else "0") for k in answer}           # 1~10번에서 오답
    copied = {k: ("1" if int(k) <= 10 else "0") for k in answer}         # 같은 줄에서 같은 오답(남의 것 복사)
    independent = {k: ("1" if 50 < int(k) <= 60 else "0") for k in answer}   # 다른 줄에서 오답(독립 모델)
    others = [("참여자 A", "가상학생02", 2, "이 제출이 먼저", independent), ("참여자 B", "가상학생03", 3, "상대가 먼저 제출", copied)]

    c = checks.v3_wrong_overlap(mine, answer, others, _project())
    assert c["level"] == "ok" and c["details"]["reference"] is True and "판정에 쓰지 않음" in c["summary"]
    assert c["details"]["top"]["real_name"] == "가상학생03" and c["details"]["rate"] == 1.0   # 비교 대상은 그대로 보인다
    assert c["details"]["top"]["first"] == "상대가 먼저 제출"
    c = checks.v3_wrong_overlap(mine, answer, others[:1], _project())
    assert c["level"] == "ok" and c["details"]["rate"] == 0.0 and c["details"]["reference"] is False


def test_V3는_높게_나와도_전체_판정에_넣지_않는다():
    """정상 짝도 같은 오답 74~91%(이진 분류 Adult, 행위 인식) → V3만으로 '주의·의심'이 되지 않는다."""
    ok = checks.check("ok", "")
    old = {k: ok for k in ("v1", "v2", "v4", "v5", "v6", "v7")}
    old["v3"] = checks.check("bad", "옛 리포트: 같은 오답 91%")   # 바꾸기 전에 저장된 리포트
    assert checks.overall(old) == "ok"
    old["v5"] = checks.check("warn", "모델 예측과 96% 일치")      # 다른 검사의 근거는 그대로 반영
    assert checks.overall(old) == "warn"


def test_V3_회귀_엑셀로_다시_저장해_표기만_바뀐_복사도_같은_값으로_센다():
    """주택 가격 데모 학생32: 친구 CSV를 엑셀로 열어 저장 → 유효숫자 15자리로 바뀌어 글자로는 7%만 같았다(놓침)."""
    project = _project(Project.Metric.RMSE)
    friend = {str(i): repr(452318.73649123457 + i * 1013.123456789123) for i in range(1, 101)}
    resaved = {k: format(float(v), ".15g") for k, v in friend.items()}            # 값은 같고 표기만 다름
    assert sum(friend[k] == v for k, v in resaved.items()) < 50                    # 글자 비교였다면 절반도 안 같다
    independent = {k: repr(float(v) * 1.03) for k, v in friend.items()}            # 다른 모델: 3% 다른 값
    others = [("참여자 A", "가상학생30", 70, "상대가 먼저 제출", independent),
              ("참여자 B", "가상학생29", 69, "상대가 먼저 제출", friend)]

    c = checks.v3_wrong_overlap(resaved, {}, others, project)
    assert c["details"]["reference"] and c["details"]["rate"] == 1.0 and c["details"]["top"]["submission_id"] == 69
    c = checks.v3_wrong_overlap(resaved, {}, others[:1], project)
    assert c["level"] == "ok" and c["details"]["rate"] == 0.0


def test_V3_회귀_같은_값_기준은_V1_V5와_같다():
    """0.01% 안이면 같은 값, 그 밖이면 다른 값(새 기준값을 만들지 않고 values_equal을 그대로 쓴다)."""
    project = _project(Project.Metric.RMSE)
    mine = {str(i): "500000.0" for i in range(1, 11)}
    near = {k: "500040.0" for k in mine}     # 0.008% 차이 → 같은 값
    far = {k: "500060.0" for k in mine}      # 0.012% 차이 → 다른 값
    assert checks.v3_wrong_overlap(mine, {}, [("A", "가", 1, "", near)], project)["details"]["rate"] == 1.0
    assert checks.v3_wrong_overlap(mine, {}, [("A", "가", 1, "", far)], project)["details"]["rate"] == 0.0


def test_V3_오답이_너무_적으면_판정하지_않는다():
    answer = {str(i): "0" for i in range(1, 101)}
    mine = dict(answer, **{"1": "1"})
    c = checks.v3_wrong_overlap(mine, answer, [("A", "가상학생02", 2, "이 제출이 먼저", mine)], _project())
    assert c["level"] == "ok" and "판정하지 않습니다" in c["summary"]


# ── V4 ─────────────────────────────────────────────────────────
def test_V4_정상_노트북은_문제_없음():
    assert checks.v4_static(_nb(*HONEST))["level"] == "ok"


def test_V4_예측_값을_직접_적은_노트북은_의심():
    hardcoded = "import pandas as pd\npred = [" + ", ".join(["1", "0"] * 40) + "]\npd.DataFrame({'id': range(80), 'label': pred}).to_csv('s.csv')"
    c = checks.v4_static(_nb(hardcoded))
    assert c["level"] == "bad"
    assert any("직접 적은" in f for f in c["details"]["flags"])
    assert any("fit" in f for f in c["details"]["flags"])


def test_V4_엑셀_열을_문자열로_붙여_넣은_예측도_직접_입력으로_의심():   # 예시 대회 demo_stu07(cheat_pasted_column)
    column = "\n".join(str(i % 10) for i in range(60))   # 가상 예측 60개를 엑셀에서 복사
    pasted = HONEST + (f'pasted = """\n{column}\n"""\npred = np.array(pasted.split(), dtype=int)',)
    c = checks.v4_static(_nb(*pasted))
    assert c["level"] == "bad" and "literal" in c["details"]["codes"]
    # 숫자 목록이 아닌 문자열, 숫자가 50개 미만인 문자열은 세지 않는다
    for text in ('"id,label\\n" + "\\n".join(rows)', '"' + " ".join(str(i) for i in range(49)) + '"'):
        assert "literal" not in checks.v4_static(_nb(*HONEST, f"s = {text}"))["details"]["codes"]


@pytest.mark.parametrize("case, literal", [("cheat_pasted_column", True), ("cheat_hand_labels", False),
                                           ("honest_sgd", False), ("honest_forest_features", False),
                                           ("honest_svc_pipeline", False)])
def test_V4_손글씨_데모_노트북의_직접_입력_판정(case, literal):   # 붙여 넣은 360개만 의심, 라벨 20개 dict·정상은 아님
    nb = json.loads((settings.BASE_DIR / "demo" / "submissions" / case / "work.ipynb").read_text(encoding="utf-8"))
    assert ("literal" in checks.v4_static(nb)["details"]["codes"]) is literal


def test_V4_공개_정답_데이터를_불러오면_의심():
    cheat = HONEST + ("from sklearn.datasets import load_breast_cancer\nX, y = load_breast_cancer(return_X_y=True)",)
    c = checks.v4_static(_nb(*cheat))
    assert c["level"] == "bad" and any("load_breast_cancer" in f for f in c["details"]["flags"])
    # sklearn.datasets 없이 이름만 load_로 시작하는 함수(예: 직접 만든 load_data)는 문제 아님
    assert checks.v4_static(_nb(*HONEST, "def load_data(): pass\nload_data()"))["level"] == "ok"


def test_V4_실행_순서_뒤바뀜과_실행_기록_없음은_주의():
    assert checks.v4_static(_nb(*HONEST, counts=[1, 3, 2]))["level"] == "warn"
    assert "실행 기록이 없습니다" in checks.v4_static(_nb(*HONEST, counts=[None] * 3))["details"]["flags"][0]
    assert checks.v4_static(_nb(*HONEST, counts=[1, 5, 9]))["level"] == "ok"   # 건너뛴 번호는 정상(다시 실행한 셀)


def test_V4_매직_명령이_있어도_분석한다():
    c = checks.v4_static(_nb("%matplotlib inline\n" + HONEST[0], *HONEST[1:], "!pip list"))
    assert "셸 명령(!)을 씁니다." in c["details"]["flags"] and c["level"] == "warn"


# ── V5 ─────────────────────────────────────────────────────────
def test_V5_모델_예측이_제출과_같으면_문제_없음_다르면_불일치():
    ok = checks.v5_model({"status": "ok", "model_type": "sklearn.LogisticRegression"}, _csv(SUBMITTED), SUBMITTED, _project())
    flipped = {k: str(1 - int(v)) for k, v in SUBMITTED.items()}
    bad = checks.v5_model({"status": "ok"}, _csv(flipped), SUBMITTED, _project())
    assert ok["level"] == "ok" and bad["level"] == "bad"


def test_V5_실행기가_열을_골라_넣었으면_근거에_적는다():
    """다른 과제 모델이 우연히 같은 이름의 열 일부로 예측한 경우(개발 DB 옛 제출 #20: 유방암 모델 f01~f30 → 손글씨 test)를
    관리자가 알아볼 수 있게. 판정 단계는 일치율 그대로."""
    note = "모델이 학습한 열 이름대로 test.csv에서 30개 열을 골라 넣었습니다(test.csv 특성 64개)"
    c = checks.v5_model({"status": "ok", "input_note": note}, _csv(SUBMITTED), SUBMITTED, _project())
    assert c["level"] == "ok" and note in c["summary"] and c["details"]["input_note"] == note
    assert "input_note" not in checks.v5_model({"status": "ok"}, _csv(SUBMITTED), SUBMITTED, _project())["details"]


def test_V5_로드_실패는_주의_라이브러리_없음은_판정_불가():
    assert checks.v5_model({"status": "error", "message": "UnpicklingError"}, b"", SUBMITTED, _project())["level"] == "warn"
    assert checks.v5_model({"status": "unsupported", "message": ".onnx"}, b"", SUBMITTED, _project())["level"] == "unknown"


# ── 전체 판정 ───────────────────────────────────────────────────
def test_전체_판정은_가장_심각한_것_V1_판정_불가면_수동_검토():
    c = lambda lvl: {"level": lvl}
    assert checks.overall({"v1": c("ok"), "v4": c("bad")}) == "bad"
    assert checks.overall({"v1": c("unknown"), "v4": c("ok")}) == "unknown"
    assert checks.overall({"v1": c("unknown"), "v4": c("warn")}) == "warn"
    assert checks.overall({"v1": c("ok"), "v5": c("unknown")}) == "ok"


# ── 노트북 안전 표시 ─────────────────────────────────────────────
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10).decode()


def test_노트북_HTML과_스크립트_출력은_그리지_않는다():
    nb = {"cells": [
        {"cell_type": "markdown", "source": "<script>alert(1)</script>"},
        {"cell_type": "code", "execution_count": 1, "source": "x", "outputs": [
            {"output_type": "display_data", "data": {"text/html": "<img src=x onerror=alert(1)>"}},
            {"output_type": "display_data", "data": {"application/javascript": "alert(1)", "text/plain": "<IPython.Javascript>"}},
            {"output_type": "display_data", "data": {"image/svg+xml": "<svg onload=alert(1)>"}},
            {"output_type": "display_data", "data": {"image/png": PNG}},
            {"output_type": "display_data", "data": {"image/png": base64.b64encode(b"<svg>").decode()}},   # 이름만 png
            {"output_type": "stream", "text": "\x1b[31m빨간 글자\x1b[0m"},
        ]},
    ]}
    md, code = notebook_view.cells(nb)
    kinds = [o["kind"] for o in code["outputs"]]
    assert md["source"] == "<script>alert(1)</script>"                       # 글자 그대로(템플릿이 이스케이프)
    assert kinds == ["skipped", "text", "skipped", "image", "skipped", "text"]
    assert code["outputs"][5]["text"] == "빨간 글자"


def test_노트북_화면에서_스크립트가_이스케이프된다(client, admin2, project_with_files):
    from django.urls import reverse
    from submissions.models import Submission, SubmissionFile
    from competitions import storage
    from competitions.models import CompetitionMember
    from accounts.models import User

    user = User.objects.create_user("mock_stu09", real_name="가상학생09")
    member = CompetitionMember.objects.create(user=user, competition=project_with_files.competition)
    sub = Submission.objects.create(project=project_with_files, member=member, status="scored", score=1,
                                    started_at="2026-11-19T10:00+09:00", last_received_at="2026-11-19T10:00+09:00")
    nb = {"cells": [{"cell_type": "code", "execution_count": 1, "source": "print('<script>alert(1)</script>')",
                     "outputs": [{"output_type": "display_data", "data": {"text/html": "<script>alert(2)</script>"}}]}]}
    SubmissionFile.objects.create(submission=sub, kind="notebook", original_name="w.ipynb",
                                  stored_name=storage.save_bytes("submissions", json.dumps(nb).encode(), ext="ipynb"))
    client.force_login(admin2)
    resp = client.get(reverse("submissions:review_notebook", args=[project_with_files.competition_id, project_with_files.pk, sub.pk]))
    html = resp.content.decode()
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)" in html and "alert(2)" not in html
    assert "script-src 'self'" in resp["Content-Security-Policy"]


# ── 시드 미고정 (정직한데 V1 불일치가 나는 경우) ─────────────────
SEEDLESS = ("import pandas as pd\nfrom sklearn.neural_network import MLPClassifier\nfrom sklearn.model_selection import train_test_split",
            "train = pd.read_csv('train.csv'); test = pd.read_csv('test.csv')\nX_tr, X_va = train_test_split(train)\n"
            "m = MLPClassifier(max_iter=40).fit(train[['x']], train['label'])",
            "pd.DataFrame({'id': test['id'], 'label': m.predict(test[['x']])}).to_csv('submission.csv', index=False)")


def test_V4_시드_없는_무작위_모델은_참고만_하고_판정은_올리지_않는다():
    c = checks.v4_static(_nb(*SEEDLESS))
    assert c["level"] == "ok" and c["details"]["unseeded"] == ["MLPClassifier", "train_test_split"]
    assert c["summary"] == "의심스러운 점이 없습니다. (참고 1개)" and c["details"]["flags"][0].startswith("참고:")
    fixed = tuple(s.replace("max_iter=40", "max_iter=40, random_state=0").replace("(train)", "(train, random_state=0)") for s in SEEDLESS)
    assert checks.v4_static(_nb(*fixed))["details"]["unseeded"] == []
    assert checks.v4_static(_nb("import numpy as np\nnp.random.seed(0)", *SEEDLESS))["details"]["unseeded"] == []   # 전체 시드 고정
    # 다른 문제와 함께면 '확인할 점'에 참고를 세지 않는다
    assert checks.v4_static(_nb(*SEEDLESS, "!pip list"))["summary"] == "확인할 점 1개 (참고 1개)"


def test_V1_불일치인데_모델_예측이_맞으면_시드_안내_판정은_그대로():
    v4 = checks.v4_static(_nb(*SEEDLESS))
    flipped = {k: (str(1 - int(v)) if int(k) <= 40 else v) for k, v in SUBMITTED.items()}
    v1 = checks.v1_rerun({"status": "ok"}, _csv(flipped), SUBMITTED, _project())
    v5_ok = checks.v5_model({"status": "ok"}, _csv(SUBMITTED), SUBMITTED, _project())
    v5_bad = checks.v5_model({"status": "ok"}, _csv(flipped), SUBMITTED, _project())

    hinted = checks.seed_hint(json.loads(json.dumps(v1)), v5_ok, v4)
    assert hinted["level"] == "bad"                                      # 가짜 모델일 수도 있으니 낮추지 않는다
    assert "100.0% 일치" in hinted["details"]["hint"] and "MLPClassifier" in hinted["details"]["hint"]
    assert "hint" not in checks.seed_hint(json.loads(json.dumps(v1)), v5_bad, v4)["details"]          # 모델도 어긋남
    ok_v1 = checks.v1_rerun({"status": "ok"}, _csv(SUBMITTED), SUBMITTED, _project())
    assert "hint" not in checks.seed_hint(ok_v1, v5_ok, v4)["details"]                                # V1 일치
    no_seedless = checks.seed_hint(json.loads(json.dumps(v1)), v5_ok, checks.v4_static(_nb(*HONEST)))
    assert "random_state 고정 여부를 확인" in no_seedless["details"]["hint"]


# ── V4: 제공하지 않은 파일·인터넷 주소 읽기 (2026-11-23 와인 시험에서 발견) ──────
def _flags(*extra):
    return checks.v4_static(_nb(*HONEST, *extra))


def test_V4_Kaggle_원본에서_정답을_찾으면_주의():
    # 가상학생14가 실제로 쓴 코드: 자기 PC에 받아 둔 정답 포함 원본을 읽어 같은 행의 quality를 가져옴
    c = _flags("orig = pd.read_csv('WineQT.csv')\nfound = test.merge(orig, on=feats)['quality']")
    assert c["level"] == "warn" and c["details"]["outside_files"] == ["WineQT.csv"]
    assert any("WineQT.csv" in f for f in c["details"]["flags"])


def test_V4_경로가_달라도_제공_파일이면_표시하지_않는다():
    c = checks.v4_static(_nb(HONEST[0], HONEST[1].replace("'train.csv'", "'/kaggle/input/wine/train.csv'")
                             .replace("'test.csv'", r"'data\\test.csv'"), HONEST[2]))
    assert c["level"] == "ok" and c["details"]["outside_files"] == []


def test_V4_노트북이_스스로_만든_파일을_다시_읽는_것은_정상():
    c = _flags("import joblib\njoblib.dump(m, 'model.pkl')\nm2 = joblib.load('model.pkl')",
               "pd.read_csv('submission.csv')",
               "with open('log.txt', 'w') as f: f.write('x')\nopen('log.txt').read()")
    assert c["level"] == "ok", c["details"]["flags"]


def test_V4_정답_목록_파일을_open으로_읽어도_주의():
    c = _flags("answers = open('answers.txt', encoding='utf-8').read().split()")
    assert c["level"] == "warn" and c["details"]["outside_files"] == ["answers.txt"]


def test_V4_인터넷_주소에서_읽으면_주의():
    c = _flags("orig = pd.read_csv('https://example.org/winequality-red.csv', sep=';')")
    assert c["level"] == "warn" and any("인터넷 주소" in f for f in c["details"]["flags"])
    assert c["details"]["outside_files"] == []


def test_V4_변수로_만든_경로는_알_수_없어_표시하지_않는다():
    # 한계(README 6절): 정적 검사는 글자 그대로 적힌 경로만 본다
    c = _flags("name = 'Wine' + 'QT.csv'\norig = pd.read_csv(name)")
    assert c["level"] == "ok"


def test_V1_없는_파일을_읽다_멈추면_파일_이름을_알리고_판정_불가를_유지한다():
    msg = "FileNotFoundError: [Errno 2] No such file or directory: 'WineQT.csv'"
    c = checks.v1_rerun({"status": "cell_error", "message": msg}, b"", SUBMITTED, _project())
    assert c["level"] == "unknown"                       # 의심으로 올리지 않는다(정직한 경로 실수와 구별 불가)
    assert c["details"]["missing_file"] == "WineQT.csv" and "WineQT.csv" in c["summary"]
    other = checks.v1_rerun({"status": "cell_error", "message": "ZeroDivisionError: division by zero"}, b"", SUBMITTED, _project())
    assert "missing_file" not in other["details"] and "셀 실행 중 오류" in other["summary"]


# V5·V6: 과제 PDF(미니 프로젝트 #2)처럼 OrdinalEncoder로 레이블을 숫자로 바꿔 학습한 모델(11-24, 데모 학생23)
ACTS = ["WALKING", "SITTING", "LAYING"]   # 이름순: LAYING=0, SITTING=1, WALKING=2


def _named_project():
    return Project(metric=Project.Metric.ACCURACY, id_column="id", target_column="label", expected_rows=6,
                   allowed_labels=sorted(ACTS))


NAMED = {str(i): ACTS[i % 3] for i in range(1, 7)}          # 제출 CSV: 레이블 이름
CODES = {i: f"{float(sorted(ACTS).index(v))}" for i, v in NAMED.items()}   # 모델 파일 예측: 2.0, 1.0, 0.0 …


def test_V5_모델이_레이블_숫자_코드를_내도_이름순으로_바꿔_비교한다():
    c = checks.v5_model({"status": "ok", "model_type": "sklearn.SGDClassifier"}, _csv(CODES), NAMED, _named_project(),
                        labels=ACTS)
    assert c["level"] == "ok" and c["details"]["rate"] == 1.0
    assert "숫자 코드" in c["summary"] and "OrdinalEncoder" in c["details"]["note"]
    # 학습 레이블을 못 읽으면 허용 레이블(정답에서 계산)로 같은 결과
    assert checks.v5_model({"status": "ok"}, _csv(CODES), NAMED, _named_project())["level"] == "ok"


def test_V5_숫자_코드라도_다른_예측이면_그대로_불일치():
    wrong = {i: f"{(float(v) + 1) % 3}" for i, v in CODES.items()}   # 모든 행이 다른 레이블
    assert checks.v5_model({"status": "ok"}, _csv(wrong), NAMED, _named_project(), labels=ACTS)["level"] == "bad"


def test_숫자_코드를_되돌리지_않는_경우():
    names = sorted(ACTS)
    assert checks.decode_label_codes({"1": "3.0"}, names) == ({"1": "3.0"}, False)          # 범위 밖
    assert checks.decode_label_codes({"1": "1.5"}, names) == ({"1": "1.5"}, False)          # 정수가 아님
    assert checks.decode_label_codes({"1": "WALKING"}, names) == ({"1": "WALKING"}, False)  # 이미 이름
    assert checks.decode_label_codes({"1": "1"}, ["0", "1"]) == ({"1": "1"}, False)         # 레이블이 숫자(손글씨 등)
    assert checks.decode_label_codes({"1": "1"}, []) == ({"1": "1"}, False)
    # 숫자 레이블 과제는 지금까지와 똑같이 비교한다
    c = checks.v5_model({"status": "ok"}, _csv(SUBMITTED), SUBMITTED, _project())
    assert c["level"] == "ok" and "note" not in c["details"]


def test_V6_모델이_숫자_코드를_내도_재실행과_비교해_수정_흔적을_본다():
    answer = dict(NAMED)
    edited = dict(NAMED)
    notebook = {i: ("LAYING" if i in ("1", "2", "4", "5", "6") else v) for i, v in NAMED.items()}   # 노트북·모델이 틀린 행
    model = {i: f"{float(sorted(ACTS).index(v))}" for i, v in notebook.items()}
    c = checks.v6_edit_trace(_csv(notebook), _csv(model), edited, answer, _named_project(), labels=ACTS)
    assert c["details"]["consistency"] == 1.0 and c["level"] in ("warn", "bad") and c["details"]["toward_rows"] >= 1


def test_학습_데이터에서_레이블_목록을_읽는다():
    from verification.services import train_labels
    project = _named_project()
    train = b"id,x,label\n1,0.1,WALKING\n2,0.2,SITTING\n3,0.3,WALKING\n"
    assert train_labels(project, train) == ["SITTING", "WALKING"]
    assert train_labels(project, b"id,x\n1,0.1\n") is None and train_labels(project, None) is None
    assert train_labels(_project(Project.Metric.RMSE), train) is None   # 회귀는 되돌리지 않는다

# ── V6: 제출 CSV 수정 흔적 (재실행 × 모델 예측 × 제출) ─────────────────────
ANSWER = dict(SUBMITTED)   # 가상 정답 100행


def _flip(mapping, ids):
    return {k: (str(1 - int(v)) if int(k) in ids else v) for k, v in mapping.items()}


def _v6(rerun, model, submitted, project=None):
    return checks.v6_edit_trace(_csv(rerun), _csv(model), submitted, ANSWER, project or _project())


def test_V6_노트북과_모델은_같고_제출만_정답쪽으로_6행_다르면_의심():
    notebook = _flip(ANSWER, range(1, 7))            # 노트북·모델은 6행을 틀림
    c = _v6(notebook, notebook, dict(ANSWER))        # 가상학생03: 틀린 6행을 정답으로 고쳐 제출
    assert c["level"] == "bad"
    d = c["details"]
    assert (d["diff_rows"], d["toward_rows"], d["consistency"]) == (6, 6, 1.0)
    assert "6개 행에서 다릅니다" in c["summary"] and "추가 검토" in c["summary"]


def test_V6_다른_행이_5개_미만이면_주의까지만():
    notebook = _flip(ANSWER, range(1, 5))            # 4행
    c = _v6(notebook, notebook, dict(ANSWER))
    assert c["level"] == "warn" and c["details"]["diff_rows"] == 4


def test_V6_더_좋은_예전_결과를_냈으면_대개_섞여서_주의():
    # 정상 사례: 노트북(새 모델)은 1~10행을 틀림. 제출(예전 모델)은 그중 8행을 맞혔지만 11·12행을 새로 틀림
    notebook = _flip(ANSWER, range(1, 11))
    old = _flip(ANSWER, {9, 10, 11, 12})
    c = _v6(notebook, notebook, old)
    assert c["level"] == "warn"                       # 정답 쪽 8/10 = 80% < 90%
    assert c["details"]["toward_rows"] == 8 and "예전 결과를 냈을 수 있음" in c["summary"]


def test_V6_재실행과_모델이_서로_다르면_보지_않는다_시드_미고정():
    notebook = _flip(ANSWER, range(1, 21))           # 재실행이 20행 다르게 나옴(시드 없음)
    c = _v6(notebook, dict(ANSWER), dict(ANSWER))
    assert c["level"] == "ok" and "보지 않았습니다" in c["summary"]


def test_V6_비교할_결과가_없으면_판정_불가():
    c = checks.v6_edit_trace(b"", _csv(ANSWER), dict(ANSWER), ANSWER, _project())
    assert c["level"] == "unknown"
    assert checks.overall({"v1": {"level": "ok"}, "v6": c}) == "ok"   # V6 판정 불가는 전체 판정을 바꾸지 않는다


def test_V6_노트북이_모델_예측을_정답쪽_값으로_덮어썼으면_재실행과_같아도_표시한다():   # 예시 대회 demo_stu06
    answer = {str(i): str(i % 2) for i in range(1, 601)}    # 가상 600행(모델·재실행 일치율 99% 이상 유지)
    model = _flip(answer, range(1, 7))                # 모델 파일은 1~6행을 틀림
    c = checks.v6_edit_trace(_csv(answer), _csv(model), dict(answer), answer, _project())   # 노트북이 6행을 덮어씀 → 제출 = 재실행
    assert c["level"] == "bad"
    d = c["details"]
    assert (d["diff_rows"], d["model_diff_rows"], d["toward_rows"]) == (0, 6, 6) and "덮어썼을" in c["summary"]
    c = _v6(dict(ANSWER), _flip(ANSWER, {1}), dict(ANSWER))   # 1행만 다르면 주의까지만(V6 기존 기준 5행과 같음)
    assert c["level"] == "warn" and c["details"]["model_diff_rows"] == 1


def test_V6_재실행과_같고_모델과_다른_행이_정답쪽이_아니면_문제_없음():
    model = _flip(ANSWER, {1})
    submitted = _flip(ANSWER, {1, 2})                 # 모델과 다른 2번 행에서 제출이 오히려 틀림(정답 쪽 0%)
    assert _v6(submitted, model, submitted)["level"] == "ok"


def test_V6_셋이_같으면_문제_없음():
    assert _v6(dict(ANSWER), dict(ANSWER), dict(ANSWER))["level"] == "ok"


def test_V6_회귀는_정답에_더_가까운지로_본다():
    project = _project(Project.Metric.RMSE)
    answer = {str(i): f"{i * 1.5:.3f}" for i in range(1, 51)}
    notebook = {k: f"{float(v) + 3:.3f}" for k, v in answer.items()}
    edited = {k: (f"{float(v) + 0.1:.3f}" if int(k) <= 6 else notebook[k]) for k, v in answer.items()}
    c = checks.v6_edit_trace(_csv(notebook), _csv(notebook), edited, answer, project)
    assert c["level"] == "bad" and c["details"]["toward_rows"] == 6


# ── V7: 학습·평가 방법 (안내) ───────────────────────────────────────
def _wine():
    return Project(metric=Project.Metric.RMSE, direction=Project.Direction.LOWER, id_column="id", target_column="quality")


NOTEBOOKS = settings.BASE_DIR / "verification" / "tests" / "notebooks"


def _demo_nb(case):
    """가상 참여자의 실행된 노트북. wine_*은 개발 중 와인 품질 데모(회귀, 학습 데이터 같은 행 196개)에서 만든 것을
    테스트 자료로 남긴 것이고, 나머지는 지금 demo/ 예시 대회의 제출물이다."""
    path = NOTEBOOKS / f"{case}.ipynb"
    if not path.exists():
        path = settings.BASE_DIR / "demo" / "submissions" / case / "work.ipynb"
    return json.loads(path.read_text(encoding="utf-8"))


def _v7(nb, project=None, duplicate_rows=0, score=None):
    return checks.v7_method(nb, project or _wine(), duplicate_rows=duplicate_rows, score=score)


@pytest.mark.parametrize("case", ["wine_honest_ridge", "wine_honest_forest",            # stu07, stu08
                                  "wine_honest_groupkfold", "wine_honest_rmse_scoring"])   # stu11, stu12
def test_V7_정석_데모_노트북은_문제_없음(case):
    r = _v7(_demo_nb(case), duplicate_rows=196)
    assert r["level"] == "ok" and r["details"]["codes"] == [] and r["details"]["student"] == []


def test_V7_중복을_그대로_두고_CV한_데모는_중복_처리_확인_필요_폴드_중복은_재지_않았다고_밝힌다():   # stu09
    r = _v7(_demo_nb("wine_mistake_kfold"), duplicate_rows=196)
    assert r["level"] == "ok" and r["details"]["codes"] == ["duplicates", "selection"]   # 중복 안내는 참고(2026-11-25 결정)
    assert r["summary"] == "확인할 점 없음 (참고 2개)" and r["details"]["items"][0]["level"] == "info"
    first = r["details"]["items"][0]
    assert first["label"] == "중복 처리 확인 필요" and "196개" in first["text"]
    assert "실제 폴드 간 중복은 측정하지 않았습니다" in first["evidence"] and "코드 셀" in first["evidence"]
    assert [s["label"] for s in r["details"]["student"]] == ["중복 처리 확인 필요", "같은 교차검증으로 최고 후보 선택"]


def test_V7_CV_전에_스케일러를_맞춘_데모는_전처리_순서_확인_필요():   # stu13
    r = _v7(_demo_nb("wine_mistake_scaler"), duplicate_rows=196)
    assert r["level"] == "warn" and r["details"]["codes"] == ["preprocess_leak"]
    assert "전처리 fit: 코드 셀" in r["details"]["items"][0]["evidence"]


def test_V7_MSE를_그대로_보고한_데모는_지표_확인_필요이고_점수_비교는_건너뛴다():   # stu14
    r = _v7(_demo_nb("wine_mistake_mse"), duplicate_rows=196, score=0.62)
    assert r["level"] == "warn" and r["details"]["codes"] == ["metric"]
    assert "평가 지표" in r["details"]["cv"]["skipped"]


def test_V7_검증_코드가_없는_노트북은_점검할_것이_없다():   # stu10(원본 조회)은 V1~V6이 맡는다
    r = _v7(_demo_nb("wine_cheat_original_lookup"), duplicate_rows=196)
    assert r["level"] == "ok" and "점검할 것이 없습니다" in r["summary"]


@pytest.mark.parametrize("duplicate_rows", [0, None])   # 같은 행이 없거나, 옛 파일이라 모름
def test_V7_같은_행이_없거나_모르면_중복_안내를_하지_않는다(duplicate_rows):
    r = _v7(_demo_nb("wine_mistake_kfold"), duplicate_rows=duplicate_rows)
    assert "duplicates" not in r["details"]["codes"] and r["level"] == "ok"


def test_V7_GroupKFold나_groups를_쓰면_중복을_다룬_것으로_본다():
    nb = _nb("from sklearn.model_selection import GroupKFold, cross_val_score",
             "s = cross_val_score(m, X, y, groups=g, cv=GroupKFold(5))")
    assert _v7(nb, duplicate_rows=10)["level"] == "ok"


@pytest.mark.parametrize("sources", [
    ("sc = StandardScaler()\nXs = sc.fit_transform(X)", "s = cross_val_score(m, Xs, y, cv=10)"),
    ("Xs = StandardScaler().fit_transform(X)", "s = cross_val_score(m, Xs, y, cv=10)"),
])
def test_V7_CV보다_먼저_전체_데이터에_전처리를_맞추면_주의(sources):
    r = _v7(_nb(*sources))
    assert r["level"] == "warn" and r["details"]["codes"] == ["preprocess_leak"]


@pytest.mark.parametrize("sources", [
    ("m = make_pipeline(StandardScaler(), Ridge())", "s = cross_val_score(m, X, y, cv=10)"),   # 파이프라인 안
    ("s = cross_val_score(m, X, y, cv=10)", "sc = StandardScaler()\nXs = sc.fit_transform(X)"),  # CV 다음
])
def test_V7_파이프라인_안의_전처리나_CV_뒤의_전처리는_괜찮다(sources):
    assert _v7(_nb(*sources))["level"] == "ok"


MSE_CV = "mse = cross_val_score(m, X, y, scoring='neg_mean_squared_error', cv=10)"


@pytest.mark.parametrize("sources, level, codes", [
    ((MSE_CV, "print(-mse.mean())"), "warn", ["metric"]),                                   # 루트 없이 출력
    ((MSE_CV, "rmse = np.sqrt(-mse)", "display_scores(rmse)"), "ok", []),                   # 루트를 씌워 출력
    ((MSE_CV, "print(((-mse) ** 0.5).mean())"), "ok", []),                                   # ** 0.5도 루트
    ((MSE_CV, "other = np.sqrt(x)", "print(-mse.mean())"), "warn", ["metric"]),             # 다른 변수에만 sqrt
    (("s = -cross_val_score(m, X, y, scoring='neg_root_mean_squared_error', cv=10)", "print(s.mean())"), "ok", []),
    (("from sklearn.metrics import mean_squared_error", "s = cross_val_score(m, X, y, cv=5)", "print(s.mean())"),
     "ok", []),                                                                              # 불러오기만 함
    (("def ev(m):\n    return cross_val_score(m, X, y, scoring='neg_mean_squared_error', cv=5)",
      "r = ev(m)", "print(r.mean())"), "unknown", ["metric_unknown"]),                       # 함수 안 → 추적 불가
])
def test_V7_RMSE_과제에서_MSE가_최종_출력으로_이어지는지_본다(sources, level, codes):
    r = _v7(_nb(*sources))
    assert r["level"] == level and r["details"]["codes"] == codes


def test_V7_추적_불가는_관리자에게만_보이고_학생에게는_보이지_않는다():
    r = _v7(_nb("def ev(m):\n    return cross_val_score(m, X, y, scoring='neg_mean_squared_error', cv=5)",
                "r = ev(m)", "print(r.mean())"))
    assert r["summary"] == "판정 불가 1개" and r["details"]["student"] == [] and r["details"]["flags"]


def test_V7_Macro_F1_과제를_accuracy로_검증하면_주의():
    r = _v7(_nb("s = cross_val_score(m, X, y, scoring='accuracy', cv=5)"), project=_project(Project.Metric.MACRO_F1))
    assert r["level"] == "warn" and r["details"]["codes"] == ["metric"]


def test_V7_GridSearchCV의_best_score는_참고만_하고_판정은_올리지_않는다():
    r = _v7(_nb("g = GridSearchCV(m, grid, cv=5).fit(X, y)", "print(g.best_score_)"))
    assert r["level"] == "ok" and r["details"]["codes"] == ["selection"] and r["summary"].endswith("(참고 1개)")


# CV 출력 읽기와 차이율 ─────────────────────────────────────────────
CV_CODE = "s = np.sqrt(-cross_val_score(m, X, y, scoring='neg_mean_squared_error', cv=10))\ndisplay_scores(s)"


def _nb_out(*stdouts, code=CV_CODE):
    """출력이 남은 노트북(셀마다 같은 CV 코드, stdout만 다름)."""
    return {"cells": [{"cell_type": "code", "source": code, "execution_count": i + 1,
                       "outputs": [{"output_type": "stream", "name": "stdout", "text": out}]}
                      for i, out in enumerate(stdouts)]}


PDF_OUT = "Scores: [0.5 0.6]\nMean: 0.55\nStandard deviation: 0.05\n"


def test_V7_PDF_형식_출력을_읽어_CV가_서버보다_5퍼센트_이상_좋으면_참고():
    r = _v7(_nb_out(PDF_OUT), score=0.62)                     # (0.62 - 0.55) / 0.62 = 11.3%
    cv = r["details"]["cv"]
    assert cv["value"] == 0.55 and cv["gap"] == 11.3 and "2개 폴드" in cv["source"]
    assert r["level"] == "ok" and r["details"]["codes"] == ["cv_gap"]   # 참고: 판정은 올리지 않음
    assert "CV가 서버보다 11.3% 좋음" in cv["text"]


def test_V7_서버가_CV보다_좋으면_두_점수만_남기고_안내하지_않는다():
    r = _v7(_nb_out(PDF_OUT), score=0.50)                     # 정석 학생에게 흔한 방향
    assert r["details"]["cv"]["gap"] == -10.0 and r["details"]["codes"] == []
    assert "서버가 CV보다 10.0% 좋음" in r["details"]["cv"]["text"]


def test_V7_높을수록_좋은_지표는_방향을_뒤집어_계산한다():
    acc = Project(metric=Project.Metric.ACCURACY, direction=Project.Direction.HIGHER, id_column="id", target_column="label")
    nb = _nb_out("Scores: [0.9 1.0]\nMean: 0.95\nStandard deviation: 0.05\n", code="s = cross_val_score(m, X, y, cv=5)")
    assert _v7(nb, project=acc, score=0.85)["details"]["cv"]["gap"] == 11.8   # (0.95 - 0.85) / 0.85


@pytest.mark.parametrize("stdouts, reason", [
    ((PDF_OUT, PDF_OUT), "2개라"),                                                    # 어느 것이 최종인지 모름
    (("Scores: [0.5 0.6]\nMean: 0.9\nStandard deviation: 0.05\n",), "찾지 못해"),     # Mean ≠ Scores 평균
    (("Mean: 0.55\n",), "찾지 못해"),                                                 # Scores 없이 Mean만
    (("Scores: [0.5 abc]\nMean: 0.55\nStandard deviation: 0.05\n",), "찾지 못해"),   # 숫자 아님
    (("CV_MAE_MEAN=0.4\n",), "과제 지표"),                                            # 지표 이름이 다름
    (("Scores: [-0.5 -0.6]\nMean: -0.55\nStandard deviation: 0.05\n",), "음수"),      # 부호를 안 바꾼 neg_ 점수
])
def test_V7_CV_출력이_불명확하면_비교하지_않고_이유를_남긴다(stdouts, reason):
    r = _v7(_nb_out(*stdouts), score=0.62)
    assert reason in r["details"]["cv"]["skipped"] and "cv_gap" not in r["details"]["codes"]


def test_V7_한_줄_규칙이_있으면_그것을_쓴다():
    r = _v7(_nb_out(PDF_OUT, "CV_RMSE_MEAN=0.58\n"), score=0.62)   # PDF 묶음보다 규칙 줄이 우선
    assert r["details"]["cv"]["value"] == 0.58 and "CV_RMSE_MEAN" in r["details"]["cv"]["source"]


def test_V7_주의는_전체_판정에_넣지_않는다():
    ok = {"level": "ok", "summary": "", "details": {}}
    report = {k: ok for k in ("v1", "v2", "v3", "v4", "v5", "v6")}
    report["v7"] = {"level": "warn", "summary": "확인할 점 1개", "details": {}}
    assert checks.overall(report) == "ok"


# ── V7: 노트북에 찍힌 CV ↔ 다시 실행한 CV, 숫자로 적은 점수 ────────────────────
def _acc():
    return Project(metric=Project.Metric.ACCURACY, direction=Project.Direction.HIGHER, id_column="id", target_column="label")


def _cells(*outputs, code="s = cross_val_score(m, X, y, cv=10)"):
    """셀마다 출력 하나. 문자열은 표준 출력, ('r', 글자)는 셀 결과(execute_result)."""
    def out(o):
        if isinstance(o, tuple):
            return {"output_type": "execute_result", "data": {"text/plain": o[1]}}
        return {"output_type": "stream", "name": "stdout", "text": o}
    return {"cells": [{"cell_type": "code", "source": code, "execution_count": i + 1, "outputs": [out(o)]}
                      for i, o in enumerate(outputs)]}


PDF1_OUT = (("r", "array([0.9, 0.8, 1. , 0.9])"), ("r", "np.float64(0.9)"))   # 미니 프로젝트 #1 16쪽 형식


def test_V7_점수_배열_다음의_평균_한_줄도_CV로_읽는다():
    r = _v7(_cells(*PDF1_OUT), project=_acc(), score=0.9)
    cv = r["details"]["cv"]
    assert cv["value"] == 0.9 and "점수 배열과 평균 출력(4개 폴드" in cv["source"]


def test_V7_print로_찍은_배열과_라벨_붙은_평균도_읽는다():
    r = _v7(_cells("[0.9 0.8 1.  0.9]\n평균: 0.9\n"), project=_acc(), score=0.9)
    assert r["details"]["cv"]["value"] == 0.9


@pytest.mark.parametrize("outputs", [
    (("r", "array([0.9, 0.8, 1. , 0.9])"), ("r", "np.float64(0.7)")),   # 평균이 배열 평균과 다름
    (("r", "array([[1, 0], [0, 1]])"), ("r", "1")),                      # 오차 행렬 같은 정수 배열
])
def test_V7_배열_평균이_맞지_않거나_점수_배열이_아니면_읽지_않는다(outputs):
    assert "찾지 못해" in _v7(_cells(*outputs), project=_acc(), score=0.9)["details"]["cv"]["skipped"]


def test_V7_손글씨_숫자_데모_PDF_그대로_노트북의_CV를_읽는다():   # stu15
    r = _v7(_demo_nb("honest_sgd"), project=_acc(), score=0.9389)
    assert round(r["details"]["cv"]["value"], 4) == 0.9151 and r["details"]["notebook_cv"]["value"] == r["details"]["cv"]["value"]


@pytest.mark.parametrize("line", ['print("Mean: 0.97")', 'print("Mean:", 0.97)', 'print("accuracy", 0.97)',
                                  'print("CV_ACCURACY_MEAN=0.97")'])
def test_V7_점수를_숫자로_적어_출력하면_주의(line):
    r = _v7(_nb("s = cross_val_score(m, X, y, cv=10)", line), project=_acc())
    assert r["level"] == "warn" and "cv_literal" in r["details"]["codes"]


def test_V7_검증_코드가_없어도_숫자로_적은_점수_출력은_주의():
    r = _v7(_nb('print("Mean: 0.97")'), project=_acc())
    assert r["level"] == "warn" and r["details"]["codes"] == ["cv_literal"]


@pytest.mark.parametrize("line", ['print(f"Mean: {s.mean():.4f}")', 'print("Mean:", s.mean())', 'print("folds:", 10)',
                                  "np.mean([0.9, 0.8, 1.0, 0.9])"])   # 마지막은 PDF 16쪽 방식: 재실행과 비교한다
def test_V7_계산한_값_출력이나_정수나_옮겨_적은_목록은_바로_주의하지_않는다(line):
    r = _v7(_nb("s = cross_val_score(m, X, y, cv=10)", line), project=_acc())
    assert "cv_literal" not in r["details"]["codes"]


def _rerun(nb, rerun_nb, project=None):
    return checks.v7_rerun(_v7(nb, project=project or _acc()), rerun_nb["cells"], project or _acc())


def test_V7_재실행_CV가_같으면_항목_없이_같음만_남긴다():
    r = _rerun(_cells(*PDF1_OUT), _cells(*PDF1_OUT))
    assert r["details"]["codes"] == [] and "(같음)" in r["details"]["rerun_cv"]["text"]


def test_V7_출력을_손으로_고쳐_재실행_CV와_다르면_주의():
    forged = _cells("Scores: [0.96 0.98]\nMean: 0.97\nStandard deviation: 0.01\n")
    actual = _cells("Scores: [0.84 0.86]\nMean: 0.85\nStandard deviation: 0.01\n")
    r = _rerun(forged, actual)
    assert r["level"] == "warn" and r["details"]["codes"] == ["cv_rerun"]
    assert r["details"]["rerun_cv"]["diff_percent"] == 14.12 and "0.9700" in r["details"]["student"][0]["text"]
    assert checks.overall({"v1": checks.check("ok", ""), "v7": r}) == "ok"   # 안내용: 전체 판정에는 넣지 않는다


def test_V7_재실행_CV가_조금_다르면_참고만():
    r = _rerun(_cells("Scores: [0.97 0.99]\nMean: 0.98\nStandard deviation: 0.01\n"),
               _cells("Scores: [0.97 0.985]\nMean: 0.9775\nStandard deviation: 0.0075\n"))
    assert r["level"] == "ok" and r["details"]["codes"] == ["cv_rerun_small"]


def test_V7_노트북에는_CV가_있는데_재실행에서_안_나오면_주의():
    r = _rerun(_cells("Scores: [0.96 0.98]\nMean: 0.97\nStandard deviation: 0.01\n"), _cells("완료\n"))
    assert r["details"]["codes"] == ["cv_rerun"] and "찾지 못해" in r["details"]["rerun_cv"]["skipped"]


def test_V7_재실행을_못_했으면_비교하지_않았다고_남긴다():
    r = checks.v7_rerun(_v7(_cells(*PDF1_OUT), project=_acc()), None, _acc())
    assert r["details"]["codes"] == [] and "다시 실행하지 못해" in r["details"]["rerun_cv"]["skipped"]


LITERAL_NB = "s = cross_val_score(m, X, y, cv=10)\nnp.mean([0.94444444, 0.93055556, 0.875])"


def test_V7_옮겨_적은_점수가_재실행_배열과_같으면_괜찮다():
    nb = _cells(("r", "array([0.94444444, 0.93055556, 0.875     ])"), code=LITERAL_NB)
    r = _rerun(nb, nb)
    assert "cv_literal_rerun" not in r["details"]["codes"] and r["details"]["rerun_cv"]["literal_checked"] == 1


def test_V7_옮겨_적은_점수가_재실행_배열과_다르면_주의():
    nb = _cells(("r", "array([0.94444444, 0.93055556, 0.875     ])"), code=LITERAL_NB)
    rerun = _cells(("r", "array([0.84444444, 0.83055556, 0.775     ])"), code=LITERAL_NB)
    r = _rerun(nb, rerun)
    assert "cv_literal_rerun" in r["details"]["codes"] and r["level"] == "warn"


@pytest.mark.parametrize("unseeded, said", [(["RandomForestClassifier"], "시드 없이 만든 것(RandomForestClassifier)"),
                                            ([], "노트북과 모델 파일이 서로 다른 결과를 냄")])
def test_V6_재실행과_모델이_다를_때_시드_문구는_무작위_모델이_있을_때만(unseeded, said):
    rerun = {str(i): str(i % 2) for i in range(1, 101)}
    model = {k: ("1" if v == "0" else "0") for k, v in rerun.items()}   # 모두 다름(서로 다른 출처)
    c = checks.v6_edit_trace(_csv(rerun), _csv(model), rerun, rerun, _project(), unseeded=unseeded)
    assert c["level"] == "ok" and said in c["summary"]
    assert ("시드" in c["summary"]) == bool(unseeded)

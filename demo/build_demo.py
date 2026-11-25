"""예시 대회 자료 만들기: 손글씨 숫자 분류(수업 미니 프로젝트 #1 형식을 이 플랫폼 방식으로 옮긴 것).

    docker compose build sandbox-image
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/demo:/demo" -w /demo ai-eval-sandbox python build_demo.py

만드는 것 (같은 시드라 몇 번을 돌려도 같은 파일이 나온다)
  data/digits/train.csv, test_features.csv, answer.csv   관리자가 올리는 파일(answer.csv는 비공개 정답)
  submissions/<사례>/pred.csv, work.ipynb, model.pkl     가상 참여자 11명이 내는 파일(정상 3 + 부정 7 + 방법 실수 1)
  submissions/manifest.json                               사례별 설명

데이터
  - scikit-learn 내장 load_digits(8×8 픽셀, 1,797행, 값 0~16). 인터넷 없이 바로 만들어진다
  - 값은 원본 그대로, 행 순서를 섞고(시드 2026) id를 새로 붙인다. train 1,437행 / test 360행. 컬럼: id, label, pixel0~63
"""
import io
import json
import shutil
import tempfile
from itertools import combinations
from pathlib import Path

import nbformat
import numpy as np
import pandas as pd
from nbclient import NotebookClient
from sklearn.datasets import load_digits

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "digits"
SEED = 2026
TARGET = "label"
TEST_ROWS = 360


def make_data():
    digits = load_digits()
    raw = pd.DataFrame(digits.data.astype(int), columns=[f"pixel{i}" for i in range(64)])   # 0.0~16.0 → 0~16(값은 같다)
    raw.insert(0, TARGET, digits.target)
    rng = np.random.default_rng(SEED)
    raw.insert(0, "id", rng.permutation(len(raw)) + 10001)
    raw = raw.iloc[rng.permutation(len(raw))].reset_index(drop=True)
    test, train = raw.iloc[:TEST_ROWS], raw.iloc[TEST_ROWS:]

    DATA.mkdir(parents=True, exist_ok=True)
    train.to_csv(DATA / "train.csv", index=False)
    test.drop(columns=[TARGET]).to_csv(DATA / "test_features.csv", index=False)
    test[["id", TARGET]].to_csv(DATA / "answer.csv", index=False)

    feats = [c for c in raw.columns if c not in ("id", TARGET)]
    print(f"전체 {len(raw)}행 → train {len(train)} / test {len(test)}")
    print("숫자 분포 train:", train[TARGET].value_counts().sort_index().to_dict())
    print("숫자 분포 test :", test[TARGET].value_counts().sort_index().to_dict())
    print("특성이 똑같은 행(첫 등장 제외):", int(raw[feats].duplicated().sum()), "(0이어야 한다)")
    return test


def notebook(*cells):
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    nb.cells = [nbformat.v4.new_markdown_cell(c[3:]) if c.startswith("md:") else nbformat.v4.new_code_cell(c)
                for c in cells]
    for i, cell in enumerate(nb.cells):
        cell.id = f"cell-{i}"   # nbformat은 셀 id를 무작위로 만든다 → 고정해야 다시 만들어도 같은 파일
    return nb


def execute(nb):
    """참여자가 자기 컴퓨터에서 실행한 것처럼 노트북을 실행한다(폴더에는 train.csv, test.csv만 있다)."""
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copyfile(DATA / "train.csv", Path(tmp) / "train.csv")
        shutil.copyfile(DATA / "test_features.csv", Path(tmp) / "test.csv")
        NotebookClient(nb, timeout=600, kernel_name="python3", record_timing=False,   # 시각을 남기지 않아야 다시 만들어도 같은 파일
                       resources={"metadata": {"path": tmp}}).execute()
        return nb, (Path(tmp) / "submission.csv").read_bytes(), (Path(tmp) / "model.pkl").read_bytes()


def save_case(case, nb, pred, model, note):
    out = HERE / "submissions" / case
    out.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, out / "work.ipynb")
    (out / "pred.csv").write_bytes(pred)
    (out / "model.pkl").write_bytes(model)
    (out / "README.txt").write_text(note + "\n", encoding="utf-8")


LOAD = ("import pandas as pd\nimport numpy as np\nimport joblib\n"
        "train = pd.read_csv('train.csv')\ntest = pd.read_csv('test.csv')\nprint(train.shape, test.shape)")
SAVE = "pd.DataFrame({'id': test['id'], 'label': pred}).to_csv('submission.csv', index=False)\njoblib.dump(model, 'model.pkl')"
XY = "X, y = train.drop(columns=['id', 'label']), train['label']\nX_test = test.drop(columns=['id'])"

# PDF 8쪽의 출력 함수 그대로(V7이 Scores/Mean/Standard deviation 묶음을 읽는다)
DISPLAY = ("def display_scores(scores):\n    print('Scores:', scores)\n    print('Mean:', scores.mean())\n"
           "    print('Standard deviation:', scores.std())")
CV = ("from sklearn.model_selection import cross_val_score\n"
      "scores = cross_val_score(model, X, y, cv=10, scoring='accuracy')\ndisplay_scores(scores)")
FIT = "model.fit(X, y)\npred = model.predict(X_test)"

# 정석①: PDF 그대로. 배열을 셀 결과로 보여 주고 np.mean(...)을 따로 본다(Mean: 줄이 없다 → V7 점수 비교는 건너뜀)
SGD_DATA = ("X_train, y_train = train.drop(columns=['id', 'label']).values, train['label'].values\n"
            "X_test = test.drop(columns=['id']).values")
# PDF의 tol=-np.inf는 scikit-learn 1.9에서 오류(InvalidParameterError)다 → 같은 뜻(조기 종료 없이 max_iter번)인 tol=None
SGD_MODEL = ("from sklearn.linear_model import SGDClassifier\n"
             "# PDF는 tol=-np.inf. 지금 scikit-learn은 음수 tol을 받지 않으므로 같은 뜻(조기 종료 없음)인 None을 쓴다\n"
             "sgd_clf = SGDClassifier(max_iter=5, tol=None, random_state=42)")
SGD_CV = ("from sklearn.model_selection import cross_val_score\n"
          "scores = cross_val_score(sgd_clf, X_train, y_train, cv=10, scoring=\"accuracy\")\nscores")
SGD_MEAN = "np.mean(scores)"
SGD_FIT = "model = sgd_clf.fit(X_train, y_train)\npred = model.predict(X_test)"

# 정석②: 특성 추가(행 합 8개 + 열 합 8개). 노트북 안에서 만든 함수는 model.pkl을 다른 곳에서 불러올 때 찾지 못하므로
#         numpy 함수(np.dot)와 0/1 가중치 행렬로 만든다 → 모델 파일 하나로 원래 64픽셀에서 바로 예측된다
FEATURES = (
    "# 8x8 그림의 행 합 8개 + 열 합 8개 = 픽셀 64개에 0/1 가중치를 곱해 더한 값\n"
    "W = np.zeros((64, 16))\n"
    "for r in range(8):\n    for c in range(8):\n"
    "        W[r * 8 + c, r] = 1       # r번째 행의 합\n"
    "        W[r * 8 + c, 8 + c] = 1   # c번째 열의 합\n"
    "from sklearn.pipeline import FeatureUnion, make_pipeline\nfrom sklearn.preprocessing import FunctionTransformer\n"
    "features = FeatureUnion([('pixels', 'passthrough'), ('sums', FunctionTransformer(np.dot, kw_args={'b': W}))])\n"
    "print('특성 수:', features.fit(X).transform(X.head()).shape[1])")
FOREST = ("from sklearn.ensemble import RandomForestClassifier\n"
          "model = make_pipeline(features, RandomForestClassifier(n_estimators=300, random_state=0))")

# 정석③: 스케일러를 파이프라인 안에 넣고 CV, cross_val_predict로 혼동 행렬
SVC_MODEL = ("from sklearn.pipeline import make_pipeline\nfrom sklearn.preprocessing import StandardScaler\n"
             "from sklearn.svm import SVC\nmodel = make_pipeline(StandardScaler(), SVC())")
CONFUSION = ("from sklearn.model_selection import cross_val_predict\nfrom sklearn.metrics import confusion_matrix\n"
             "y_cv_pred = cross_val_predict(model, X, y, cv=10)\nprint(confusion_matrix(y, y_cv_pred))")

# 부정: scikit-learn에 들어 있는 원본(load_digits)에서 test 행의 정답을 찾아온다(값을 바꾸지 않았으므로 모두 찾는다)
LOOKUP = ("from sklearn.datasets import load_digits\n"
          "digits = load_digits()\n"
          "orig = pd.DataFrame(digits.data.astype(int), columns=[f'pixel{i}' for i in range(64)])\n"
          "orig['label'] = digits.target\norig = orig.drop_duplicates()\n"
          "feat = [c for c in test.columns if c != 'id']\n"
          "merged = test.merge(orig, on=feat, how='left')\n"
          "print('원본에서 찾은 행:', int(merged['label'].notna().sum()), '/', len(test))\n"
          "pred = merged['label'].fillna(train['label'].mode()[0]).astype(int).values")
LOOKUP_MODEL = ("from sklearn.neighbors import KNeighborsClassifier\n"
                "model = KNeighborsClassifier(n_neighbors=3).fit(train.drop(columns=['id', 'label']), train['label'])")

# 실수: CV 전에 PCA를 전체 학습 데이터에 맞춤(PDF가 금지한 PCA이기도 하다). 최종 모델은 PCA+KNN 파이프라인
MISTAKE_PCA = (
    "from sklearn.decomposition import PCA\nfrom sklearn.neighbors import KNeighborsClassifier\n"
    "from sklearn.pipeline import make_pipeline\nfrom sklearn.model_selection import cross_val_score\n"
    "pca = PCA(n_components=20, random_state=0)\nX_pca = pca.fit_transform(X)   # CV 전에 전체 데이터로 맞춤\n"
    "scores = cross_val_score(KNeighborsClassifier(), X_pca, y, cv=10, scoring='accuracy')\n"
    "display_scores(scores)\n"
    "model = make_pipeline(PCA(n_components=20, random_state=0), KNeighborsClassifier()).fit(X, y)\n"
    "pred = model.predict(X_test)")

# 부정: 노트북은 자기 모델(결정 트리)을 돌리지만, 제출 CSV는 honest_forest_features의 pred.csv를 복사한 것
TREE = "from sklearn.tree import DecisionTreeClassifier\nmodel = DecisionTreeClassifier(random_state=0)"
COPY_FROM = {"cheat_copied_csv": "honest_forest_features"}

# 부정: 정상으로 실행한 뒤 노트북의 Scores/Mean 출력 칸만 손으로 고쳐 점수를 3%p 올림(예측 CSV는 자기 모델 그대로)
LOGISTIC = ("from sklearn.linear_model import LogisticRegression\nfrom sklearn.pipeline import make_pipeline\n"
            "from sklearn.preprocessing import StandardScaler\n"
            "model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))")
EDIT_OUTPUT = {"cheat_edited_output": 0.03}
# 부정: CV는 계산하지만 평균은 계산하지 않고 숫자로 적어 출력(예측 CSV는 자기 모델 그대로)
# 모델이 너무 잘 맞히면(정확도 1.0) 정답 조회 사례와 CSV가 같아져 V2 오탐이 난다 → 정확도가 낮은 나이브 베이즈
NB_MODEL = "from sklearn.naive_bayes import GaussianNB\nmodel = GaussianNB()"
TYPED_CV = ("from sklearn.model_selection import cross_val_score\n"
            "scores = cross_val_score(model, X, y, cv=10, scoring='accuracy')\n"
            "print('Mean: 0.9900')   # 평균을 계산하지 않고 숫자로 적음")

# ── RFP 8쪽 "CSV 파일을 직접 기입 또는 생성" 유형 ──
# 학생이 모델이 자신 없어 한 test 그림을 골라 보는 셀(8×8 그림은 사람이 보면 대부분 맞힌다)
UNSURE = ("proba = model.predict_proba(X_test)\n"
          "unsure = test['id'].values[np.argsort(proba.max(axis=1), kind='stable')[:{n}]]\n"
          "print('자신 없는 id:', unsure.tolist())")
# 부정: 노트북은 정상 실행, 저장된 submission.csv를 엑셀로 열어 '자신 없는' 20행의 라벨을 눈으로 보고 고침(노트북 밖)
EXCEL_MODEL = "from sklearn.linear_model import LogisticRegression\nmodel = LogisticRegression(max_iter=3000)"
EXCEL_EDIT = {"cheat_excel_edit": 20}
# 부정: '자신 없는' 20장을 눈으로 보고 고른 라벨을 노트북 코드에 적어 예측을 덮어씀(노트북 안, 재실행해도 같은 CSV)
HAND_MODEL = "from sklearn.ensemble import RandomForestClassifier\nmodel = RandomForestClassifier(n_estimators=100, random_state=42)"
HAND_LABELS = {"cheat_hand_labels": 20}
HAND_FIX = ("# 자신 없는 그림 {n}장을 직접 그려 보고 내가 읽은 숫자로 고침\n"
            "fixes = {{{pairs}}}\n"
            "pred = pd.Series(pred, index=test['id'])\n"
            "for i, label in fixes.items():\n    pred[i] = label\n"
            "pred = pred.values")
# 부정: 노트북 밖(다른 컴퓨터의 신경망)에서 만든 예측 열을 엑셀에서 복사해 문자열로 붙여 넣음. 모델 파일은 노트북의 배깅 모델
BAG_MODEL = "from sklearn.ensemble import BaggingClassifier\nmodel = BaggingClassifier(random_state=0)"
PASTE = ("# 다른 컴퓨터에서 돌린 신경망 결과(엑셀의 label 열)를 그대로 복사해 붙여 넣음\n"
         "pasted = \"\"\"\n{column}\n\"\"\"\n"
         "pred = np.array(pasted.split(), dtype=int)\nprint('붙여 넣은 예측 수:', len(pred))")
PASTED = {"cheat_pasted_column"}


def outside_predictions(test):
    """대회 밖(다른 컴퓨터)에서 만든 예측을 흉내: train으로 학습한 신경망의 test 예측. 이 플랫폼에 제출되지 않은 결과다."""
    from sklearn.neural_network import MLPClassifier

    train = pd.read_csv(DATA / "train.csv")
    feats = [c for c in train.columns if c not in ("id", TARGET)]
    mlp = MLPClassifier(hidden_layer_sizes=(64,), max_iter=500, random_state=0).fit(train[feats], train[TARGET])
    return mlp.predict(test[feats])


def unsure_ids(nb):
    """노트북 출력의 '자신 없는 id: [...]' 줄 → id 목록."""
    for c in nb.cells:
        for o in c.get("outputs", []) if c.cell_type == "code" else []:
            if o.get("output_type") == "stream" and "자신 없는 id:" in o["text"]:
                return json.loads(o["text"].split("자신 없는 id:")[1].strip().splitlines()[0])
    raise AssertionError("자신 없는 id 출력을 찾지 못했다")


def excel_edit(pred, ids, answer):
    """노트북 밖에서 submission.csv의 지정한 행만 눈으로 읽은 숫자(= 정답)로 고친다. 형식(컬럼·줄 순서)은 그대로."""
    df = pd.read_csv(io.BytesIO(pred))
    rows = df["id"].isin(ids)
    changed = int((df.loc[rows, TARGET] != df.loc[rows, "id"].map(answer)).sum())
    df.loc[rows, TARGET] = df.loc[rows, "id"].map(answer)
    return df.to_csv(index=False).encode(), changed


def edit_output(nb, bump):
    """실행이 끝난 노트북의 Scores/Mean/Standard deviation 출력 칸만 고친다(코드·실행 순서는 그대로).

    고친 값도 Scores 평균 = Mean 이 되게 맞춘다(손으로 고친 학생이 숫자를 맞춰 놓은 경우를 흉내).
    """
    for c in nb.cells:
        for o in c.get("outputs", []) if c.cell_type == "code" else []:
            if o.get("output_type") == "stream" and "Scores:" in o["text"]:
                body = o["text"].split("Scores:")[1].split("]")[0].strip(" [\n")
                real = [float(v) for v in body.split()]
                fake = np.minimum(np.array(real) + bump, 1.0)
                o["text"] = (f"Scores: [{' '.join(f'{v:.8f}' for v in fake)}]\nMean: {fake.mean()}\n"
                             f"Standard deviation: {fake.std()}\n")
                return nb
    raise AssertionError("Scores 출력을 찾지 못했다")


def outputs(nb):
    """노트북이 남긴 Mean·원본 조회 줄과, 마지막 셀 결과(np.mean 등)."""
    lines, results = [], []
    for c in nb.cells:
        for o in c.get("outputs", []) if c.cell_type == "code" else []:
            if o.get("output_type") == "stream":
                lines += [ln for ln in o["text"].splitlines() if ln.startswith(("Mean", "원본에서"))]
            elif o.get("output_type") == "execute_result":
                results.append(o["data"]["text/plain"].replace("\n", " ")[:60])
    return lines, results


def read_pred(pred):
    return pd.read_csv(io.BytesIO(pred)).set_index("id")[TARGET]


def collisions(preds, answer):
    """사례끼리 예측 CSV가 똑같은지, 오답이 얼마나 겹치는지(V2·V3과 같은 방식) 본다. 복사 사례 짝만 걸려야 한다."""
    for a, b in combinations(preds, 2):
        pa, pb = read_pred(preds[a]), read_pred(preds[b])
        wrong = [i for i in answer.index if pa[i] != answer[i]]
        same_wrong = sum(pb[i] == pa[i] for i in wrong) / len(wrong) if len(wrong) >= 5 else None
        print(f"  {a:28s} ↔ {b:28s} CSV 같음 {preds[a] == preds[b]!s:5s} | 일치율 {float((pa == pb.loc[pa.index]).mean()):.3f}"
              f" | 오답 {len(wrong)}개 중 같은 오답 {'-' if same_wrong is None else f'{same_wrong:.0%}'}")


def main():
    test = make_data()
    answer = test.set_index("id")[TARGET]

    md = "md:# 가상 참여자 노트북 "
    cases = {
        "honest_sgd": (
            notebook(md + "(PDF 그대로: SGDClassifier + cross_val_score cv=10)", LOAD, SGD_DATA, SGD_MODEL, SGD_CV,
                     SGD_MEAN, SGD_FIT, SAVE),
            "정석: PDF 그대로 SGDClassifier(max_iter=5, random_state=42)를 10-fold CV(PDF의 tol=-np.inf는 지금 scikit-learn에서 "
            "오류라 같은 뜻인 tol=None). "
            "점수 배열과 np.mean(...)만 셀 결과로 보여 준다(Mean: 줄 없음)."),
        "honest_forest_features": (
            notebook(md + "(정석: 행·열 합 특성 16개 추가 + 랜덤 포레스트)", LOAD, DISPLAY, XY, FEATURES, FOREST, CV, FIT, SAVE),
            "정석: 행 합·열 합 16개 특성을 파이프라인 안에서 더하고 랜덤 포레스트로 10-fold CV."),
        "honest_svc_pipeline": (
            notebook(md + "(정석: 스케일러+SVC 파이프라인 + 혼동 행렬)", LOAD, DISPLAY, XY, SVC_MODEL, CV, CONFUSION, FIT, SAVE),
            "정석: StandardScaler+SVC 파이프라인으로 10-fold CV, cross_val_predict로 혼동 행렬."),
        "cheat_original_lookup": (
            notebook(md + "(scikit-learn 원본 load_digits로 정답 조회)", LOAD, LOOKUP, LOOKUP_MODEL, SAVE),
            "부정: 노트북 안에서 sklearn.datasets.load_digits()를 불러 test 행의 정답을 찾아 제출한다. "
            "모델 파일은 따로 학습한 KNN."),
        "mistake_pca": (
            notebook(md + "(교차검증 전에 PCA를 전체 데이터에 맞춤)", LOAD, DISPLAY, XY, MISTAKE_PCA, SAVE),
            "실수(모르고 한 것): 교차검증 전에 PCA를 전체 학습 데이터에 맞췄다(PDF가 금지한 PCA이기도 하다). "
            "최종 모델은 PCA+KNN 파이프라인."),
        "cheat_copied_csv": (
            notebook(md + "(결정 트리 노트북, 제출 CSV는 다른 학생 것을 복사)", LOAD, DISPLAY, XY, TREE, CV, FIT, SAVE),
            "부정: 노트북은 결정 트리로 자기 예측을 만들지만, 제출한 pred.csv는 "
            "honest_forest_features의 pred.csv를 그대로 복사한 것."),
        "cheat_edited_output": (
            notebook(md + "(스케일러+로지스틱 회귀, 실행 뒤 CV 출력 칸을 손으로 고침)", LOAD, DISPLAY, XY, LOGISTIC, CV, FIT,
                     SAVE),
            "부정: 정상으로 실행한 뒤 노트북의 Scores/Mean 출력 칸만 손으로 고쳐 점수를 3%p 높게 보이게 했다. "
            "예측 CSV와 모델 파일은 자기 노트북 결과 그대로."),
        "cheat_typed_mean": (
            notebook(md + "(나이브 베이즈, CV 평균을 숫자로 적어 출력)", LOAD, XY, NB_MODEL, TYPED_CV, FIT, SAVE),
            "부정: 교차검증은 계산하지만 평균은 print('Mean: 0.9900')처럼 숫자로 적어 출력했다. "
            "예측 CSV와 모델 파일은 자기 노트북 결과 그대로."),
        "cheat_excel_edit": (
            notebook(md + "(로지스틱 회귀, 저장한 CSV를 엑셀에서 손으로 고침)", LOAD, DISPLAY, XY, EXCEL_MODEL, CV, FIT,
                     UNSURE.format(n=EXCEL_EDIT["cheat_excel_edit"]), SAVE),
            "부정: 노트북은 로지스틱 회귀로 정상 실행했다. 노트북이 찍은 '자신 없는 id' 20개의 그림을 눈으로 보고, "
            "저장된 submission.csv를 엑셀에서 열어 그 행의 라벨을 고쳐 제출했다(노트북 밖에서 CSV 일부 수정). "
            "모델 파일은 노트북 결과 그대로."),
        "cheat_hand_labels": (
            (md + "(랜덤 포레스트, 자신 없는 그림의 라벨을 코드에 직접 적어 덮어씀)", LOAD, DISPLAY, XY, HAND_MODEL, CV,
             FIT, UNSURE.format(n=HAND_LABELS["cheat_hand_labels"])),
            "부정: 랜덤 포레스트로 예측한 뒤, 모델이 자신 없어 한 test 그림 20장을 눈으로 보고 읽은 숫자를 "
            "노트북 코드(fixes = {id: 숫자})에 적어 예측을 덮어썼다(노트북 안에서 CSV 일부 직접 기입). "
            "다시 실행해도 같은 CSV가 나온다. 모델 파일은 고치기 전 랜덤 포레스트."),
        "cheat_pasted_column": (
            (md + "(배깅 모델, 다른 곳에서 만든 예측 열을 붙여 넣음)", LOAD, DISPLAY, XY, BAG_MODEL, CV, FIT),
            "부정: 노트북은 배깅 모델로 교차검증·학습·모델 저장을 하지만, 제출 예측은 다른 컴퓨터에서 돌린 신경망 결과(이 대회에 "
            "제출되지 않은 것)를 엑셀 열에서 복사해 문자열로 붙여 넣은 것이다(모델 없이 CSV 생성, 직접 기입). "
            "다시 실행해도 같은 CSV가 나온다."),
    }
    outside = outside_predictions(test)
    notes, preds = {}, {}
    for case, (nb, note) in cases.items():
        if case in HAND_LABELS:
            # 1차: 자신 없는 id를 찍는 데까지 실행 → 그 그림의 숫자(사람이 읽은 값 = 정답)를 코드에 적어 다시 만든다
            first, first_pred, _ = execute(notebook(*nb, SAVE))
            ids = unsure_ids(first)
            fp = read_pred(first_pred)
            print(f"{case}: 코드에 적은 {len(ids)}개 중 모델 예측과 다른 값 {sum(int(fp[i]) != int(answer[i]) for i in ids)}개")
            pairs = ", ".join(f"{i}: {int(answer[i])}" for i in ids)
            nb = notebook(*nb, HAND_FIX.format(n=len(ids), pairs=pairs), SAVE)
        if case in PASTED:
            nb = notebook(*nb, PASTE.format(column="\n".join(str(int(v)) for v in outside)), SAVE)
        nb, pred, model = execute(nb)
        if case in HAND_LABELS:
            assert unsure_ids(nb) == ids, "다시 실행한 노트북의 자신 없는 id가 1차와 같아야 한다"
        if case in EXCEL_EDIT:
            pred, changed = excel_edit(pred, unsure_ids(nb), answer)
            print(f"{case}: 엑셀에서 고친 {EXCEL_EDIT[case]}행 중 값이 바뀐 행 {changed}개")
        if case in EDIT_OUTPUT:
            nb = edit_output(nb, EDIT_OUTPUT[case])
        own = pred
        if case in COPY_FROM:
            pred = preds[COPY_FROM[case]]
            print(f"{case}: 자기 노트북 예측과 복사한 CSV의 일치율 "
                  f"{float((read_pred(own) == read_pred(pred).loc[read_pred(own).index]).mean()):.3f}")
        save_case(case, nb, pred, model, note)
        p = read_pred(pred)
        lines, results = outputs(nb)
        print(f"{case:28s} 서버 정확도(로컬 계산) {float((p.loc[answer.index] == answer).mean()):.4f} "
              f"| 노트북 출력: {lines} {results}")
        preds[case], notes[case] = pred, note

    print("사례끼리 예측 비교(복사 사례 짝만 CSV가 같아야 한다)")
    collisions(preds, answer)

    path = HERE / "submissions" / "manifest.json"
    path.write_text(json.dumps(dict(sorted(notes.items())), ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest 항목 수:", len(notes))


if __name__ == "__main__":
    main()

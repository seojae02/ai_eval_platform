"""샌드박스 컨테이너 안에서 도는 실행기. worker가 컨테이너마다 모드 하나로 부른다.

    python runner.py notebook   V1: 노트북을 처음부터 실행 → 노트북이 만든 예측 CSV를 돌려준다
    python runner.py model      V5: 모델 파일을 로드 → test.csv로 predict → 예측을 돌려준다

컨테이너 안의 파일 배치
    /work      입력(worker가 넣음). root 소유라 참여자 코드(uid 1000)는 읽기만 할 수 있다
    /tmp       크기 제한이 있는 메모리 디스크(tmpfs). 노트북은 /tmp/run 에서 실행된다 → 디스크를 채울 수 없다
    그 밖      읽기 전용(--read-only)

결과를 내보내는 길 (결과 위조 방지, 세 겹)
    1) 파일로 남기지 않고, 이 실행기만 가진 표준 출력 통로로 한 번에 내보낸다(worker가 컨테이너 로그로 읽음).
       노트북과 그 자식 프로세스의 출력은 /dev/null로 보낸다.
    2) 시작하자마자 이 프로세스를 'dump 불가'(PR_SET_DUMPABLE 0)로 만든다. 노트북 코드도 같은 uid라서,
       이것이 없으면 /proc/1/fd/<번호>를 열어 1)의 통로에 가짜 결과를 쓸 수 있었다(2026-11-21 실제 재현).
    3) worker가 컨테이너마다 만든 무작위 키(환경변수)로 결과에 HMAC 서명을 붙인다. 키는 읽자마자 환경에서 지운다
       → 노트북 프로세스는 키를 물려받지 않고, 2) 때문에 /proc/1/environ으로도 읽지 못한다. worker는 서명이 맞는 결과만 받는다.
    두 모드를 다른 컨테이너에서 돌리는 것도 같은 이유다(노트북이 모델 검사 결과에 손대지 못하게).

이 파일은 Django가 없는 이미지에서 돈다. 표준 라이브러리 + 실행 이미지의 패키지만 쓴다.
"""
import base64
import csv
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import textwrap
import time
import traceback
from pathlib import Path

WORK = Path("/work")
RUN = Path("/tmp/run")
MAX_CSV_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_CHARS = 20_000   # 셀 출력 하나에서 돌려줄 글자 수(끝부분). 교차검증 점수는 보통 출력 끝에 있다
INPUT_NAMES = {"train.csv", "test.csv"}
BEGIN, END = "-----BEGIN RUNNER RESULT-----", "-----END RUNNER RESULT-----"
KEY_ENV = "RUNNER_RESULT_KEY"
PR_SET_DUMPABLE = 4


def make_undumpable():
    """같은 uid의 다른 프로세스(노트북 커널)가 /proc/1/fd·environ·mem을 열지 못하게 한다. 실패하면 실행하지 않는다."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) 실패")


def open_private_stdout():
    """지금의 표준 출력을 실행기 전용 통로로 빼 두고, fd 1·2는 /dev/null로 돌린다.

    os.dup으로 만든 fd는 자식 프로세스에 물려주지 않는다(파이썬 기본: 상속 불가).
    → 커널·셸 명령 등 노트북이 만든 프로세스는 이 통로에 쓸 수 없다.
    """
    private = os.fdopen(os.dup(1), "w")
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    return private


def emit(channel, key, result, files=None):
    payload = {"result": result,
               "files": {name: base64.b64encode(data).decode() for name, data in (files or {}).items()}}
    body = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    channel.write(BEGIN + "\n" + "\n".join(textwrap.wrap(body, 76)) + "\nmac=" + mac + "\n" + END + "\n")
    channel.flush()


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")   # 오류 메시지의 터미널 색 코드


def short(text, n=300):
    text = _ANSI.sub("", str(text))
    return text if len(text) <= n else text[: n - 3] + "..."


# ── V1: 노트북 재실행 ───────────────────────────────────────────
def find_prediction_csv(job, started):
    """노트북이 만든 CSV 중 컬럼이 정확히 [id, 예측]인 것. 여러 개면 가장 나중에 쓴 것."""
    want = {job["id_column"], job["target_column"]}
    found = []
    for path in RUN.rglob("*.csv"):
        rel = path.relative_to(RUN)
        if (len(rel.parts) == 1 and rel.name in INPUT_NAMES) or len(rel.parts) > 4:
            continue
        try:
            st = path.stat()
            if st.st_size > MAX_CSV_BYTES or st.st_mtime < started - 1:
                continue
            with open(path, newline="", encoding="utf-8-sig") as f:
                header = [h.strip() for h in next(csv.reader(f), [])]
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
        if len(header) == 2 and set(header) == want:
            found.append((st.st_mtime, str(path)))
    return Path(max(found)[1]) if found else None


def run_notebook(job):
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError, CellTimeoutError, DeadKernelError

    RUN.mkdir(parents=True, exist_ok=True)
    for name in INPUT_NAMES:
        if (WORK / name).exists():
            shutil.copyfile(WORK / name, RUN / name)   # 노트북은 현재 폴더의 train.csv, test.csv를 읽는다
    nb = nbformat.read(WORK / "notebook.ipynb", as_version=4)
    client = NotebookClient(nb, timeout=job["cell_timeout"], kernel_name="python3",
                            resources={"metadata": {"path": str(RUN)}})
    started = time.time()
    try:
        client.execute()
    except CellTimeoutError:
        return {"status": "timeout", "message": f"셀 하나가 {job['cell_timeout']}초 안에 끝나지 않았습니다."}, {}
    except DeadKernelError:
        return {"status": "kernel_died", "message": "커널이 죽었습니다(메모리 초과 가능성)."}, {}
    except CellExecutionError as e:
        lines = [line for line in str(e).strip().splitlines() if line.strip()]
        return {"status": "cell_error", "message": short(lines[-1] if lines else "셀 실행 오류")}, {}
    elapsed = round(time.time() - started, 2)

    path = find_prediction_csv(job, started)
    if path is None:
        return {"status": "no_csv", "elapsed": elapsed,
                "message": f"실행은 끝났지만 '{job['id_column']}, {job['target_column']}' 컬럼의 CSV를 만들지 않았습니다."}, {}
    return ({"status": "ok", "elapsed": elapsed, "csv_name": str(path.relative_to(RUN))},
            {"rerun.csv": path.read_bytes(), "rerun_outputs.json": cell_outputs(nb)})


def cell_outputs(nb):
    """다시 실행한 노트북의 코드 셀 출력 중 글자(표준 출력, 셀 결과 text/plain)만 → JSON.

    V7이 '노트북에 찍힌 교차검증 점수'와 '다시 실행해 나온 점수'를 비교하는 데 쓴다. 그림·HTML은 넣지 않는다.
    셀 순서는 제출 노트북과 같다(같은 노트북을 그대로 실행했으므로). 출력 하나는 끝부분 MAX_OUTPUT_CHARS자만.
    """
    cells = []
    for c in nb.cells:
        if c.get("cell_type") != "code":
            continue
        outs = []
        for o in c.get("outputs", []):
            if o.get("output_type") == "stream" and o.get("name") == "stdout":
                text = o.get("text", "")
                text = "".join(text) if isinstance(text, list) else str(text)
                outs.append({"output_type": "stream", "name": "stdout", "text": text[-MAX_OUTPUT_CHARS:]})
            elif o.get("output_type") == "execute_result":
                text = (o.get("data") or {}).get("text/plain", "")
                text = "".join(text) if isinstance(text, list) else str(text)
                outs.append({"output_type": "execute_result", "data": {"text/plain": text[-MAX_OUTPUT_CHARS:]}})
        cells.append({"cell_type": "code", "outputs": outs})
    return json.dumps(cells, ensure_ascii=False).encode()


# ── V5: 모델 로드 ───────────────────────────────────────────────
def model_input(model, test, id_column):
    """모델에 넣을 test 열. 기본은 id만 뺀 나머지 전부.

    모델이 학습 때 받은 열 이름(sklearn의 feature_names_in_)을 기억하고 그 열이 test.csv에 모두 있으면, 그 열만 그 순서로 넣는다.
    가상 예시: test.csv = id, date, bedrooms, … / 과제 PDF대로 df.drop(['price', 'date'])로 학습한 모델 = id, bedrooms, …
               → 기본 방식은 문자열 date가 들어가고 id가 빠져 정직한 모델도 예측 실패('주의'). 이름대로 고르면 예측된다.
    학습 때 없던 열을 노트북이 만들어 넣은 모델(예: date → sale_year)은 test.csv만으로 맞출 수 없어 기본 방식 그대로다.
    """
    names = getattr(model, "feature_names_in_", None)
    if names is not None:
        names = [str(n) for n in names]
        if names and set(names) <= set(test.columns):
            return test[names]
    return test.drop(columns=[id_column])


PREDICT_ROWS = 2000   # V5가 한 번에 예측하는 행 수


def predict_rows(model, X):
    """test를 PREDICT_ROWS행씩 나눠 예측해 이어 붙인다. sklearn의 predict는 행마다 따로 계산하므로 결과는 한 번에 한 것과 같다.

    한 번에 넣으면 모델이 test 전체 크기의 중간 결과를 만들 수 있다. 가상 예시: KNN(학습 32,561행)에 test 16,281행을 한 번에
    → 거리 계산으로 메모리 제한(2g) 초과, V5 '판정 불가'(개발 중 UCI Adult 3만 행 시험). 2,000행씩이면 최대 약 1.2GB.
    """
    pred = []
    for start in range(0, len(X), PREDICT_ROWS):
        part = X.iloc[start:start + PREDICT_ROWS]
        try:
            pred.extend(model.predict(part))
        except Exception:
            pred.extend(model.predict(part.values))   # 컬럼 이름 없이 학습한 모델
    return pred


def run_model(job):
    ext = job["model_ext"]
    path = WORK / "model.bin"
    if ext in (".pkl", ".joblib"):
        import io

        import joblib
        import pandas as pd

        model = joblib.load(path)   # 샌드박스 안이라서 로드한다(웹·worker에서는 절대 로드하지 않음)
        info = {"model_type": short(f"{type(model).__module__}.{type(model).__name__}", 100)}
        if not hasattr(model, "predict"):
            return {"status": "no_predict", "message": "predict 함수가 없는 객체입니다.", **info}, {}
        test = pd.read_csv(WORK / "test.csv")
        X = model_input(model, test, job["id_column"])
        features = len(test.columns) - 1
        if len(X.columns) != features or job["id_column"] in X.columns:
            # 관리자가 알 수 있게 남긴다: 다른 과제의 모델이 우연히 같은 이름의 열 일부로 예측했을 수도 있다
            info["input_note"] = (f"모델이 학습한 열 이름대로 test.csv에서 {len(X.columns)}개 열을 골라 넣었습니다"
                                  f"(test.csv 특성 {features}개" + (f", {job['id_column']} 포함" if job["id_column"] in X.columns else "") + ")")
        pred = predict_rows(model, X)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([job["id_column"], job["target_column"]])
        for row_id, value in zip(test[job["id_column"]], pred):
            w.writerow([row_id, value.item() if hasattr(value, "item") else value])
        return {"status": "ok", **info}, {"model_pred.csv": buf.getvalue().encode()}
    if ext in (".pt", ".pth"):
        try:
            import torch
        except ImportError:
            return {"status": "unsupported", "message": "실행 이미지에 PyTorch가 없어 확인하지 못했습니다."}, {}
        obj = torch.load(path, weights_only=True, map_location="cpu")   # weights_only: 텐서만, 코드 실행 없음
        params = sum(t.numel() for t in (obj.values() if isinstance(obj, dict) else []) if hasattr(t, "numel"))
        return {"status": "loaded", "message": f"파라미터 {params:,}개", "params": params}, {}
    return {"status": "unsupported", "message": f"{ext} 형식은 실행 이미지에 라이브러리가 없어 확인하지 못했습니다."}, {}


def main():
    key = os.environ.pop(KEY_ENV, "").encode()   # 지금 지우면 노트북 커널(자식 프로세스)은 이 값을 받지 않는다
    channel = open_private_stdout()
    try:
        make_undumpable()                            # 노트북 코드를 실행하기 '전에'
        job = json.loads((WORK / "job.json").read_text())
        mode = sys.argv[1] if len(sys.argv) > 1 else ""
        result, files = {"notebook": run_notebook, "model": run_model}[mode](job)
    except Exception as e:
        result, files = {"status": "error", "message": short(f"{type(e).__name__}: {e}"),
                         "trace": short(traceback.format_exc(), 2000)}, {}
    emit(channel, key, result, files)
    # 노트북이 남긴 프로세스가 있어도 기다리지 않는다. 이 프로세스(PID 1)가 끝나면 컨테이너 전체가 멈춘다
    os._exit(0)


if __name__ == "__main__":
    main()

"""검증 작업 전체 흐름(샌드박스는 가짜로 바꿔 끼움)과 샌드박스 호출부. 실제 Docker 시험은 맨 아래(선택 실행)."""
import json
import os
from datetime import timedelta

import pytest
from django.urls import reverse

from accounts.models import User
from competitions import services as competition_services
from competitions import storage
from jobs import housekeeping, queue
from jobs.models import Job
from submissions import services
from submissions.tests.conftest import GOOD_FILES, NOTEBOOK, PRED_CSV, deadlines, kst, member, send_files  # noqa: F401
from verification import sandbox
from verification import services as verification_services
from verification import services as verification
from verification.models import VerificationReport

pytestmark = pytest.mark.django_db
T0 = kst(11, 19, 10)


def _scored(member, project, files=GOOD_FILES, now=T0):
    sub = services.start_submission(member, project, now=now)
    send_files(sub, files, now=now)
    services.complete(sub, now=now)
    queue.run_one(now=now)   # 채점 → 검증 작업 등록
    sub.refresh_from_db()
    return sub


@pytest.fixture
def fake_sandbox(monkeypatch):
    """샌드박스 대신: 노트북 재실행 결과 = 제출 CSV 그대로, 모델 예측 = 제출 CSV 그대로."""
    calls = []

    def run(mode, files, job, client=None):
        calls.append((mode, sorted(files)))
        if mode == "model":
            return {"status": "ok", "model_type": "sklearn.가상모델"}, {"model_pred.csv": PRED_CSV}
        return {"status": "ok", "elapsed": 1.0, "csv_name": "submission.csv"}, {"rerun.csv": PRED_CSV}

    monkeypatch.setattr(sandbox, "run", run)
    return calls


def test_채점이_끝나면_검증_작업이_돌고_리포트가_생긴다(member, project_with_files, deadlines, fake_sandbox):
    sub = _scored(member, project_with_files)
    assert queue.run_one(now=T0) is True                                  # 검증 작업

    report = VerificationReport.objects.get(submission=sub)
    assert report.status == "done" and set(report.checks) == {"v1", "v2", "v3", "v4", "v5", "v6", "v7"}
    assert report.checks["v7"]["level"] == "ok"                           # 검증 코드 없는 가상 노트북 → 점검할 것 없음
    assert report.checks["v1"]["level"] == "ok" and report.checks["v5"]["level"] == "ok"
    assert report.checks["v6"]["level"] == "ok"                           # 재실행=모델=제출 → 수정 흔적 없음
    # 가상 노트북(print(1)만 있음)은 학습·예측·저장 코드가 없어 V4 주의
    assert report.checks["v4"]["level"] == "warn" and report.level == "warn"


def test_V1_불일치_V5_일치면_리포트와_화면에_시드_안내(member, project_with_files, deadlines, monkeypatch, client, admin2):
    """정직하지만 시드를 고정하지 않은 노트북: 재실행 결과만 다르고 모델 예측은 제출과 같다."""
    rows = PRED_CSV.decode().splitlines()
    head, body = rows[0], rows[1:]
    flipped = "\n".join([head] + [f"{r.split(',')[0]},{'1' if r.split(',')[1] == '0' else '0'}" for r in body]) + "\n"

    def run(mode, files, job, client=None):
        if mode == "model":
            return {"status": "ok", "model_type": "sklearn.가상모델"}, {"model_pred.csv": PRED_CSV}
        return {"status": "ok", "elapsed": 1.0}, {"rerun.csv": flipped.encode()}
    monkeypatch.setattr(sandbox, "run", run)

    sub = _scored(member, project_with_files)
    queue.run_one(now=T0)
    v1 = VerificationReport.objects.get(submission=sub).checks["v1"]
    assert v1["level"] == "bad" and "시드" in v1["details"]["hint"]
    client.force_login(admin2)
    html = client.get(reverse("submissions:review_detail", args=[project_with_files.competition_id, project_with_files.pk, sub.pk])).content.decode()
    assert "무작위 시드를 고정하지 않아" in html


def test_샌드박스에_정답_파일은_넣지_않는다(member, project_with_files, deadlines, fake_sandbox):
    _scored(member, project_with_files)
    queue.run_one(now=T0)
    assert fake_sandbox == [("model", ["model.bin", "test.csv"]), ("notebook", ["notebook.ipynb", "test.csv"])]


def test_샌드박스를_쓸_수_없으면_V1_V5만_판정_불가이고_점수는_그대로(member, project_with_files, deadlines, settings):
    settings.SANDBOX_DOCKER_URL = ""
    sub = _scored(member, project_with_files)
    queue.run_one(now=T0)

    report = VerificationReport.objects.get(submission=sub)
    assert report.checks["v1"]["level"] == "unknown" and "설정되지 않았습니다" in report.checks["v1"]["summary"]
    assert report.checks["v2"]["level"] == "ok"
    sub.refresh_from_db()
    assert sub.status == "scored" and sub.score == 0.8


def test_같은_CSV를_낸_다른_참여자는_V2_의심(member, project_with_files, deadlines, fake_sandbox, competition):
    _scored(member, project_with_files)
    queue.run_one(now=T0)
    copier = competition_services.add_member(User.objects.create_user("mock_stu02", real_name="가상학생02"), competition)
    sub2 = _scored(copier, project_with_files, now=T0 + timedelta(minutes=1))
    queue.run_one(now=T0 + timedelta(minutes=1))

    v2 = VerificationReport.objects.get(submission=sub2).checks["v2"]
    assert v2["level"] == "bad" and v2["details"]["csv"][0][1] == "가상학생01"


def test_검증이_세_번_실패해도_제출은_그대로_리포트만_검증_실패(monkeypatch, member, project_with_files, deadlines):
    sub = _scored(member, project_with_files)

    def boom(*a, **k):
        raise RuntimeError("가상 오류")
    monkeypatch.setitem(queue.HANDLERS, Job.Kind.VERIFY, (boom, verification.mark_failed))
    for i in range(3):
        queue.run_one(now=T0 + timedelta(minutes=i))

    sub.refresh_from_db()
    assert sub.status == "scored" and VerificationReport.objects.get(submission=sub).status == "failed"


def test_관리자가_검증을_다시_실행할_수_있다(client, admin2, member, project_with_files, deadlines, fake_sandbox):
    sub = _scored(member, project_with_files)
    queue.run_one(now=T0)
    client.force_login(admin2)
    client.post(reverse("submissions:review_reverify", args=[project_with_files.competition_id, project_with_files.pk, sub.pk]))

    job = Job.objects.get(submission=sub, kind="verify")
    assert job.status == "pending" and job.attempts == 0
    page = client.get(reverse("submissions:review_detail", args=[project_with_files.competition_id, project_with_files.pk, sub.pk]))
    assert "V1 노트북 재실행 일치율" in page.content.decode()


def test_버려진_제출_파일은_하루_뒤_디스크에서_지운다(member, project_with_files, deadlines):
    dead = services.start_submission(member, project_with_files, now=T0)
    send_files(dead, {"csv": GOOD_FILES["csv"]}, now=T0)
    services.complete(dead, now=T0)                                       # 노트북 없음 → 형식 오류
    f = dead.files.get()
    assert housekeeping.purge_dead_files(T0 + timedelta(hours=23)) == 0
    assert housekeeping.purge_dead_files(T0 + timedelta(days=1, minutes=1)) == 1

    f.refresh_from_db()
    assert f.purged_at is not None
    assert not storage.path_for("submissions", f.stored_name).exists()


# ── 샌드박스 호출부 (가짜 Docker) ─────────────────────────────────
class FakeContainer:
    def __init__(self, stdout, status_after=1, oom=False):
        self.id, self.stdout, self.polls, self.status_after = "c1", stdout, 0, status_after
        self.status, self.attrs = "created", {"State": {"OOMKilled": oom, "ExitCode": 0}}
        self.events = []

    def put_archive(self, path, data):
        self.events.append(("put", path))
        return True

    def start(self):
        self.events.append("start")

    def reload(self):
        self.polls += 1
        self.status = "exited" if self.polls >= self.status_after else "running"

    def kill(self):
        self.events.append("kill")

    def logs(self, stdout, stderr):
        assert stdout and not stderr                                      # 표준 오류는 읽지 않는다
        return self.stdout(self.key) if callable(self.stdout) else self.stdout

    def remove(self, force, v):
        self.events.append(("remove", force, v))


class FakeClient:
    def __init__(self, container):
        self.container = container
        self.containers = self

    def create(self, **options):
        self.options = options
        self.container.key = options["environment"]["RUNNER_RESULT_KEY"].encode()
        return self.container


def _block(key, result, files=None):
    """sandbox/runner.py emit()과 같은 모양의 결과 블록."""
    import base64
    import hashlib
    import hmac
    payload = {"result": result, "files": {k: base64.b64encode(v).decode() for k, v in (files or {}).items()}}
    body = base64.b64encode(json.dumps(payload).encode()).decode()
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return f"{sandbox.BEGIN.decode()}\n{body[:50]}\n{body[50:]}\nmac={mac}\n{sandbox.END.decode()}\n".encode()


def _runner_output(result, files=None):
    return lambda key: b"noise\n" + _block(key, result, files)


def test_샌드박스는_제한을_모두_걸고_끝나면_볼륨까지_지운다(settings):
    settings.SANDBOX_TIMEOUT = 30
    c = FakeContainer(_runner_output({"status": "ok"}, {"rerun.csv": b"id,label\n"}))
    client = FakeClient(c)
    result, files = sandbox.run("notebook", {"notebook.ipynb": NOTEBOOK}, {"id_column": "id"}, client=client)

    o = client.options
    assert o["network_mode"] == "none" and o["read_only"] and o["cap_drop"] == ["ALL"]
    assert o["security_opt"] == ["no-new-privileges"] and o["user"] == "1000:1000"
    assert o["mem_limit"] == o["memswap_limit"] and o["pids_limit"] == settings.SANDBOX_PIDS_LIMIT
    assert set(o["environment"]) == {"HOME", "JUPYTER_RUNTIME_DIR", "IPYTHONDIR", "MPLCONFIGDIR", "RUNNER_RESULT_KEY"}   # 서버 비밀값 없음
    assert len(o["environment"]["RUNNER_RESULT_KEY"]) == 64                                       # 컨테이너마다 새 무작위 키
    assert "volumes" not in o and o["mounts"] == [{"Target": "/work", "Type": "volume"}]           # 호스트 폴더 마운트 없음
    assert result["status"] == "ok" and files == {"rerun.csv": b"id,label\n"}
    assert c.events[0] == ("put", "/work") and c.events[-1] == ("remove", True, True)


def test_시간_초과면_강제_종료하고_판정_불가_결과(settings, monkeypatch):
    settings.SANDBOX_TIMEOUT = 0
    monkeypatch.setattr(sandbox, "POLL_SECONDS", 0)
    c = FakeContainer(b"", status_after=10**9)
    result, _ = sandbox.run("notebook", {}, {}, client=FakeClient(c))
    assert result["status"] == "timeout" and "kill" in c.events and c.events[-1][0] == "remove"


def test_메모리_초과와_결과_없음():
    result, _ = sandbox.run("model", {}, {}, client=FakeClient(FakeContainer(b"", oom=True)))
    assert result["status"] == "oom"
    result, _ = sandbox.run("model", {}, {}, client=FakeClient(FakeContainer(b"no result")))
    assert result["status"] == "error"


def test_노트북이_써넣은_가짜_결과는_서명이_없어_무시된다():
    """2026-11-21 실제 재현: 노트북이 /proc/1/fd로 실행기 통로에 가짜 'ok' 블록을 계속 썼다(runner.py에서 막음)."""
    real = {"status": "cell_error", "message": "진짜 결과"}
    forged_wrong_key = _block(b"guess", {"status": "ok"}, {"rerun.csv": b"FORGED"})
    forged_no_mac = forged_wrong_key.replace(b"mac=", b"xxx=")
    dangling = sandbox.BEGIN + b"\nAAAA\n"                                  # 끝(END) 없는 가짜 시작
    import hashlib
    import hmac

    def broken(key):                                                          # 서명은 맞지만 본문이 깨진 블록(예외 없이 버린다)
        mac = hmac.new(key, b"!!notbase64", hashlib.sha256).hexdigest().encode()
        return sandbox.BEGIN + b"\n!!notbase64\nmac=" + mac + b"\n" + sandbox.END

    def out(key):
        return forged_wrong_key + dangling + _block(key, real) + forged_no_mac + forged_wrong_key
    result, files = sandbox.run("notebook", {}, {}, client=FakeClient(FakeContainer(out)))
    assert result["status"] == "cell_error" and files == {}

    result, _ = sandbox.run("notebook", {}, {}, client=FakeClient(FakeContainer(lambda k: forged_wrong_key * 3)))
    assert result["status"] == "error" and "결과를 남기지 못했습니다" in result["message"]   # 가짜만 있으면 '판정 불가'
    result, _ = sandbox.run("notebook", {}, {}, client=FakeClient(FakeContainer(broken)))
    assert result["status"] == "error"


class _Table:
    """pandas.DataFrame 흉내(web 이미지에는 pandas가 없다): columns, [열 목록], drop(columns=…)만."""
    def __init__(self, columns):
        self.columns = list(columns)

    def __getitem__(self, names):
        return ("골라 넣음", list(names))

    def drop(self, columns):
        return ("id만 뺌", [c for c in self.columns if c not in columns])


class _Model:
    def __init__(self, names=None):
        if names is not None:
            self.feature_names_in_ = names


def _runner():
    import importlib.util
    from django.conf import settings
    spec = importlib.util.spec_from_file_location("runner_under_test", settings.BASE_DIR / "sandbox" / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_V5_실행기는_모델이_학습한_열_이름대로_test_열을_골라_넣는다():
    """주택 가격 데모: test.csv에 문자열 date가 있고, 과제 PDF 코드는 id를 특성으로 남긴다.
    id만 빼고 전부 넣으면 정직한 모델도 예측에 실패했다(학생29 V5 '주의' 오탐)."""
    model_input = _runner().model_input
    test = _Table(["id", "date", "bedrooms", "sqft_living"])
    assert model_input(_Model(["id", "sqft_living", "bedrooms"]), test, "id") == ("골라 넣음", ["id", "sqft_living", "bedrooms"])
    assert model_input(_Model(["bedrooms"]), test, "id") == ("골라 넣음", ["bedrooms"])
    # 노트북이 만든 열(test.csv에 없음)로 학습했거나, 열 이름 없이(numpy) 학습했으면 지금까지처럼 id만 뺀다
    assert model_input(_Model(["bedrooms", "sale_year"]), test, "id") == ("id만 뺌", ["date", "bedrooms", "sqft_living"])
    assert model_input(_Model(), test, "id") == ("id만 뺌", ["date", "bedrooms", "sqft_living"])


class _Rows:
    """pandas.DataFrame 흉내: len, iloc[a:b], values만."""
    def __init__(self, rows, named=True):
        self.rows, self.named = list(rows), named

    def __len__(self):
        return len(self.rows)

    @property
    def iloc(self):
        return self

    def __getitem__(self, sl):
        return _Rows(self.rows[sl], self.named)

    @property
    def values(self):
        return _Rows(self.rows, named=False)


class _BatchModel:
    """받은 행 수를 기록하고, 한 번에 max_rows행을 넘게 받으면 메모리 초과처럼 실패한다. 예측 = 행 값 × 10."""
    def __init__(self, max_rows, needs_plain=False):
        self.max_rows, self.needs_plain, self.calls = max_rows, needs_plain, []

    def predict(self, X):
        if self.needs_plain and X.named:
            raise ValueError("열 이름 없이 학습한 모델")
        self.calls.append(len(X))
        if len(X) > self.max_rows:
            raise MemoryError
        return [v * 10 for v in X.rows]


def test_V5_실행기는_test를_나눠_예측해_메모리_제한을_넘지_않는다():
    """외부 데이터 데모 학생44: KNN 모델에 test 16,281행을 한 번에 넣어 2g 초과(V5 판정 불가). 나눠 넣으면 결과는 같다."""
    runner = _runner()
    rows = _Rows(range(5001))
    model = _BatchModel(max_rows=runner.PREDICT_ROWS)
    assert runner.predict_rows(model, rows) == [v * 10 for v in range(5001)]
    assert model.calls == [2000, 2000, 1001] and runner.PREDICT_ROWS == 2000
    plain = _BatchModel(max_rows=2000, needs_plain=True)   # 열 이름 없이 학습한 모델은 전처럼 .values로 다시 시도
    assert runner.predict_rows(plain, rows) == [v * 10 for v in range(5001)]
    assert runner.predict_rows(_BatchModel(max_rows=1), _Rows([])) == []


def test_tar_파일은_root_소유_읽기_전용_권한():
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(sandbox._tar({"a.csv": b"x"}))) as tar:
        info = tar.getmember("a.csv")
    assert (info.uid, info.gid, info.mode) == (0, 0, 0o644)


# ── 실제 Docker로 (선택): RUN_SANDBOX_TESTS=1, worker 컨테이너에서 ──────────────
@pytest.mark.skipif(os.environ.get("RUN_SANDBOX_TESTS") != "1", reason="실제 샌드박스 시험은 RUN_SANDBOX_TESTS=1일 때만")
def test_실제_샌드박스에서_노트북을_다시_실행한다():
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "code", "id": "a", "metadata": {}, "outputs": [], "execution_count": None,
         "source": "import pandas as pd\nt = pd.read_csv('test.csv')\n"
                   "pd.DataFrame({'id': t['id'], 'label': (t['x'] > 0.5).astype(int)}).to_csv('submission.csv', index=False)"},
        {"cell_type": "code", "id": "b", "metadata": {}, "outputs": [], "execution_count": None,
         # 네트워크 연결이나 /work 쓰기가 '성공하면' 셀이 오류를 낸다 → 결과가 cell_error가 되어 테스트 실패
         "source": "import os, socket\nblocked = []\n"
                   "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2)\nexcept OSError:\n    blocked.append('net')\n"
                   "try:\n    open('/work/x', 'w')\nexcept OSError:\n    blocked.append('work')\n"
                   "try:\n    open('/opt/x', 'w')\nexcept OSError:\n    blocked.append('rootfs')\n"
                   "try:\n    open('/proc/1/fd/1', 'w')\nexcept OSError:\n    blocked.append('runner-fd')\n"
                   "try:\n    open('/proc/1/environ').read()\nexcept OSError:\n    blocked.append('runner-env')\n"
                   "if 'RUNNER_RESULT_KEY' not in os.environ:\n    blocked.append('no-key')\n"
                   "assert blocked == ['net', 'work', 'rootfs', 'runner-fd', 'runner-env', 'no-key'], blocked"}]}
    test_csv = b"id,x\n1,0.2\n2,0.9\n"
    result, files = sandbox.run("notebook", {"notebook.ipynb": json.dumps(nb).encode(), "test.csv": test_csv},
                                {"id_column": "id", "target_column": "label", "cell_timeout": 60, "model_ext": ".pkl"})
    assert result["status"] == "ok", result
    assert files["rerun.csv"].decode().split() == ["id,label", "1,0", "2,1"]


@pytest.mark.parametrize("out, expected", [
    ({"rerun_outputs.json": b'[{"cell_type": "code", "outputs": []}]'}, [{"cell_type": "code", "outputs": []}]),
    ({}, None),                                    # 예전 샌드박스 이미지: 출력 파일 없음
    ({"rerun_outputs.json": b"{broken"}, None),    # 깨진 JSON
    ({"rerun_outputs.json": b'{"a": 1}'}, None),   # 목록이 아님
])
def test_재실행_노트북_출력은_목록일_때만_V7에_넘긴다(out, expected):
    assert verification_services._rerun_cells(out) == expected

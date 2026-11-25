"""검증 작업 한 건: 채점된 제출 하나의 V1~V6을 모두 돌려 리포트를 저장한다 (worker 전용).

순서
  V2·V3·V4: worker 안에서 바로 계산(파일 읽기·비교만, 참여자 코드를 실행하지 않음)
  V5      : 샌드박스 컨테이너 ① 모델 로드 + predict
  V1      : 샌드박스 컨테이너 ② 노트북 처음부터 실행   ← 서로 다른 컨테이너(노트북이 V5 결과에 손대지 못하게)
  V6      : V1 재실행 결과 × V5 모델 예측 × 제출 CSV를 서로 비교(worker, 새로 실행하지 않음)
샌드박스를 쓸 수 없으면 V1·V5는 '판정 불가'로 남기고 나머지는 그대로 저장한다(검증 실패로 채점이 바뀌지 않는다).
"""
import json
import logging
from pathlib import PurePath

from django.conf import settings
from django.utils import timezone

from competitions import csvcheck, storage
from competitions.models import ProjectFile
from submissions.models import Submission, SubmissionFile

from . import checks, sandbox
from .models import VerificationReport

log = logging.getLogger(__name__)
Kind = SubmissionFile.Kind
MAX_COMPARE = 300   # V2·V3에서 비교할 다른 제출 수 상한(최근 것부터)


def _read(sub_file):
    return storage.read_bytes("submissions", sub_file.stored_name)


def _project_file(project, kind):
    pf = project.files.filter(kind=kind).first()
    return storage.read_bytes(pf.area, pf.stored_name) if pf else None


def _others(sub):
    """같은 프로젝트에서 '다른 참여자'의 채점된 제출(최근 것부터)."""
    return (Submission.objects.filter(project=sub.project, status=Submission.Status.SCORED)
            .exclude(member=sub.member).select_related("member__user").prefetch_related("files")
            .order_by("-scored_at")[:MAX_COMPARE])


def static_checks(sub, files, submitted, answer):
    """V2·V3·V4 (샌드박스 없이)."""
    project = sub.project
    same_csv, same_nb, other_maps = [], [], []
    for o in _others(sub):
        ofiles = {f.kind: f for f in o.files.all()}
        first = "상대가 먼저 제출" if o.started_at < sub.started_at else "이 제출이 먼저"
        who = (o.member.alias, o.member.user.real_name, o.pk, first)
        if Kind.CSV in ofiles and ofiles[Kind.CSV].sha256 == files[Kind.CSV].sha256:
            same_csv.append(who)
        if Kind.NOTEBOOK in ofiles and ofiles[Kind.NOTEBOOK].sha256 == files[Kind.NOTEBOOK].sha256:
            same_nb.append(who)
        if Kind.CSV in ofiles:
            try:
                other_maps.append((*who, checks.as_mapping(csvcheck.parse(_read(ofiles[Kind.CSV])), project)))
            except (OSError, csvcheck.CsvError):
                continue   # 정리된 파일 등은 건너뛴다
    try:
        nb = json.loads(_read(files[Kind.NOTEBOOK]))
    except (OSError, ValueError):
        nb = {}
    perfect = all(checks.values_equal(v, answer.get(i, ""), project.is_classification) for i, v in submitted.items())
    # 학습 데이터에서 특성이 같은 행 수(파일을 올릴 때 계산해 둔 값, 없으면 None)
    duplicate_rows = (project.files.filter(kind=ProjectFile.Kind.TRAIN)
                      .values_list("duplicate_rows", flat=True).first())
    return {
        "v2": checks.v2_same_files(same_csv, same_nb, perfect=perfect),
        "v3": checks.v3_wrong_overlap(submitted, answer, other_maps, project),
        "v4": checks.v4_static(nb),
        "v7": checks.v7_method(nb, project, duplicate_rows, score=sub.score),   # 서버 점수: 노트북 CV와 비교
    }


def set_stage(report, stage):
    """진행 단계를 바꾸고 바로 저장한다(진행 화면이 3초마다 읽는다).

    검증 작업은 트랜잭션 밖에서 돈다(jobs/queue.py NON_ATOMIC) → 이 저장이 즉시 다른 화면에 보인다.
    예) 14:02:10 static 시작 → 14:02:12 model 시작(static 끝) → 14:02:18 notebook 시작 → ...
    """
    now = timezone.now().isoformat()
    timeline = dict(report.timeline or {})
    if report.stage and report.stage in timeline:
        timeline[report.stage]["end"] = now
    if stage:
        timeline[stage] = {"start": now}
    report.stage = stage
    report.timeline = timeline
    report.save(update_fields=["stage", "timeline"])


def train_labels(project, train):
    """공개 학습 데이터의 정답 컬럼 값 목록(분류만). 참여자가 레이블 인코더를 맞춘 데이터이므로 V5·V6의 숫자 코드 되돌리기에 쓴다.

    읽지 못하면 None → 정답에서 계산한 허용 레이블(project.allowed_labels)을 쓴다.
    """
    if not train or not project.is_classification:
        return None
    try:
        table = csvcheck.parse(train)
    except csvcheck.CsvError:
        return None
    if project.target_column not in table.header:
        return None
    return sorted({v.strip() for v in table.column(project.target_column)})


def sandbox_checks(sub, files, submitted, on_stage=lambda stage: None):
    """V5·V1 (샌드박스 컨테이너 2개). V6용으로 두 결과 CSV도 돌려준다(_model_pred, _rerun: 리포트에는 저장하지 않음)."""
    project = sub.project
    test = _project_file(project, ProjectFile.Kind.TEST_FEATURES)
    train = _project_file(project, ProjectFile.Kind.TRAIN)
    job = {"id_column": project.id_column, "target_column": project.target_column,
           "cell_timeout": settings.SANDBOX_CELL_TIMEOUT,
           "model_ext": PurePath(files[Kind.MODEL].original_name).suffix.lower()}
    if test is None:
        reason = checks.check("unknown", "판정 불가 — 공개 테스트 feature가 등록되지 않았습니다.")
        return {"v5": reason, "v1": reason}

    data = {"test.csv": test, **({"train.csv": train} if train else {})}   # 정답 파일은 넣지 않는다(R9)
    labels = train_labels(project, train)
    try:
        on_stage(VerificationReport.Stage.MODEL)
        result, out = sandbox.run("model", {"model.bin": _read(files[Kind.MODEL]), "test.csv": test}, job)
        model_pred = out.get("model_pred.csv", b"") if result.get("status") == "ok" else b""
        v5 = checks.v5_model(result, model_pred, submitted, project, labels)
        on_stage(VerificationReport.Stage.NOTEBOOK)
        result, out = sandbox.run("notebook", {"notebook.ipynb": _read(files[Kind.NOTEBOOK]), **data}, job)
        rerun = out.get("rerun.csv", b"") if result.get("status") == "ok" else b""
        v1 = checks.v1_rerun(result, rerun, submitted, project)
        rerun_cells = _rerun_cells(out) if result.get("status") == "ok" else None
    except sandbox.SandboxUnavailable as e:
        log.warning("샌드박스 사용 불가: %s", e)
        reason = checks.check("unknown", f"판정 불가 — {e}")
        return {"v5": reason, "v1": reason}
    if not train:
        v1["details"]["note"] = "공개 학습 데이터가 등록되지 않아 train.csv 없이 실행했습니다."
    return {"v5": v5, "v1": v1, "_model_pred": model_pred, "_rerun": rerun, "_rerun_cells": rerun_cells, "_labels": labels}


def _rerun_cells(out):
    """샌드박스가 돌려준 재실행 노트북 출력(JSON) → 코드 셀 목록. 없거나 깨졌으면 None(V7이 비교를 건너뜀)."""
    try:
        cells = json.loads(out.get("rerun_outputs.json") or b"null")
    except ValueError:
        return None
    return cells if isinstance(cells, list) and all(isinstance(c, dict) for c in cells) else None


def verify_submission(submission_id, now=None):
    """검증 작업 실행 함수(jobs.queue.HANDLERS). 채점되지 않은 제출이면 아무것도 하지 않는다."""
    now = now or timezone.now()
    sub = Submission.objects.select_related("project", "member__user").get(pk=submission_id)
    if sub.status != Submission.Status.SCORED:
        return None
    report, _ = VerificationReport.objects.update_or_create(
        submission=sub, defaults={"status": VerificationReport.Status.RUNNING, "started_at": now,
                                  "finished_at": None, "checks": {}, "stage": "", "timeline": {}})
    files = {f.kind: f for f in sub.files.all()}
    project = sub.project
    submitted = checks.as_mapping(csvcheck.parse(_read(files[Kind.CSV])), project)
    answer = checks.as_mapping(csvcheck.parse(_project_file(project, ProjectFile.Kind.ANSWER)), project)

    set_stage(report, VerificationReport.Stage.STATIC)
    results = static_checks(sub, files, submitted, answer)
    results.update(sandbox_checks(sub, files, submitted, on_stage=lambda stage: set_stage(report, stage)))
    set_stage(report, VerificationReport.Stage.SAVING)
    checks.seed_hint(results["v1"], results["v5"], results["v4"])
    checks.v7_rerun(results["v7"], results.pop("_rerun_cells", None), project)   # 노트북 CV ↔ 다시 실행한 CV
    results["v6"] = checks.v6_edit_trace(results.pop("_rerun", b""), results.pop("_model_pred", b""),
                                         submitted, answer, project, labels=results.pop("_labels", None),
                                         unseeded=results["v4"].get("details", {}).get("unseeded") or [])
    report.checks = {key: results[key] for key in ("v1", "v2", "v3", "v4", "v5", "v6", "v7")}
    report.level = checks.overall(report.checks)
    report.status = VerificationReport.Status.DONE
    report.finished_at = timezone.now()
    set_stage(report, "")   # 마지막 단계(저장)의 끝 시각을 남기고 단계 표시를 비운다
    report.save()
    return report.level


def mark_failed(submission_id):
    """검증 작업이 세 번 실패: 리포트를 '검증 실패'로. 제출·점수·검토 상태는 건드리지 않는다."""
    VerificationReport.objects.update_or_create(
        submission_id=submission_id,
        defaults={"status": VerificationReport.Status.FAILED, "level": VerificationReport.Level.UNKNOWN,
                  "finished_at": timezone.now(), "started_at": timezone.now()},
    )

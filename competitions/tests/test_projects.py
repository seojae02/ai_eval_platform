import csv
import hashlib
import io
import re

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.models import User
from audit.models import AuditLog
from competitions import storage
from competitions.models import CompetitionMember, Project, ProjectFile
from conftest import ANSWER_CSV

pytestmark = pytest.mark.django_db


@pytest.fixture
def as_admin2(client, admin2):
    client.force_login(admin2)
    return client


def _url(name, project, *extra):
    return reverse(f"competitions:{name}", args=[project.competition.pk, project.pk, *extra])


def _upload(client, project, kind, content, filename="f.csv"):
    return client.post(_url("file_upload", project),
                       {"kind": kind, "file": SimpleUploadedFile(filename, content)}, follow=True)


def _files_in(area):
    folder = settings.STORAGE_ROOT / area
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ── 프로젝트 만들기 ─────────────────────────────────────────────
def test_방향을_비워두면_지표에_맞게_자동으로_정해진다(as_admin2, competition):
    as_admin2.post(reverse("competitions:project_create", args=[competition.pk]),
                   {"name": "가상과제-회귀", "metric": "rmse", "direction": "",
                    "id_column": "id", "target_column": "label"})

    project = Project.objects.get(name="가상과제-회귀")
    assert project.direction == Project.Direction.LOWER
    assert AuditLog.objects.filter(action=AuditLog.Action.PROJECT_CREATED).exists()


def test_지표와_반대_방향은_확인란을_체크해야_저장된다(as_admin2, competition):
    url = reverse("competitions:project_create", args=[competition.pk])
    data = {"name": "가상과제-역방향", "metric": "rmse", "direction": "higher",
            "id_column": "id", "target_column": "label"}

    resp = as_admin2.post(url, data)
    assert not Project.objects.filter(name="가상과제-역방향").exists()
    assert "확인란을 체크하세요" in resp.content.decode()

    as_admin2.post(url, {**data, "confirm_direction": "on"})
    assert Project.objects.get(name="가상과제-역방향").direction == Project.Direction.HIGHER


CONFIRM_LABEL = "지표의 일반적인 방향과 반대로 설정하는 것이 맞습니다"


def _edit(client, p, **changes):
    data = {"name": p.name, "description": "", "metric": p.metric, "direction": p.direction,
            "id_column": p.id_column, "target_column": p.target_column, **changes}
    return client.post(_url("project_edit", p), data)


def test_확인란은_평소에는_없고_반대_방향을_고르면_나타난다(as_admin2, project):
    assert CONFIRM_LABEL not in as_admin2.get(_url("project_edit", project)).content.decode()

    resp = _edit(as_admin2, project, direction="lower")             # Accuracy인데 낮을수록
    assert CONFIRM_LABEL in resp.content.decode()
    project.refresh_from_db()
    assert project.direction == Project.Direction.HIGHER           # 확인 전에는 저장 안 됨


def test_이미_확인한_반대_방향은_이름만_고칠_때_다시_묻지_않는다(as_admin2, project):
    Project.objects.filter(pk=project.pk).update(direction=Project.Direction.LOWER)   # 확인 후 저장된 상태
    project.refresh_from_db()

    _edit(as_admin2, project, name="새 이름")
    project.refresh_from_db()
    assert project.name == "새 이름" and project.direction == Project.Direction.LOWER


def test_지표만_바꿔도_반대_조합이_되면_확인을_요구한다(as_admin2, project):
    Project.objects.filter(pk=project.pk).update(metric=Project.Metric.RMSE, direction=Project.Direction.LOWER)
    project.refresh_from_db()

    _edit(as_admin2, project, metric="accuracy")                    # 방향 '낮을수록'은 그대로 둠
    project.refresh_from_db()
    assert project.metric == Project.Metric.RMSE                   # 저장 안 됨


def test_정답만_있을_때는_방향을_바꿀_수_있고_채점_대기_제출이_생기면_잠긴다(as_admin2, student, project_with_files):
    from django.utils import timezone

    from submissions.models import Submission

    p = project_with_files
    _edit(as_admin2, p, direction="lower", confirm_direction="on")
    p.refresh_from_db()
    assert p.direction == Project.Direction.LOWER                  # 제출 전: 변경 가능

    member = CompetitionMember.objects.get(user=student)
    Submission.objects.create(project=p, member=member, status=Submission.Status.QUEUED,
                              started_at=timezone.now(), last_received_at=timezone.now())
    resp = as_admin2.get(_url("project_edit", p))
    assert "채점된 제출이 있어 바꿀 수 없습니다" in resp.content.decode()

    _edit(as_admin2, p, direction="higher")                         # 요청에 값을 넣어 보내도
    p.refresh_from_db()
    assert p.direction == Project.Direction.LOWER                  # 그대로 (disabled 칸은 무시됨)


# ── 파일 등록 ──────────────────────────────────────────────────
def test_정답을_올리면_행수와_레이블이_자동_계산되고_비공개_구역에_저장된다(as_admin2, admin2, project):
    _upload(as_admin2, project, "answer", ANSWER_CSV, "my answer.csv")

    project.refresh_from_db()
    assert project.expected_rows == 5 and project.allowed_labels == ["0", "1"]
    pf = ProjectFile.objects.get(project=project, kind="answer")
    assert re.fullmatch(r"[0-9a-f]{32}\.csv", pf.stored_name)        # 저장 이름은 UUID
    assert pf.original_name == "my answer.csv"                       # 원래 이름은 DB에만
    assert _files_in("private") == [pf.stored_name] and _files_in("shared") == []
    log = AuditLog.objects.get(action=AuditLog.Action.FILE_UPLOADED)
    assert log.detail["sha256"] == hashlib.sha256(ANSWER_CSV).hexdigest()
    assert log.actor == admin2


def test_정답_컬럼이_든_테스트_feature는_저장하지_않는다(as_admin2, project):   # R9
    resp = _upload(as_admin2, project, "test_features", b"id,x,label\n1,0.1,0\n")

    assert not ProjectFile.objects.filter(project=project).exists()
    assert _files_in("shared") == []
    assert "정답 컬럼" in resp.content.decode()


def test_feature와_정답의_id가_다르면_거부한다(as_admin2, project):
    _upload(as_admin2, project, "answer", ANSWER_CSV)
    resp = _upload(as_admin2, project, "test_features", b"id,x\n1,0\n2,0\n3,0\n4,0\n")

    assert not ProjectFile.objects.filter(project=project, kind="test_features").exists()
    assert "정답에만 1개" in resp.content.decode()


def test_정답을_다시_올리면_옛_파일은_지워진다(as_admin2, project):
    _upload(as_admin2, project, "answer", ANSWER_CSV)
    old = ProjectFile.objects.get(project=project, kind="answer").stored_name
    _upload(as_admin2, project, "answer", b"id,label\n1,1\n2,1\n3,0\n4,0\n5,0\n")

    new = ProjectFile.objects.get(project=project, kind="answer").stored_name
    assert _files_in("private") == [new] and new != old
    assert AuditLog.objects.filter(action=AuditLog.Action.FILE_UPLOADED).latest("created_at").detail["replaced"]


def test_정답을_올린_뒤에는_지표와_컬럼을_바꿀_수_없다(as_admin2, project_with_files):
    p = project_with_files
    as_admin2.post(_url("project_edit", p), {
        "name": "새 이름", "metric": "rmse", "direction": "", "id_column": "no", "target_column": "y",
    })

    p.refresh_from_db()
    assert p.name == "새 이름"                                     # 이름은 바뀜
    assert (p.metric, p.id_column, p.target_column) == ("accuracy", "id", "label")   # 잠긴 칸은 그대로


# ── 다운로드 ──────────────────────────────────────────────────
def test_정답_다운로드는_기록되고_브라우저에_저장되지_않는다(client, competition, project_with_files):
    ta = User.objects.create_user("mock_ta01", real_name="가상조교01")
    CompetitionMember.objects.create(user=ta, competition=competition, role=CompetitionMember.Role.ADMIN1)
    client.force_login(ta)

    resp = client.get(_url("file_download", project_with_files, "answer"))

    assert b"".join(resp.streaming_content) == ANSWER_CSV
    assert resp["Cache-Control"] == "private, no-store"
    assert resp["Content-Disposition"].startswith("attachment")
    log = AuditLog.objects.get(action=AuditLog.Action.ANSWER_DOWNLOADED)
    assert log.actor == ta


def test_참여자_화면에는_정답_링크가_없다(client, student, project_with_files):
    client.force_login(student)
    html = client.get(_url("project_detail", project_with_files)).content.decode()

    assert _url("file_download", project_with_files, "answer") not in html
    assert _url("file_download", project_with_files, "test_features") in html


def test_제출_견본에는_정답_정보가_없다(client, student, project_with_files):
    client.force_login(student)
    resp = client.get(_url("sample_csv", project_with_files))

    rows = list(csv.reader(io.StringIO(resp.content.decode())))
    assert rows[0] == ["id", "label"]
    assert [r[0] for r in rows[1:]] == ["1", "2", "3", "4", "5"]
    assert len({r[1] for r in rows[1:]}) == 1            # 예측 칸은 모두 같은 값 → 정답을 알 수 없음


def test_없는_파일_종류는_404(as_admin2, project_with_files):
    assert as_admin2.get(_url("file_download", project_with_files, "evil")).status_code == 404


def test_다른_대회_프로젝트_번호로는_열_수_없다(as_admin2, project, other_competition):
    url = reverse("competitions:project_detail", args=[other_competition.pk, project.pk])
    assert as_admin2.get(url).status_code == 404


@pytest.mark.parametrize("bad_name", ["../../etc/passwd", "a.csv", "../private/x.csv", ""])
def test_저장_이름으로_경로를_조작할_수_없다(bad_name):
    with pytest.raises(ValueError):
        storage.path_for("private", bad_name)


# ── 학습 데이터 중복 행 ───────────────────────────────────────────
TRAIN_WITH_DUPLICATES = b"id,x,label\n1,0.1,0\n2,0.1,1\n3,0.2,0\n4,0.1,0\n"   # x=0.1이 3번 → 같은 행 2개


def test_학습_데이터를_올리면_특성이_같은_행_수를_계산해_저장한다(as_admin2, project):
    _upload(as_admin2, project, "train", TRAIN_WITH_DUPLICATES)

    pf = ProjectFile.objects.get(project=project, kind="train")
    assert pf.rows == 4 and pf.duplicate_rows == 2 and pf.duplicate_percent == 50.0


def test_학습_데이터가_아닌_파일은_계산하지_않는다(as_admin2, project):
    _upload(as_admin2, project, "answer", ANSWER_CSV)

    pf = ProjectFile.objects.get(project=project, kind="answer")
    assert pf.duplicate_rows is None and pf.duplicate_percent is None


def test_학습_데이터_중복은_관리자_화면에만_나온다(client, admin2, student, project):
    client.force_login(admin2)
    _upload(client, project, "train", TRAIN_WITH_DUPLICATES)

    assert "학습 데이터 중복" in client.get(_url("project_detail", project)).content.decode()
    client.force_login(student)
    assert "학습 데이터 중복" not in client.get(_url("project_detail", project)).content.decode()

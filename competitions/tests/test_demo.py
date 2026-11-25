"""load_demo: 예시 대회를 만들고, 여러 번 실행해도 제출이 늘지 않으며, 세 파일 중 하나라도 바뀐 사례만 다시 제출한다."""
import shutil
from itertools import combinations

import pytest
from django.conf import settings
from django.core.management import call_command

from accounts.models import User
from competitions.management.commands import load_demo as cmd
from competitions.models import Competition, CompetitionMember, Project
from submissions.models import Submission

pytestmark = pytest.mark.django_db


@pytest.fixture
def demo(db, tmp_path, monkeypatch):
    """데모 자료 사본(시험에서 파일을 고쳐도 진짜 demo/ 폴더는 그대로)."""
    dst = tmp_path / "demo"
    shutil.copytree(settings.BASE_DIR / "demo" / "data", dst / "data")
    shutil.copytree(settings.BASE_DIR / "demo" / "submissions", dst / "submissions")
    monkeypatch.setattr(cmd, "DEMO", dst)
    return dst


def _count(username=None):
    qs = Submission.objects.filter(project__name=cmd.PROJECT_NAME)
    return qs.filter(member__user__username=username).count() if username else qs.count()


def test_예시_대회와_계정을_만들고_두_번_실행해도_늘지_않는다(demo):
    call_command("load_demo")
    assert _count() == 11 and Competition.objects.filter(name=cmd.NAME).count() == 1
    project = Project.objects.get(name=cmd.PROJECT_NAME)
    assert (project.metric, project.id_column, project.target_column) == (Project.Metric.ACCURACY, "id", "label")
    prof, ta = User.objects.get(username="demo_prof"), User.objects.get(username="demo_ta")
    assert prof.is_admin2 and prof.check_password("demo_prof") and ta.check_password("demo_ta")   # 비밀번호 = 아이디
    assert CompetitionMember.objects.get(user=ta).role == CompetitionMember.Role.ADMIN1
    assert User.objects.filter(username__startswith="demo_stu").count() == 11
    call_command("load_demo")
    assert _count() == 11 and Project.objects.filter(name=cmd.PROJECT_NAME).count() == 1


def test_노트북이_같아도_CSV만_다르면_다시_제출한다(demo):
    call_command("load_demo")
    csv = demo / "submissions" / "honest_sgd" / "pred.csv"
    lines = csv.read_text().splitlines()
    i, v = lines[1].split(",")
    lines[1] = f"{i},{(int(v) + 1) % 10}"
    csv.write_text("\n".join(lines) + "\n")

    call_command("load_demo")
    assert _count("demo_stu01") == 2 and _count() == 12


def test_업로드가_끝나지_않은_제출은_이미_낸_것으로_보지_않는다(demo):
    call_command("load_demo")
    Submission.objects.filter(member__user__username="demo_stu01").update(status=Submission.Status.EXPIRED)   # 업로드 중에 끊겨 만료

    call_command("load_demo")
    assert _count("demo_stu01") == 2


def test_예측_CSV는_복사_사례_짝만_똑같다():
    """정직·실수 사례끼리 우연히 CSV가 같으면 V2(같은 CSV) 오탐이 난다 → 복사 사례와 그 원본 짝만 같아야 한다."""
    folder = settings.BASE_DIR / "demo" / "submissions"
    pred = {case: (folder / case / "pred.csv").read_bytes() for _, case in cmd.STUDENTS}
    same = {(a, b) for a, b in combinations(pred, 2) if pred[a] == pred[b]}
    assert same == {("honest_forest_features", "cheat_copied_csv")}

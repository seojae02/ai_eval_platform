"""예시 대회 불러오기. 여러 번 실행해도 된다(이미 있는 것은 건너뛴다).

    docker compose exec web python manage.py load_demo      (또는 ./dev.sh demo)

만드는 것 (모두 가상 인물, demo/ 폴더의 파일 사용 — demo/build_demo.py가 만든다)
  대회   "예시 대회 — 손글씨 숫자 분류", 과제 1개(정확도), 7일 뒤 마감, 5회
  계정   demo_prof(관리자2) · demo_ta(관리자1) · demo_stu01~11(참여자). 비밀번호는 아이디와 같다
         → 로컬 시연 전용. 서버에 올린 곳에서는 실행하지 말 것
  제출   정상 3 · 부정 7 · 방법 실수 1 (demo/README.md 표). CSV·노트북·모델 세 파일의 SHA-256이 같은 제출(업로드가
         끝난 것)이 이미 있으면 건너뛴다 → 자료를 다시 만들면 바뀐 사례만 다시 제출된다. 채점·검증은 worker가 한다
"""
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from audit.services import record
from competitions import services
from competitions.models import DEFAULT_DIRECTION, Competition, CompetitionMember, Deadline, Project
from submissions import services as submission_services
from submissions.models import Submission

DEMO = Path(settings.BASE_DIR) / "demo"
NAME = "예시 대회 — 손글씨 숫자 분류"
PROJECT_NAME = "손글씨 숫자 (10종 분류, 정확도)"
DESCRIPTION = ("8×8 손글씨 숫자 그림(픽셀 64개, 값 0~16)이 0~9 중 어떤 숫자인지 분류합니다\n"
               "cross_val_score(cv=10, scoring=\"accuracy\")의 평균으로 확인하세요\n"
               "노트북 재실행 규칙: 현재 폴더의 train.csv(학습), test.csv(테스트 feature)만 읽고, "
               "id, label 두 컬럼의 CSV(예: submission.csv)를 저장하세요. "
               "그 밖의 파일(원본 데이터, sklearn.datasets 포함)은 쓰지 마세요(인터넷은 막혀 있음)")
# (참여자 번호, 제출 사례 폴더) — 정상 01~03, 부정 04~10, 방법 실수 11
STUDENTS = [
    (1, "honest_sgd"), (2, "honest_forest_features"), (3, "honest_svc_pipeline"),
    (4, "cheat_copied_csv"), (5, "cheat_excel_edit"), (6, "cheat_hand_labels"), (7, "cheat_pasted_column"),
    (8, "cheat_original_lookup"), (9, "cheat_edited_output"), (10, "cheat_typed_mean"),
    (11, "mistake_pca"),
]
FILES = (("csv", "pred.csv"), ("notebook", "work.ipynb"), ("model", "model.pkl"))


def account(username, real_name, **extra):
    """없으면 만든다. 비밀번호 = 아이디(시연 전용)."""
    return User.objects.filter(username=username).first() or \
        User.objects.create_user(username, password=username, real_name=real_name, **extra)


def already_sent(member, project, folder):
    """세 파일의 해시가 모두 같은 제출이 이미 있나. 업로드가 끝난 제출(채점 대기·채점됨)만 본다."""
    want = {kind: hashlib.sha256((folder / name).read_bytes()).hexdigest() for kind, name in FILES}
    subs = member.submissions.filter(project=project, status__in=Submission.GRADED_OR_WAITING).prefetch_related("files")
    return any({f.kind: f.sha256 for f in sub.files.all()} == want for sub in subs)


class Command(BaseCommand):
    help = "예시 대회(가상 계정 13개, 과제 1개, 제출 11건)를 만든다. 여러 번 실행해도 된다."

    def handle(self, *args, **options):
        data = DEMO / "data" / "digits"
        if not (data / "train.csv").exists():
            raise CommandError("demo/data/digits 자료가 없습니다. demo/build_demo.py를 먼저 실행하세요(demo/README.md)")
        manifest = json.loads((DEMO / "submissions" / "manifest.json").read_text(encoding="utf-8"))

        with transaction.atomic():
            prof = account("demo_prof", "가상교수(데모)", is_admin2=True)
            competition = Competition.objects.filter(name=NAME).first()
            if competition is None:
                record(None, AuditLog.Action.ADMIN2_CREATED, target_user=prof, detail={"via": "load_demo"})
                competition = services.create_competition(NAME, prof)
                record(prof, AuditLog.Action.COMPETITION_CREATED, competition=competition, detail={"via": "load_demo"})
            ta = account("demo_ta", "가상조교(데모)")
            if not CompetitionMember.objects.filter(user=ta, competition=competition).exists():
                services.add_member(ta, competition, role=CompetitionMember.Role.ADMIN1)
            project = competition.projects.filter(name=PROJECT_NAME).first()
            if project is None:
                metric = Project.Metric.ACCURACY
                project = Project.objects.create(competition=competition, name=PROJECT_NAME, metric=metric,
                                                 direction=DEFAULT_DIRECTION[metric], id_column="id",
                                                 target_column="label", description=DESCRIPTION)
                for kind, filename in (("train", "train.csv"), ("test_features", "test_features.csv"), ("answer", "answer.csv")):
                    services.upload_project_file(project, kind, SimpleUploadedFile(filename, (data / filename).read_bytes()), prof)
                project.refresh_from_db()
                Deadline.objects.create(project=project, due_at=timezone.now() + timedelta(days=7), max_submissions=5)
            members = {}
            for no, _ in STUDENTS:
                user = account(f"demo_stu{no:02d}", f"가상학생{no:02d}(데모)")
                members[no] = (CompetitionMember.objects.filter(user=user, competition=competition).first()
                               or services.add_member(user, competition))

        self.stdout.write(f"예시 대회: {NAME} (참여 코드 {competition.display_code()})")
        for no, case in STUDENTS:
            folder = DEMO / "submissions" / case
            if already_sent(members[no], project, folder):
                self.stdout.write(f"  건너뜀(이미 제출): demo_stu{no:02d} · {case}")
                continue
            self._submit(members[no], project, folder)
            self.stdout.write(f"  제출함: demo_stu{no:02d} · {case} — {manifest[case]}")
        self.stdout.write("계정: demo_prof(관리자2) · demo_ta(관리자1) · demo_stu01~11(참여자), 비밀번호는 아이디와 같음")
        self.stdout.write("worker가 채점·검증을 마칠 때까지 1~2분 기다린 뒤 demo_ta로 로그인해 '검토'를 여세요.")

    def _submit(self, member, project, folder):
        """브라우저와 같은 순서(시작 → 조각 → 완료)로 제출한다."""
        sub = submission_services.start_submission(member, project, comment=f"데모 제출: {folder.name}")
        for kind, name in FILES:
            data = (folder / name).read_bytes()
            for offset in range(0, len(data), submission_services.MAX_CHUNK):
                submission_services.receive_chunk(sub, kind, offset, name, data[offset:offset + submission_services.MAX_CHUNK])
        sub = submission_services.complete(sub)
        if sub.status != sub.Status.QUEUED:
            raise CommandError(f"{folder.name} 제출이 '{sub.get_status_display()}'가 되었습니다: {sub.error_message}")

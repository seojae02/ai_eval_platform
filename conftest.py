"""여러 테스트 파일이 함께 쓰는 가상 데이터(fixture)."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import User
from competitions import services
from competitions.models import Competition, CompetitionMember, Project

# 가상 정답·테스트 feature (5행, 이진 분류)
ANSWER_CSV = b"id,label\n1,0\n2,1\n3,1\n4,0\n5,1\n"
FEATURES_CSV = b"id,x1,x2\n1,0.1,0.2\n2,0.3,0.4\n3,0.5,0.6\n4,0.7,0.8\n5,0.9,1.0\n"


@pytest.fixture(autouse=True)
def _isolated_storage(settings, tmp_path):
    """모든 테스트는 매번 새 임시 폴더를 저장소로 쓴다(실제 storage/ 폴더를 건드리지 않음)."""
    settings.STORAGE_ROOT = tmp_path / "storage"


@pytest.fixture
def pw():
    """규칙(10자 이상 등)을 통과하는 가상 비밀번호."""
    return "가상비밀번호-2026-01"


@pytest.fixture
def admin2(db, pw):
    return User.objects.create_user("mock_prof01", password=pw, real_name="가상교수01", is_admin2=True)


@pytest.fixture
def competition(admin2):
    return Competition.objects.create(name="가상대회-이미지분류", created_by=admin2)


@pytest.fixture
def other_competition(admin2):
    return Competition.objects.create(name="가상대회-수요예측", created_by=admin2)


@pytest.fixture
def student(competition, pw):
    user = User.objects.create_user("mock_stu01", password=pw, real_name="가상학생01")
    CompetitionMember.objects.create(user=user, competition=competition)
    return user


@pytest.fixture
def signup_data(competition, pw):
    return {
        "username": "mock_stu01",
        "real_name": "가상학생01",
        "student_id": "99990001",     # 가상 학번(실제 학번 형식과 겹치지 않게 9999로 시작)
        "password1": pw,
        "password2": pw,
        "join_code": competition.display_code(),
    }


@pytest.fixture
def project(competition):
    return Project.objects.create(
        competition=competition, name="가상과제-분류",
        metric=Project.Metric.ACCURACY, direction=Project.Direction.HIGHER,
    )


@pytest.fixture
def project_with_files(project, admin2):
    """정답과 공개 테스트 feature가 등록된 프로젝트."""
    services.upload_project_file(project, "answer", SimpleUploadedFile("answer.csv", ANSWER_CSV), admin2)
    services.upload_project_file(project, "test_features", SimpleUploadedFile("test.csv", FEATURES_CSV), admin2)
    project.refresh_from_db()
    return project

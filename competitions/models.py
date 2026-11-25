import secrets

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

# 헷갈리는 글자(0/O, 1/I/L)를 뺀 31자. 8자리면 31^8 ≈ 8,500억 가지.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 8
ALIAS_LENGTH = 4


def _random_string(length):
    # random 모듈이 아니라 secrets: 예측할 수 없는 난수(암호용)를 쓴다.
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def generate_join_code():
    return _random_string(JOIN_CODE_LENGTH)


def generate_alias():
    return "참여자 " + _random_string(ALIAS_LENGTH)


def normalize_join_code(raw):
    """사용자 입력 'k7qm-3xpa ' → 'K7QM3XPA'. 저장은 하이픈 없이 한다."""
    return raw.strip().upper().replace("-", "").replace(" ", "")


class Competition(models.Model):
    name = models.CharField("대회명", max_length=100)
    # unique: 같은 코드가 두 대회에 생기는 것을 DB가 막는다.
    join_code = models.CharField(
        "참여 코드", max_length=JOIN_CODE_LENGTH, unique=True, default=generate_join_code
    )
    # PROTECT: 대회가 남아 있는 한 개설자 계정을 지울 수 없다(기록 보존).
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_competitions",
        verbose_name="개설자",
    )
    created_at = models.DateTimeField("개설 시각", auto_now_add=True)

    def display_code(self):
        """화면 표시용 'K7QM-3XPA'."""
        return f"{self.join_code[:4]}-{self.join_code[4:]}"

    def __str__(self):
        return self.name


class CompetitionMember(models.Model):
    """'누가 어느 대회에서 무엇인가' 한 줄. 대회별 권한 검사는 이 테이블만 본다."""

    class Role(models.TextChoices):
        PARTICIPANT = "participant", "참여자"
        ADMIN1 = "admin1", "관리자1"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="memberships",
        verbose_name="사용자",
    )
    competition = models.ForeignKey(
        Competition,
        on_delete=models.PROTECT,
        related_name="members",
        verbose_name="대회",
    )
    role = models.CharField("역할", max_length=20, choices=Role.choices, default=Role.PARTICIPANT)
    # 리더보드 익명 표시용. 사용자 번호를 쓰면 가입 순서가 드러나므로 무작위로 만든다.
    alias = models.CharField("익명 별칭", max_length=20, default=generate_alias)
    # 관리자1을 해제할 때 줄을 지우거나 참여자로 되돌리지 않고 '비활성'으로만 만든다.
    # 정답을 본 사람이 같은 대회에 참여자로 다시 들어오는 것을 막기 위해서다(5-3).
    is_active = models.BooleanField("활성", default=True)
    joined_at = models.DateTimeField("참여 시각", auto_now_add=True)

    class Meta:
        constraints = [
            # 한 사람은 한 대회에 한 줄만 → 같은 대회에서 참여자 겸 관리자1 불가(D5)
            models.UniqueConstraint(fields=["user", "competition"], name="uniq_member_per_competition"),
            # 같은 대회 안에서 별칭 중복 불가
            models.UniqueConstraint(fields=["competition", "alias"], name="uniq_alias_per_competition"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.competition} ({self.get_role_display()})"


class Project(models.Model):
    """대회 안의 과제 하나. 채점 규칙(지표·방향·CSV 형식)을 가진다 [RFP 4·5쪽]."""

    class Metric(models.TextChoices):
        ACCURACY = "accuracy", "Accuracy (정확도)"
        MACRO_F1 = "macro_f1", "Macro-F1"
        RMSE = "rmse", "RMSE"
        MAE = "mae", "MAE"

    class Direction(models.TextChoices):
        HIGHER = "higher", "높을수록 좋음"
        LOWER = "lower", "낮을수록 좋음"

    competition = models.ForeignKey(
        Competition, on_delete=models.PROTECT, related_name="projects", verbose_name="대회"
    )
    name = models.CharField("프로젝트명", max_length=100)
    description = models.TextField("설명", blank=True)
    metric = models.CharField("평가지표", max_length=20, choices=Metric.choices)
    direction = models.CharField("점수 방향", max_length=10, choices=Direction.choices)
    id_column = models.CharField("id 컬럼 이름", max_length=50, default="id")
    target_column = models.CharField("정답 컬럼 이름", max_length=50, default="label")

    # 아래 두 칸은 사람이 입력하지 않는다. 정답 파일을 올리면 자동으로 채워진다(오타 방지).
    expected_rows = models.PositiveIntegerField("행 수", null=True, blank=True)
    allowed_labels = models.JSONField("허용 레이블", default=list, blank=True)

    created_at = models.DateTimeField("만든 시각", auto_now_add=True)

    @property
    def is_classification(self):
        return self.metric in CLASSIFICATION_METRICS

    def __str__(self):
        return self.name


# 분류 지표인지(레이블 목록이 필요) 회귀 지표인지(숫자) 구분
CLASSIFICATION_METRICS = {Project.Metric.ACCURACY, Project.Metric.MACRO_F1}

# 지표를 고르면 점수 방향을 이 값으로 자동으로 채운다
DEFAULT_DIRECTION = {
    Project.Metric.ACCURACY: Project.Direction.HIGHER,
    Project.Metric.MACRO_F1: Project.Direction.HIGHER,
    Project.Metric.RMSE: Project.Direction.LOWER,
    Project.Metric.MAE: Project.Direction.LOWER,
}


class ProjectFile(models.Model):
    """프로젝트에 등록된 CSV 파일 정보. 파일 자체는 storage 폴더에 UUID 이름으로 있다."""

    class Kind(models.TextChoices):
        TRAIN = "train", "공개 학습 데이터"
        TEST_FEATURES = "test_features", "공개 테스트 feature"
        ANSWER = "answer", "정답 (비공개)"

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="files")
    kind = models.CharField("종류", max_length=20, choices=Kind.choices)
    stored_name = models.CharField("저장 이름(UUID)", max_length=40)
    original_name = models.CharField("원래 파일명", max_length=100)
    size = models.PositiveIntegerField("크기(바이트)")
    sha256 = models.CharField("SHA-256", max_length=64)
    rows = models.PositiveIntegerField("데이터 행 수")
    # 공개 학습 데이터만 올릴 때 계산한다(나머지 종류는 비어 있음). 이전에 올린 파일도 비어 있다.
    duplicate_rows = models.PositiveIntegerField("특성이 같은 행 수", null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField("올린 시각", auto_now_add=True)

    class Meta:
        constraints = [
            # 프로젝트마다 종류별로 파일 하나 (다시 올리면 교체)
            models.UniqueConstraint(fields=["project", "kind"], name="uniq_file_kind_per_project"),
        ]

    @property
    def area(self):
        """저장 구역: 정답은 private, 나머지는 shared (둘 다 웹 주소로 직접 열 수 없음)."""
        return "private" if self.kind == self.Kind.ANSWER else "shared"

    @property
    def duplicate_percent(self):
        """특성이 같은 행의 비율(%). 계산하지 않았거나 행이 없으면 None."""
        if self.duplicate_rows is None or not self.rows:
            return None
        return round(100 * self.duplicate_rows / self.rows, 1)


class Deadline(models.Model):
    """프로젝트의 마감일 하나와 그 구간의 제출 횟수 제한 [RFP 5쪽]."""

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="deadlines")
    due_at = models.DateTimeField("마감 시각")
    max_submissions = models.PositiveIntegerField("제출 횟수 제한", validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["due_at"]
        constraints = [
            models.UniqueConstraint(fields=["project", "due_at"], name="uniq_deadline_per_project"),
            models.CheckConstraint(condition=models.Q(max_submissions__gte=1), name="deadline_max_submissions_gte_1"),
        ]

    def __str__(self):
        return f"{self.project} 마감 {self.due_at:%Y-%m-%d %H:%M}"

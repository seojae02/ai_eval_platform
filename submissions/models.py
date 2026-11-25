"""제출 한 건 = Submission 한 줄. '업로드 중'으로 태어나 상태가 바뀌어 간다.

업로드 세션을 따로 두지 않고 이 한 테이블로 합쳤다(결정 1).
→ 제출 횟수는 "상태가 COUNTED 중 하나인 줄의 개수" 쿼리 하나로 센다. 횟수를 숫자 칸에 저장하지 않는다.

상태 흐름 (가상 예시: 가상참여자01, 1차 마감 17:00)
    16:58 제출 시작 → 업로드 중 ─┬─ 파일 3개 검사 통과 → 채점 대기 → (worker) 채점됨 / 시스템 오류
                                 ├─ 검사 실패         → 형식 오류
                                 ├─ 10분 무수신       → 만료
                                 ├─ 참여자가 취소     → 취소
                                 └─ (마감 후 시작) 통과 → 마감 후 접수
"""
from decimal import ROUND_HALF_UP, Decimal
from pathlib import PurePath

from django.conf import settings
from django.db import models

from competitions.models import CompetitionMember, Deadline, Project

SCORE_DIGITS = 4                              # 화면 표시 자릿수 = 리더보드 동점 판단 자릿수 (A안, 2026-11-21)
_QUANTUM = Decimal(1).scaleb(-SCORE_DIGITS)   # 0.0001


def display_score(score):
    """점수 → 화면 문자열 '0.9123'. 리더보드 동점 판단에도 이 값을 쓴다 → 보이는 숫자가 같으면 순위도 같다.

    Python round()는 이진수 근삿값 때문에 round(0.90025, 4)가 0.9002가 된다(0.90025는 실제로 0.900249999...로 저장됨).
    그래서 사람이 읽는 십진수(repr)를 Decimal로 바꿔 사사오입(ROUND_HALF_UP)한다 → 0.9003
    """
    return str(Decimal(repr(score)).quantize(_QUANTUM, rounding=ROUND_HALF_UP))


class Submission(models.Model):
    class Status(models.TextChoices):
        UPLOADING = "uploading", "업로드 중"
        EXPIRED = "expired", "만료"
        CANCELLED = "cancelled", "취소"
        INVALID = "invalid", "형식 오류"
        LATE = "late", "마감 후 접수"
        QUEUED = "queued", "채점 대기"
        SCORED = "scored", "채점됨"
        ERROR = "error", "시스템 오류"

    # 제출 횟수에 들어가는 상태 (R3·R4·R6). 업로드 중 = '잡아 둔' 1회
    COUNTED = (Status.UPLOADING, Status.QUEUED, Status.SCORED)
    # 정답 교체를 막는 상태 (채점 기준이 바뀌면 안 됨)
    GRADED_OR_WAITING = (Status.QUEUED, Status.SCORED)

    class ReviewStatus(models.TextChoices):
        """검토 상태 (R10~R12). 위의 제출 상태(Status)와 따로 간다 — 검토는 '채점됨'인 제출에만 한다."""
        PENDING = "pending", "검증 대기"
        PROPOSED_OK = "proposed_ok", "적합 제안"
        PROPOSED_REJECT = "proposed_reject", "부적합 제안"
        APPROVED = "approved", "적합(확정)"
        REJECTED = "rejected", "부적합(확정)"

    PROPOSED = (ReviewStatus.PROPOSED_OK, ReviewStatus.PROPOSED_REJECT)
    DECIDED = (ReviewStatus.APPROVED, ReviewStatus.REJECTED)
    # 참여자 화면용 표시. 확정 전의 '부적합 제안'을 다른 학생에게 알리지 않는다(결정 2, 2026-11-21)
    PUBLIC_REVIEW_LABEL = {
        ReviewStatus.PENDING: "검증 대기",
        ReviewStatus.PROPOSED_OK: "검토 중",
        ReviewStatus.PROPOSED_REJECT: "검토 중",
        ReviewStatus.APPROVED: "검증 완료",
        ReviewStatus.REJECTED: "부적합",
    }

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="submissions")
    # 사용자 대신 '대회 소속 줄'을 가리킨다: 익명 별칭(리더보드)과 행 잠금(R6)의 대상이 이 줄이다
    member = models.ForeignKey(CompetitionMember, on_delete=models.PROTECT, related_name="submissions")
    # PROTECT: 제출이 걸린 마감일은 지울 수 없다. 비어 있으면 '최종 마감 후 시작'(R2)
    deadline = models.ForeignKey(
        Deadline, on_delete=models.PROTECT, null=True, blank=True, related_name="submissions",
    )
    status = models.CharField("상태", max_length=20, choices=Status.choices, default=Status.UPLOADING)

    started_at = models.DateTimeField("시작 시각")              # 서버가 기록 (R1의 기준)
    last_received_at = models.DateTimeField("마지막 수신 시각")  # 10분 무수신 판정 (D3)
    completed_at = models.DateTimeField("수신 완료 시각", null=True, blank=True)

    comment = models.TextField("코멘트", blank=True, max_length=1000)
    error_message = models.CharField("오류 내용", max_length=300, blank=True)
    score = models.FloatField("점수", null=True, blank=True)   # worker가 채운다 (scoring.py)
    scored_at = models.DateTimeField("채점 시각", null=True, blank=True)
    # 지금 검토 상태. 바꾸는 길은 submissions/review.py 하나뿐이고, 바꿀 때마다 Review 한 줄이 함께 생긴다
    review_status = models.CharField(
        "검토 상태", max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PENDING,
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["member", "deadline", "status"])]   # 횟수 세기 쿼리용

    @property
    def score_text(self):
        return "-" if self.score is None else display_score(self.score)

    @property
    def public_review_label(self):
        return self.PUBLIC_REVIEW_LABEL[self.review_status]

    @property
    def is_counted(self):
        return self.status in self.COUNTED

    def __str__(self):
        return f"제출 #{self.pk} ({self.get_status_display()})"


class SubmissionFile(models.Model):
    class Kind(models.TextChoices):
        CSV = "csv", "예측 CSV"
        NOTEBOOK = "notebook", "노트북(.ipynb)"
        MODEL = "model", "모델 파일"

    # 저장 확장자. 모델은 원래 확장자(.pkl 등)를 쓰지 않는다 → 실수로라도 '열기'로 실행되지 않게
    EXT = {Kind.CSV: "csv", Kind.NOTEBOOK: "ipynb", Kind.MODEL: "bin"}

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="files")
    kind = models.CharField("종류", max_length=20, choices=Kind.choices)
    stored_name = models.CharField("저장 이름(UUID)", max_length=40)
    original_name = models.CharField("원래 파일명", max_length=100)
    received = models.PositiveBigIntegerField("받은 바이트", default=0)
    sha256 = models.CharField("SHA-256", max_length=64, blank=True)   # 수신 완료 후 계산
    # 형식 오류·만료·취소된 제출의 파일은 하루 뒤 디스크에서 지운다(worker 정리 작업). 기록(DB 줄)은 남긴다
    purged_at = models.DateTimeField("파일 삭제 시각", null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "kind"], name="uniq_file_kind_per_submission"),
        ]

    # 내려받을 때 이름에 붙는 종류 표시. 가상 예시: 별칭 '참여자 58AS'의 CSV → 58AS_CSV.csv
    DOWNLOAD_LABEL = {Kind.CSV: "CSV", Kind.NOTEBOOK: "NOTEBOOK", Kind.MODEL: "MODEL"}

    @property
    def download_name(self):
        """관리자가 내려받을 때의 파일 이름: 익명 별칭 코드 + 종류. 서버 저장 이름(UUID)은 그대로다.

        원래 파일명을 쓰지 않는다 → 학생이 파일명에 실명을 적었어도 내려받은 파일에 남지 않고, 별칭으로 바로 찾는다.
        모델은 원래 확장자(.pkl, .pt 등, 제출 때 검사한 목록 안)를 붙여 어떤 형식인지 알 수 있게 한다.
        """
        code = self.submission.member.alias.rsplit(" ", 1)[-1]   # '참여자 58AS' → '58AS'
        ext = "." + self.EXT[self.kind] if self.kind != self.Kind.MODEL else PurePath(self.original_name).suffix.lower()
        return f"{code}_{self.DOWNLOAD_LABEL[self.kind]}{ext}"


class Review(models.Model):
    """검토 이력 한 줄 = 누가 언제 무엇을 했나 (R10~R12). 감사 로그처럼 추가만 할 수 있다.

    가상 예시: 가상조교01 '부적합 제안'(사유: CSV가 노트북 출력과 다름) → 가상교수01 '결정: 부적합'
    """

    class Action(models.TextChoices):
        PROPOSE = "propose", "제안"            # 관리자1
        DECIDE = "decide", "결정"              # 관리자2 (제안 승인, 또는 제안 없이 직접)
        SEND_BACK = "send_back", "반려"        # 관리자2: 제안을 받아들이지 않고 검증 대기로
        REOPEN = "reopen", "재검토"            # 관리자2: 확정을 풀고 검증 대기로

    class Verdict(models.TextChoices):
        OK = "ok", "적합"
        REJECT = "reject", "부적합"

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="reviews")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    action = models.CharField("동작", max_length=20, choices=Action.choices)
    verdict = models.CharField("판단", max_length=10, choices=Verdict.choices, blank=True)
    direct = models.BooleanField("제안 없이 직접 결정", default=False)
    reason = models.TextField("사유", blank=True, max_length=1000)
    from_status = models.CharField("이전 상태", max_length=20, choices=Submission.ReviewStatus.choices)
    to_status = models.CharField("바뀐 상태", max_length=20, choices=Submission.ReviewStatus.choices)
    created_at = models.DateTimeField("시각", auto_now_add=True)

    class Meta:
        ordering = ["pk"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("검토 이력은 수정할 수 없습니다.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("검토 이력은 삭제할 수 없습니다.")

"""검증 리포트: 제출 하나당 한 장. worker의 검증 작업이 만든다(채점이 끝난 뒤 자동)."""
from django.db import models

from submissions.models import Submission


class VerificationReport(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "검증 중"
        DONE = "done", "완료"
        FAILED = "failed", "검증 실패"

    class Level(models.TextChoices):
        OK = "ok", "문제 없음"
        UNKNOWN = "unknown", "판정 불가"
        WARN = "warn", "주의"
        BAD = "bad", "의심"

    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="verification")
    status = models.CharField("상태", max_length=20, choices=Status.choices, default=Status.RUNNING)
    level = models.CharField("전체 판정", max_length=10, choices=Level.choices, default=Level.UNKNOWN)
    # {"v1": {"level", "summary", "details"}, ..., "v5": {...}}  (verification/checks.py 형식)
    checks = models.JSONField("검사 결과", default=dict)
    started_at = models.DateTimeField("시작 시각")
    finished_at = models.DateTimeField("끝난 시각", null=True, blank=True)

    class Stage(models.TextChoices):
        """검증 진행 단계(진행 화면용). 검사 내용과 무관하게 '지금 어디까지 왔나'만 기록한다."""
        STATIC = "static", "파일·코드 검사 (V2·V3·V4)"
        MODEL = "model", "모델 재예측 (V5)"
        NOTEBOOK = "notebook", "노트북 재실행 (V1)"
        SAVING = "saving", "리포트 저장"

    stage = models.CharField("진행 단계", max_length=20, choices=Stage.choices, blank=True)
    # {"static": {"start": ISO 시각, "end": ISO 시각}, "model": {...}, ...}  단계별 걸린 시간 표시용
    timeline = models.JSONField("단계별 시각", default=dict, blank=True)

    TITLES = {
        "v1": "V1 노트북 재실행 일치율 (핵심)",
        "v2": "V2 같은 파일",
        "v3": "V3 다른 참여자와 오답 일치율 (참고, 전체 판정에 넣지 않음)",
        "v4": "V4 노트북 정적 검사",
        "v5": "V5 모델 파일",
        "v6": "V6 제출 CSV 수정 흔적",
        "v7": "V7 학습·평가 방법 (안내, 전체 판정에 넣지 않음)",
    }

    def items(self):
        """화면용: [(제목, 검사 결과)] 순서대로."""
        return [(title, self.checks[key]) for key, title in self.TITLES.items() if key in self.checks]

    def __str__(self):
        return f"제출 #{self.submission_id} 검증 ({self.get_level_display()})"

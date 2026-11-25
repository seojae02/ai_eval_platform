"""작업 큐 = PostgreSQL 테이블 한 개 (D2: Celery·Redis를 쓰지 않는다).

web은 줄을 추가만 하고, worker가 줄을 하나씩 가져가 처리한다.
worker가 죽어도 작업은 이 테이블에 그대로 남아 있다가 다시 처리된다(장애 대책).

상태 흐름 (가상 예시: 가상참여자01의 제출 #12)
    대기 ──(worker가 가져감)──▶ 실행 중 ─┬─ 성공 ─────────────▶ 완료
     ▲                                  ├─ 실패, 시도 < 3회 ──▶ 대기 (잠시 뒤 다시)
     └──── 실행 중인데 15분 넘게 소식 없음 ┘└─ 실패, 시도 = 3회 ──▶ 실패 (제출은 '시스템 오류')
          (worker가 죽은 것으로 보고 되돌림)
"""
from django.db import models

from submissions.models import Submission


class Job(models.Model):
    class Kind(models.TextChoices):
        SCORE = "score", "채점"
        VERIFY = "verify", "검증"   # 채점이 성공하면 자동으로 이어서 등록된다(V1~V5)

    class Status(models.TextChoices):
        PENDING = "pending", "대기"
        RUNNING = "running", "실행 중"
        DONE = "done", "완료"
        FAILED = "failed", "실패"

    kind = models.CharField("종류", max_length=20, choices=Kind.choices)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="jobs")
    status = models.CharField("상태", max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField("시도 횟수", default=0)
    run_after = models.DateTimeField("이 시각 이후 실행")   # 재시도할 때 바로 다시 하지 않고 조금 기다린다
    locked_at = models.DateTimeField("가져간 시각", null=True, blank=True)
    finished_at = models.DateTimeField("끝난 시각", null=True, blank=True)
    # 서버 관리자용 오류 내용. 참여자 화면에는 보여 주지 않는다(경로·내부 정보 노출 방지)
    last_error = models.TextField("마지막 오류", blank=True)
    created_at = models.DateTimeField("만든 시각", auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # 한 제출에 같은 종류 작업은 하나 → '채점'이 두 번 등록되는 일을 DB가 막는다
            models.UniqueConstraint(fields=["submission", "kind"], name="uniq_job_kind_per_submission"),
        ]
        indexes = [models.Index(fields=["status", "run_after"])]   # worker가 '대기 중이고 때가 된 작업'을 찾는 쿼리용

    def __str__(self):
        return f"작업 #{self.pk} {self.get_kind_display()} ({self.get_status_display()})"

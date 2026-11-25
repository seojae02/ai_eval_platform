from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """중요 변경 기록 [RFP 7쪽 "중요 변경 사항 기록"].

    추가만 할 수 있고 고치거나 지울 수 없다(아래 save/delete).
    기록하지 않는 것: 비밀번호, 참여 코드 원문, 정답 값.
    """

    class Action(models.TextChoices):
        ADMIN2_CREATED = "admin2_created", "관리자2 생성"
        COMPETITION_CREATED = "competition_created", "대회 개설"
        JOIN_CODE_REISSUED = "join_code_reissued", "참여 코드 재발급"
        ADMIN1_PROMOTED = "admin1_promoted", "관리자1 지정"
        ADMIN1_REVOKED = "admin1_revoked", "관리자1 해제"
        PROJECT_CREATED = "project_created", "프로젝트 생성"
        PROJECT_UPDATED = "project_updated", "프로젝트 설정 변경"
        DEADLINE_ADDED = "deadline_added", "마감일 추가"
        DEADLINE_DELETED = "deadline_deleted", "마감일 삭제"
        FILE_UPLOADED = "file_uploaded", "파일 등록"
        ANSWER_DOWNLOADED = "answer_downloaded", "정답 파일 다운로드"
        REVIEW_CHANGED = "review_changed", "검토 상태 변경"
        SUBMISSION_FILE_DOWNLOADED = "submission_file_downloaded", "제출 파일 다운로드"
        RESULTS_EXPORTED = "results_exported", "최종 성적 다운로드"
        PASSWORD_RESET = "password_reset", "임시 비밀번호 발급"
        LOGIN_UNLOCKED = "login_unlocked", "로그인 잠금 해제"
        MEMBER_SUSPENDED = "member_suspended", "참가 정지"
        MEMBER_REINSTATED = "member_reinstated", "참가 정지 해제"

    # 누가: 비어 있으면 서버 명령(manage.py)으로 한 일
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+", verbose_name="수행자",
    )
    action = models.CharField("동작", max_length=40, choices=Action.choices)
    competition = models.ForeignKey(
        "competitions.Competition", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+", verbose_name="대회",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+", verbose_name="대상 사용자",
    )
    detail = models.JSONField("상세", default=dict, blank=True)
    created_at = models.DateTimeField("시각", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        # 처음 저장(추가)만 허용. 이미 저장된 기록을 고치려 하면 막는다.
        if not self._state.adding:
            raise ValueError("감사 로그는 수정할 수 없습니다.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("감사 로그는 삭제할 수 없습니다.")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.get_action_display()}"

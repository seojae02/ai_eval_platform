"""worker가 1분마다 하는 정리 작업. 각 함수는 바뀐 줄 수를 돌려준다."""
from datetime import timedelta

from django.utils import timezone

from accounts.models import FailedAttempt
from competitions import storage
from submissions.models import Submission, SubmissionFile
from submissions.services import expire_idle

from . import queue

# 로그인·참여 코드 실패 기록 보관 기간. 잠금 판정은 최근 10분만 보므로 하루면 충분하다(개인정보 최소 보관)
FAILED_ATTEMPT_KEEP = timedelta(days=1)


def purge_failed_attempts(now):
    """하루 지난 실패 기록(IP·로그인ID)을 지운다. IP·아이디를 오래 쌓아 두지 않는다."""
    deleted, _ = FailedAttempt.objects.filter(created_at__lt=now - FAILED_ATTEMPT_KEEP).delete()
    return deleted


# 형식 오류·만료·취소된 제출의 파일 보관 기간. 채점 대상이 아니므로 하루 뒤 디스크에서 지운다(DB 기록은 남김)
DEAD_FILE_KEEP = timedelta(days=1)
DEAD_STATUSES = (Submission.Status.INVALID, Submission.Status.EXPIRED, Submission.Status.CANCELLED)


def purge_dead_files(now):
    """버려진 업로드가 디스크를 채우지 않게 한다.

    마감 후 접수(LATE)·채점된 제출의 파일은 지우지 않는다(검토·검증에 필요).
    """
    count = 0
    for f in SubmissionFile.objects.filter(
        purged_at__isnull=True, submission__status__in=DEAD_STATUSES,
        submission__last_received_at__lt=now - DEAD_FILE_KEEP,
    ):
        storage.delete("submissions", f.stored_name)
        f.purged_at = now
        f.save(update_fields=["purged_at"])
        count += 1
    return count


def run_all(now=None):
    now = now or timezone.now()
    return {
        "멈춘 작업 복구": queue.recover_stale(now),
        "작업 없는 채점 대기": queue.enqueue_orphans(now),
        # 지금까지는 누가 횟수를 셀 때만 만료 판정을 했다. 아무도 안 보는 동안에도 '업로드 중'이 정리되게 한다
        "업로드 만료": expire_idle(now),
        "실패 기록 삭제": purge_failed_attempts(now),
        "버려진 제출 파일 삭제": purge_dead_files(now),
    }

"""제출의 세 단계: ① 시작(start_submission) ② 조각 수신(receive_chunk) ③ 완료·검사(complete).

뷰는 권한 검사와 화면만 맡고, 규칙(R1~R6, R15, D3)은 전부 이 파일에 있다.
모든 함수는 now(현재 시각)를 인자로 받을 수 있다 → 테스트가 '16:59:59에 시작' 같은 상황을 그대로 만든다.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from competitions import csvcheck, storage
from competitions import services as competition_services
from competitions.csvcheck import CsvError
from competitions.deadlines import deadline_for
from competitions.models import CompetitionMember, ProjectFile

from jobs import queue as job_queue

from . import filecheck
from .models import Submission, SubmissionFile

AREA = "submissions"
MAX_CHUNK = 1024 * 1024   # 조각 하나의 최대 크기(1MB). 브라우저 JS도 이 크기로 자른다.
Status = Submission.Status
Kind = SubmissionFile.Kind


class SubmissionRefused(Exception):
    """사용자에게 그대로 보여 줄 수 있는 거부 사유."""


# ── 횟수와 만료 ────────────────────────────────────────────────
def idle_timeout():
    return timedelta(seconds=settings.SUBMISSION_IDLE_SECONDS)


def expire_idle(now, **filters):
    """D3: '업로드 중'인데 마지막 수신이 10분 넘게 지난 제출을 '만료'로 바꾼다.

    타이머를 따로 돌리지 않고, 횟수를 세거나 조각을 받기 직전에 이 UPDATE 한 번으로 판정한다.
    예) 16:40 시작, 16:45 이후 무수신 → 16:55:01 이후 누가 세는 순간 만료 → 횟수에서 빠짐
    """
    return Submission.objects.filter(
        status=Status.UPLOADING, last_received_at__lt=now - idle_timeout(), **filters,
    ).update(status=Status.EXPIRED, error_message="10분 동안 파일을 받지 못해 만료되었습니다.")


def count_used(member, deadline):
    """R5: 이 구간에서 쓴 횟수 = 상태가 [업로드 중, 채점 대기, 채점됨]인 제출 수.

    횟수를 숫자 칸에 저장하지 않고 매번 센다(엑셀의 COUNTIFS 수식처럼) → 값이 실제와 어긋날 수 없다.
    """
    return Submission.objects.filter(member=member, deadline=deadline, status__in=Submission.COUNTED).count()


def usage(member, project, now=None):
    """화면 표시용: 지금 구간의 사용/제한/남은 횟수. 모든 마감이 지났으면 None."""
    now = now or timezone.now()
    expire_idle(now, member=member)
    deadline = deadline_for(project, now)
    if deadline is None:
        return None
    used = count_used(member, deadline)
    return {"deadline": deadline, "used": used, "limit": deadline.max_submissions,
            "remaining": max(0, deadline.max_submissions - used)}


# ── ① 시작 ─────────────────────────────────────────────────────
def start_submission(member, project, comment="", now=None):
    """[제출 시작] 버튼. 시작 시각을 서버가 기록하고 1회를 잡아 둔다.

    R1: 소속 구간은 '지금(시작 시각)'으로 정하고, 이후 수신이 마감을 넘겨도 바꾸지 않는다.
    R2: 최종 마감 후 시작이면 deadline=None → 횟수 확인 없이 받기만 한다(완료 시 '마감 후 접수').
    R6: 같은 참여자의 동시 요청은 행 잠금으로 한 줄로 세운다.
    """
    now = now or timezone.now()
    if member.role != CompetitionMember.Role.PARTICIPANT or not member.is_active:
        raise SubmissionRefused("참여자만 제출할 수 있습니다.")
    if not project.files.filter(kind=ProjectFile.Kind.ANSWER).exists():
        raise SubmissionRefused("아직 제출을 받지 않는 프로젝트입니다(채점 기준 미등록).")
    deadline = deadline_for(project, now)

    with transaction.atomic():
        # R6 핵심 한 줄: 이 참여자의 '대회 소속 줄'을 잠근다(SELECT ... FOR UPDATE).
        # 같은 사람이 탭 두 개로 동시에 누르면, 두 번째 요청은 첫 번째가 끝날(커밋) 때까지 여기서 기다린다.
        # 다른 참여자는 다른 줄을 잠그므로 서로 기다리지 않는다.
        CompetitionMember.objects.select_for_update().get(pk=member.pk)

        expire_idle(now, member=member)
        if Submission.objects.filter(member=member, project=project, status=Status.UPLOADING).exists():
            raise SubmissionRefused("이미 업로드 중인 제출이 있으니, 그 제출을 마치거나 10분 동안 전송이 없어 만료된 뒤 다시 시작하세요")
        if deadline is not None:
            used = count_used(member, deadline)
            if used >= deadline.max_submissions:
                raise SubmissionRefused(
                    f"이번 마감({timezone.localtime(deadline.due_at):%m/%d %H:%M}) 구간의 "
                    f"제출 횟수 {deadline.max_submissions}회를 모두 사용했습니다"
                )
        return Submission.objects.create(
            project=project, member=member, deadline=deadline,
            started_at=now, last_received_at=now, comment=comment,
        )


# ── ② 조각 수신 ───────────────────────────────────────────────
def _fail(submission, message):
    submission.status = Status.INVALID
    submission.error_message = message[:300]
    submission.save(update_fields=["status", "error_message"])


def receive_chunk(submission, kind, offset, name, data, now=None):
    """파일 한 조각(최대 1MB)을 offset 위치에 쓴다. 지금까지 받은 바이트 수를 돌려준다.

    조각마다 '마지막 수신 시각'을 갱신한다 → 느려도 계속 받고 있으면 만료되지 않는다(D3).
    """
    now = now or timezone.now()
    if kind not in Kind.values:
        raise SubmissionRefused("알 수 없는 파일 종류입니다.")
    if not 0 < len(data) <= MAX_CHUNK:
        raise SubmissionRefused("조각 크기가 올바르지 않습니다.")

    expire_idle(now, pk=submission.pk)
    error = None
    with transaction.atomic():
        # 같은 제출에 조각이 동시에 들어와도 순서대로 처리되게 제출 줄을 잠근다
        sub = Submission.objects.select_for_update().get(pk=submission.pk)
        if sub.status != Status.UPLOADING:
            raise SubmissionRefused(f"이 제출은 '{sub.get_status_display()}' 상태라 파일을 더 받을 수 없습니다.")

        f = sub.files.filter(kind=kind).first()
        if f is None:
            if offset != 0:
                raise SubmissionRefused("파일의 첫 조각부터 보내야 합니다.")
            f = SubmissionFile(
                submission=sub, kind=kind,
                stored_name=storage.save_bytes(AREA, b"", ext=SubmissionFile.EXT[kind]),
                original_name=competition_services._safe_original_name(name),
            )
        # 빈칸 없이 이어서 받는다: 보낸 위치가 지금까지 받은 양과 같아야 한다
        if offset != f.received:
            raise SubmissionRefused(f"조각 순서가 맞지 않습니다(서버가 받은 양: {f.received}바이트).")

        limit = filecheck.MAX_BYTES[kind]
        if offset + len(data) > limit:
            # 크기 초과는 형식 오류로 끝낸다 → 잡아 둔 1회가 풀린다(R3)
            error = f"{Kind(kind).label}이(가) 너무 큽니다(최대 {limit // filecheck.MB}MB)."
            _fail(sub, error)
        else:
            storage.write_at(AREA, f.stored_name, offset, data)
            f.received = offset + len(data)
            f.save()
            sub.last_received_at = now
            sub.save(update_fields=["last_received_at"])
    if error:
        raise SubmissionRefused(error)
    return f.received


def cancel(submission, now=None):
    """참여자가 업로드를 그만둔다 → '취소', 잡아 둔 1회가 풀린다. 업로드 중일 때만 가능."""
    changed = Submission.objects.filter(pk=submission.pk, status=Status.UPLOADING).update(
        status=Status.CANCELLED, completed_at=now or timezone.now(),
    )
    if not changed:
        raise SubmissionRefused("업로드 중인 제출만 취소할 수 있습니다.")


# ── ③ 완료·검사 ───────────────────────────────────────────────
def _validate(project, files):
    """파일 3종 내용 검사. 실패하면 CsvError(사용자에게 보여 줄 문구)."""
    missing = [label for kind, label in Kind.choices if kind not in files]
    if missing:
        raise CsvError(f"필수 파일이 없습니다: {', '.join(missing)}")   # R3

    # 예측 CSV: 프로젝트 규칙(컬럼·행 수·id·허용 값)으로 (R15)
    table = csvcheck.parse(storage.read_bytes(AREA, files[Kind.CSV].stored_name))
    answer_ids = competition_services.read_ids(project, ProjectFile.Kind.ANSWER)
    csvcheck.check_prediction(table, project, answer_ids)
    # 노트북: Jupyter 공식 스키마로 (실행하지 않음)
    filecheck.check_notebook(storage.read_bytes(AREA, files[Kind.NOTEBOOK].stored_name))
    # 모델: 확장자와 앞 8바이트만 (로드하지 않음)
    model = files[Kind.MODEL]
    filecheck.check_model(model.original_name, storage.read_head(AREA, model.stored_name, filecheck.HEAD_BYTES))


def complete(submission, now=None):
    """[제출 완료] 버튼. 검사 결과에 따라 상태를 정한다.

    통과: 마감 전 시작 → 채점 대기(잡아 둔 1회 유지) + 채점 작업 등록 / 마감 후 시작 → 마감 후 접수(횟수 무관)
    실패: 형식 오류 → 잡아 둔 1회가 풀린다(R3)
    """
    now = now or timezone.now()
    expire_idle(now, pk=submission.pk)
    with transaction.atomic():
        sub = Submission.objects.select_for_update().select_related("project").get(pk=submission.pk)
        if sub.status != Status.UPLOADING:
            raise SubmissionRefused(f"이 제출은 이미 '{sub.get_status_display()}' 상태입니다.")
        files = {f.kind: f for f in sub.files.all()}
        try:
            _validate(sub.project, files)
        except CsvError as e:
            sub.status = Status.INVALID
            sub.error_message = str(e)[:300]
        else:
            for f in files.values():
                f.sha256 = storage.sha256_file(AREA, f.stored_name)   # 검증 리포트(V2: 같은 파일 찾기)용
                f.save(update_fields=["sha256"])
            sub.status = Status.QUEUED if sub.deadline_id else Status.LATE
        sub.completed_at = now
        sub.save(update_fields=["status", "error_message", "completed_at"])
        if sub.status == Status.QUEUED:
            # 같은 트랜잭션에서 채점 작업을 등록한다 → '채점 대기'인데 작업이 없는 상태가 생기지 않는다.
            # 채점은 여기서 하지 않는다(worker가 한다). 웹 요청은 바로 끝난다.
            job_queue.enqueue(sub, now=now)
    return sub

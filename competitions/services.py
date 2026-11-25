from pathlib import Path

from django.db import IntegrityError, transaction

from audit.models import AuditLog
from audit.services import record

from . import csvcheck, storage
from .models import Competition, CompetitionMember, ProjectFile, generate_join_code


class AlreadyMember(Exception):
    """이미 그 대회에 소속된 사용자."""


class PromotionRefused(Exception):
    """관리자1 지정·해제를 할 수 없는 상태."""


def add_member(user, competition, role=CompetitionMember.Role.PARTICIPANT):
    """사용자를 대회에 소속시킨다.

    익명 별칭은 무작위라 아주 드물게 같은 대회 안에서 겹칠 수 있다(약 92만 분의 1).
    겹치면 DB 유일 조건이 거부하므로, 새 별칭으로 몇 번 다시 시도한다.
    """
    for _ in range(5):
        try:
            # 안쪽 atomic = 저장점. 실패해도 바깥 트랜잭션(가입 전체)은 살아 있다.
            with transaction.atomic():
                return CompetitionMember.objects.create(user=user, competition=competition, role=role)
        except IntegrityError:
            # 거부 이유가 "이미 소속"이면 재시도해도 소용없으므로 바로 알린다.
            if CompetitionMember.objects.filter(user=user, competition=competition).exists():
                raise AlreadyMember from None
    raise RuntimeError("익명 별칭을 만들지 못했습니다.")


def create_competition(name, created_by):
    """대회를 만든다. 참여 코드는 자동 생성되고, 드물게 겹치면 다시 뽑는다."""
    for _ in range(5):
        try:
            with transaction.atomic():
                return Competition.objects.create(name=name, created_by=created_by)
        except IntegrityError:
            continue
    raise RuntimeError("참여 코드를 만들지 못했습니다.")


def reissue_join_code(competition):
    """참여 코드를 새로 만든다. 옛 코드는 즉시 무효, 이미 가입한 사람은 영향 없음."""
    for _ in range(5):
        competition.join_code = generate_join_code()
        try:
            with transaction.atomic():
                competition.save(update_fields=["join_code"])
                return
        except IntegrityError:
            continue
    raise RuntimeError("참여 코드를 만들지 못했습니다.")


def has_submissions(user, competition):
    """이 대회에 제출 기록이 하나라도 있는가 (D5 검사용). 형식 오류·만료된 제출도 기록으로 본다."""
    from submissions.models import Submission   # 앱끼리 서로 불러오는 순환을 피하려고 함수 안에서 불러온다

    return Submission.objects.filter(member__user=user, member__competition=competition).exists()


def promote_to_admin1(member):
    """참여자를 관리자1로 지정하거나, 해제된 관리자1을 다시 활성화한다.

    반환값: "promoted"(새로 지정) 또는 "reactivated"(다시 활성화)
    """
    if member.role == CompetitionMember.Role.ADMIN1:
        if member.is_active:
            raise PromotionRefused("이미 관리자1입니다.")
        member.is_active = True
        member.save(update_fields=["is_active"])
        return "reactivated"

    # 정지된 참여자를 관리자1로 올리면 정지가 풀리는 셈이 된다 → 먼저 정지를 해제하게 한다
    if not member.is_active:
        raise PromotionRefused("참가 정지된 참여자는 관리자1로 지정할 수 없습니다.")
    # D5: 자기 제출을 자기가 검토하는 일을 막는다.
    if has_submissions(member.user, member.competition):
        raise PromotionRefused("이 대회에 제출 기록이 있어 관리자1로 지정할 수 없습니다.")
    member.role = CompetitionMember.Role.ADMIN1
    member.save(update_fields=["role"])
    return "promoted"


def revoke_admin1(member):
    """관리자1 해제 = 비활성. 참여자로 되돌리지 않는다(정답을 본 사람이므로)."""
    if member.role != CompetitionMember.Role.ADMIN1 or not member.is_active:
        raise PromotionRefused("활성 관리자1이 아닙니다.")
    member.is_active = False
    member.save(update_fields=["is_active"])


# ── 프로젝트 파일 ──────────────────────────────────────────────
Kind = ProjectFile.Kind


def read_ids(project, kind):
    """등록된 파일의 id 목록(순서 그대로). 파일이 없으면 None."""
    pf = project.files.filter(kind=kind).first()
    if pf is None:
        return None
    table = csvcheck.parse(storage.path_for(pf.area, pf.stored_name).read_bytes())
    return table.column(project.id_column)


def _safe_original_name(name):
    # 경로 부분은 버리고 파일명만, 길이 제한. 화면 표시·다운로드 이름으로만 쓴다(저장 이름은 UUID).
    return (Path(name or "").name or "upload.csv")[:100]


def upload_project_file(project, kind, uploaded, user):
    """CSV를 검증하고 저장한다. 실패하면 csvcheck.CsvError (아무것도 저장되지 않음).

    파일(디스크)과 DB를 함께 다루므로, 감사 기록까지 이 함수가 한 번에 맡는다.
    """
    # ① 내용 검증 (저장 전에 끝낸다)
    data = csvcheck.read_upload(uploaded)
    table = csvcheck.parse(data)
    project_updates = {}
    if kind == Kind.ANSWER:
        from submissions.models import Submission

        # 채점 대기·채점된 제출이 있는데 정답을 바꾸면 같은 대회에서 채점 기준이 둘이 된다 → 거부
        if Submission.objects.filter(project=project, status__in=Submission.GRADED_OR_WAITING).exists():
            raise csvcheck.CsvError("채점 대기 중이거나 채점된 제출이 있어 정답 파일을 바꿀 수 없습니다.")
        info = csvcheck.check_answer(table, project)
        feature_ids = read_ids(project, Kind.TEST_FEATURES)
        if feature_ids is not None:
            csvcheck.compare_ids(feature_ids, info["ids"])
        project_updates = {"expected_rows": info["rows"], "allowed_labels": info["labels"]}
    elif kind == Kind.TEST_FEATURES:
        ids = csvcheck.check_test_features(table, project)
        answer_ids = read_ids(project, Kind.ANSWER)
        if answer_ids is not None:
            csvcheck.compare_ids(ids, answer_ids)
    # (공개 학습 데이터는 CSV로 읽히는지만 확인한다)

    # ② 새 파일 저장 → ③ DB 갱신. DB에서 실패하면 새 파일을 지운다.
    area = "private" if kind == Kind.ANSWER else "shared"
    stored_name = storage.save_bytes(area, data)
    old = project.files.filter(kind=kind).first()
    try:
        with transaction.atomic():
            if old:
                old.delete()
            pf = ProjectFile.objects.create(
                project=project, kind=kind, stored_name=stored_name,
                original_name=_safe_original_name(uploaded.name),
                size=len(data), sha256=storage.sha256(data), rows=len(table.rows),
                duplicate_rows=csvcheck.count_duplicate_rows(table, project) if kind == Kind.TRAIN else None,
                uploaded_by=user,
            )
            if project_updates:
                for field, value in project_updates.items():
                    setattr(project, field, value)
                project.save(update_fields=list(project_updates))
            record(user, AuditLog.Action.FILE_UPLOADED, competition=project.competition, detail={
                "project_id": project.pk, "kind": kind, "rows": pf.rows,
                "sha256": pf.sha256, "replaced": old is not None,
            })
    except Exception:
        storage.delete(area, stored_name)
        raise
    # ④ DB 반영이 끝난 뒤에 옛 파일을 지운다
    if old:
        storage.delete(area, old.stored_name)
    return pf

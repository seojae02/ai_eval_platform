"""회원 관리: 임시 비밀번호 발급, 로그인 잠금 해제, 참가 정지·해제.

누가 누구에게 할 수 있는가 (뷰는 먼저 '이 대회의 관리자인가'만 확인하고, 세부 규칙은 여기서 본다)

    동작              관리자2                         관리자1
    임시 비밀번호      이 대회 회원(관리자2 계정 제외)    '순수 참여자'만 (아래 is_plain_participant)
    로그인 잠금 해제   이 대회 회원                    이 대회 회원
    참가 정지·해제     이 대회 참여자                  불가

관리자1의 비밀번호 초기화를 '순수 참여자'로 제한하는 이유:
    가상조교01이 다른 대회 관리자인 가상조교02의 비밀번호를 초기화하면, 그 임시 비밀번호로
    가상조교02 계정에 로그인해 그 대회의 정답을 볼 수 있다(계정 탈취). 그래서 어느 대회에서든
    관리자인 사람은 관리자1이 건드릴 수 없다.

모든 동작은 감사 로그에 남는다. 임시 비밀번호 값은 어디에도 기록하지 않는다.
"""
import secrets

from django.core.exceptions import PermissionDenied
from django.db import transaction

from accounts.models import FailedAttempt
from accounts.throttle import clear_failures, is_locked
from audit.models import AuditLog
from audit.services import record

from .models import CompetitionMember

# 헷갈리는 글자(0/O, 1/l/I)를 뺀 문자. 12자리 → 약 4.7 × 10^20 가지
TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
Role = CompetitionMember.Role


class MemberActionRefused(Exception):
    """화면에 그대로 보여 줄 수 있는 거부 사유(권한 문제가 아닌 상태 문제)."""


def login_key(user):
    # 로그인 잠금은 소문자 아이디로 센다(accounts/views.py ThrottledLoginView._key와 같게)
    return user.username.lower()


def is_login_locked(user):
    return is_locked(FailedAttempt.Kind.LOGIN, login_key(user))


def is_plain_participant(user):
    """어느 대회에서도 관리자(관리자2, 관리자1, 해제된 관리자1 포함)가 아닌 사용자인가."""
    return not user.is_admin2 and not user.memberships.filter(role=Role.ADMIN1).exists()


def generate_temp_password():
    raw = "".join(secrets.choice(TEMP_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"   # 예: 'Kp7d-xQ3m-RtwZ' (불러 주기 쉽게 4자씩)


def reset_password(actor, member):
    """임시 비밀번호를 만들어 돌려준다(화면에 한 번만 보여 줄 값). 권한이 없으면 PermissionDenied(403)."""
    target = member.user
    if target.is_admin2 or target == actor:
        raise PermissionDenied
    if not actor.is_admin2 and not is_plain_participant(target):
        raise PermissionDenied
    temp = generate_temp_password()
    with transaction.atomic():
        target.set_password(temp)   # 비밀번호가 바뀌면 그 사람이 이미 로그인해 둔 세션도 모두 끊긴다(Django 기본)
        target.must_change_password = True
        target.save(update_fields=["password", "must_change_password"])
        clear_failures(FailedAttempt.Kind.LOGIN, login_key(target))   # 잠겨 있었다면 함께 푼다
        record(actor, AuditLog.Action.PASSWORD_RESET, competition=member.competition, target_user=target)
    return temp


def unlock_login(actor, member):
    if not is_login_locked(member.user):
        raise MemberActionRefused("잠겨 있지 않은 계정입니다.")
    with transaction.atomic():
        clear_failures(FailedAttempt.Kind.LOGIN, login_key(member.user))
        record(actor, AuditLog.Action.LOGIN_UNLOCKED, competition=member.competition, target_user=member.user)


def _require_admin2_on_participant(actor, member):
    if not actor.is_admin2:
        raise PermissionDenied
    if member.role != Role.PARTICIPANT:
        raise MemberActionRefused("참여자만 정지할 수 있습니다(관리자1은 '관리자1 해제'를 쓰세요).")


def suspend(actor, member):
    """참가 정지: 이 대회 화면에 들어오지도, 제출하지도 못한다. 제출 기록·점수는 그대로 둔다.

    성적에서 빼야 하면 검토에서 '부적합'으로 결정한다(정지와 성적 판정을 섞지 않는다).
    """
    _require_admin2_on_participant(actor, member)
    if not member.is_active:
        raise MemberActionRefused("이미 정지된 참여자입니다.")
    with transaction.atomic():
        member.is_active = False
        member.save(update_fields=["is_active"])
        record(actor, AuditLog.Action.MEMBER_SUSPENDED, competition=member.competition, target_user=member.user)


def reinstate(actor, member):
    _require_admin2_on_participant(actor, member)
    if member.is_active:
        raise MemberActionRefused("정지된 참여자가 아닙니다.")
    with transaction.atomic():
        member.is_active = True
        member.save(update_fields=["is_active"])
        record(actor, AuditLog.Action.MEMBER_REINSTATED, competition=member.competition, target_user=member.user)

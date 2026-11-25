"""대회별 권한 검사. 모든 대회 화면은 맨 앞에서 이 함수들 중 하나를 거친다.

404와 403을 나눠 쓴다.
- 404: 소속이 아닌 대회 → "없는 대회"처럼 보이게 해서 대회 존재 여부를 숨긴다.
- 403: 소속은 맞지만 그 기능의 권한이 없음 (예: 참여자가 회원 목록을 열 때).
"""
from django.core.exceptions import PermissionDenied
from django.http import Http404

from .models import Competition, CompetitionMember


def require_admin2(user):
    if not user.is_admin2:
        raise PermissionDenied


def active_membership(user, competition):
    """활성 소속 줄. 해제된 관리자1(비활성)은 None으로 본다."""
    return CompetitionMember.objects.filter(user=user, competition=competition, is_active=True).first()


def is_staff_of(user, competition):
    """관리자2이거나, 이 대회의 활성 관리자1인가."""
    if user.is_admin2:
        return True
    membership = active_membership(user, competition)
    return membership is not None and membership.role == CompetitionMember.Role.ADMIN1


def get_competition_for_member(user, competition_id):
    """관리자2 또는 활성 소속만 통과. 나머지는 없는 대회와 똑같이 404."""
    competition = Competition.objects.filter(pk=competition_id).first()
    if competition is None:
        raise Http404
    if user.is_admin2:
        return competition
    if active_membership(user, competition) is None:
        raise Http404
    return competition


def get_competition_for_staff(user, competition_id):
    """관리자2 또는 이 대회의 활성 관리자1만 통과."""
    competition = get_competition_for_member(user, competition_id)
    if not is_staff_of(user, competition):
        raise PermissionDenied
    return competition


def get_competition_for_admin2(user, competition_id):
    """관리자2만 통과. 소속이 아니면 404가 먼저 나간다."""
    competition = get_competition_for_member(user, competition_id)
    require_admin2(user)
    return competition

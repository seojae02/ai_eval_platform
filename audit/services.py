from .models import AuditLog


def record(actor, action, *, competition=None, target_user=None, detail=None):
    """감사 로그 한 줄을 남긴다. actor=None이면 서버 명령으로 한 일."""
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        competition=competition,
        target_user=target_user,
        detail=detail or {},
    )

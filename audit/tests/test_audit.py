import pytest

from audit.models import AuditLog
from audit.services import record

pytestmark = pytest.mark.django_db


def test_기록을_남긴다(admin2, competition):
    log = record(admin2, AuditLog.Action.COMPETITION_CREATED, competition=competition, detail={"name": "x"})

    assert AuditLog.objects.get(pk=log.pk).detail == {"name": "x"}


def test_기록은_수정할_수_없다(admin2):
    log = record(admin2, AuditLog.Action.COMPETITION_CREATED)
    log.action = AuditLog.Action.ADMIN1_REVOKED

    with pytest.raises(ValueError):
        log.save()


def test_기록은_삭제할_수_없다(admin2):
    log = record(admin2, AuditLog.Action.COMPETITION_CREATED)

    with pytest.raises(ValueError):
        log.delete()
    assert AuditLog.objects.filter(pk=log.pk).exists()

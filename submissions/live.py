"""실시간 상태: 완료 알림과 검증 진행 화면이 3초마다 읽는 값 (static/js/live.js가 부른다).

화면에 보일 문구도 여기서 만든다 → JS는 받은 문구를 그대로 넣기만 한다(JS가 작고 단순해진다).
오류 원문(Job.last_error)은 넣지 않는다(서버 경로 등 내부 정보 노출 방지).

가상 예시 (가상조교01이 제출 #12의 검증을 다시 실행)
    대기    {"state": "waiting", "headline": "대기열: 앞에 2건", ...}
    실행 중 {"state": "running", "headline": "실행 중: 노트북 재실행 (V1)", "elapsed": 58, "limit": 240, ...}
    완료    {"state": "done", "done": true, "text": "제출 #12 검증 완료: 주의", ...}
"""
from datetime import datetime

from django.conf import settings
from django.db.models import Count, Max, Q
from django.utils import timezone

from jobs.models import Job
from jobs.queue import PRIORITY
from verification.models import VerificationReport

from .models import Review, Submission

MAX_IDS = 50
S = Submission.Status
STEPS = [("queue", "대기열")] + list(VerificationReport.Stage.choices)
SANDBOX_STAGES = {VerificationReport.Stage.MODEL, VerificationReport.Stage.NOTEBOOK}


def parse_ids(raw):
    """'12,13,x,14' → [12, 13, 14]. 숫자가 아닌 값은 버리고 최대 50개."""
    return [int(p) for p in (raw or "").split(",")[:MAX_IDS] if p.strip().isdigit()]


def queue_ahead(job):
    """이 작업보다 먼저 처리될 작업 수 = 실행 중인 작업 + worker가 이것보다 먼저 가져갈 대기 작업.

    worker가 고르는 순서(jobs/queue.py claim)와 같게 센다: 채점 우선 → 실행 가능 시각 → 번호.
    """
    mine = 0 if job.kind == Job.Kind.SCORE else 1
    ahead = (Job.objects.filter(status=Job.Status.PENDING).exclude(pk=job.pk).annotate(p=PRIORITY)
             .filter(Q(p__lt=mine) | Q(p=mine, run_after__lt=job.run_after)
                     | Q(p=mine, run_after=job.run_after, pk__lt=job.pk))
             .count())
    return ahead + Job.objects.filter(status=Job.Status.RUNNING).exclude(pk=job.pk).count()


def score_item(sub):
    """참여자 '내 제출'용: 채점이 끝났는가."""
    if sub.status == S.QUEUED:
        return {"done": False, "state": "waiting", "text": f"제출 #{sub.pk} 채점 대기"}
    if sub.status == S.SCORED:
        return {"done": True, "state": "done", "text": f"제출 #{sub.pk} 채점 완료 ({sub.score:.4f})"}
    return {"done": True, "state": "done", "text": f"제출 #{sub.pk} {sub.get_status_display()}"}


def _seconds(start, end=None):
    start = datetime.fromisoformat(start)
    end = datetime.fromisoformat(end) if end else timezone.now()
    return max(0, int((end - start).total_seconds()))


def _steps(state, report, current):
    """진행 화면의 단계 5개와 각 단계의 상태(done/current/todo)·걸린 초."""
    timeline = (report.timeline if report else None) or {}
    order = [key for key, _ in STEPS]
    if state == "waiting":
        at = 0
    elif state == "running":
        at = order.index(current) if current in order else 1   # 막 가져가 아직 단계가 없으면 첫 검사 단계로
    else:
        at = len(order)                                      # 완료·실패: 모두 지나감
    steps = []
    for i, (key, label) in enumerate(STEPS):
        span = timeline.get(key, {})
        seconds = _seconds(span["start"], span.get("end")) if span.get("start") else None
        steps.append({"key": key, "label": label, "seconds": seconds,
                      "state": "done" if i < at else "current" if i == at else "todo"})
    return steps


def verify_item(sub, detail=False):
    """관리자용: 검증 진행 상황. detail=True면 진행 화면에 필요한 단계·시간까지."""
    if sub.status == S.QUEUED:
        item = {"done": False, "state": "scoring", "headline": "채점 대기 중(채점이 끝나면 검증이 이어집니다)",
                "text": f"제출 #{sub.pk} 채점 대기"}
        return {**item, "steps": _steps("waiting", None, "")} if detail else item
    if sub.status != S.SCORED:
        return {"done": True, "state": "none", "headline": f"검증 대상이 아닙니다({sub.get_status_display()})",
                "text": f"제출 #{sub.pk} {sub.get_status_display()}"}

    job = sub.jobs.filter(kind=Job.Kind.VERIFY).first()
    report = VerificationReport.objects.filter(submission=sub).first()
    item = {"done": False, "elapsed": None, "limit": None, "level": None, "level_label": None}
    current = ""
    if job is None or job.status == Job.Status.PENDING:
        ahead = queue_ahead(job) if job else 0
        retry = f", 재시도 {job.attempts + 1}/{settings.JOB_MAX_ATTEMPTS}" if job and job.attempts else ""
        item.update(state="waiting", headline=f"대기열: 앞에 {ahead}건{retry}", text=f"제출 #{sub.pk} 검증 대기")
    elif job.status == Job.Status.RUNNING:
        running = report is not None and report.status == VerificationReport.Status.RUNNING
        current = report.stage if running else ""
        label = VerificationReport.Stage(current).label if current else "시작하는 중"
        item.update(state="running", headline=f"실행 중: {label}", text=f"제출 #{sub.pk} 검증 중")
        span = (report.timeline or {}).get(current, {}) if running else {}
        if span.get("start"):
            item["elapsed"] = _seconds(span["start"])
            if current in SANDBOX_STAGES:
                item["limit"] = settings.SANDBOX_TIMEOUT   # '예상 시간'이 아니라 '제한 시간'
    elif job.status == Job.Status.FAILED or (report and report.status == VerificationReport.Status.FAILED):
        item.update(done=True, state="failed", headline=f"검증 실패({settings.JOB_MAX_ATTEMPTS}번 시도). 다시 실행할 수 있습니다",
                    text=f"제출 #{sub.pk} 검증 실패")
    else:
        level = report.get_level_display() if report else "-"
        item.update(done=True, state="done", level=report.level if report else None, level_label=level,
                    headline=f"검증 완료: 전체 '{level}'", text=f"제출 #{sub.pk} 검증 완료: {level}")
    if detail:
        item["steps"] = _steps(item["state"], report, current)
    return item


def pending_ids(project, member=None):
    """알림을 기다릴 제출 번호.

    member가 있으면(참여자) 그 사람의 '채점 대기'만, 없으면(관리자) 채점 대기 + 검증이 아직 안 끝난 제출.
    """
    subs = project.submissions.all()
    if member is not None:
        return list(subs.filter(member=member, status=S.QUEUED).values_list("pk", flat=True)[:MAX_IDS])
    verifying = Job.objects.filter(submission__project=project, kind=Job.Kind.VERIFY,
                                   status__in=[Job.Status.PENDING, Job.Status.RUNNING]).values_list("submission_id", flat=True)
    return list(subs.filter(Q(status=S.QUEUED) | Q(pk__in=verifying)).values_list("pk", flat=True)[:MAX_IDS])


def board_version(project):
    """리더보드가 바뀌었는지 비교하는 표시값: 채점된 제출 수 · 마지막 채점 시각 · 마지막 검토 이력 번호."""
    scored = project.submissions.filter(status=S.SCORED).aggregate(n=Count("id"), t=Max("scored_at"))
    last_review = Review.objects.filter(submission__project=project).aggregate(m=Max("id"))["m"] or 0
    t = scored["t"].timestamp() if scored["t"] else 0
    return f"{scored['n']}-{t:.0f}-{last_review}"

"""관리자 화면: 제출 목록, 검토 목록, 검토 상세, 제안·결정, 제출 파일 다운로드, 최종 성적.

권한(맨 앞에서 확인)
  보기·다운로드: 이 대회의 활성 관리자1 + 관리자2     (남의 대회·해제된 관리자1 404, 참여자 403)
                 최종 성적 화면·CSV도 같다(실명이 들어 있으므로)
  제안:          이 대회의 활성 관리자1만               (관리자2는 '결정'을 쓴다 → 403)
  결정·반려·재검토: 관리자2만                           (관리자1 403: 혼자서 순위를 바꿀 수 없다)
"""
import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.forms import NoColonMixin
from audit.models import AuditLog
from audit.services import record
from competitions import storage
from competitions.models import CompetitionMember, Project
from competitions.permissions import active_membership, get_competition_for_staff
from jobs import queue as job_queue
from jobs.models import Job
from verification import notebook_view
from verification.models import VerificationReport

from . import live
from . import results as results_service
from . import review
from .models import Review, Submission, SubmissionFile

RS = Submission.ReviewStatus


class VerdictForm(NoColonMixin, forms.Form):
    verdict = forms.ChoiceField(label="판단", choices=Review.Verdict.choices, widget=forms.RadioSelect)
    reason = forms.CharField(label="사유", required=False, max_length=1000,
                             widget=forms.Textarea(attrs={"rows": 3}), help_text="부적합이면 꼭 적어 주세요")
    seen = forms.IntegerField(required=False, widget=forms.HiddenInput)


class ReasonForm(NoColonMixin, forms.Form):
    reason = forms.CharField(label="사유", max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))
    seen = forms.IntegerField(required=False, widget=forms.HiddenInput)


def _staff_project(request, competition_id, project_id):
    competition = get_competition_for_staff(request.user, competition_id)
    return competition, get_object_or_404(Project, pk=project_id, competition=competition)


def _submission(project, submission_id):
    # 다른 프로젝트의 제출 번호를 이 주소에 넣으면 404
    return get_object_or_404(Submission.objects.select_related("member__user", "deadline"),
                             pk=submission_id, project=project)


def _is_admin1(user, competition):
    m = active_membership(user, competition)
    return m is not None and m.role == CompetitionMember.Role.ADMIN1


@login_required
def submission_list(request, competition_id, project_id):
    """관리자용 제출 목록: 모든 상태의 제출, 실명 포함.

    검토가 끝나면 검토 목록에서는 빠지므로, 확정된 제출의 파일·리포트는 여기서 다시 연다(줄마다 파일 링크).
    """
    competition, project = _staff_project(request, competition_id, project_id)
    subs = project.submissions.select_related("member__user", "deadline", "verification").prefetch_related("files")
    method_only = request.GET.get("method") == "warn"   # V7(학습·평가 방법) '주의'만 골라 보기
    if method_only:
        subs = subs.filter(verification__checks__v7__level="warn")
    subs = subs.order_by("-started_at")[:500]   # 줄마다 파일·리포트를 따로 조회하지 않게
    return render(request, "submissions/admin_list.html",
                  {"competition": competition, "project": project, "submissions": subs, "method_only": method_only,
                   "live_ids": live.pending_ids(project)})   # 채점·검증이 끝나면 알림


@login_required
def results(request, competition_id, project_id):
    """최종 성적: 참여자별 대표 기록과 확정 여부, 진행 상황, 리더보드에 없는 참여자."""
    competition, project = _staff_project(request, competition_id, project_id)
    rows, missing, summary = results_service.build(project)
    return render(request, "submissions/results.html", {
        "competition": competition, "project": project, "rows": rows, "missing": missing, "summary": summary,
    })


@login_required
def results_csv(request, competition_id, project_id):
    """최종 성적 CSV(실명 포함). 첨부로만, 캐시 금지, 감사 기록."""
    competition, project = _staff_project(request, competition_id, project_id)
    data, summary = results_service.to_csv(project)
    record(request.user, AuditLog.Action.RESULTS_EXPORTED, competition=competition,
           detail={"project_id": project.pk, "rows": summary["total"], "final": summary["final"]})
    response = HttpResponse(data, content_type="text/csv; charset=utf-8")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response["Content-Disposition"] = f'attachment; filename="results_project{project.pk}_{stamp}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def queue(request, competition_id, project_id):
    competition, project = _staff_project(request, competition_id, project_id)
    entries = review.review_queue(project)
    # 검증 리포트를 한 번에 불러와 줄에 붙인다(줄마다 조회하면 참여자 30명 = 쿼리 30개 추가)
    reports = VerificationReport.objects.in_bulk([e.submission.pk for e in entries], field_name="submission_id")
    for e in entries:
        e.report = reports.get(e.submission.pk)
    return render(request, "submissions/review_queue.html", {
        "competition": competition, "project": project,
        "entries": entries,
        "pending_rows": review.pending_rows(project) if request.user.is_admin2 else None,
        "live_ids": live.pending_ids(project),
    })


@login_required
@require_POST
def bulk_approve(request, competition_id, project_id):
    """관리자2: 승인 대기에서 고른 '적합 제안'을 한 번에 적합(확정)한다(RFP 8쪽 관리자2의 승인 부담).

    폼은 줄마다 '제출번호:화면을 연 시점의 이력 번호'를 보낸다. 대상 규칙은 서버가 다시 검사한다(review.bulk_block_reason).
    """
    competition, project = _staff_project(request, competition_id, project_id)
    if not request.user.is_admin2:
        raise PermissionDenied
    picks = []
    for raw in request.POST.getlist("pick")[:500]:
        sid, _, seen = raw.partition(":")
        if sid.isdigit():
            picks.append((int(sid), int(seen) if seen.isdigit() else None))
    if not picks:
        messages.error(request, "확정할 제출을 골라 주세요")
        return redirect("submissions:review_queue", competition.pk, project.pk)
    done, skipped = review.bulk_approve(project, request.user, picks)
    if done:
        messages.success(request, f"{len(done)}건을 적합(확정)했습니다")
    for sid, why in skipped:
        messages.error(request, f"#{sid} 건너뜀: {why}")
    return redirect("submissions:review_queue", competition.pk, project.pk)


@login_required
def detail(request, competition_id, project_id, submission_id):
    competition, project = _staff_project(request, competition_id, project_id)
    sub = _submission(project, submission_id)
    seen = {"seen": review.latest_review_id(sub)}
    scored = sub.status == Submission.Status.SCORED
    is_admin1 = _is_admin1(request.user, competition)
    is_admin2 = request.user.is_admin2
    proposal = sub.reviews.filter(action=Review.Action.PROPOSE).order_by("-pk").first()
    return render(request, "submissions/review_detail.html", {
        "competition": competition, "project": project, "s": sub,
        "files": sub.files.all(),
        "report": VerificationReport.objects.filter(submission=sub).first(),
        "verify_job": sub.jobs.filter(kind=Job.Kind.VERIFY).first(),
        "live_item": live.verify_item(sub),   # 검증이 진행 중이면 화면에 진행 패널 + 끝나면 알림
        "live_ids": [sub.pk],
        "history": sub.reviews.select_related("actor"),
        "others": project.submissions.filter(member=sub.member, status=Submission.Status.SCORED).exclude(pk=sub.pk),
        # 상태에 맞는 폼만 보여 준다(서버도 같은 규칙으로 다시 검사한다)
        "propose_form": VerdictForm(initial=seen) if is_admin1 and scored and sub.review_status not in Submission.DECIDED else None,
        "decide_form": VerdictForm(initial={**seen, **_decide_initial(sub, proposal)})
                       if is_admin2 and scored and sub.review_status not in Submission.DECIDED else None,
        "send_back_form": ReasonForm(initial=seen) if is_admin2 and sub.review_status in Submission.PROPOSED else None,
        "reopen_form": ReasonForm(initial=seen) if is_admin2 and sub.review_status in Submission.DECIDED else None,
    })


def _decide_initial(sub, proposal):
    """결정 폼 초기값: 관리자1 제안이 있으면 그 판단을, 부적합 제안이면 사유도 미리 채운다.

    부적합 결정 사유는 학생 본인에게 그대로 보인다. 관리자1 메모에는 다른 참여자 이름이 있을 수 있어
    자동으로 보여 주지 않고, 여기 채워 두면 관리자2가 읽고 다듬은 문장만 학생에게 나간다.
    가상 예시: 제안 "V5 66.2% 불일치, 원본 WineQT.csv 조회" → 결정 칸에 같은 문장 → 학생14 화면에 그 문장
    """
    if not proposal or sub.review_status not in Submission.PROPOSED:
        return {}
    initial = {"verdict": proposal.verdict}
    if sub.review_status == RS.PROPOSED_REJECT:
        initial["reason"] = proposal.reason
    return initial


def _act(request, competition_id, project_id, submission_id, who, form_class, action):
    """제안·결정·반려·재검토 공통: 권한 → 폼 → 서비스 → 결과 메시지 → 상세 화면으로."""
    competition, project = _staff_project(request, competition_id, project_id)
    allowed = _is_admin1(request.user, competition) if who == "admin1" else request.user.is_admin2
    if not allowed:
        raise PermissionDenied
    sub = _submission(project, submission_id)
    form = form_class(request.POST)
    if not form.is_valid():
        messages.error(request, "입력을 확인해 주세요(판단 선택, 사유 1000자 이하).")
    else:
        data = form.cleaned_data
        args = [data["verdict"]] if "verdict" in data else []
        try:
            action(sub, request.user, *args, reason=data["reason"], seen=data["seen"])
        except review.ReviewRefused as e:
            messages.error(request, str(e))
        else:
            sub.refresh_from_db()
            messages.success(request, f"검토 상태: {sub.get_review_status_display()}")
    return redirect("submissions:review_detail", competition.pk, project.pk, sub.pk)


@login_required
@require_POST
def propose(request, competition_id, project_id, submission_id):
    return _act(request, competition_id, project_id, submission_id, "admin1", VerdictForm, review.propose)


@login_required
@require_POST
def decide(request, competition_id, project_id, submission_id):
    return _act(request, competition_id, project_id, submission_id, "admin2", VerdictForm, review.decide)


@login_required
@require_POST
def send_back(request, competition_id, project_id, submission_id):
    return _act(request, competition_id, project_id, submission_id, "admin2", ReasonForm, review.send_back)


@login_required
@require_POST
def reopen(request, competition_id, project_id, submission_id):
    return _act(request, competition_id, project_id, submission_id, "admin2", ReasonForm, review.reopen)


@login_required
def file_download(request, competition_id, project_id, submission_id, kind):
    """검토용 제출 파일 다운로드. 첨부로만(브라우저가 열거나 실행하지 않음), 캐시 금지, 감사 기록."""
    competition, project = _staff_project(request, competition_id, project_id)
    sub = _submission(project, submission_id)
    f = get_object_or_404(SubmissionFile.objects.select_related("submission__member"),
                          submission=sub, kind=kind, purged_at__isnull=True)
    record(request.user, AuditLog.Action.SUBMISSION_FILE_DOWNLOADED, competition=competition,
           target_user=sub.member.user, detail={"submission_id": sub.pk, "kind": kind, "sha256": f.sha256})
    response = FileResponse(
        storage.path_for("submissions", f.stored_name).open("rb"),
        as_attachment=True,
        filename=f.download_name,   # 예: 58AS_CSV.csv (별칭 코드 + 종류)
        # 모델 파일(.pkl 등)도 '그냥 바이트'로 내려준다. nosniff는 SecurityMiddleware가 붙인다
        content_type="text/csv" if kind == SubmissionFile.Kind.CSV else "application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def notebook(request, competition_id, project_id, submission_id):
    """제출 노트북을 안전하게 보여 준다(스크립트·HTML 출력은 그리지 않음, verification/notebook_view.py)."""
    competition, project = _staff_project(request, competition_id, project_id)
    sub = _submission(project, submission_id)
    f = get_object_or_404(SubmissionFile, submission=sub, kind=SubmissionFile.Kind.NOTEBOOK, purged_at__isnull=True)
    try:
        nb = json.loads(storage.read_bytes("submissions", f.stored_name))
    except (OSError, ValueError):
        nb = {}
    response = render(request, "submissions/notebook.html", {
        "competition": competition, "project": project, "s": sub, "cells": notebook_view.cells(nb),
    })
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def reverify(request, competition_id, project_id, submission_id):
    """검증 다시 실행(담당 관리자1·관리자2). 예) 샌드박스가 꺼져 있어 '판정 불가'였던 제출"""
    competition, project = _staff_project(request, competition_id, project_id)
    sub = _submission(project, submission_id)
    if sub.status != Submission.Status.SCORED:
        messages.error(request, "채점된 제출만 검증할 수 있습니다.")
    else:
        job = job_queue.enqueue(sub, Job.Kind.VERIFY)
        if job.status == Job.Status.RUNNING:
            messages.error(request, "지금 검증 중입니다.")
        else:
            Job.objects.filter(pk=job.pk).update(status=Job.Status.PENDING, attempts=0, run_after=timezone.now(),
                                                 finished_at=None, last_error="")
            return redirect("submissions:review_progress", competition.pk, project.pk, sub.pk)   # 진행 화면으로
    return redirect("submissions:review_detail", competition.pk, project.pk, sub.pk)


@login_required
def progress(request, competition_id, project_id, submission_id):
    """검증 진행 화면: 대기열 → V2·V3·V4 → V5 → V1 → 저장. live.js가 3초마다 갱신하고, 끝나면 리포트로 안내."""
    competition, project = _staff_project(request, competition_id, project_id)
    sub = _submission(project, submission_id)
    return render(request, "submissions/verify_progress.html", {
        "competition": competition, "project": project, "s": sub, "item": live.verify_item(sub, detail=True),
        "live_ids": [sub.pk],
    })

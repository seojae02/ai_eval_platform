"""제출 화면. 권한 검사 → 서비스 함수 호출 → 결과 표시만 한다(규칙은 services.py)."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.forms import NoColonMixin
from competitions.models import CompetitionMember, Project
from competitions.permissions import active_membership, get_competition_for_member, is_staff_of

from . import filecheck, leaderboard as leaderboard_service, live, results as results_service, services
from .models import Submission, SubmissionFile


class StartForm(NoColonMixin, forms.Form):
    comment = forms.CharField(
        label="코멘트(선택)", required=False, max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="이번 제출에서 바꾼 점 등을 적으면 담당 관리자가 검토할 때 봅니다",
    )


def _project(request, competition_id, project_id):
    competition = get_competition_for_member(request.user, competition_id)
    return competition, get_object_or_404(Project, pk=project_id, competition=competition)


def _own_submission(request, competition_id, project_id, submission_id):
    """본인 제출만. 남의 제출 번호를 넣으면 '없는 제출'과 똑같이 404."""
    _, project = _project(request, competition_id, project_id)
    return get_object_or_404(
        Submission.objects.select_related("deadline"),
        pk=submission_id, project=project, member__user=request.user, member__is_active=True,
    )


def _back(submission_or_project):
    p = getattr(submission_or_project, "project", submission_or_project)
    return redirect("competitions:project_detail", p.competition_id, p.pk)


@login_required
@require_POST
def start(request, competition_id, project_id):
    competition, project = _project(request, competition_id, project_id)
    member = active_membership(request.user, competition)
    # 제출은 참여자만. 관리자1·관리자2는 403 (D5: 검토하는 사람이 제출하지 않는다)
    if member is None or member.role != CompetitionMember.Role.PARTICIPANT:
        raise PermissionDenied
    form = StartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "코멘트는 1000자 이하로 써 주세요.")
        return _back(project)
    try:
        submission = services.start_submission(member, project, form.cleaned_data["comment"])
    except services.SubmissionRefused as e:
        messages.error(request, str(e))
        return _back(project)
    return redirect("submissions:upload", competition.pk, project.pk, submission.pk)


@login_required
def upload(request, competition_id, project_id, submission_id):
    submission = _own_submission(request, competition_id, project_id, submission_id)
    if submission.status != Submission.Status.UPLOADING:
        messages.error(request, f"이 제출은 '{submission.get_status_display()}' 상태입니다.")
        return _back(submission)
    kinds = [
        {"kind": kind, "label": label, "max_mb": filecheck.MAX_BYTES[kind] // filecheck.MB,
         "accept": {"csv": ".csv", "notebook": ".ipynb", "model": ",".join(filecheck.MODEL_MAGIC)}[kind]}
        for kind, label in SubmissionFile.Kind.choices
    ]
    return render(request, "submissions/upload.html", {
        "submission": submission, "project": submission.project, "kinds": kinds,
        "chunk_size": services.MAX_CHUNK,
        "idle_minutes": services.idle_timeout().seconds // 60,
    })


@login_required
@require_POST
def chunk(request, competition_id, project_id, submission_id):
    """조각 하나 받기. 브라우저 JS가 fetch로 부른다. 본문 = 파일 조각(바이트), 정보는 주소 뒤 ?kind=&offset=&name=."""
    submission = _own_submission(request, competition_id, project_id, submission_id)
    try:
        offset = int(request.GET.get("offset", ""))
    except ValueError:
        return JsonResponse({"error": "offset 값이 올바르지 않습니다."}, status=400)
    try:
        received = services.receive_chunk(
            submission, request.GET.get("kind", ""), offset, request.GET.get("name", ""), request.body,
        )
    except services.SubmissionRefused as e:
        return JsonResponse({"error": str(e)}, status=409)
    return JsonResponse({"received": received})


@login_required
@require_POST
def complete(request, competition_id, project_id, submission_id):
    submission = _own_submission(request, competition_id, project_id, submission_id)
    try:
        submission = services.complete(submission)
    except services.SubmissionRefused as e:
        messages.error(request, str(e))
        return _back(submission)
    if submission.status == Submission.Status.INVALID:
        messages.error(request, f"형식 오류로 접수되지 않았습니다(횟수 차감 없음): {submission.error_message}")
    elif submission.status == Submission.Status.LATE:
        messages.success(request, "마감 후 제출로 접수했습니다(채점하지 않고 횟수도 차감하지 않음)")
    else:
        messages.success(request, "제출을 접수했고 채점 대기 중입니다")
    return _back(submission)


@login_required
@require_POST
def cancel(request, competition_id, project_id, submission_id):
    submission = _own_submission(request, competition_id, project_id, submission_id)
    try:
        services.cancel(submission)
    except services.SubmissionRefused as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "업로드를 취소했고, 잡아 두었던 제출 횟수 1회가 돌아왔습니다")
    return _back(submission)


@login_required
def leaderboard(request, competition_id, project_id):
    """리더보드 (R7·R8). 대회 소속(+관리자2)만, 남의 대회는 404.

    R8: 실명·아이디는 이 대회의 활성 관리자1과 관리자2일 때만 템플릿에 넘긴다.
    CSS로 숨기는 것이 아니라 응답 HTML에 아예 들어가지 않는다.
    """
    competition, project = _project(request, competition_id, project_id)
    me = active_membership(request.user, competition)
    entries = leaderboard_service.build(project)
    return render(request, "submissions/leaderboard.html", {
        "competition": competition,
        "project": project,
        "entries": entries,
        "final": results_service.is_final(entries),
        "show_real_name": is_staff_of(request.user, competition),
        "my_member_id": me.pk if me else None,
        "board_version": live.board_version(project),   # live.js가 이 값이 바뀌면 '순위가 바뀌었습니다' 알림
    })


@login_required
def status(request, competition_id, project_id):
    """완료 알림·진행 화면용 상태(JSON). static/js/live.js가 3초마다 부른다.

    ?ids=12,13           확인할 제출 번호(최대 50개)
    ?watch=score|verify  채점 완료를 기다리는가(기본) / 검증 완료를 기다리는가(관리자만)
    ?detail=1            진행 화면용 단계·시간까지
    ?board=1             리더보드 비교값

    권한: 대회 소속이 아니면 404(다른 화면과 같다). 참여자는 '본인 제출'만 응답에 들어간다
    (남의 번호를 넣으면 그 번호는 조용히 빠진다 → 존재 여부도 알 수 없다).
    """
    competition, project = _project(request, competition_id, project_id)
    staff = is_staff_of(request.user, competition)
    watch = request.GET.get("watch", "score")
    if watch == "verify" and not staff:
        raise PermissionDenied
    subs = project.submissions.filter(pk__in=live.parse_ids(request.GET.get("ids")))
    if not staff:
        subs = subs.filter(member__user=request.user, member__is_active=True)
    detail = request.GET.get("detail") == "1"
    items = {str(s.pk): live.verify_item(s, detail) if watch == "verify" else live.score_item(s) for s in subs}
    data = {"items": items}
    if request.GET.get("board") == "1":
        data["board"] = live.board_version(project)
    response = JsonResponse(data)
    response["Cache-Control"] = "private, no-store"
    return response

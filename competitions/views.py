import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, ProtectedError, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from audit.models import AuditLog
from audit.services import record
from submissions import live as submission_live
from submissions import review as submission_review
from submissions import services as submission_services
from submissions.models import Submission
from submissions.views import StartForm

from . import member_admin, services, storage
from .csvcheck import CsvError
from .deadlines import deadline_for
from .forms import CompetitionForm, DeadlineForm, ProjectFileForm, ProjectForm
from .models import CompetitionMember, Deadline, Project, ProjectFile
from .permissions import (
    active_membership,
    get_competition_for_admin2,
    get_competition_for_member,
    get_competition_for_staff,
    is_staff_of,
    require_admin2,
)


@login_required
@require_http_methods(["GET", "POST"])
def create(request):
    require_admin2(request.user)  # R14: 대회 개설은 관리자2만
    form = CompetitionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            competition = services.create_competition(form.cleaned_data["name"], request.user)
            record(request.user, AuditLog.Action.COMPETITION_CREATED,
                   competition=competition, detail={"name": competition.name})
        messages.success(request, f"'{competition.name}' 대회를 개설했으니 참여 코드를 공유하세요")
        return redirect("competitions:detail", competition.pk)
    return render(request, "competitions/create.html", {"form": form})


@login_required
def detail(request, competition_id):
    competition = get_competition_for_member(request.user, competition_id)
    return render(request, "competitions/detail.html", {
        "competition": competition,
        "is_staff": is_staff_of(request.user, competition),
        "membership": active_membership(request.user, competition),
        "projects": competition.projects.order_by("created_at"),
    })


@login_required
@require_POST
def reissue_code(request, competition_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    with transaction.atomic():
        services.reissue_join_code(competition)
        # 새 코드 값은 기록하지 않는다(감사 로그를 보는 사람에게 코드가 퍼지지 않도록).
        record(request.user, AuditLog.Action.JOIN_CODE_REISSUED, competition=competition)
    messages.success(request, "참여 코드를 재발급했고, 옛 코드는 더 이상 쓸 수 없습니다")
    return redirect("competitions:detail", competition.pk)


@login_required
def members(request, competition_id):
    # 실명이 보이는 화면 → 담당 관리자1과 관리자2만 (R8)
    competition = get_competition_for_staff(request.user, competition_id)
    q = request.GET.get("q", "").strip()[:50]
    rows = (competition.members.select_related("user")
            .annotate(submission_count=Count("submissions"))
            .order_by("role", "user__real_name"))
    if q:   # 실명·학번·아이디·익명 별칭 중 하나라도 들어 있으면 (엑셀 필터처럼). 이 대회 회원 안에서만 찾는다
        rows = rows.filter(Q(user__real_name__icontains=q) | Q(user__student_id__icontains=q)
                           | Q(user__username__icontains=q) | Q(alias__icontains=q))
    rows = list(rows)
    for m in rows:
        m.locked = member_admin.is_login_locked(m.user)
        # 관리자1에게는 '순수 참여자'에게만 임시 비밀번호 버튼을 보여 준다(서버에서도 같은 규칙으로 막음)
        m.can_reset = not m.user.is_admin2 and (request.user.is_admin2 or member_admin.is_plain_participant(m.user))
    return render(request, "competitions/members.html", {"competition": competition, "rows": rows, "q": q})


def _member_action(request, competition_id, member_id, action, done_message):
    """잠금 해제·정지·해제 공통: 대회 관리자 확인 → 이 대회 회원 찾기 → 동작 → 회원 관리로 돌아감."""
    competition = get_competition_for_staff(request.user, competition_id)
    member = _member_or_404(competition, member_id)
    try:
        action(request.user, member)   # 관리자1에게 허용되지 않은 동작이면 PermissionDenied → 403
    except member_admin.MemberActionRefused as e:
        messages.error(request, str(e))
    else:
        messages.success(request, done_message.format(name=member.user.real_name))
    return redirect("competitions:members", competition.pk)


@login_required
@require_POST
def member_unlock(request, competition_id, member_id):
    return _member_action(request, competition_id, member_id, member_admin.unlock_login,
                          "{name} 님의 로그인 잠금을 풀었습니다.")


@login_required
@require_POST
def member_suspend(request, competition_id, member_id):
    return _member_action(request, competition_id, member_id, member_admin.suspend,
                          "{name} 님을 참가 정지했습니다. 이 대회에 들어오거나 제출할 수 없습니다.")


@login_required
@require_POST
def member_reinstate(request, competition_id, member_id):
    return _member_action(request, competition_id, member_id, member_admin.reinstate,
                          "{name} 님의 참가 정지를 풀었습니다.")


@login_required
@require_POST
def member_reset_password(request, competition_id, member_id):
    """임시 비밀번호를 발급하고 이 응답에서 한 번만 보여 준다(다시 볼 수 없음)."""
    competition = get_competition_for_staff(request.user, competition_id)
    member = _member_or_404(competition, member_id)
    temp = member_admin.reset_password(request.user, member)
    response = render(request, "competitions/temp_password.html",
                      {"competition": competition, "member": member, "temp_password": temp})
    response["Cache-Control"] = "private, no-store"   # 공용 PC 브라우저 캐시·뒤로 가기에 남기지 않음
    return response


def _member_or_404(competition, member_id):
    # 다른 대회의 회원 번호를 이 대회 주소에 넣어도 찾지 못하게, 대회로 한 번 더 거른다.
    return get_object_or_404(CompetitionMember, pk=member_id, competition=competition)


@login_required
@require_POST
def promote(request, competition_id, member_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    member = _member_or_404(competition, member_id)
    try:
        with transaction.atomic():
            result = services.promote_to_admin1(member)
            record(request.user, AuditLog.Action.ADMIN1_PROMOTED, competition=competition,
                   target_user=member.user, detail={"reactivated": result == "reactivated"})
    except services.PromotionRefused as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{member.user.real_name} 님을 관리자1로 지정했습니다.")
    return redirect("competitions:members", competition.pk)


@login_required
@require_POST
def revoke(request, competition_id, member_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    member = _member_or_404(competition, member_id)
    try:
        with transaction.atomic():
            services.revoke_admin1(member)
            record(request.user, AuditLog.Action.ADMIN1_REVOKED, competition=competition,
                   target_user=member.user)
    except services.PromotionRefused as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{member.user.real_name} 님의 관리자1 권한을 해제했습니다.")
    return redirect("competitions:members", competition.pk)


# ── 프로젝트 ───────────────────────────────────────────────────
def _project_or_404(competition, project_id):
    # 다른 대회의 프로젝트 번호를 이 대회 주소에 넣어도 찾지 못하게 대회로 한 번 더 거른다.
    return get_object_or_404(Project, pk=project_id, competition=competition)


def _errors_text(form):
    return " ".join(e for errors in form.errors.values() for e in errors)


@login_required
@require_http_methods(["GET", "POST"])
def project_create(request, competition_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            project = form.save(commit=False)
            project.competition = competition
            project.save()
            record(request.user, AuditLog.Action.PROJECT_CREATED, competition=competition,
                   detail={"project_id": project.pk, "name": project.name, "metric": project.metric,
                           "direction": project.direction})
        messages.success(request, f"'{project.name}' 프로젝트를 만들었으니 정답, 공개 파일과 마감일을 등록하세요")
        return redirect("competitions:project_detail", competition.pk, project.pk)
    return render(request, "competitions/project_form.html",
                  {"form": form, "competition": competition, "title": "프로젝트 추가"})


@login_required
def project_detail(request, competition_id, project_id):
    competition = get_competition_for_member(request.user, competition_id)
    project = _project_or_404(competition, project_id)
    now = timezone.now()
    context = {
        "competition": competition,
        "project": project,
        "is_staff": is_staff_of(request.user, competition),
        "files": {f.kind: f for f in project.files.all()},
        "deadlines": list(project.deadlines.all()),
        "current": deadline_for(project, now),
        "now": now,
    }
    if request.user.is_admin2:
        context["deadline_form"] = DeadlineForm(project=project)
        context["upload_form"] = ProjectFileForm()
    membership = active_membership(request.user, competition)
    if membership and membership.role == CompetitionMember.Role.PARTICIPANT:
        # 참여자에게만: 지금 구간의 사용/남은 횟수(R5), 내 제출 목록, 제출 시작 폼
        context["usage"] = submission_services.usage(membership, project, now)
        my = list(membership.submissions.filter(project=project).select_related("deadline", "verification")[:50])
        for sub in my:
            # 학습·평가 방법 안내(V7): 학생에게는 주의·참고 문구와 점수 비교 한 줄만. 부정 검사(V1~V6)는 보이지 않는다
            report = getattr(sub, "verification", None)   # 리포트가 아직 없으면 None
            v7 = report.checks.get("v7") if report and report.status == report.Status.DONE else None
            details = (v7 or {}).get("details") or {}
            sub.method_notes = details.get("student") or []
            sub.method_cv = (details.get("cv") or {}).get("text", "")
            # 부적합(확정) 사유는 본인에게만 보여 준다. 다른 참여자 화면에는 나오지 않는다
            decision = submission_review.last_decision(sub) if sub.review_status == sub.ReviewStatus.REJECTED else None
            sub.rejection_reason = decision.reason if decision else ""
        context["my_submissions"] = my
        context["start_form"] = StartForm()
        context["accepting"] = "answer" in context["files"]
        context["live_ids"] = submission_live.pending_ids(project, member=membership)   # 채점이 끝나면 알림
    return render(request, "competitions/project_detail.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def project_edit(request, competition_id, project_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    project = _project_or_404(competition, project_id)
    locked = project.files.filter(kind=ProjectFile.Kind.ANSWER).exists()
    # 채점 대기·채점된 제출이 있으면 점수 방향도 잠근다(정답 교체 금지와 같은 기준)
    scored = project.submissions.filter(status__in=Submission.GRADED_OR_WAITING).exists()
    fields = ProjectForm.Meta.fields
    before = {f: getattr(project, f) for f in fields}
    form = ProjectForm(request.POST or None, instance=project, locked=locked, scored=scored)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            changed = {f: [before[f], getattr(project, f)] for f in fields if before[f] != getattr(project, f)}
            if changed:
                record(request.user, AuditLog.Action.PROJECT_UPDATED, competition=competition,
                       detail={"project_id": project.pk, "changed": changed})
        messages.success(request, "프로젝트 설정을 저장했습니다.")
        return redirect("competitions:project_detail", competition.pk, project.pk)
    return render(request, "competitions/project_form.html",
                  {"form": form, "competition": competition, "title": f"{project.name} 설정"})


@login_required
@require_POST
def deadline_add(request, competition_id, project_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    project = _project_or_404(competition, project_id)
    form = DeadlineForm(request.POST, project=project)
    if form.is_valid():
        with transaction.atomic():
            deadline = form.save(commit=False)
            deadline.project = project
            deadline.save()
            record(request.user, AuditLog.Action.DEADLINE_ADDED, competition=competition, detail={
                "project_id": project.pk, "due_at": deadline.due_at.isoformat(),
                "max_submissions": deadline.max_submissions,
            })
        messages.success(request, "마감일을 추가했습니다.")
    else:
        messages.error(request, f"마감일을 추가하지 않았습니다: {_errors_text(form)}")
    return redirect("competitions:project_detail", competition.pk, project.pk)


@login_required
@require_POST
def deadline_delete(request, competition_id, project_id, deadline_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    project = _project_or_404(competition, project_id)
    deadline = get_object_or_404(Deadline, pk=deadline_id, project=project)
    try:
        with transaction.atomic():
            record(request.user, AuditLog.Action.DEADLINE_DELETED, competition=competition, detail={
                "project_id": project.pk, "due_at": deadline.due_at.isoformat(),
                "max_submissions": deadline.max_submissions,
            })
            deadline.delete()
    except ProtectedError:
        # 제출 → 마감일 FK가 PROTECT라서 DB가 막는다. 위의 기록도 함께 취소된다(같은 트랜잭션).
        count = deadline.submissions.count()
        messages.error(request, f"이 마감일에 연결된 제출이 {count}건 있어 삭제할 수 없습니다.")
    else:
        messages.success(request, "마감일을 삭제했습니다.")
    return redirect("competitions:project_detail", competition.pk, project.pk)


@login_required
@require_POST
def file_upload(request, competition_id, project_id):
    competition = get_competition_for_admin2(request.user, competition_id)
    project = _project_or_404(competition, project_id)
    form = ProjectFileForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, f"업로드하지 않았습니다: {_errors_text(form)}")
    else:
        try:
            pf = services.upload_project_file(project, form.cleaned_data["kind"], form.cleaned_data["file"], request.user)
        except CsvError as e:
            messages.error(request, f"업로드하지 않았습니다: {e}")
        else:
            messages.success(request, f"{pf.get_kind_display()} 등록 완료 ({pf.rows}행).")
    return redirect("competitions:project_detail", competition.pk, project.pk)


@login_required
def file_download(request, competition_id, project_id, kind):
    if kind not in ProjectFile.Kind.values:
        raise Http404
    is_answer = kind == ProjectFile.Kind.ANSWER
    # R9: 정답은 담당 관리자1·관리자2만, 공개 파일은 대회 소속이면 누구나
    if is_answer:
        competition = get_competition_for_staff(request.user, competition_id)
    else:
        competition = get_competition_for_member(request.user, competition_id)
    project = _project_or_404(competition, project_id)
    pf = get_object_or_404(ProjectFile, project=project, kind=kind)

    if is_answer:
        record(request.user, AuditLog.Action.ANSWER_DOWNLOADED, competition=competition,
               detail={"project_id": project.pk, "sha256": pf.sha256})
    response = FileResponse(
        storage.path_for(pf.area, pf.stored_name).open("rb"),
        as_attachment=True,              # 브라우저가 열지 않고 저장하게
        filename=pf.original_name,
        content_type="text/csv",
    )
    if is_answer:
        response["Cache-Control"] = "private, no-store"   # 공용 PC 브라우저 캐시에 남기지 않음
    return response


@login_required
def sample_csv(request, competition_id, project_id):
    """제출 견본: id는 테스트 feature에서, 예측 칸은 모두 같은 자리표시 값 (정답 정보 없음)."""
    competition = get_competition_for_member(request.user, competition_id)
    project = _project_or_404(competition, project_id)
    ids = services.read_ids(project, ProjectFile.Kind.TEST_FEATURES)
    if ids is None:
        raise Http404
    placeholder = project.allowed_labels[0] if project.is_classification and project.allowed_labels else "0"

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([project.id_column, project.target_column])
    for row_id in ids:
        writer.writerow([row_id, placeholder])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="sample_submission.csv"'
    return response

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_http_methods

from competitions import services
from competitions.models import Competition, normalize_join_code

from .forms import JoinForm, LoginForm, OwnPasswordChangeForm, SignupForm
from .models import FailedAttempt, User
from .throttle import LOCKED_MESSAGE, clear_failures, client_ip, is_locked, record_failure

INVALID_CODE = "유효하지 않은 참여 코드입니다"
CODE = FailedAttempt.Kind.JOIN_CODE
LOGIN = FailedAttempt.Kind.LOGIN


def _find_competition(raw_code):
    return Competition.objects.filter(join_code=normalize_join_code(raw_code)).first()


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "GET":
        return render(request, "accounts/signup.html", {"form": SignupForm()})

    # ① 대입 제한: 폼을 보기 전에 먼저 확인
    key = f"ip:{client_ip(request)}"
    if is_locked(CODE, key):
        return render(
            request, "accounts/signup.html",
            {"form": SignupForm(), "locked": LOCKED_MESSAGE}, status=429,
        )

    # ② 입력 검사
    form = SignupForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        # ③ 코드로 대회 찾기
        competition = _find_competition(data["join_code"])
        if competition is None:
            record_failure(CODE, key)
            form.add_error("join_code", INVALID_CODE)
        else:
            # ④ 사용자 생성 + 대회 소속: 둘 다 되거나 둘 다 취소 (R13: 대회 없는 회원 방지)
            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=data["username"],
                        password=data["password1"],
                        real_name=data["real_name"],
                        student_id=data["student_id"],
                    )
                    services.add_member(user, competition)
            except IntegrityError:
                # 폼 검사와 저장 사이에 다른 사람이 같은 학번·로그인ID로 먼저 가입한 경우(동시 가입). 500 대신 폼 오류로
                form.add_error(None, "방금 같은 학번이나 로그인ID로 가입한 사람이 있습니다. 확인 후 다시 시도해 주세요.")
                return render(request, "accounts/signup.html", {"form": form})
            # ⑤ 로그인 (Django가 세션 ID를 새로 발급 → 세션 고정 공격 방지)
            login(request, user)
            messages.success(request, f"가입을 마쳤고 '{competition.name}' 대회에 참여했습니다")
            return redirect("home")

    return render(request, "accounts/signup.html", {"form": form})


class ThrottledLoginView(LoginView):
    """Django 기본 로그인 + 로그인ID별 실패 횟수 제한."""

    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def _key(self):
        return self.request.POST.get("username", "").strip().lower()[:150]

    def post(self, request, *args, **kwargs):
        key = self._key()
        # 없는 아이디도 똑같이 세고 똑같이 잠근다 → 잠김 여부로 가입 여부를 알아낼 수 없다.
        if key and is_locked(LOGIN, key):
            context = self.get_context_data(form=LoginForm(request), locked=LOCKED_MESSAGE)
            return self.render_to_response(context, status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        key = self._key()
        if key:
            record_failure(LOGIN, key)
        return super().form_invalid(form)

    def form_valid(self, form):
        clear_failures(LOGIN, self._key())
        return super().form_valid(form)


class OwnPasswordChangeView(PasswordChangeView):
    """내 비밀번호 변경. 임시 비밀번호를 받은 사람은 로그인 직후 여기로만 올 수 있다(middleware)."""

    template_name = "accounts/password_change.html"
    form_class = OwnPasswordChangeForm
    success_url = reverse_lazy("home")

    def form_valid(self, form):
        # 부모가 새 비밀번호 저장 + 지금 세션 유지(update_session_auth_hash)까지 한다
        response = super().form_valid(form)
        if self.request.user.must_change_password:
            self.request.user.must_change_password = False
            self.request.user.save(update_fields=["must_change_password"])
        messages.success(self.request, "비밀번호를 바꿨습니다.")
        return response


@login_required
def home(request):
    if request.user.is_admin2:
        # 관리자2는 모든 대회를 본다(5-2 결정)
        competitions = Competition.objects.annotate(member_count=Count("members")).order_by("-created_at")
        return render(request, "accounts/home.html", {"competitions": competitions})
    memberships = request.user.memberships.select_related("competition").order_by("-joined_at")
    return render(request, "accounts/home.html", {"memberships": memberships})


@login_required
@require_http_methods(["GET", "POST"])
def join(request):
    if request.user.is_admin2:
        messages.info(request, "관리자2는 참여 코드로 대회에 참여하지 않습니다.")
        return redirect("home")

    if request.method == "GET":
        return render(request, "accounts/join.html", {"form": JoinForm()})

    key = f"user:{request.user.pk}"
    if is_locked(CODE, key):
        return render(
            request, "accounts/join.html",
            {"form": JoinForm(), "locked": LOCKED_MESSAGE}, status=429,
        )

    form = JoinForm(request.POST)
    if form.is_valid():
        competition = _find_competition(form.cleaned_data["join_code"])
        if competition is None:
            record_failure(CODE, key)
            form.add_error("join_code", INVALID_CODE)
        else:
            try:
                services.add_member(request.user, competition)
            except services.AlreadyMember:
                form.add_error("join_code", "이미 참여 중인 대회입니다.")
            else:
                messages.success(request, f"'{competition.name}' 대회에 참여했습니다.")
                return redirect("home")

    return render(request, "accounts/join.html", {"form": form})

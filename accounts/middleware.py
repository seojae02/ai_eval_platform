"""임시 비밀번호로 로그인한 사용자는 비밀번호를 바꾸기 전에는 다른 화면을 쓸 수 없다.

예) 가상조교01이 가상학생01에게 임시 비밀번호 'Kp7d-xQ3m-RtwZ'를 발급
    → 가상학생01이 그 값으로 로그인 → 어느 주소를 열어도 '비밀번호 변경' 화면으로 이동
    → 새 비밀번호로 바꾸면 그때부터 정상 사용. 조교는 새 비밀번호를 모른다.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

ALLOWED_NAMES = ("password_change", "logout")


class ForcePasswordChangeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.must_change_password:
            allowed = [reverse(name) for name in ALLOWED_NAMES]
            if request.path not in allowed and not request.path.startswith("/" + settings.STATIC_URL.lstrip("/")):
                return redirect("password_change")
        return self.get_response(request)


class NoStoreForLoggedInMiddleware:
    """로그인한 사용자의 응답은 브라우저에 저장하지 않게 한다(공용 PC 뒤로 가기로 실명·성적이 보이는 것 방지).

    예) 가상조교01이 실습실 PC에서 최종 성적 화면을 보고 로그아웃
        → 가상학생03이 뒤로 가기 → 브라우저에 저장된 화면이 없으니 서버에 다시 묻고 → 로그인 화면으로 이동
    이미 Cache-Control을 정한 응답(예: 성적표 CSV)은 건드리지 않는다.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and "Cache-Control" not in response:
            response["Cache-Control"] = "private, no-store"
        return response

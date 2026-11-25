from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("login/", views.ThrottledLoginView.as_view(), name="login"),
    # Django 5부터 LogoutView는 POST만 받는다(GET이면 405). 링크 하나로 강제 로그아웃시키는 공격 방지.
    path("logout/", LogoutView.as_view(), name="logout"),
    path("join/", views.join, name="join"),
    path("password/", views.OwnPasswordChangeView.as_view(), name="password_change"),
]

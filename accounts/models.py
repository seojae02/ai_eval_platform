from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """사이트 사용자.

    Django 기본 사용자(로그인ID, 비밀번호 해시, 로그인 시각 등)를 그대로 물려받고
    우리에게 필요한 두 칸만 더한다.
    대회별 역할(참여자/관리자1)은 여기가 아니라 competitions.CompetitionMember에 둔다.
    """

    # 기본의 이름 두 칸(first_name, last_name)은 쓰지 않고 실명 한 칸으로 대신한다.
    first_name = None
    last_name = None

    real_name = models.CharField("실명", max_length=50)

    # 학번. 가입 폼에서는 필수(settings.STUDENT_ID_DIGITS자리 숫자), 모델에서는 비워 둘 수 있다:
    # 서버 명령으로 만드는 관리자2, 이 칸이 생기기 전에 가입한 계정. 관리자 화면·성적표에만 보이고 참여자 화면에는 안 나간다.
    student_id = models.CharField("학번", max_length=20, blank=True, default="")

    # 전역 역할. 가입 폼에는 이 칸을 넣지 않는다 → 웹에서 스스로 관리자2가 될 수 없다.
    # True로 만드는 길은 서버 명령(create_admin2)뿐이다.
    is_admin2 = models.BooleanField("관리자2", default=False)

    # 관리자가 임시 비밀번호를 발급하면 True. 로그인 후 비밀번호를 바꾸기 전에는 다른 화면을 쓸 수 없다
    # (accounts/middleware.py). 그래서 관리자도 학생의 최종 비밀번호를 모른다.
    must_change_password = models.BooleanField("비밀번호 변경 필요", default=False)

    class Meta(AbstractUser.Meta):
        constraints = [
            # 같은 학번으로 두 계정 금지(다중 계정 방지). 빈 값('')은 여러 명이어도 된다.
            models.UniqueConstraint(
                fields=["student_id"], condition=~models.Q(student_id=""), name="uniq_student_id_when_set",
            ),
        ]

    def get_full_name(self):
        return self.real_name

    def get_short_name(self):
        return self.real_name


class FailedAttempt(models.Model):
    """로그인·참여 코드 실패 기록. 최근 10분 실패 횟수로 대입 공격을 막는다(보안 C)."""

    class Kind(models.TextChoices):
        LOGIN = "login", "로그인"
        JOIN_CODE = "join_code", "참여 코드"

    kind = models.CharField("종류", max_length=20, choices=Kind.choices)
    key = models.CharField("대상(IP 또는 로그인ID)", max_length=150)
    created_at = models.DateTimeField("시각", auto_now_add=True)

    class Meta:
        # "이 대상의 최근 실패 수"를 빨리 세기 위한 색인
        indexes = [models.Index(fields=["kind", "key", "created_at"])]

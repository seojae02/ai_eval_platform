import re

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError

from .models import User


class NoColonMixin:
    """라벨 뒤 쌍점(':')을 붙이지 않는다(화면 문구 스타일). 모든 입력 폼이 쓴다."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)


class LoginForm(NoColonMixin, AuthenticationForm):
    # Django 기본 문구는 두 문장이라 한 문장으로 바꾼다
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "아이디나 비밀번호가 맞지 않습니다(대문자와 소문자를 구별합니다)",
    }


class SignupForm(NoColonMixin, forms.Form):
    """회원가입 폼.

    여기 적힌 6칸이 화이트리스트다. 요청에 is_admin2 같은 다른 칸이 섞여 와도
    cleaned_data에 들어오지 않는다(mass assignment 방어).
    """

    username = forms.CharField(
        label="로그인ID",
        max_length=150,
        validators=[UnicodeUsernameValidator()],
        help_text="영문, 숫자, 한글과 @ . + - _ 만 쓸 수 있습니다",
    )
    real_name = forms.CharField(label="실명", max_length=50)
    student_id = forms.CharField(label="학번", max_length=20)
    password1 = forms.CharField(
        label="비밀번호",
        strip=False,  # 비밀번호 앞뒤 공백도 그대로 비밀번호의 일부로 본다
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="10자 이상, 흔한 비밀번호나 숫자로만 된 비밀번호, 로그인ID나 실명과 비슷한 비밀번호는 쓸 수 없습니다",
    )
    password2 = forms.CharField(
        label="비밀번호 확인",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    join_code = forms.CharField(label="참여 코드", max_length=20, help_text="예: K7QM-3XPA")

    def clean_username(self):
        username = self.cleaned_data["username"]
        # 대소문자만 다른 아이디(mock_stu01 / MOCK_STU01)는 사칭에 쓰일 수 있어 중복으로 본다.
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("이미 사용 중인 로그인ID입니다.")
        return username

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        digits = settings.STUDENT_ID_DIGITS
        self.fields["student_id"].help_text = f"숫자 {digits}자리. 관리자 화면과 성적표에만 보이고 다른 참여자에게는 보이지 않습니다"
        self.fields["student_id"].widget.attrs.update({"inputmode": "numeric", "autocomplete": "off"})

    def clean_student_id(self):
        student_id = self.cleaned_data["student_id"]          # CharField가 앞뒤 공백은 이미 지웠다
        digits = settings.STUDENT_ID_DIGITS
        # \d 대신 [0-9]: \d는 '２０２３'(전각)·아랍 숫자도 통과시킨다
        if not re.fullmatch(rf"[0-9]{{{digits}}}", student_id):
            raise ValidationError(f"학번은 숫자 {digits}자리로 써 주세요.")
        if User.objects.filter(student_id=student_id).exists():
            raise ValidationError("이미 가입한 학번입니다. 계정이 기억나지 않으면 대회 관리자에게 문의하세요.")
        return student_id

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2:
            if p1 != p2:
                self.add_error("password2", "비밀번호가 서로 다릅니다.")
            else:
                # 아직 저장하지 않은 '후보' 사용자로 규칙을 검사한다(로그인ID·실명과 비슷한지 보기 위해).
                candidate = User(username=cleaned.get("username", ""), real_name=cleaned.get("real_name", ""),
                                 student_id=cleaned.get("student_id", ""))
                try:
                    validate_password(p1, user=candidate)
                except ValidationError as e:
                    self.add_error("password1", e)
        return cleaned


class JoinForm(NoColonMixin, forms.Form):
    """기존 회원이 참여 코드로 대회를 추가하는 폼."""

    join_code = forms.CharField(label="참여 코드", max_length=20, help_text="예: K7QM-3XPA")


class OwnPasswordChangeForm(NoColonMixin, PasswordChangeForm):
    """내 비밀번호 변경. 현재 비밀번호 확인 + 가입 때와 같은 비밀번호 규칙(10자 이상 등)은 Django 기본."""

"""관리자2 계정 생성 명령.

    docker compose exec web python manage.py create_admin2

웹에는 관리자2를 만드는 화면이 없다. 서버 터미널에 접근할 수 있는 사람만 만들 수 있다.
생성 기록은 감사 로그에 남는다(수행자 비어 있음 = 서버 명령).
"""
import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from audit.models import AuditLog
from audit.services import record


class Command(BaseCommand):
    help = "관리자2 계정을 만든다(비밀번호는 화면에 보이지 않게 입력받는다)"

    def add_arguments(self, parser):
        # 로그인ID·실명은 인자로 줄 수 있다. 비밀번호는 인자로 받지 않는다
        # (셸 기록과 프로세스 목록에 남기 때문).
        parser.add_argument("--username")
        parser.add_argument("--real-name")

    def handle(self, *args, **options):
        username = (options["username"] or input("로그인ID: ")).strip()
        real_name = (options["real_name"] or input("실명: ")).strip()
        if not username or not real_name:
            raise CommandError("로그인ID와 실명은 비울 수 없습니다.")

        # 이미 있는 계정을 관리자2로 '승격'하지 않는다(참여자 계정을 몰래 바꾸는 것 방지).
        if User.objects.filter(username__iexact=username).exists():
            raise CommandError("이미 있는 로그인ID입니다(기존 계정은 관리자2로 바꾸지 않습니다)")

        password = getpass.getpass("비밀번호: ")
        if password != getpass.getpass("비밀번호 확인: "):
            raise CommandError("비밀번호가 서로 다릅니다.")

        # 가입 화면과 같은 비밀번호 규칙을 적용한다.
        try:
            validate_password(password, user=User(username=username, real_name=real_name))
        except ValidationError as e:
            raise CommandError(" ".join(e.messages)) from None

        with transaction.atomic():
            user = User.objects.create_user(
                username=username, password=password, real_name=real_name, is_admin2=True
            )
            record(None, AuditLog.Action.ADMIN2_CREATED, target_user=user, detail={"via": "manage.py"})
        self.stdout.write(self.style.SUCCESS(f"관리자2 계정을 만들었습니다: {username}"))

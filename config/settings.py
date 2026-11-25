import os
from pathlib import Path

from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent

# ── 비밀 정보: 코드에 쓰지 않고 환경변수에서만 읽는다 ──────────────
# 값이 없으면 기본값으로 넘어가지 않고 바로 실패한다(빈 비밀키로 서버가 뜨는 사고 방지).
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

# 기본값은 False. .env에 True라고 적은 개발 환경에서만 켜진다.
DEBUG = os.environ.get("DJANGO_DEBUG", "False") == "True"

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

# ── 앱과 미들웨어 ──────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",   # 폼 기본 템플릿을 우리 templates/에서 덮어쓰기 위해(FORM_RENDERER)
    # 우리 앱
    "accounts",
    "competitions",
    "audit",
    "submissions",
    "jobs",
    "verification",
]

# 사용자 모델을 직접 정의한 것으로 지정 (첫 migrate 전에 정해야 함)
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",          # 보안 헤더
    "django.middleware.csp.ContentSecurityPolicyMiddleware",  # CSP 헤더 (아래 SECURE_CSP)
    "django.contrib.sessions.middleware.SessionMiddleware",   # 세션
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",              # CSRF 검사
    "django.contrib.auth.middleware.AuthenticationMiddleware",  # request.user
    "accounts.middleware.ForcePasswordChangeMiddleware",       # 임시 비밀번호 → 변경 전에는 다른 화면 금지
    "accounts.middleware.NoStoreForLoggedInMiddleware",        # 로그인 후 화면은 브라우저에 저장 안 함(공용 PC)
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",  # 클릭재킹 방지
]

# 운영(DEBUG=False)에서만: collectstatic한 정적 파일을 whitenoise가 내준다. 개발 중에는 runserver가 내준다
if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"
# 폼 오류 목록을 templates/django/forms/errors/list/ul.html로 그린다(온점 정리 필터 적용)
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ── DB ─────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": "5432",
    }
}

# ── 비밀번호 (보안 B) ──────────────────────────────────────────
# 맨 앞의 해셔로 저장한다. 뒤의 PBKDF2는 혹시 남아 있을 옛 해시를 읽기 위한 것.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# 학번 자릿수(가입 폼 검사). 학교 학번 형식이 다르면 이 값만 바꾼다. 예: 8 → 20230001
STUDENT_ID_DIGITS = int(os.environ.get("STUDENT_ID_DIGITS", 8))

AUTH_PASSWORD_VALIDATORS = [
    # 로그인ID·실명과 비슷한 비밀번호 금지 (예: 로그인ID mock_stu01 → 비밀번호 mock_stu01!! 거부)
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
     "OPTIONS": {"user_attributes": ("username", "real_name", "student_id")}},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── 세션·쿠키 (보안 B) ─────────────────────────────────────────
SESSION_COOKIE_HTTPONLY = True     # 자바스크립트가 세션 쿠키를 못 읽음
SESSION_COOKIE_SAMESITE = "Lax"    # 다른 사이트발 요청에 쿠키 동봉 제한
SESSION_COOKIE_AGE = 60 * 60 * 8   # 8시간 후 만료 (기본 2주)
# https에서만 쿠키 전송. HTTPS로 운영할 때(DJANGO_HTTPS=True, 아래) 켠다.
# DEBUG만 끄고 http로 시연하면 Secure 쿠키를 브라우저가 돌려보내지 않아 로그인(CSRF)이 실패하므로 DEBUG와 묶지 않는다.
HTTPS = os.environ.get("DJANGO_HTTPS", "False") == "True"
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS

# ── 시간 ───────────────────────────────────────────────────────
# 마감 판정(R1)은 시각 비교가 핵심이므로 시간대 정보가 붙은 시각(aware)만 쓴다.
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"   # collectstatic 결과(운영). git에서 제외

# ── 브라우저 보안 헤더 (보안 B) ─────────────────────────────────
# CSP: 우리 서버의 스크립트·스타일만. 인라인 스크립트, 외부 CDN, eval 금지.
# 템플릿에 인라인 스크립트·style 속성이 하나도 없어서 가장 엄격하게 걸 수 있다.
# 노트북 그림(PNG·JPEG)만 data: 주소를 허용한다(verification/notebook_view.py).
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],     # upload.js의 조각 전송(fetch)은 같은 서버로만
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],  # 다른 사이트가 이 화면을 iframe에 넣지 못하게(클릭재킹)
}
X_FRAME_OPTIONS = "DENY"

# HTTPS 운영(앞단 프록시가 TLS를 처리할 때) — DJANGO_HTTPS=True로 켠다. 로컬 http 시연에서는 끈다.
# 켜면 `manage.py check --deploy` 경고가 0개가 된다.
if HTTPS:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")   # 앞단 프록시가 붙이는 헤더만 믿는다
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── 업로드 파일 저장소 ─────────────────────────────────────────
# 어떤 URL에도 연결하지 않는다. 파일은 권한 검사를 하는 뷰를 통해서만 내려준다(R9).
STORAGE_ROOT = Path(os.environ.get("STORAGE_ROOT", BASE_DIR / "storage"))

# ── 제출 ───────────────────────────────────────────────────────
# D3: 이 시간 동안 파일 조각이 하나도 오지 않으면 업로드를 만료시킨다(기본 10분).
SUBMISSION_IDLE_SECONDS = int(os.environ.get("SUBMISSION_IDLE_SECONDS", 600))
# 조각(1MB) 요청 본문 상한은 Django 기본값 DATA_UPLOAD_MAX_MEMORY_SIZE(2.5MB)가 막는다.

# ── worker (작업 큐, D2) ──────────────────────────────────────
WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", 2))   # 작업이 없을 때 쉬는 시간
JOB_MAX_ATTEMPTS = 3          # 이만큼 실패하면 제출을 '시스템 오류'로(R4: 미차감)
JOB_STALE_SECONDS = 900       # '실행 중'으로 15분 넘게 끝나지 않으면 worker가 죽은 것으로 본다(검증 = 샌드박스 2회, 최대 약 8분)

# ── 샌드박스·검증 (V1~V5, docs/ARCHITECTURE.md 5·6절) ─────────────────────────
# worker에만 설정된다(compose). web에는 이 값이 없어 샌드박스를 띄울 수 없다
SANDBOX_DOCKER_URL = os.environ.get("SANDBOX_DOCKER_URL", "")
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "ai-eval-sandbox")
SANDBOX_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", 240))           # 컨테이너 하나의 전체 실행 시간(초)
SANDBOX_CELL_TIMEOUT = int(os.environ.get("SANDBOX_CELL_TIMEOUT", 120))  # 노트북 셀 하나의 실행 시간(초)
SANDBOX_MEMORY = os.environ.get("SANDBOX_MEMORY", "2g")
SANDBOX_TMPFS_SIZE = "512m"    # 노트북이 쓸 수 있는 유일한 공간(/tmp)의 크기
SANDBOX_PIDS_LIMIT = 256       # 프로세스 수 (fork 폭탄 방지)
# V1·V5 일치율 기준: 이상이면 '일치', 그 아래 WARN 이상이면 '주의', 더 낮으면 '불일치'
VERIFY_MATCH_OK = 0.99
VERIFY_MATCH_WARN = 0.90
# V6 제출 CSV 수정 흔적: 노트북 재실행과 모델 예측이 서로 같은데 제출 CSV만 다를 때,
# 다른 행이 MIN_ROWS개 이상이고 그중 TOWARD 비율 이상에서 제출 값이 정답에 더 가까우면 '의심'(추가 검토), 그 밖의 차이는 '주의'
VERIFY_EDIT_MIN_ROWS = 5      # V3의 '오답 5개 미만은 판정하지 않음'과 같은 기준
VERIFY_EDIT_TOWARD = 0.90

# 오류 상세는 화면이 아니라 서버 로그(컨테이너 표준 출력)에만 남긴다
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"jobs": {"handlers": ["console"], "level": "INFO"},
                "verification": {"handlers": ["console"], "level": "INFO"}},
}

# ── 로그인 이동 경로 ───────────────────────────────────────────
LOGIN_URL = "login"            # 로그인이 필요한 화면에 그냥 들어오면 여기로
LOGIN_REDIRECT_URL = "home"    # 로그인 성공 후
LOGOUT_REDIRECT_URL = "login"  # 로그아웃 후

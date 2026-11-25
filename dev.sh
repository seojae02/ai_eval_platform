#!/usr/bin/env bash
# 개발용 실행 도우미 (맥·WSL 공통). 사용법: ./dev.sh help
#
# 이 스크립트는 docker compose 명령을 짧게 묶은 것뿐이다. 하는 일은 모두 아래 함수에 그대로 보인다.
# 비밀값(.env)은 무작위로 만들어 파일에만 쓰고 화면에 출력하지 않는다.
set -euo pipefail
cd "$(dirname "$0")"

DC="docker compose"
URL="http://127.0.0.1:8000"

say() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

# 길이가 대략 $1인 무작위 문자열 (python3이 없으면 openssl)
rand() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets; print(secrets.token_urlsafe($1))"
  else
    openssl rand -base64 $(( $1 * 2 )) | tr -dc 'A-Za-z0-9' | head -c "$(( $1 + 10 ))"; echo
  fi
}

# .env의 KEY= 줄을 VALUE로 바꾼다(없으면 추가). 값은 출력하지 않는다.
set_env() {
  local tmp; tmp=$(mktemp)
  awk -v k="$1" -v v="$2" -F= '$1==k {print k "=" v; found=1; next} {print} END {if (!found) print k "=" v}' .env > "$tmp"
  cat "$tmp" > .env && rm -f "$tmp"
  chmod 600 .env
}

confirm() {   # --yes 를 주면 묻지 않는다
  [ "${YES:-0}" = "1" ] && return 0
  read -r -p "$1 [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

make_env() {
  if [ -f .env ]; then
    chmod 600 .env
    echo ".env 있음 (그대로 씀)"
    return
  fi
  cp .env.example .env
  set_env DJANGO_SECRET_KEY "$(rand 50)"
  set_env POSTGRES_PASSWORD "$(rand 24)"
  echo ".env를 새로 만들었습니다 (비밀값은 무작위, 화면에 출력하지 않음)"
}

wait_web() {
  printf "web 준비 기다리는 중"
  for _ in $(seq 1 90); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL/login/" || true)" = "200" ]; then
      echo " → $URL 열림"
      return 0
    fi
    printf "."; sleep 2
  done
  echo
  echo "web이 뜨지 않았습니다. ./dev.sh logs web 으로 원인을 보세요."
  echo "로그에 'password authentication failed'가 있으면 .env와 DB 비밀번호가 다른 것입니다 → ./dev.sh reset"
  return 1
}

cmd_start() {
  say "1/3 .env 확인"; make_env
  say "2/3 이미지 빌드 (처음에는 몇 분 걸림)"; $DC build
  say "3/3 실행 (db · web · worker · docker-proxy)"; $DC up -d
  wait_web
}

cmd_demo() {
  say "예시 대회 불러오기 (이미 있는 것은 건너뜀)"
  $DC exec -T web python manage.py load_demo
}

cmd_reset() {
  echo "이 프로젝트의 DB(Docker 볼륨 db-data)를 지우고 처음부터 다시 만듭니다. 계정·대회·제출 기록이 모두 사라집니다."
  echo "(다른 프로젝트의 컨테이너·볼륨은 건드리지 않습니다)"
  confirm "계속할까요?" || { echo "취소했습니다."; return 1; }
  $DC down -v
  cmd_start
}

cmd_new_secrets() {
  echo ".env의 DJANGO_SECRET_KEY와 POSTGRES_PASSWORD를 새 무작위 값으로 바꿉니다."
  echo "DB 비밀번호가 바뀌므로 이어서 ./dev.sh reset 이 필요합니다(DB 데이터 삭제)."
  confirm "계속할까요?" || { echo "취소했습니다."; return 1; }
  make_env
  set_env DJANGO_SECRET_KEY "$(rand 50)"
  set_env POSTGRES_PASSWORD "$(rand 24)"
  echo "바꿨습니다(값은 출력하지 않음). 다음: ./dev.sh reset"
}

cmd_help() {
  cat <<'EOF'
사용법: ./dev.sh <명령>

  start          처음 실행·평소 실행. .env가 없으면 만들고 → 빌드 → 실행 → 접속 확인
  demo           예시 대회 불러오기(가상 계정 13개, 제출 11건). 비밀번호 = 아이디
  stop           멈추기 (데이터는 남음)
  status         컨테이너 상태
  logs [서비스]   로그 보기 (예: ./dev.sh logs web, ./dev.sh logs worker). Ctrl+C로 나감
  restart        web·worker 다시 시작 (worker는 코드 변경을 자동으로 다시 읽지 않음)
  test [인자]     자동 테스트 (예: ./dev.sh test -k R6)
  test-sandbox   실제 샌드박스 컨테이너로 격리 확인 테스트 (start 후)
  admin2         관리자2 계정 만들기 (아이디·이름·비밀번호를 물어봄)
  shell          Django 셸
  reset [--yes]  DB를 지우고 처음부터 (비밀번호 불일치로 web이 안 뜰 때)
  new-secrets    .env 비밀값 교체 (노출됐을 때). 이어서 reset 필요
  prod           운영 모드로 실행 (gunicorn, DEBUG=False)
EOF
}

[ "${2:-}" = "--yes" ] && YES=1
case "${1:-help}" in
  start)        cmd_start ;;
  demo)         cmd_demo ;;
  stop)         $DC stop ;;
  status)       $DC ps ;;
  logs)         $DC logs -f --tail 50 ${2:-} ;;
  restart)      $DC restart web worker ;;
  test)         shift; $DC run --rm web python -m pytest -q "$@" ;;
  test-sandbox) $DC exec -e RUN_SANDBOX_TESTS=1 worker python -m pytest -q -k 실제 ;;
  admin2)       $DC exec web python manage.py create_admin2 ;;
  shell)        $DC exec web python manage.py shell ;;
  reset)        cmd_reset ;;
  new-secrets)  cmd_new_secrets ;;
  prod)         $DC -f docker-compose.yml -f docker-compose.prod.yml up -d --build ;;
  help|-h|--help) cmd_help ;;
  *)            echo "모르는 명령: $1"; cmd_help; exit 1 ;;
esac

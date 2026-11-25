"""worker 프로세스: python manage.py run_worker

반복: 작업이 있으면 하나 처리 → 없으면 잠깐 쉼. 1분마다 정리 작업.
compose의 worker 서비스가 이 명령으로 뜬다. web과 같은 코드·같은 DB·같은 storage를 쓰지만 외부 요청은 받지 않는다.
"""
import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from jobs import housekeeping, queue

log = logging.getLogger("jobs.worker")
HOUSEKEEPING_EVERY = 60   # 초


class Command(BaseCommand):
    help = "작업 큐(Job 테이블)를 처리하는 worker를 실행한다."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="쌓인 작업을 모두 처리하고 끝낸다(시연·점검용).")

    def handle(self, *args, once=False, **options):
        self.stopping = False
        # docker compose stop → SIGTERM. 지금 하던 작업은 끝내고 멈춘다(작업 중간에 끊기지 않게)
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        self.stdout.write(f"worker 시작 (대기 간격 {settings.WORKER_POLL_SECONDS}초)")
        last_housekeeping = 0.0
        while not self.stopping:
            close_old_connections()   # 오래 쉬는 동안 끊긴 DB 연결을 새로 연다
            if time.monotonic() - last_housekeeping >= HOUSEKEEPING_EVERY:
                self._housekeeping()
                last_housekeeping = time.monotonic()
            try:
                worked = queue.run_one()
            except Exception:
                # 작업 실행 오류는 queue.run이 처리한다. 여기 오는 것은 DB 연결 끊김 같은 일이다 → 쉬었다 다시
                log.exception("작업 가져오기 실패")
                worked = False
            if once and not worked:
                break
            if not worked:
                time.sleep(settings.WORKER_POLL_SECONDS)
        self.stdout.write("worker 종료")

    def _housekeeping(self):
        try:
            result = housekeeping.run_all()
        except Exception:
            log.exception("정리 작업 실패")
            return
        changed = {k: v for k, v in result.items() if v}
        if changed:
            self.stdout.write(f"정리: {changed}")

    def _stop(self, signum, frame):
        self.stopping = True

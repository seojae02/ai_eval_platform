"""샌드박스 컨테이너 한 번 실행하기 (worker 전용). 컨테이너 안쪽 동작은 sandbox/runner.py.

순서: 만들기 → 입력 파일 넣기 → 시작 → 끝날 때까지 기다리기(시간 초과면 강제 종료) → 결과 읽기 → 삭제
Docker에는 docker-socket-proxy를 거쳐서만 닿는다(SANDBOX_DOCKER_URL). 쓰는 API는 /containers 하나다.

제한 (docs/ARCHITECTURE.md 6-1절)
  --network none           외부로 정답·데이터를 보내거나 받아올 수 없다
  --read-only + tmpfs /tmp  시스템 파일 변조 불가, 쓸 수 있는 곳은 크기 제한된 /tmp뿐
  /work (입력)             root 소유 → 참여자 코드는 읽기만
  --memory = --memory-swap  메모리 폭주 방지(스왑으로 새지 않게)
  --cpus 1, --pids-limit    무한 루프·fork 폭탄이 서버를 멈추지 못하게
  --cap-drop ALL, no-new-privileges, uid 1000   권한 상승 방지
  환경변수                  HOME 등 경로 + 이 컨테이너 전용 서명 키(매번 새로 만든 무작위 값). 서버 비밀키·DB 주소 없음 [RFP 7쪽]
  결과 서명                 서명(HMAC)이 맞는 결과 블록만 받는다 → 노트북이 가짜 결과를 써 넣어도 무시된다(runner.py 머리말)
  마운트                    없음. 그 제출의 파일과 공개 데이터만 복사해 넣는다(정답 파일은 넣지 않는다)
"""
import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
import secrets
import tarfile
import time

from django.conf import settings

log = logging.getLogger(__name__)
BEGIN, END = b"-----BEGIN RUNNER RESULT-----", b"-----END RUNNER RESULT-----"
MAX_LOG_BYTES = 120 * 1024 * 1024
POLL_SECONDS = 1.0


class SandboxUnavailable(Exception):
    """샌드박스를 쓸 수 없음(설정 없음, Docker·이미지 없음). 검증은 '판정 불가'로 남긴다."""


def _tar(files):
    """{이름: 바이트} → tar. 소유자 root, 권한 644 → 컨테이너 안의 참여자 코드(uid 1000)는 읽기만 할 수 있다."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o644, 0, 0, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def parse_output(stdout, key):
    """실행기가 보낸 결과 블록 중 서명이 맞는 것을 읽는다. 없으면 None(실행기가 끝까지 못 갔거나 위조만 있음).

    END마다 바로 앞의 BEGIN을 짝으로 본다 → 끝이 없는 가짜 BEGIN이 진짜 블록을 가리지 못한다.
    """
    found, pos = None, 0
    while (end := stdout.find(END, pos)) >= 0:
        pos = end + len(END)
        start = stdout.rfind(BEGIN, 0, end)
        if start < 0:
            continue
        lines = stdout[start + len(BEGIN):end].split()
        if len(lines) < 2 or not lines[-1].startswith(b"mac="):
            continue
        body = b"".join(lines[:-1])
        if not hmac.compare_digest(lines[-1][4:], hmac.new(key, body, hashlib.sha256).hexdigest().encode()):
            log.warning("샌드박스 결과 블록의 서명이 맞지 않아 버렸습니다(위조 시도 가능성).")
            continue
        try:
            payload = json.loads(base64.b64decode(body))
            files = {name: base64.b64decode(data) for name, data in payload.get("files", {}).items()}
            found = (payload["result"], files)
        except (ValueError, KeyError, TypeError, binascii.Error):
            continue
    return found


def _client():
    if not settings.SANDBOX_DOCKER_URL:
        raise SandboxUnavailable("샌드박스가 설정되지 않았습니다(SANDBOX_DOCKER_URL).")
    import docker

    try:
        client = docker.DockerClient(base_url=settings.SANDBOX_DOCKER_URL, timeout=30)
        client.ping()
    except docker.errors.DockerException as e:
        raise SandboxUnavailable(f"Docker에 연결하지 못했습니다: {e}") from None
    return client


def container_options(mode, key):
    return dict(
        image=settings.SANDBOX_IMAGE,
        command=["python", "/opt/runner/runner.py", mode],
        network_mode="none",
        read_only=True,
        tmpfs={"/tmp": f"rw,size={settings.SANDBOX_TMPFS_SIZE},mode=1777"},
        # 이름 없는 볼륨: 이미지의 /work(root 소유 755)를 물려받는다. 컨테이너를 지울 때 함께 지운다(v=True)
        mounts=[{"Target": "/work", "Type": "volume"}],
        mem_limit=settings.SANDBOX_MEMORY,
        memswap_limit=settings.SANDBOX_MEMORY,
        nano_cpus=1_000_000_000,
        pids_limit=settings.SANDBOX_PIDS_LIMIT,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        user="1000:1000",
        environment={"HOME": "/tmp", "JUPYTER_RUNTIME_DIR": "/tmp/jupyter", "IPYTHONDIR": "/tmp/ipython",
                     "MPLCONFIGDIR": "/tmp/matplotlib", "RUNNER_RESULT_KEY": key},
        labels={"ai-eval": "sandbox"},
        detach=True,
    )


def run(mode, files, job, client=None):
    """컨테이너 하나를 돌려 (결과 dict, 결과 파일 dict)를 돌려준다.

    시간 초과·메모리 초과·실행기 비정상 종료도 예외가 아니라 결과(status)로 돌려준다 → 리포트에 '판정 불가'로 남는다.
    """
    import docker

    client = client or _client()
    files = {**files, "job.json": json.dumps(job).encode()}
    key = secrets.token_hex(32)   # 이 컨테이너 한 번만 쓰는 서명 키
    started = time.monotonic()
    try:
        container = client.containers.create(**container_options(mode, key))
    except docker.errors.ImageNotFound:
        raise SandboxUnavailable(f"샌드박스 이미지 '{settings.SANDBOX_IMAGE}'가 없습니다(docker compose build).") from None
    except docker.errors.DockerException as e:
        raise SandboxUnavailable(f"샌드박스 컨테이너를 만들지 못했습니다: {e}") from None
    try:
        container.put_archive("/work", _tar(files))
        container.start()
        # 긴 연결 하나로 기다리지 않고 1초마다 상태를 본다(프록시·네트워크 시간 제한에 걸리지 않게)
        deadline = started + settings.SANDBOX_TIMEOUT
        while True:
            container.reload()
            if container.status in ("exited", "dead"):
                break
            if time.monotonic() > deadline:
                container.kill()
                return {"status": "timeout", "message": f"전체 실행 시간 {settings.SANDBOX_TIMEOUT}초를 넘겨 강제 종료했습니다."}, {}
            time.sleep(POLL_SECONDS)
        state = container.attrs.get("State", {})
        if state.get("OOMKilled"):
            return {"status": "oom", "message": f"메모리 제한({settings.SANDBOX_MEMORY})을 넘겨 강제 종료되었습니다."}, {}
        stdout = container.logs(stdout=True, stderr=False)[:MAX_LOG_BYTES]
        parsed = parse_output(stdout, key.encode())
        if parsed is None:
            return {"status": "error", "message": f"실행기가 결과를 남기지 못했습니다(종료 코드 {state.get('ExitCode')})."}, {}
        result, out_files = parsed
        result["wall_seconds"] = round(time.monotonic() - started, 1)
        return result, out_files
    finally:
        try:
            container.remove(force=True, v=True)
        except docker.errors.DockerException:
            log.exception("샌드박스 컨테이너 삭제 실패: %s", container.id)

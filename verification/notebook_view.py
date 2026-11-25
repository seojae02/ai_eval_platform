"""노트북을 관리자 화면에 안전하게 보여 주기 위한 변환 [RFP 7쪽 "노트북 HTML 렌더링 시 스크립트 실행 방지"].

방침: HTML을 '걸러서' 그리지 않고, 처음부터 그리지 않는다.
  - 코드·마크다운: 글자 그대로(템플릿 자동 이스케이프) → <script>가 있어도 글자로 보인다
  - 출력: text/plain, stream(print), 오류 메시지만 글자로. PNG·JPEG 그림만 이미지로
  - text/html, javascript, 위젯, SVG(스크립트를 품을 수 있음)는 그리지 않고 "표시하지 않음"으로 알린다
  - 그 위에 CSP(script-src 'self')가 한 번 더 막는다
"""
import base64
import binascii
import re

MAX_TEXT = 20_000             # 출력 하나당 글자 수 상한(화면이 멈추지 않게)
MAX_IMAGE_BYTES = 2 * 1024 * 1024
SAFE_IMAGES = ("image/png", "image/jpeg")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")   # 오류 메시지의 터미널 색 코드


def _text(value):
    text = "".join(value) if isinstance(value, list) else str(value or "")
    text = _ANSI.sub("", text)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n… (너무 길어 생략)"


def _image(mime, data):
    data = "".join(data) if isinstance(data, list) else str(data)
    data = "".join(data.split())
    try:
        raw = base64.b64decode(data, validate=True)   # 올바른 base64가 아니면 그리지 않는다
    except (binascii.Error, ValueError):
        return None
    if len(raw) > MAX_IMAGE_BYTES:
        return None
    # 앞 바이트로 실제 형식 확인(이름만 png인 다른 파일 방지)
    if (mime == "image/png" and not raw.startswith(b"\x89PNG")) or (mime == "image/jpeg" and not raw.startswith(b"\xff\xd8")):
        return None
    return {"kind": "image", "src": f"data:{mime};base64,{data}"}


def _output(out):
    kind = out.get("output_type")
    if kind == "stream":
        return {"kind": "text", "text": _text(out.get("text"))}
    if kind == "error":
        return {"kind": "error", "text": _text("\n".join(out.get("traceback") or [f"{out.get('ename')}: {out.get('evalue')}"]))}
    if kind in ("execute_result", "display_data"):
        data = out.get("data", {})
        for mime in SAFE_IMAGES:
            if mime in data:
                image = _image(mime, data[mime])
                if image:
                    return image
        if "text/plain" in data:
            return {"kind": "text", "text": _text(data["text/plain"])}
        return {"kind": "skipped", "text": f"표시하지 않는 출력 형식: {', '.join(sorted(data))[:100]}"}
    return {"kind": "skipped", "text": "알 수 없는 출력"}


def cells(nb):
    """노트북(dict) → 템플릿이 그릴 칸 목록. 템플릿은 글자를 자동 이스케이프한다."""
    result = []
    for c in nb.get("cells", []):
        ctype = c.get("cell_type")
        result.append({
            "type": ctype,
            "count": c.get("execution_count"),
            "source": _text(c.get("source")),
            "outputs": [_output(o) for o in c.get("outputs", [])] if ctype == "code" else [],
        })
    return result

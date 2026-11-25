"""화면 표시용 필터. {% load ui %}"""
import re

from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

register = template.Library()

# 문장 경계: 숫자·공백이 아닌 글자 뒤의 온점 + 공백(0.95 같은 숫자, 말줄임표는 건드리지 않음)
_SENTENCE_END = re.compile(r"(?<=[^\d\s.])\.\s+(?=\S)")


@register.filter
def nodot(text):
    """화면 문구에서 온점을 없앤다(화면 문구 스타일).

    - 문장 끝 온점은 뗀다: '접수했습니다.' → '접수했습니다'
    - 문장 사이 온점은 줄바꿈으로: 'A입니다. B하세요.' → 'A입니다<br>B하세요'
    우리 코드의 문구는 처음부터 한 문장으로 쓰고, 이 필터는 Django 기본 문구(비밀번호 규칙 등)와
    예전에 저장된 검증 리포트 문구를 위한 안전망이다. 각 조각은 이스케이프한다(XSS 방지).
    """
    text = "" if text is None else str(text)
    text = text.rstrip()
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    parts = _SENTENCE_END.split(text)
    return mark_safe("<br>".join(conditional_escape(p) for p in parts))

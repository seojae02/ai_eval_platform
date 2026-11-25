"""제출 테스트용 가상 파일과 fixture."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from competitions.models import CompetitionMember, Deadline
from submissions import services

KST = ZoneInfo("Asia/Seoul")

# 가상 예측 CSV (정답 ANSWER_CSV와 같은 id 1~5, 허용 값 0/1)
PRED_CSV = b"id,label\n1,0\n2,1\n3,0\n4,0\n5,1\n"
# 가장 작은 올바른 노트북(코드 셀 1개)
NOTEBOOK = json.dumps({
    "nbformat": 4, "nbformat_minor": 5, "metadata": {},
    "cells": [{"cell_type": "code", "id": "c1", "metadata": {}, "source": "print(1)",
               "outputs": [], "execution_count": None}],
}).encode()
# pickle 프로토콜 4의 머리 바이트 + 가상 내용 (로드하지 않으므로 실제 모델일 필요가 없다)
MODEL = b"\x80\x04\x95" + b"\x00" * 20

GOOD_FILES = {"csv": ("pred.csv", PRED_CSV), "notebook": ("work.ipynb", NOTEBOOK), "model": ("model.pkl", MODEL)}


def kst(month, day, hour, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=KST)


def send_files(submission, files=GOOD_FILES, now=None):
    for kind, (name, data) in files.items():
        services.receive_chunk(submission, kind, 0, name, data, now=now)


@pytest.fixture
def member(student, competition):
    return CompetitionMember.objects.get(user=student, competition=competition)


@pytest.fixture
def deadlines(project_with_files):
    """1차 11/20 17:00 (3회), 2차 11/22 17:00 (5회)."""
    first = Deadline.objects.create(project=project_with_files, due_at=kst(11, 20, 17), max_submissions=3)
    second = Deadline.objects.create(project=project_with_files, due_at=kst(11, 22, 17), max_submissions=5)
    return first, second

"""제출 파일 중 노트북과 모델 파일의 내용 검사.

중요: 여기서는 파일을 '실행'하거나 '로드'하지 않는다.
- 노트북: JSON 구조만 확인한다. 셀 안의 코드는 나중에 샌드박스에서만 실행한다(V1).
- 모델: 앞 몇 바이트(magic bytes)만 본다. pickle 형식(.pkl·.joblib·옛 .pt)은
  pickle.load 하는 순간 파일 안의 코드가 실행되므로 web 서버에서는 절대 열지 않는다(V5는 샌드박스에서).
"""
from pathlib import PurePath

import nbformat

# 이름은 CsvError지만 뜻은 "사용자에게 그대로 보여 줄 수 있는 검사 실패". 제출 검사 전체가 이 예외 하나로 실패를 알린다.
from competitions.csvcheck import CsvError

MB = 1024 * 1024
# 종류별 크기 상한 (결정 3)
MAX_BYTES = {"csv": 20 * MB, "notebook": 20 * MB, "model": 200 * MB}

# 확장자 → 그 형식이면 파일 맨 앞에 와야 하는 바이트들
_PICKLE = (b"\x80\x02", b"\x80\x03", b"\x80\x04", b"\x80\x05")   # pickle 프로토콜 2~5
_ZIP = (b"PK\x03\x04",)                                         # PyTorch 1.6+ .pt, Keras 3 .keras
_ZLIB = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")    # joblib 압축 저장
_HDF5 = (b"\x89HDF\r\n\x1a\n",)                                  # Keras .h5
_ONNX = (b"\x08",)                                               # protobuf 첫 필드(ir_version)
MODEL_MAGIC = {
    ".pkl": _PICKLE,
    ".joblib": _PICKLE + _ZLIB,
    ".pt": _ZIP + _PICKLE,
    ".pth": _ZIP + _PICKLE,
    ".h5": _HDF5,
    ".keras": _ZIP,
    ".onnx": _ONNX,
}
HEAD_BYTES = 8


def check_model(original_name, head):
    """원래 파일명의 확장자와 파일 앞부분(head)이 맞는지."""
    ext = PurePath(original_name).suffix.lower()
    if ext not in MODEL_MAGIC:
        raise CsvError(f"모델 파일 형식은 {', '.join(MODEL_MAGIC)} 중 하나여야 합니다.")
    if not head.startswith(MODEL_MAGIC[ext]):
        raise CsvError(f"모델 파일의 내용이 {ext} 형식이 아닙니다(확장자만 바꾼 파일인지 확인하세요).")


def check_notebook(data):
    """Jupyter 공식 스키마(nbformat)로 구조를 검사한다. 코드 셀이 하나도 없으면 거부."""
    try:
        nb = nbformat.reads(data.decode("utf-8"), as_version=4)
        nbformat.validate(nb)
    except UnicodeDecodeError:
        raise CsvError("노트북 파일이 UTF-8 텍스트가 아닙니다.") from None
    except Exception:   # JSON이 아님, 필수 키 누락, 스키마 위반 등 → 이유와 상관없이 '형식 오류'
        raise CsvError("올바른 Jupyter 노트북(.ipynb) 파일이 아닙니다.") from None
    if not any(cell.cell_type == "code" for cell in nb.cells):
        raise CsvError("노트북에 코드 셀이 없습니다.")

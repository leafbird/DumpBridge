"""DumpBridge wire protocol & stream-framing helpers.

이 모듈은 bridge.py 와 client 들이 공유한다. 순수 함수로 분리해
덤프 없이도 단위 테스트할 수 있게 했다.

두 가지 프레이밍이 있다:

1. dotnet-dump stdout 프레이밍 (마커 기반)
   dotnet-dump 는 명령(및 초기 배너)마다 끝에 `<END_COMMAND_OUTPUT>` 를 출력한다.
   `extract_nth_block` 으로 마커 정렬을 유지한다 — 파일 끝으로 seek 하지 않고
   "지금까지 소비한 마커 위치"만 전진시키므로, 한 명령이 타임아웃돼도
   스트림이 어긋나지 않는다 (개선 A).

2. TCP 응답 프레이밍 (길이 프리픽스)
   `MAGIC + 8바이트 길이 + payload`. client 가 길이만큼 정확히 수신하므로
   중간 연결 끊김을 "정상 완료"로 오인하지 않는다 (개선 C).
"""

import struct

END_MARKER = "<END_COMMAND_OUTPUT>"
END_MARKER_BYTES = END_MARKER.encode("ascii")

# --- 1. dotnet-dump 마커 정렬 -------------------------------------------------


def extract_nth_block(buf: bytes, n: int, marker: bytes = END_MARKER_BYTES):
    """`buf` 안에서 n번째 마커까지를 찾는다.

    Returns (found, block_bytes, end_offset):
      - found: n개의 마커를 모두 찾았는지
      - block_bytes: (n-1)번째 마커 직후 ~ n번째 마커 직전 사이의 바이트
                     (= 마지막/현재 명령의 출력). 앞쪽 블록들은 버려진다.
      - end_offset: n번째 마커 끝의 바이트 오프셋 (다음 읽기 시작점)

    n>=2 인 경우(타임아웃 백로그 드레인) 중간 블록은 폐기하고
    마지막 블록만 반환한다.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    positions = []
    s = 0
    mlen = len(marker)
    while len(positions) < n:
        p = buf.find(marker, s)
        if p < 0:
            return False, b"", 0
        positions.append(p)
        s = p + mlen
    nth = positions[n - 1]
    block_start = positions[n - 2] + mlen if n >= 2 else 0
    return True, buf[block_start:nth], nth + mlen


def decode_block(block: bytes) -> str:
    """블록 전체를 한 번에 디코드한다 (청크 경계 오염 방지, 개선 D).

    정상 UTF-8 이면 strict, 깨졌으면 replace 로 폴백하되
    호출측이 알 수 있도록 그대로 둔다."""
    try:
        return block.decode("utf-8")
    except UnicodeDecodeError:
        return block.decode("utf-8", errors="replace")


# --- 2. TCP 길이 프레이밍 -----------------------------------------------------

MAGIC = b"DBRG"
_HEADER = struct.Struct(">4sQ")  # magic + uint64 length


def encode_frame(payload: bytes) -> bytes:
    return _HEADER.pack(MAGIC, len(payload)) + payload


def recv_exact(recv_fn, n: int) -> bytes:
    """recv_fn(k) 를 반복 호출해 정확히 n바이트를 모은다.
    연결이 일찍 닫히면 TruncatedFrame 을 던진다."""
    buf = b""
    while len(buf) < n:
        chunk = recv_fn(n - len(buf))
        if not chunk:
            raise TruncatedFrame(
                f"connection closed after {len(buf)} of {n} expected bytes"
            )
        buf += chunk
    return buf


class TruncatedFrame(Exception):
    """프레임을 끝까지 받지 못함 = 응답이 절단됨."""


def recv_frame(recv_fn) -> bytes:
    """길이 프리픽스 프레임 하나를 수신해 payload 를 반환한다."""
    header = recv_exact(recv_fn, _HEADER.size)
    magic, length = _HEADER.unpack(header)
    if magic != MAGIC:
        raise ValueError(f"bad frame magic: {magic!r} (expected {MAGIC!r})")
    return recv_exact(recv_fn, length)

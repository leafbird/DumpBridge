"""DumpBridge 단위 테스트 — 덤프 없이 순수 로직을 검증한다.

실행: python -m unittest test_dumpbridge   (또는 python test_dumpbridge.py)
"""

import unittest

from protocol import (
    END_MARKER_BYTES,
    extract_nth_block,
    decode_block,
    encode_frame,
    recv_frame,
    recv_exact,
    TruncatedFrame,
    MAGIC,
)
from analyzers import HeapAnalyzer

M = END_MARKER_BYTES


class TestMarkerAlignment(unittest.TestCase):
    def test_single_block(self):
        buf = b"banner stuff" + M + b"\r\n"
        found, block, end = extract_nth_block(buf, 1)
        self.assertTrue(found)
        self.assertEqual(block, b"banner stuff")
        self.assertEqual(end, len(b"banner stuff") + len(M))

    def test_second_block_is_returned_when_draining(self):
        # n=2: 첫 블록(타임아웃으로 버려진 명령)은 폐기, 두 번째(현재 명령)만 반환
        buf = b"abandoned-output" + M + b"\r\n> realcmd\r\nreal-output" + M + b"\r\n"
        found, block, end = extract_nth_block(buf, 2)
        self.assertTrue(found)
        self.assertIn(b"real-output", block)
        self.assertNotIn(b"abandoned-output", block)

    def test_not_enough_markers(self):
        buf = b"only one" + M + b" then partial no marker"
        found, block, end = extract_nth_block(buf, 2)
        self.assertFalse(found)
        self.assertEqual(block, b"")

    def test_partial_marker_not_matched(self):
        buf = b"output <END_COMMAND_OUTP"  # 마커가 잘림
        found, _, _ = extract_nth_block(buf, 1)
        self.assertFalse(found)

    def test_end_offset_advances_past_marker(self):
        b1 = b"first" + M + b"second" + M
        found, block, end = extract_nth_block(b1, 1)
        # 다음 읽기는 end 부터 → 두 번째 블록만 보임
        found2, block2, _ = extract_nth_block(b1[end:], 1)
        self.assertEqual(block, b"first")
        self.assertEqual(block2, b"second")


class TestDecodeBlock(unittest.TestCase):
    def test_clean_utf8(self):
        self.assertEqual(decode_block("한글 ok".encode("utf-8")), "한글 ok")

    def test_invalid_falls_back(self):
        # 잘린 멀티바이트 — replace 폴백, 예외 없음
        out = decode_block("한".encode("utf-8")[:-1])
        self.assertIsInstance(out, str)


class TestFraming(unittest.TestCase):
    def _chunked_recv(self, data: bytes, chunk: int):
        """data 를 chunk 크기로 잘라 내주는 recv 흉내. (경계 분할 테스트)"""
        state = {"pos": 0}

        def recv(n):
            if state["pos"] >= len(data):
                return b""
            take = min(n, chunk, len(data) - state["pos"])
            out = data[state["pos"]:state["pos"] + take]
            state["pos"] += take
            return out

        return recv

    def test_roundtrip(self):
        payload = b"hello world" * 1000
        frame = encode_frame(payload)
        got = recv_frame(self._chunked_recv(frame, chunk=7))
        self.assertEqual(got, payload)

    def test_empty_payload(self):
        frame = encode_frame(b"")
        self.assertEqual(recv_frame(self._chunked_recv(frame, 4)), b"")

    def test_truncated_payload_raises(self):
        frame = encode_frame(b"abcdefghij")
        truncated = frame[:-3]  # payload 끝 3바이트 유실 (연결 조기 종료 흉내)
        with self.assertRaises(TruncatedFrame):
            recv_frame(self._chunked_recv(truncated, 4))

    def test_bad_magic_raises(self):
        import struct
        bad = struct.pack(">4sQ", b"XXXX", 3) + b"abc"
        with self.assertRaises(ValueError):
            recv_frame(self._chunked_recv(bad, 4))


# 실제 dotnet-dump dumpheap -stat 출력 형태 (정상)
CLEAN_STAT = """\
Statistics:
          MT    Count    TotalSize Class Name
7ffd16175b10      100         4800 System.String
7ffd1609f160       50         2000 Mad.Foo.Bar
7ffd16175b11        2          160 Mad.Core.Utility.Time.TimingWheel+ReservedTask
Total 152 objects, 6,960 bytes
"""

# 어제 오염 캡처 재현: 같은 타입이 반복 + 주소가 count/size 칸에 (합이 Total 초과)
CORRUPT_STAT = """\
Statistics:
          MT    Count    TotalSize Class Name
029954900048   556984  29954988000 Mad.Core.Utility.Time.TimingWheel+ReservedTask
0299604f0db8   672704  29960595178 Mad.Core.Utility.Time.TimingWheel+ReservedTask
029965400028  2397144  29965649400 Mad.Core.Utility.Time.TimingWheel+ReservedTask
Total 152 objects, 6,960 bytes
"""


class TestHeapCorruptionGuard(unittest.TestCase):
    def test_clean_capture_passes(self):
        h = HeapAnalyzer()
        out = h.query(["--sort", "size", "--desc"], lambda cmd: CLEAN_STAT)
        self.assertNotIn("CORRUPT", out)
        self.assertIn("System.String", out)
        self.assertIsNone(h._corrupt)

    def test_corrupt_capture_is_rejected(self):
        h = HeapAnalyzer()
        out = h.query([], lambda cmd: CORRUPT_STAT)
        self.assertIn("CORRUPT", out)
        # 단일 타입 size 가 Total bytes 초과를 사유로 잡아야 함
        self.assertIsNotNone(h._corrupt)

    def test_single_type_count_exceeds_total(self):
        h = HeapAnalyzer()
        reason = h._detect_corruption(
            [{"name": "T", "count": 400_000_000, "size": 10, "mt": "x"}],
            total_count=187_000_000, total_size=1_000_000_000,
        )
        self.assertIsNotNone(reason)
        self.assertIn("exceeds heap Total", reason)

    def test_missing_total_flagged(self):
        h = HeapAnalyzer()
        reason = h._detect_corruption(
            [{"name": "T", "count": 1, "size": 1, "mt": "x"}],
            total_count=0, total_size=0,
        )
        self.assertIsNotNone(reason)

    def test_sum_exceeds_total(self):
        h = HeapAnalyzer()
        # 개별은 Total 미만이지만 합이 초과
        entries = [{"name": f"T{i}", "count": 60, "size": 60, "mt": "x"} for i in range(3)]
        reason = h._detect_corruption(entries, total_count=100, total_size=100)
        self.assertIsNotNone(reason)
        self.assertIn("sum", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)

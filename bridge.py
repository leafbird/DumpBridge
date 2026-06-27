import subprocess
import threading
import logging
import socket
import argparse
import tempfile
import time
import os
import shlex
from analyzers import page_output, HeapAnalyzer, StackAnalyzer
from protocol import (
    END_MARKER,
    END_MARKER_BYTES,
    extract_nth_block,
    decode_block,
    encode_frame,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dumpbridge")

DEFAULT_COMMAND_TIMEOUT = 1800  # 초. 풀 dumpheap -stat / 첫 gcroot 는 수~십 분 걸린다.
DEFAULT_READY_TIMEOUT = 300     # 초. 덤프 로딩 + 초기 배너.


class CommandRouter:
    """Routes @ smart commands to appropriate handlers."""

    _HELP = """\
DumpBridge Smart Commands
=========================
Any command without @ prefix is passed directly to dotnet-dump.

@help
  Show this help message.

@page [--offset=N] [--limit=N] <command>
  Line-based paging for any dotnet-dump command output.
  Defaults: offset=0, limit=50

@heap-stats [--sort=count|size|name] [--desc] [--offset=N] [--limit=N] [--filter=PATTERN] [--refresh]
  Cached dumpheap -stat with sort/filter/paging. First call runs dumpheap -stat
  and caches the result. Subsequent calls use cache (instant).
  --filter uses regex (case-insensitive) on class name.
  --refresh forces re-execution and cache update.
  Defaults: offset=0, limit=50

@stack-groups [--max-frames=N] [--limit=N]
  Groups threads by identical call stack from clrstack -all.
  --max-frames: only compare top N frames (0=all).
  --limit: show top N groups (0=all).
  Sorted by thread count descending.

EXIT
  Shut down the DumpBridge server."""

    def __init__(self):
        self._heap = HeapAnalyzer()
        self._stack = StackAnalyzer()

    def handle(self, command: str, execute_fn) -> str:
        try:
            parts = shlex.split(command)
        except ValueError as e:
            return f"[ERROR] Invalid command syntax: {e}"

        if not parts:
            return "[ERROR] Empty command."

        cmd_name = parts[0]
        cmd_args = parts[1:]

        try:
            if cmd_name == "@help":
                return self._HELP
            elif cmd_name == "@page":
                return page_output(cmd_args, execute_fn)
            elif cmd_name == "@heap-stats":
                return self._heap.query(cmd_args, execute_fn)
            elif cmd_name == "@stack-groups":
                return self._stack.query(cmd_args, execute_fn)
            else:
                return f"[ERROR] Unknown command: {cmd_name}\nRun @help for available commands."
        except Exception as e:
            return f"[ERROR] Command failed: {e}"


class DumpSession:
    """dotnet-dump analyze 프로세스를 관리하는 클래스.

    Windows에서 파이프 버퍼링 문제를 회피하기 위해 stdout을 임시 파일로
    리다이렉트하고, 파일 폴링으로 출력을 수집한다.
    완료 감지는 dotnet-dump가 명령마다 출력하는 <END_COMMAND_OUTPUT> 마커를 사용한다.

    스트림 동기화(개선 A)
    --------------------
    dotnet-dump 는 싱글스레드 REPL 이라 명령을 받은 순서대로 처리하고,
    각 명령(및 초기 배너)의 끝에 정확히 하나의 마커를 출력한다. 따라서
    "지금까지 소비한 마커 직후"(`_read_pos`)에서부터 다음 마커까지를 읽으면
    명령과 출력이 1:1 로 정렬된다.

    예전 구현은 매 명령마다 파일 끝으로 seek 했는데, 직전 명령이 타임아웃으로
    아직 출력 중이면 그 한가운데로 점프해 이후 모든 출력이 어긋났다(= 어제
    dumpheap -stat 오염의 원인). 지금은 끝으로 seek 하지 않고 `_outstanding`
    (아직 마커를 못 본 명령 수)을 추적한다. 한 명령이 타임아웃해도 다음 명령이
    백로그 마커를 드레인하며 자기 블록만 반환하므로 영구히 동기 상태를 유지한다.
    """

    def __init__(self, dump_path: str, command_timeout: float = DEFAULT_COMMAND_TIMEOUT):
        self._lock = threading.Lock()
        self._outfile_path = tempfile.mktemp(suffix=".dumpbridge.txt")
        self._outfile = open(self._outfile_path, "w+b")
        self._read_pos = 0          # 소비 완료한 마지막 마커 직후의 바이트 오프셋
        self._outstanding = 0       # stdin 에 보냈으나 아직 마커를 못 본 명령 수
        self._command_timeout = command_timeout

        log.info("Starting dotnet-dump analyze: %s", dump_path)
        self._proc = subprocess.Popen(
            ["dotnet-dump", "analyze", dump_path],
            stdin=subprocess.PIPE,
            stdout=self._outfile,
            stderr=subprocess.STDOUT,
        )

        # 초기 배너도 마커 하나로 끝난다 → 그 블록을 소비해 _read_pos 를 정렬.
        log.info("Waiting for initial prompt...")
        self._outstanding = 1
        ok, _ = self._drain_and_capture(timeout=DEFAULT_READY_TIMEOUT)
        if not ok:
            raise RuntimeError("dotnet-dump did not become ready (no initial marker).")
        log.info("dotnet-dump session ready.")

    def _drain_and_capture(self, timeout: float):
        """`_read_pos` 부터 전진하며 `_outstanding` 개의 마커를 찾는다.

        성공: (True, block_bytes). `_read_pos` 를 마지막 마커 뒤로 전진시키고
              `_outstanding` 을 0 으로 리셋. block 은 마지막(=현재) 명령의 출력.
        실패(타임아웃/프로세스 종료): (False, b""). 상태를 그대로 둔다 →
              다음 호출이 더 큰 _outstanding 으로 재시도하며 자연 복구된다.

        파일 끝으로 seek 하지 않는다. 매 호출 `_read_pos` 부터 다시 읽으므로
        타임아웃으로 남은 미완 출력도 다음에 그대로 이어 읽힌다.
        """
        start = time.time()
        base = self._read_pos
        pending = b""
        while True:
            self._outfile.flush()
            self._outfile.seek(0, 2)
            size = self._outfile.tell()
            avail = size - (base + len(pending))
            if avail > 0:
                self._outfile.seek(base + len(pending))
                pending += self._outfile.read()
                found, block, end_off = extract_nth_block(
                    pending, self._outstanding, END_MARKER_BYTES
                )
                if found:
                    self._read_pos = base + end_off
                    self._outstanding = 0
                    return True, block
            if self._proc.poll() is not None:
                return False, b""
            if time.time() - start >= timeout:
                return False, b""
            time.sleep(0.1)

    def execute(self, command: str) -> str:
        """명령을 실행하고 결과를 반환한다. 스레드 안전."""
        with self._lock:
            if self._proc.poll() is not None:
                return "[ERROR] dotnet-dump process is not running."

            log.info("Executing: %s", command)
            self._proc.stdin.write((command + "\n").encode())
            self._proc.stdin.flush()
            self._outstanding += 1

            ok, block = self._drain_and_capture(self._command_timeout)
            if not ok:
                if self._proc.poll() is not None:
                    return "[ERROR] dotnet-dump process died while executing."
                return (
                    f"[ERROR] Command timed out after {self._command_timeout:g}s. "
                    f"dotnet-dump may still be processing it; {self._outstanding} "
                    f"command(s) now outstanding. The stream stays marker-aligned, so "
                    f"the next command will drain the backlog and return its own output "
                    f"(no corruption). Re-run with a larger --timeout if this command is "
                    f"genuinely slow (full 'dumpheap -stat' on a huge dump can take 10min+)."
                )

            text = decode_block(block)
            return self._parse_output(text, command)

    @staticmethod
    def _parse_output(raw: str, command: str) -> str:
        """에코된 명령, 프롬프트, END_MARKER를 제거하고 순수 출력만 반환."""
        # "> command\r\n출력...\r\n<END_COMMAND_OUTPUT>\r\n" 형태
        # END_MARKER 이후 제거
        idx = raw.find(END_MARKER)
        if idx >= 0:
            raw = raw[:idx]

        # 에코된 명령 줄 제거 (">" 프롬프트 포함)
        lines = raw.split("\n")
        start = 0
        for i, line in enumerate(lines):
            stripped = line.rstrip("\r")
            if stripped == "> " + command or stripped == ">" + command:
                start = i + 1
                break
        result = "\n".join(lines[start:])

        # 앞뒤 공백 정리
        return result.strip("\r\n")

    def close(self):
        """dotnet-dump 프로세스를 종료한다."""
        try:
            if self._proc.poll() is None:
                self._proc.stdin.write(b"exit\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
        try:
            self._outfile.close()
            os.unlink(self._outfile_path)
        except Exception:
            pass
        log.info("dotnet-dump session closed.")


class BridgeServer:
    """TCP 소켓 서버. 클라이언트 명령을 DumpSession에 전달한다."""

    def __init__(self, session: DumpSession, port: int = 9999):
        self._session = session
        self._port = port
        self._server_socket: socket.socket | None = None
        self._running = False
        self._router = CommandRouter()

    def start(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", self._port))
        self._server_socket.listen(1)
        self._server_socket.settimeout(1.0)  # accept 타임아웃 (종료 체크용)
        self._running = True
        log.info("READY - listening on 127.0.0.1:%d", self._port)

        while self._running:
            try:
                conn, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_client(conn)
            except Exception as e:
                log.error("Error handling client: %s", e)
            finally:
                conn.close()

    def _handle_client(self, conn: socket.socket):
        # 클라이언트가 shutdown(SHUT_WR) 할 때까지 명령 수신
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk

        command = data.decode("utf-8", errors="replace").strip()
        if not command:
            return

        if command.upper() == "EXIT":
            log.info("EXIT command received. Shutting down.")
            conn.sendall(encode_frame(b"Server shutting down.\n"))
            self._running = False
            return

        if command.startswith("@"):
            result = self._router.handle(command, self._session.execute)
        else:
            result = self._session.execute(command)
        # 길이 프리픽스 프레임으로 전송 (개선 C): client 가 절단을 감지할 수 있다.
        conn.sendall(encode_frame(result.encode("utf-8")))

    def stop(self):
        self._running = False
        if self._server_socket:
            self._server_socket.close()


def main():
    parser = argparse.ArgumentParser(description="DumpBridge - dotnet-dump TCP bridge")
    parser.add_argument("dump_path", help="Path to the dump file")
    parser.add_argument("--port", type=int, default=9999, help="TCP port (default: 9999)")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_COMMAND_TIMEOUT,
        help=f"Per-command timeout in seconds (default: {DEFAULT_COMMAND_TIMEOUT}). "
             f"Full 'dumpheap -stat' or first 'gcroot' on a large dump can take 10min+.",
    )
    args = parser.parse_args()

    session = DumpSession(args.dump_path, command_timeout=args.timeout)
    server = BridgeServer(session, args.port)
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        server.stop()
        session.close()


if __name__ == "__main__":
    main()

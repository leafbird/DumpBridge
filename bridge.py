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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dumpbridge")

END_MARKER = "<END_COMMAND_OUTPUT>"


class CommandRouter:
    """Routes @ smart commands to appropriate handlers."""

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
            if cmd_name == "@page":
                return page_output(cmd_args, execute_fn)
            elif cmd_name == "@heap-stats":
                return self._heap.query(cmd_args, execute_fn)
            elif cmd_name == "@stack-groups":
                return self._stack.query(cmd_args, execute_fn)
            else:
                return f"[ERROR] Unknown command: {cmd_name}\nAvailable: @page, @heap-stats, @stack-groups"
        except Exception as e:
            return f"[ERROR] Command failed: {e}"


class DumpSession:
    """dotnet-dump analyze 프로세스를 관리하는 클래스.

    Windows에서 파이프 버퍼링 문제를 회피하기 위해 stdout을 임시 파일로
    리다이렉트하고, 파일 폴링으로 출력을 수집한다.
    완료 감지는 dotnet-dump가 출력하는 <END_COMMAND_OUTPUT> 마커를 사용한다.
    """

    def __init__(self, dump_path: str):
        self._lock = threading.Lock()
        self._outfile_path = tempfile.mktemp(suffix=".dumpbridge.txt")
        self._outfile = open(self._outfile_path, "w+b")
        self._read_pos = 0

        log.info("Starting dotnet-dump analyze: %s", dump_path)
        self._proc = subprocess.Popen(
            ["dotnet-dump", "analyze", dump_path],
            stdin=subprocess.PIPE,
            stdout=self._outfile,
            stderr=subprocess.STDOUT,
        )

        # 초기 배너 + END_MARKER 대기
        log.info("Waiting for initial prompt...")
        self._wait_for_marker(timeout=120)
        log.info("dotnet-dump session ready.")

    def _wait_for_marker(self, timeout: float = 600) -> str:
        """출력 파일을 폴링하여 END_MARKER가 나올 때까지 대기한다."""
        start = time.time()
        buf = ""
        while time.time() - start < timeout:
            self._outfile.flush()
            self._outfile.seek(0, 2)
            size = self._outfile.tell()
            if size > self._read_pos:
                self._outfile.seek(self._read_pos)
                data = self._outfile.read()
                self._read_pos = size
                buf += data.decode("utf-8", errors="replace")
                if END_MARKER in buf:
                    return buf
            if self._proc.poll() is not None:
                return buf
            time.sleep(0.1)
        return ""

    def execute(self, command: str) -> str:
        """명령을 실행하고 결과를 반환한다. 스레드 안전."""
        with self._lock:
            if self._proc.poll() is not None:
                return "[ERROR] dotnet-dump process is not running."

            # 현재 위치 기록 (이전 출력 건너뛰기)
            self._outfile.seek(0, 2)
            self._read_pos = self._outfile.tell()

            log.info("Executing: %s", command)
            self._proc.stdin.write((command + "\n").encode())
            self._proc.stdin.flush()

            raw = self._wait_for_marker(timeout=600)
            if not raw:
                return "[ERROR] Command timed out after 600 seconds."

            # 결과 파싱: 에코된 명령과 END_MARKER 제거
            result = self._parse_output(raw, command)
            return result

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
            conn.sendall(b"Server shutting down.\n")
            self._running = False
            return

        if command.startswith("@"):
            result = self._router.handle(command, self._session.execute)
        else:
            result = self._session.execute(command)
        # 대용량 출력 대비: sendall로 전체 전송
        conn.sendall(result.encode("utf-8"))

    def stop(self):
        self._running = False
        if self._server_socket:
            self._server_socket.close()


def main():
    parser = argparse.ArgumentParser(description="DumpBridge - dotnet-dump TCP bridge")
    parser.add_argument("dump_path", help="Path to the dump file")
    parser.add_argument("--port", type=int, default=9999, help="TCP port (default: 9999)")
    args = parser.parse_args()

    session = DumpSession(args.dump_path)
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

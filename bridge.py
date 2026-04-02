import subprocess
import threading
import logging
import socket
import argparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dumpbridge")


class DumpSession:
    """dotnet-dump analyze 프로세스를 관리하는 클래스."""

    def __init__(self, dump_path: str):
        self._lock = threading.Lock()
        self._result_ready = threading.Event()
        self._result: str = ""
        self._process_dead = False

        log.info("Starting dotnet-dump analyze: %s", dump_path)
        self._proc = subprocess.Popen(
            ["dotnet-dump", "analyze", dump_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )

        self._reader_thread = threading.Thread(
            target=self._read_stdout, daemon=True
        )
        self._reader_thread.start()

        # 초기 프롬프트 대기
        log.info("Waiting for initial prompt...")
        if not self._result_ready.wait(timeout=120):
            raise RuntimeError("Timed out waiting for initial dotnet-dump prompt")
        self._result_ready.clear()
        log.info("dotnet-dump session ready.")

    def _read_stdout(self):
        """stdout을 byte 단위로 읽어 프롬프트를 감지한다."""
        buf = b""
        while True:
            byte = self._proc.stdout.read(1)
            if not byte:
                self._process_dead = True
                if buf:
                    self._result = buf.decode("utf-8", errors="replace")
                self._result_ready.set()
                log.warning("dotnet-dump process exited.")
                return
            buf += byte
            text = buf.decode("utf-8", errors="replace")
            if text.endswith("\n> ") or text == "> ":
                self._result = text[:-2]
                buf = b""
                self._result_ready.set()

    def execute(self, command: str) -> str:
        """명령을 실행하고 결과를 반환한다. 스레드 안전."""
        with self._lock:
            if self._process_dead:
                return "[ERROR] dotnet-dump process is not running."

            self._result_ready.clear()
            log.info("Executing: %s", command)
            self._proc.stdin.write((command + "\n").encode())
            self._proc.stdin.flush()

            if not self._result_ready.wait(timeout=600):
                return "[ERROR] Command timed out after 600 seconds."

            result = self._result
            self._result_ready.clear()
            return result

    def close(self):
        """dotnet-dump 프로세스를 종료한다."""
        try:
            if self._proc.poll() is None:
                self._proc.stdin.write(b"exit\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
        log.info("dotnet-dump session closed.")


class BridgeServer:
    """TCP 소켓 서버. 클라이언트 명령을 DumpSession에 전달한다."""

    def __init__(self, session: DumpSession, port: int = 9999):
        self._session = session
        self._port = port
        self._server_socket: socket.socket | None = None
        self._running = False

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

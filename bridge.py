import subprocess
import threading
import logging

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


if __name__ == "__main__":
    import sys
    session = DumpSession(sys.argv[1])
    print("=== execute test ===")
    print(session.execute("dumpheap -stat")[:500])
    session.close()

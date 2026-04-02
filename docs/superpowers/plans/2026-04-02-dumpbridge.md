# DumpBridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dotnet-dump analyze 세션을 백그라운드에 유지하면서 TCP 소켓으로 명령을 주고받는 Python 브릿지 서버 구현

**Architecture:** DumpSession이 dotnet-dump subprocess를 관리하고, stdout 리더 스레드가 byte 단위로 출력을 수집하며 프롬프트를 감지한다. BridgeServer가 TCP accept 루프를 돌며 클라이언트 명령을 DumpSession에 전달하고 결과를 응답한다.

**Tech Stack:** Python 3.11, 표준 라이브러리만 사용 (subprocess, socket, threading, argparse)

---

## File Structure

```
D:/Dev/DumpBridge/
├── bridge.py        # DumpSession + BridgeServer + main (메인 서버)
├── client.py        # TCP 클라이언트 유틸리티
├── SPEC.md          # (기존)
└── docs/            # (기존)
```

- `bridge.py`: DumpSession 클래스, BridgeServer 클래스, argparse main 엔트리포인트
- `client.py`: argparse 기반 TCP 클라이언트, 명령 전송 + 결과 출력

---

### Task 1: DumpSession - subprocess 시작 및 프롬프트 대기

**Files:**
- Create: `bridge.py`

- [ ] **Step 1: DumpSession 클래스 기본 구조 작성**

```python
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
                # 남은 버퍼가 있으면 결과로 전달
                if buf:
                    self._result = buf.decode("utf-8", errors="replace")
                self._result_ready.set()
                log.warning("dotnet-dump process exited.")
                return
            buf += byte
            text = buf.decode("utf-8", errors="replace")
            # 프롬프트 감지: 줄 시작의 "> "
            if text.endswith("\n> ") or text == "> ":
                self._result = text[:-2]  # 프롬프트 제거
                buf = b""
                self._result_ready.set()
```

- [ ] **Step 2: execute()와 close() 메서드 작성**

`bridge.py`의 DumpSession 클래스에 이어서 추가:

```python
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
```

- [ ] **Step 3: 수동 확인 - DumpSession만 단독 테스트**

`bridge.py` 끝에 임시 테스트 코드를 추가하여 확인:

```python
if __name__ == "__main__":
    import sys
    session = DumpSession(sys.argv[1])
    print("=== execute test ===")
    print(session.execute("dumpheap -stat")[:500])
    session.close()
```

Run: `python3 D:/Dev/DumpBridge/bridge.py D:\temp\ioserver2\ioserver2.dmp`
Expected: dotnet-dump가 시작되고, `dumpheap -stat` 결과의 처음 500자가 출력된 후 종료.

- [ ] **Step 4: 커밋**

```bash
cd D:/Dev/DumpBridge && git init && git add bridge.py && git commit -m "feat: DumpSession - dotnet-dump subprocess 관리 클래스"
```

---

### Task 2: BridgeServer - TCP 소켓 서버

**Files:**
- Modify: `bridge.py`

- [ ] **Step 1: BridgeServer 클래스 작성**

`bridge.py`에 DumpSession 클래스 뒤에 추가:

```python
import socket


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
```

- [ ] **Step 2: main 엔트리포인트 작성**

`bridge.py`의 임시 테스트 코드를 아래로 교체:

```python
import argparse


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
```

- [ ] **Step 3: 수동 확인 - 서버 시작 및 명령 전송**

터미널 1:
```bash
python3 D:/Dev/DumpBridge/bridge.py D:\temp\ioserver2\ioserver2.dmp &
```
Expected: `READY - listening on 127.0.0.1:9999` 출력.

터미널 2 (명령 전송):
```bash
python3 -c "
import socket
s = socket.socket()
s.connect(('127.0.0.1', 9999))
s.send(b'dumpheap -stat')
s.shutdown(socket.SHUT_WR)
result = b''
while True:
    chunk = s.recv(65536)
    if not chunk: break
    result += chunk
print(result.decode()[:500])
s.close()
"
```
Expected: `dumpheap -stat` 결과 출력.

종료:
```bash
python3 -c "
import socket
s = socket.socket()
s.connect(('127.0.0.1', 9999))
s.send(b'EXIT')
s.shutdown(socket.SHUT_WR)
print(s.recv(4096).decode())
s.close()
"
```
Expected: `Server shutting down.` 출력, 서버 프로세스 종료.

- [ ] **Step 4: 커밋**

```bash
git add bridge.py && git commit -m "feat: BridgeServer - TCP 소켓 서버 및 main 엔트리포인트"
```

---

### Task 3: client.py - TCP 클라이언트 유틸리티

**Files:**
- Create: `client.py`

- [ ] **Step 1: client.py 작성**

```python
"""DumpBridge TCP 클라이언트 유틸리티."""

import argparse
import socket
import sys


def main():
    parser = argparse.ArgumentParser(description="DumpBridge client")
    parser.add_argument("command", help="Command to send to DumpBridge")
    parser.add_argument("--port", type=int, default=9999, help="TCP port (default: 9999)")
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    args = parser.parse_args()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((args.host, args.port))
        s.sendall(args.command.encode("utf-8"))
        s.shutdown(socket.SHUT_WR)

        result = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            result += chunk

        sys.stdout.buffer.write(result)
        if result and not result.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
    except ConnectionRefusedError:
        print("ERROR: Cannot connect to DumpBridge. Is the server running?", file=sys.stderr)
        sys.exit(1)
    finally:
        s.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 수동 확인**

서버가 실행 중인 상태에서:
```bash
python3 D:/Dev/DumpBridge/client.py "dumpheap -stat"
```
Expected: `dumpheap -stat` 결과 출력.

```bash
python3 D:/Dev/DumpBridge/client.py "EXIT"
```
Expected: `Server shutting down.` 출력, 서버 종료.

- [ ] **Step 3: 커밋**

```bash
git add client.py && git commit -m "feat: client.py - TCP 클라이언트 유틸리티"
```

---

### Task 4: 통합 테스트

실제 덤프 파일(`D:\temp\ioserver2\ioserver2.dmp`)로 SPEC.md의 테스트 시나리오 수행.

- [ ] **Step 1: 서버 시작 → "READY" 메시지 확인**

```bash
python3 D:/Dev/DumpBridge/bridge.py D:\temp\ioserver2\ioserver2.dmp &
```
Expected: `READY - listening on 127.0.0.1:9999`

- [ ] **Step 2: dumpheap -stat 실행**

```bash
python3 D:/Dev/DumpBridge/client.py "dumpheap -stat"
```
Expected: 타입별 통계 테이블 출력.

- [ ] **Step 3: dumpheap -type 실행**

```bash
python3 D:/Dev/DumpBridge/client.py "dumpheap -type Cs.Memory.LohPool"
```
Expected: 주소 포함 출력.

- [ ] **Step 4: dumpobj 실행**

이전 출력에서 얻은 주소로:
```bash
python3 D:/Dev/DumpBridge/client.py "dumpobj <주소>"
```
Expected: 필드 정보 출력.

- [ ] **Step 5: gcroot 실행 (캐싱 테스트)**

```bash
python3 D:/Dev/DumpBridge/client.py "gcroot <주소>"
```
Expected: 첫 실행 시 "Caching GC roots" 메시지 포함, 수 분 소요 가능. 재실행 시 즉시 응답.

- [ ] **Step 6: EXIT으로 종료**

```bash
python3 D:/Dev/DumpBridge/client.py "EXIT"
```
Expected: `Server shutting down.`, 서버 프로세스 종료.

- [ ] **Step 7: 최종 커밋 (필요 시)**

변경사항이 있으면 커밋.

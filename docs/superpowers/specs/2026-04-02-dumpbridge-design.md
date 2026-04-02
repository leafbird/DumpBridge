# DumpBridge 설계 문서

## 개요

dotnet-dump analyze 세션을 백그라운드에 유지하면서 TCP 소켓으로 명령을 주고받는 Python 브릿지 서버.

## 아키텍처

```
Claude Code (Bash) --TCP 9999--> BridgeServer --stdin/stdout--> dotnet-dump analyze
```

### 스레딩 모델: 단일 스레드 + 블로킹 읽기

- **stdout 리더 스레드**: dotnet-dump stdout을 byte 단위로 읽어 버퍼에 쌓고, 프롬프트(`\n> ` 또는 `> `)를 감지하면 `threading.Event`로 알림
- **메인 스레드**: TCP accept 루프 → 명령 수신 → stdin에 쓰기 → Event 대기 → 결과 TCP 응답
- **동시성 제어**: `threading.Lock`으로 한 번에 하나의 명령만 처리

## 컴포넌트

### DumpSession

dotnet-dump 프로세스를 관리하는 클래스.

```python
class DumpSession:
    def __init__(self, dump_path: str)
    def execute(self, command: str) -> str
    def close(self)
```

- `__init__`: `dotnet-dump analyze <dump_path>` subprocess 시작, stdout 리더 스레드 시작, 초기 프롬프트 대기
- `execute`: 명령을 stdin에 쓰고, 프롬프트가 나올 때까지 stdout 수집 후 반환. Lock으로 동시 실행 방지
- `close`: `exit` 명령 전송 후 프로세스 종료 대기

### stdout 리더 스레드

```
buffer = b""
while True:
    byte = proc.stdout.read(1)
    if not byte: break
    buffer += byte
    text = buffer.decode("utf-8", errors="replace")
    if text.endswith("\n> ") or text == "> ":
        result = text[:-2]  # 프롬프트 제거
        -> result_queue에 넣기
        -> buffer 초기화
```

- byte 단위 읽기로 Windows 블록 버퍼링 문제 회피
- 프롬프트 감지: `\n> ` (일반) 또는 `> ` (첫 프롬프트)
- 타임아웃: execute() 측에서 Event.wait(timeout=600) — gcroot 캐싱 등 장시간 명령 대비 10분

### BridgeServer

TCP 소켓 서버.

```python
class BridgeServer:
    def __init__(self, session: DumpSession, port: int = 9999)
    def start(self)
    def stop(self)
```

- `start`: 127.0.0.1:port에서 listen, accept 루프
- 클라이언트 연결 → shutdown(SHUT_WR) 될 때까지 명령 수신 → `session.execute()` → 결과 전송 → 연결 종료
- `EXIT` 명령 수신 시 서버 종료
- `SO_REUSEADDR` 설정

### client.py

간단한 TCP 클라이언트 유틸리티.

```bash
python3 client.py "dumpheap -stat"
python3 client.py --port 9999 "dumpheap -stat"
```

- argparse로 명령과 포트 인자 처리
- TCP 연결 → 명령 전송 → shutdown(SHUT_WR) → 결과 수신 → stdout 출력

## 에러 처리

- dotnet-dump 프로세스가 예기치 않게 종료되면 서버도 종료
- TCP 클라이언트 연결 중 에러 발생 시 해당 연결만 닫고 서버는 계속 동작
- stdout에서 30초간 새 데이터 없으면 타임아웃 (단, execute 전체 타임아웃은 600초)

## 파일 구조

```
D:/Dev/DumpBridge/
├── SPEC.md
├── bridge.py        # DumpSession + BridgeServer + main
├── client.py        # TCP 클라이언트 유틸리티
└── docs/
    └── superpowers/specs/
        └── 2026-04-02-dumpbridge-design.md
```

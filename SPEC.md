# DumpBridge - dotnet-dump 세션 브릿지

## 개요

`dotnet-dump analyze`는 인터랙티브 REPL로 동작한다. Claude Code의 Bash 도구에서 비대화형으로 사용하려면 매 명령마다 덤프를 열고 닫아야 하는데, 이는 극심한 성능 저하를 유발한다 (특히 `gcroot`의 GC root 캐싱이 매번 초기화됨).

DumpBridge는 dotnet-dump 프로세스를 백그라운드에 유지하면서, TCP 소켓을 통해 명령을 주고받는 Python 브릿지 서버다.

## 아키텍처

```
┌─────────────┐     TCP 9999     ┌──────────────┐     stdin/stdout     ┌─────────────┐
│  Claude Code │ ──────────────→ │  DumpBridge   │ ──────────────────→ │  dotnet-dump │
│  (Bash tool) │ ←────────────── │  (Python)     │ ←────────────────── │  analyze     │
└─────────────┘                  └──────────────┘                      └─────────────┘
```

### 동작 흐름

1. 사용자가 DumpBridge를 백그라운드로 실행 (덤프 경로 지정)
2. DumpBridge가 `dotnet-dump analyze <dump>` 를 subprocess로 시작
3. 초기 프롬프트(`> `)가 나올 때까지 대기 → TCP 소켓(127.0.0.1:9999) 리스닝 시작
4. Claude Code가 Bash에서 명령 전송: `echo "dumpheap -type LohPool" | nc localhost 9999`
5. DumpBridge가 명령을 dotnet-dump stdin에 전달 → stdout에서 결과 수집 → TCP로 응답
6. 덤프 세션은 계속 유지 (GC root 캐시 등 보존)

## 기술 요구사항

### 필수

- **Python 3.x** (Windows에 설치되어 있음)
- 외부 패키지 의존성 최소화 (가능하면 표준 라이브러리만 사용)

### 핵심 기술 과제

#### 1. 프롬프트 감지
- dotnet-dump는 명령 완료 후 `> ` 프롬프트를 출력한다
- 출력 중간에 `>` 문자가 포함될 수 있으므로 단순 문자 매칭은 불안정
- 접근: `\n> ` 패턴 또는 줄 시작의 `> ` 감지. 첫 프롬프트는 `\n` 없이 나올 수 있으므로 별도 처리

#### 2. stdout 버퍼링
- Windows에서 subprocess의 stdout이 블록 버퍼링되면 read가 블로킹될 수 있음
- 접근: byte 단위 읽기 또는 PTY 에뮬레이션. Windows에서 PTY는 제한적이므로 byte 단위가 현실적

#### 3. 타임아웃
- `gcroot` 첫 실행 시 "Caching GC roots, this may take a while." 후 수 분 소요
- TCP 클라이언트 측에서 응답을 기다리는 동안 타임아웃이 발생하면 안 됨
- 접근: 충분히 긴 타임아웃 (예: 10분) 또는 타임아웃 없음

#### 4. 대용량 출력
- `dumpheap` 같은 명령은 수십만 줄을 출력할 수 있음
- TCP 응답 버퍼가 부족하면 데이터 유실
- 접근: 충분한 recv 버퍼 크기 + 반복 읽기

#### 5. 동시성
- dotnet-dump는 싱글 스레드 → 동시에 여러 명령 불가
- TCP 요청은 순차 처리 (lock 또는 단일 accept 루프)

## 사용 시나리오

### 서버 시작
```bash
# 백그라운드 실행
python3 d:/dev/DumpBridge/bridge.py D:\temp\ioserver2\ioserver2.dmp &

# 또는 포트 지정
python3 d:/dev/DumpBridge/bridge.py D:\temp\ioserver2\ioserver2.dmp --port 9999
```

### 명령 실행 (Claude Code Bash에서)
```bash
# 방법 1: netcat
echo "dumpheap -type LohPool" | nc localhost 9999

# 방법 2: python one-liner (nc가 없을 때)
python3 -c "
import socket
s = socket.socket()
s.connect(('127.0.0.1', 9999))
s.send(b'dumpheap -type LohPool')
s.shutdown(socket.SHUT_WR)
result = b''
while True:
    chunk = s.recv(65536)
    if not chunk:
        break
    result += chunk
print(result.decode())
s.close()
"
```

### 서버 종료
```bash
echo "EXIT" | nc localhost 9999
```

## 구현 구조

```
D:/dev/DumpBridge/
├── SPEC.md          ← 이 문서
├── bridge.py        ← 메인 브릿지 서버
└── client.py        ← (선택) 클라이언트 유틸리티
```

### bridge.py 주요 클래스

```python
class DumpSession:
    """dotnet-dump 프로세스를 관리하는 클래스"""
    def __init__(self, dump_path: str)
    def execute(self, command: str) -> str  # 명령 실행 및 결과 반환
    def close(self)                          # 프로세스 종료

class BridgeServer:
    """TCP 소켓 서버"""
    def __init__(self, session: DumpSession, port: int)
    def start(self)  # accept 루프 시작
    def stop(self)
```

### 프롬프트 감지 알고리즘 (의사코드)

```
buffer = ""
while True:
    byte = proc.stdout.read(1)
    if not byte:
        break  # 프로세스 종료
    buffer += byte.decode()
    
    # 프롬프트 감지: 줄 시작의 "> "
    if buffer.endswith("\n> ") or buffer == "> ":
        return buffer[:-2]  # 프롬프트 제거 후 반환
```

주의: dotnet-dump가 `> ` 없이 출력만 하고 멈추는 에러 상황도 고려해서, 일정 시간(예: 30초) 동안 새 출력이 없으면 타임아웃 처리 필요.

## 배경 정보 (이전 세션 맥락)

이 도구는 IoServer 메모리 누수 분석 과정에서 필요성이 확인되었다.
덤프 분석 시 수행한 명령 예시:

- `dumpheap -type Cs.Memory.LohPool` → 풀 인스턴스 찾기
- `dumpobj <주소>` → 인스턴스 필드 확인
- `dumpheap -type Cs.Memory.LohSegment` → 세그먼트 통계
- `gcroot <주소>` → GC root 추적 (첫 실행 시 캐싱으로 수 분 소요)
- `dumpvc <MT> <주소>` → 값 타입 덤프
- `dumpheap -mt <MT>` → 특정 MethodTable의 인스턴스 목록

일반적으로 한 번의 분석에 10~20회 이상의 명령이 필요하며, 앞 명령의 출력에서 주소를 추출하여 다음 명령에 사용하는 탐색적 패턴이다.

## 테스트

구현 후 `D:\temp\ioserver2\ioserver2.dmp` 파일로 실제 분석 명령을 테스트한다.

테스트 시나리오:
1. 서버 시작 → "READY" 메시지 확인
2. `dumpheap -stat` → 정상 출력 확인
3. `dumpheap -type Cs.Memory.LohPool` → 주소 포함 출력 확인
4. `dumpobj <주소>` → 필드 정보 출력 확인
5. `gcroot <주소>` → 첫 실행 시 캐싱 메시지 포함 출력 확인, 이후 재실행 시 즉시 응답
6. 대용량 출력 (`dumpheap -mt <MT>`) → 데이터 유실 없이 전체 수신 확인
7. `EXIT` → 서버 정상 종료 확인

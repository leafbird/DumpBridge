# DumpBridge

`dotnet-dump analyze` 세션을 유지한 채 TCP 소켓으로 SOS 디버깅 명령을 보낼 수 있는 Python 브릿지 서버입니다.

## 왜 필요한가

`dotnet-dump`는 인터랙티브 REPL로 동작합니다. 비대화형으로 사용하려면 매 명령마다 덤프를 열고 닫아야 하는데, `gcroot`의 GC root 캐싱이 매번 초기화되어 수 분씩 소요됩니다. DumpBridge는 프로세스를 백그라운드에 유지하여 이 문제를 해결합니다.

## 아키텍처

```
Claude Code (Bash)  →  TCP 9999  →  DumpBridge (Python)  →  dotnet-dump analyze
```

## 요구사항

- Python 3.10+
- `dotnet-dump` ([설치](https://learn.microsoft.com/en-us/dotnet/core/diagnostics/dotnet-dump))

## 사용법

### 서버 시작

```bash
python bridge.py <dump_path> [--port 9999]
```

### 명령 실행

```bash
# client.py 사용
python client.py "dumpheap -stat"
python client.py "@heap-stats --sort=size --desc --limit=20"

# 또는 netcat
echo "dumpheap -stat" | nc localhost 9999
```

### 서버 종료

```bash
python client.py "EXIT"
```

## 스마트 커맨드

`@`로 시작하는 명령은 DumpBridge가 가로채서 서버 측에서 처리합니다. 그 외는 dotnet-dump에 그대로 전달됩니다.

**에이전트 참고**: 사용 가능한 스마트 커맨드와 옵션을 모르면 `@help`를 먼저 실행하세요. 서버가 전체 명령 레퍼런스를 반환합니다.

```bash
python client.py "@help"
```

### @page

임의 명령 결과에 라인 기반 페이징을 적용합니다.

```bash
@page [--offset=N] [--limit=N] <command>
```

```bash
python client.py "@page --limit=10 dumpheap -stat"
python client.py "@page --offset=100 --limit=50 dumpheap -type System.String"
```

### @heap-stats

`dumpheap -stat` 결과를 파싱/캐싱하여 정렬, 필터, 페이징을 제공합니다. 첫 호출 시 캐싱되며 이후 호출은 즉시 응답합니다.

```bash
@heap-stats [--sort=count|size|name] [--desc] [--offset=N] [--limit=N] [--filter=PATTERN] [--refresh]
```

```bash
# TotalSize 기준 상위 20개
python client.py "@heap-stats --sort=size --desc --limit=20"

# 특정 네임스페이스 필터링
python client.py "@heap-stats --filter=Cs\.Memory --sort=size --desc"

# 캐시 갱신
python client.py "@heap-stats --refresh --sort=size --desc"
```

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--sort` | (원본 순서) | 정렬 기준: `count`, `size`, `name` |
| `--desc` | 오름차순 | 내림차순 정렬 |
| `--offset` | 0 | 시작 위치 |
| `--limit` | 50 | 표시 개수 |
| `--filter` | (없음) | 클래스명 정규식 필터 (대소문자 무시) |
| `--refresh` | (없음) | 캐시를 무시하고 다시 실행 |

### @stack-groups

`clrstack -all` 결과를 파싱하여 동일 콜스택을 가진 스레드를 그룹화합니다. 스레드 수가 많은 덤프에서 핵심을 빠르게 파악할 수 있습니다.

```bash
@stack-groups [--max-frames=N] [--limit=N]
```

```bash
# 전체 그룹
python client.py "@stack-groups"

# 상위 3프레임만으로 그룹핑 (더 넓은 그룹)
python client.py "@stack-groups --max-frames=3"

# 상위 5그룹만 표시
python client.py "@stack-groups --limit=5"
```

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--max-frames` | 0 (전체) | 그룹핑에 사용할 상위 N개 프레임 |
| `--limit` | 0 (전체) | 표시할 그룹 수 |

## 파일 구조

```
DumpBridge/
├── bridge.py       # BridgeServer, DumpSession, CommandRouter
├── analyzers.py    # HeapAnalyzer, StackAnalyzer, page_output
├── client.py       # TCP 클라이언트 유틸리티
├── SPEC.md         # 설계 문서
└── README.md
```

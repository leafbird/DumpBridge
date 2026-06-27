# DumpBridge

`dotnet-dump analyze` 세션을 유지한 채 TCP 소켓으로 SOS 디버깅 명령을 보낼 수 있는 Python 브릿지 서버입니다.

> 🤖 **이 도구로 실제 덤프를 분석하는 에이전트는 [`AGENTS.md`](AGENTS.md) 를 먼저 읽으세요.** 사용 절차·결과 해석 규율(보존법칙 등)·함정을 정리한 운영 가이드입니다.

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
python bridge.py <dump_path> [--port 9999] [--timeout 1800]
```

- `--timeout` 은 명령당 타임아웃(초)입니다. 큰 덤프의 풀 `dumpheap -stat` 이나
  첫 `gcroot` 는 10분 이상 걸릴 수 있어 기본값을 1800초로 둡니다. 타임아웃이
  나도 스트림은 마커 정렬을 유지하므로(아래 "신뢰성" 참고) 다음 명령이
  자동으로 백로그를 정리하고 자기 출력을 정확히 반환합니다.

### 명령 실행

```bash
# client.py 사용
python client.py "dumpheap -stat"
python client.py "@heap-stats --sort=size --desc --limit=20"

# kclient.py — 포트 9999 고정 단축 클라이언트
python kclient.py "gcroot 028bda9ce2b8"
```

> ⚠️ 응답이 **길이 프리픽스 프레임**으로 전송되므로 `nc` 같은 raw 클라이언트는
> 더 이상 호환되지 않습니다. `client.py` / `kclient.py`(또는 `protocol.recv_frame`)
> 를 사용하세요. 프레이밍 덕분에 응답 절단을 조용히 넘기지 않고 감지합니다.

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

## 신뢰성 (대용량 출력 안전장치)

큰 덤프에서 풀 `dumpheap -stat` 같은 대용량 출력이 깨져 잘못된 분석으로
이어지는 사고가 있었습니다(주소가 카운트/사이즈로 오독되어 한 타입이 힙
전체보다 커 보이는 식). 이를 막기 위한 장치:

- **마커 정렬 (A)** — dotnet-dump 는 명령마다 끝에 `<END_COMMAND_OUTPUT>` 를
  출력합니다. DumpBridge 는 파일 끝으로 점프하지 않고 "소비한 마커 직후"부터
  다음 마커까지만 읽습니다. 한 명령이 타임아웃해도 스트림이 어긋나지 않습니다.
- **길이 프레이밍 (C)** — 응답은 `MAGIC + 8B 길이 + payload`. 클라이언트가
  정확히 그 길이만큼 받으므로 중간 끊김을 "정상 완료"로 오인하지 않습니다.
- **블록 단위 디코드 (D)** — UTF-8 디코드를 청크 경계가 아니라 완성된 블록
  전체에 한 번 적용해 멀티바이트 깨짐을 방지합니다.
- **보존법칙 가드 (E)** — `@heap-stats` 는 어떤 타입의 count/size 도 힙 Total 을
  넘지 않는지, 행 합이 Total 을 초과하지 않는지 검사합니다. 위반하면 데이터를
  보고하지 않고 `CORRUPT` 에러를 반환합니다.

단위 테스트: `python -m unittest test_dumpbridge`

## 파일 구조

```
DumpBridge/
├── bridge.py          # BridgeServer, DumpSession, CommandRouter
├── analyzers.py       # HeapAnalyzer(+오염 가드), StackAnalyzer, page_output
├── protocol.py        # 마커 정렬 + TCP 프레이밍 (공유 순수 함수)
├── client.py          # TCP 클라이언트 유틸리티 (--port/--host)
├── kclient.py         # 포트 9999 고정 단축 클라이언트
├── test_dumpbridge.py # 단위 테스트 (덤프 불필요)
├── AGENTS.md          # 에이전트 사용 지침 (절차·해석 규율·함정)
├── SPEC.md            # 설계 문서
└── README.md
```

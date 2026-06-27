# DumpBridge — 에이전트 사용 지침

이 문서는 **DumpBridge 를 실제로 사용해 .NET 덤프를 분석하는 에이전트**를 위한
운영 가이드입니다. 도구의 동작 모델·정확한 사용 절차·결과 해석 규율·함정을
다룹니다. 인간용 개요/옵션 표는 `README.md` 를, 설계 배경은 `SPEC.md` 를 보세요.

> 핵심 한 줄: DumpBridge 는 `dotnet-dump analyze` 세션을 살려둔 채 TCP 로 SOS
> 명령을 주고받게 해줍니다. **명령은 직렬 처리**되고, **결과는 길이 프레임**으로
> 오며, `@heap-stats` 는 **깨진 캡처를 거부**합니다. 그래도 **수치 해석의 최종
> 책임은 에이전트에게** 있습니다(§5 분석 규율 필수).

---

## 1. 언제 쓰나

- 하나의 덤프에 대해 **여러 SOS 명령을 탐색적으로** 던질 때 (주소를 뽑아 다음
  명령에 쓰는 패턴). 특히 `gcroot` 는 첫 실행에 GC root 캐싱으로 수 분 걸리는데,
  세션을 유지하면 이후 `gcroot` 는 즉시 응답합니다.
- 단발성 한 명령이면 굳이 브리지 없이 `dotnet-dump analyze ... -c "<cmd>"` 도
  됩니다. 2회 이상이면 브리지가 압도적으로 빠릅니다.

## 2. 생명주기 — 시작 / 실행 / 종료

### 2.1 서버 시작 (백그라운드)

```bash
python bridge.py "<dump_path>" [--port 9999] [--timeout 1800]
```

- **백그라운드로 띄우세요.** 서버는 accept 루프로 블로킹됩니다.
- 시작 직후 덤프를 로드하고 초기 배너를 소비합니다. 로그에
  `READY - listening on 127.0.0.1:<port>` 가 보이면 준비 완료입니다.
  **이 줄을 확인한 뒤** 명령을 보내세요(보통 수 초, 매우 큰 덤프는 더 걸림).
- 같은 덤프를 동시에 둘 띄우지 마세요. 다른 덤프를 병행하려면 `--port` 를 분리.
- `--timeout` 은 명령당 한도(초). 큰 덤프의 풀 `dumpheap -stat`/첫 `gcroot` 는
  10분+ 가능하므로 기본 1800. 느린 작업이 예상되면 더 키우세요.

### 2.2 명령 실행 (클라이언트)

```bash
python kclient.py "<command>"            # 포트 9999 고정, 가장 짧음
python client.py "<command>" [--port N] [--host H]   # 포트/호스트 지정 가능
```

- 클라이언트는 명령을 보내고 **완성된 응답 한 프레임**을 받아 stdout 으로 그대로
  출력합니다. 한 번 호출 = 한 명령.
- **`nc`/`echo | nc` 는 더 이상 동작하지 않습니다.** 응답이 길이 프레임이라
  raw 소켓 도구로는 파싱이 깨집니다. 반드시 `client.py`/`kclient.py`(또는
  `protocol.recv_frame`)를 쓰세요.

### 2.3 서버 종료

```bash
python kclient.py "EXIT"      # 또는 python client.py "EXIT"
```

- 분석이 끝나면 반드시 종료하세요. dotnet-dump 가 덤프 크기만큼 메모리·핸들을
  잡고 있습니다. 종료를 깜빡하면 다음 세션/장비에 부담이 됩니다.
- 프로세스를 강제로 죽여야 하면 **PID/이름으로** 죽이세요. **CommandLine
  문자열 매칭으로 kill 하지 마세요** — 자기 셸이나 다른 클라이언트를 같이
  죽일 수 있습니다.

## 3. 명령 모델

- `@` 로 시작하지 **않는** 명령은 dotnet-dump(SOS)로 **그대로 전달**됩니다.
  예: `gcheapstat`, `dumpheap -stat`, `dumpheap -mt <MT>`, `gcroot <addr>`,
  `dumpobj <addr>`, `clrthreads`, `eeversion`, `dumpdomain` …
- `@` 로 시작하는 명령은 **DumpBridge 가 가로채** 서버 측에서 가공합니다(§4).
- **명령은 직렬 처리**됩니다(dotnet-dump 는 싱글스레드 REPL). 클라이언트를
  동시에 여러 개 띄워도 서버가 락으로 순차 실행하니, 순서가 중요하면 한 번에
  하나씩 보내고 응답을 받은 뒤 다음을 보내세요.

## 4. 스마트 커맨드 (`@`)

옵션을 모르면 **먼저 `@help`** 를 실행하세요(전체 레퍼런스 반환). 요약:

| 커맨드 | 용도 | 자주 쓰는 형태 |
|---|---|---|
| `@help` | 스마트 커맨드 목록/옵션 | `@help` |
| `@page` | 임의 명령 출력에 라인 페이징 | `@page --offset=0 --limit=50 <command>` |
| `@heap-stats` | `dumpheap -stat` 캐시 + 정렬/필터/페이징 **+ 오염 가드** | `@heap-stats --sort=size --desc --limit=20` |
| `@stack-groups` | `clrstack -all` 을 동일 콜스택끼리 그룹화 | `@stack-groups --max-frames=3 --limit=10` |

- **`@heap-stats` 를 우선 쓰세요** — 풀 `dumpheap -stat` 을 raw 로 받아 직접
  파싱하지 말 것. `@heap-stats` 는 (1) 첫 호출만 무거운 -stat 을 돌리고 캐시,
  (2) 정렬/필터/페이징을 서버에서 처리해 전송량을 줄이며, (3) **보존법칙
  오염 가드**(§5)가 내장돼 깨진 캡처를 자동 거부합니다.
- `--filter` 는 정규식(대소문자 무시). 점은 이스케이프: `--filter=Cs\.Memory`.
- 캐시를 새로 뜨려면 `--refresh`.

## 5. 결과 해석 규율 (★ 가장 중요)

대용량 출력 분석에서 **틀린 수치를 사실로 보고하는 것**이 가장 큰 위험입니다.
도구에 가드가 있어도, 아래 규율을 에이전트가 직접 적용하세요.

1. **보존법칙(conservation) 체크 — 절대 원칙.**
   힙의 어떤 단일 타입도 count·size 가 **힙 Total 을 초과할 수 없습니다.**
   초과하거나, 같은 타입이 -stat 에서 여러 번 나오거나, 행 합이 Total 을 넘으면
   → **즉시 "캡처 오염"으로 판정하고 그 데이터로 결론 내지 마세요.**
   (`@heap-stats` 는 이 경우 `CORRUPT` 에러를 반환합니다 — 그 에러를 우회해
   raw 파싱으로 강행하지 말 것.)

2. **2-소스 교차검증.**
   메모리 결론은 **최소 두 독립 지표**가 일치해야 합니다:
   - `gcheapstat` (세대별 Allocated/Free → **live = Allocated − Free**)
   - `@heap-stats` 상위 타입 (committed/누적 관점)
   - `gcroot <addr>` (누가 잡고 있나 — 누수 root)
   자릿수가 어긋나면 측정이 오염됐거나 "live vs committed"를 혼동한 것입니다.
   (committed 와 live 는 다릅니다 — Server GC 는 빈 세그먼트를 바로 반환하지
   않아 committed 가 live 보다 훨씬 커 보일 수 있습니다.)

3. **포맷 이상 = 하드 실패.**
   행 오정렬, 숫자 칸에 16진수 주소가 보이는 등 포맷이 어긋나면 파싱을 강행하지
   말고 실패로 처리하세요(`--refresh` 재시도 또는 포커스 쿼리로 전환).

4. **예비결론에도 sanity 를 먼저.**
   "정확 수치는 곧" 하면서 검증 안 된 수치를 먼저 던지지 마세요. 최소한
   보존법칙(규율 1)이라도 통과한 뒤에 수치를 제시합니다.

> **실제 사고 사례(반면교사):** 한 타입이 "4억+ 객체 / 55.9GB 로 압도적 1위"로
> 보고됐으나, 같은 캡처의 Total 은 1.87억 객체 / 172.8GB 였습니다. **단일 타입
> (4억) > 전체(1.87억)** — 물리적으로 불가능. 깨진 캡처에서 힙 주소가 count/size
> 칸으로 섞여 합산된 산물이었고, 규율 1 한 줄이면 즉시 걸렀을 오류입니다.

## 6. 동작상 알아둘 것 (행동 모델)

- **타임아웃은 데이터를 오염시키지 않습니다.** 한 명령이 `--timeout` 을 넘기면
  그 명령은 `[ERROR] ... timed out` 을 반환하지만, 스트림은 마커 정렬을 유지해
  **다음 명령이 백로그를 정리하고 자기 출력을 정확히 반환**합니다. 대응: 더 큰
  `--timeout` 으로 재시도하거나, 더 좁은 쿼리로 바꾸세요.
- **`CORRUPT` 응답**(`@heap-stats`) = 보존법칙 위반 감지. 그 수치를 쓰지 말고
  `--refresh` 재시도 → 그래도면 포커스 쿼리/`gcheapstat` 로 전환.
- **`TruncatedFrame` / `truncated response`**(클라이언트) = 응답이 끝까지 안 옴
  (서버 비정상 종료 등). 결과를 신뢰하지 말고 서버 로그 확인 후 재실행.
- **`dotnet-dump process is not running` / `... died`** = 세션이 죽음. 서버를
  다시 시작해야 합니다(캐시도 초기화됨).
- **첫 `gcroot` 는 느립니다**(GC root 캐싱). 이후 `gcroot` 는 빠릅니다. 첫 호출이
  오래 걸려도 정상.

## 7. 성능·전송량 팁

- 풀 `dumpheap -stat` 은 거대 덤프에서 수 분~십수 분 + 수 MB 출력입니다. 꼭
  필요할 때만, 그리고 raw 대신 `@heap-stats` 로 받으세요.
- 특정 타입만 볼 땐 **포커스 쿼리**가 훨씬 빠릅니다:
  `dumpheap -stat -type <부분일치이름>` (집계) / `dumpheap -mt <MT>` (인스턴스
  목록). MT 는 `-stat`/`@heap-stats` 출력 1열에서 얻습니다.
- live(실사용) 메모리는 `gcheapstat` 로 봅니다(= Allocated − Free). "committed"
  지표(빈 세그먼트 포함)를 live 로 착각하지 마세요.
- 스레드가 많은 덤프의 행 분석은 `@stack-groups` 로 동일 콜스택을 접어서 보세요.
- 큰 출력을 훑을 땐 `@page` 또는 `@heap-stats --offset/--limit` 으로 잘라 받기.

## 8. 메모리 누수 추적 표준 흐름 (예시)

```
1) gcheapstat                         # live(Alloc-Free) 와 세대 분포 파악
2) @heap-stats --sort=count --desc    # 인스턴스 수 폭증 타입 후보 (가드 통과 확인)
   @heap-stats --sort=size  --desc    # 바이트 폭증 타입 후보
3) dumpheap -mt <후보 MT>             # 인스턴스 주소 몇 개 표집
4) gcroot <addr>                      # 누가 잡고 있는지(누수 root) 확인 — 표본 여러 개
5) 교차검증: (2)의 상위타입 ↔ (1) live ↔ (4) root 가 한 이야기로 수렴하는가?
   수렴 안 하면 측정 오염/혼동 → 규율 5 로 복귀
```

## 9. 빠른 점검

- 덤프 없이 도구 로직 검증: `python -m unittest test_dumpbridge`
- 옵션이 헷갈리면: `python kclient.py "@help"`
- 응답이 이상하면: 서버 로그(시작 시 리다이렉트한 파일)와 §6 표를 대조.

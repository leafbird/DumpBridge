"""DumpBridge smart command analyzers."""

import argparse
import re


def page_output(args: list[str], execute_fn) -> str:
    """Apply line-based paging to any command output.

    Usage: @page [--offset=N] [--limit=N] <raw command...>
    """
    parser = argparse.ArgumentParser(prog="@page", add_help=False)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    known, remaining = parser.parse_known_args(args)

    if not remaining:
        return "[ERROR] Usage: @page [--offset=N] [--limit=N] <command>"

    raw_command = " ".join(remaining)
    result = execute_fn(raw_command)
    lines = result.split("\n")
    total = len(lines)
    page = lines[known.offset : known.offset + known.limit]

    if not page:
        return f"[No lines to show (offset {known.offset} exceeds {total} total lines)]"

    output = "\n".join(page)
    end = min(known.offset + known.limit, total)
    output += f"\n[Lines {known.offset + 1}-{end} of {total} total]"
    return output


class HeapAnalyzer:
    """dumpheap -stat 결과를 파싱/캐싱하여 정렬·필터·페이징을 제공한다."""

    _ROW_RE = re.compile(r"^\s*([0-9a-fA-F]+)\s+([\d,]+)\s+([\d,]+)\s+(.+)$")
    _TOTAL_RE = re.compile(r"^Total\s+([\d,]+)\s+objects.*?([\d,]+)\s+bytes", re.IGNORECASE)

    def __init__(self):
        self._entries: list[dict] | None = None
        self._total_count: int = 0
        self._total_size: int = 0
        self._corrupt: str | None = None  # 오염 감지 사유 (정상이면 None)

    def query(self, args: list[str], execute_fn) -> str:
        parser = argparse.ArgumentParser(prog="@heap-stats", add_help=False)
        parser.add_argument("--sort", choices=["count", "size", "name"], default=None)
        parser.add_argument("--desc", action="store_true")
        parser.add_argument("--offset", type=int, default=0)
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--filter", default=None)
        parser.add_argument("--refresh", action="store_true")
        try:
            opts = parser.parse_args(args)
        except SystemExit:
            return "[ERROR] Usage: @heap-stats [--sort=count|size|name] [--desc] [--offset=N] [--limit=N] [--filter=PATTERN] [--refresh]"

        if self._entries is None or opts.refresh:
            raw = execute_fn("dumpheap -stat")
            self._parse(raw)

        if self._entries is None:
            return "[ERROR] Failed to parse dumpheap -stat output."

        # 보존법칙 가드 (개선 E): 깨진 캡처를 데이터로 보고하지 않는다.
        if self._corrupt:
            return (
                "[ERROR] dumpheap -stat capture appears CORRUPT — refusing to report.\n"
                f"  Reason: {self._corrupt}\n"
                "  This is the classic DumpBridge large-output desync (heap addresses\n"
                "  parsed as counts/sizes). Do NOT trust any aggregate from it.\n"
                "  Fix: re-run with '@heap-stats --refresh', or use a focused\n"
                "  'dumpheap -stat -type <T>' / 'gcheapstat' instead, and cross-check\n"
                "  that no single type's count or size exceeds the heap Total."
            )

        entries = self._entries

        # Filter
        if opts.filter:
            try:
                pattern = re.compile(opts.filter, re.IGNORECASE)
            except re.error as e:
                return f"[ERROR] Invalid filter pattern: {e}"
            entries = [e for e in entries if pattern.search(e["name"])]

        # Sort
        if opts.sort:
            key_map = {"count": "count", "size": "size", "name": "name"}
            entries = sorted(entries, key=lambda e: e[key_map[opts.sort]],
                           reverse=opts.desc)
        elif opts.desc:
            entries = list(reversed(entries))

        # Paging
        filtered_total = len(entries)
        page = entries[opts.offset : opts.offset + opts.limit]

        if not page:
            return f"[No entries to show (offset {opts.offset} exceeds {filtered_total} filtered entries)]"

        # Format
        lines = [f"{'MT':>18s} {'Count':>10s} {'TotalSize':>12s} Class Name"]
        for e in page:
            lines.append(f"{e['mt']:>18s} {e['count']:>10d} {e['size']:>12d} {e['name']}")

        end = min(opts.offset + opts.limit, filtered_total)
        lines.append(f"[Showing {opts.offset + 1}-{end} of {filtered_total} entries | "
                     f"Total: {self._total_count} objects, {self._total_size:,} bytes]")
        return "\n".join(lines)

    def _parse(self, raw: str):
        entries = []
        total_count = 0
        total_size = 0
        for line in raw.split("\n"):
            m = self._ROW_RE.match(line)
            if m:
                entries.append({
                    "mt": m.group(1),
                    "count": int(m.group(2).replace(",", "")),
                    "size": int(m.group(3).replace(",", "")),
                    "name": m.group(4).strip(),
                })
                continue
            m = self._TOTAL_RE.match(line.strip())
            if m:
                total_count = int(m.group(1).replace(",", ""))
                total_size = int(m.group(2).replace(",", ""))

        self._entries = entries
        self._total_count = total_count
        self._total_size = total_size
        self._corrupt = self._detect_corruption(entries, total_count, total_size)

    @staticmethod
    def _detect_corruption(entries, total_count, total_size) -> str | None:
        """dumpheap -stat 캡처가 깨졌는지 보존법칙으로 판정한다.

        진짜 -stat 은 타입당 1행이고, Total = 모든 행의 합이다. 어떤 단일 타입의
        count/size 도 Total 을 넘을 수 없다. 넘으면 스트림 desync 로 주소가
        count/size 칸에 섞여 들어온 것 → 신뢰 불가.
        """
        if not entries:
            return None  # 빈 결과는 별도 처리 (호출측에서 parse 실패 메시지)

        if total_count <= 0 or total_size <= 0:
            return ("missing/zero 'Total ... objects ... bytes' line "
                    f"(parsed {len(entries)} rows but no valid Total — likely truncated)")

        # 개별 행이 전체를 초과 (단일 타입 > 힙 전체 = 물리적 불가능)
        for e in entries:
            if e["count"] > total_count:
                return (f"type {e['name']!r} count {e['count']:,} exceeds heap Total "
                        f"{total_count:,} objects")
            if e["size"] > total_size:
                return (f"type {e['name']!r} size {e['size']:,} exceeds heap Total "
                        f"{total_size:,} bytes")

        # 행 합이 Total 을 유의미하게 초과 (5% slack: -stat 은 정확히 일치하나
        # 파싱 누락/중복 여지를 둠)
        sum_count = sum(e["count"] for e in entries)
        sum_size = sum(e["size"] for e in entries)
        if sum_count > total_count * 1.05:
            return (f"sum of per-type counts {sum_count:,} exceeds heap Total "
                    f"{total_count:,} objects by >5%")
        if sum_size > total_size * 1.05:
            return (f"sum of per-type sizes {sum_size:,} exceeds heap Total "
                    f"{total_size:,} bytes by >5%")
        return None


class StackAnalyzer:
    """clrstack -all 결과를 파싱하여 동일 콜스택 스레드를 그룹화한다."""

    _THREAD_HEADER_RE = re.compile(r"^OS Thread Id:\s*0x([0-9a-fA-F]+)(?:\s*\((\d+)\))?")

    _FRAME_RE = re.compile(r"^[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+(.+)$")

    def query(self, args: list[str], execute_fn) -> str:
        parser = argparse.ArgumentParser(prog="@stack-groups", add_help=False)
        parser.add_argument("--max-frames", type=int, default=0)
        parser.add_argument("--limit", type=int, default=0)
        try:
            opts = parser.parse_args(args)
        except SystemExit:
            return "[ERROR] Usage: @stack-groups [--max-frames=N] [--limit=N]"

        raw = execute_fn("clrstack -all")
        threads = self._parse(raw)

        if not threads:
            return "[ERROR] No threads found in clrstack -all output."

        # Group by identical call stack
        groups: dict[tuple, list] = {}
        for t in threads:
            frames = t["frames"]
            if opts.max_frames > 0:
                frames = frames[:opts.max_frames]
            key = tuple(frames)
            groups.setdefault(key, []).append(t)

        # Sort by thread count descending
        sorted_groups = sorted(groups.items(), key=lambda g: len(g[1]), reverse=True)

        if opts.limit > 0:
            sorted_groups = sorted_groups[:opts.limit]

        # Format output
        lines = []
        for i, (frames, group_threads) in enumerate(sorted_groups, 1):
            count = len(group_threads)
            ids = [f"0x{t['os_id']}" for t in group_threads]
            if len(ids) > 5:
                id_str = ", ".join(ids[:5]) + f" (+{len(ids) - 5} more)"
            else:
                id_str = ", ".join(ids)

            lines.append(f"=== Group {i}: {count} thread(s) ===")
            lines.append(f"Thread IDs: {id_str}")
            lines.append("Call Stack:")
            if frames:
                for frame in frames:
                    lines.append(f"  {frame}")
            else:
                lines.append("  (empty stack)")
            lines.append("")

        total_threads = len(threads)
        total_groups = len(sorted_groups)
        lines.append(f"[{total_groups} groups from {total_threads} threads]")
        return "\n".join(lines)

    def _parse(self, raw: str) -> list[dict]:
        threads = []
        current = None
        in_frames = False

        for line in raw.split("\n"):
            m = self._THREAD_HEADER_RE.match(line)
            if m:
                if current:
                    threads.append(current)
                current = {"os_id": m.group(1), "index": m.group(2) or "?", "frames": []}
                in_frames = False
                continue

            if current is None:
                continue

            # "Child SP" header line → frames start next
            if "Child SP" in line:
                in_frames = True
                continue

            if in_frames:
                fm = self._FRAME_RE.match(line.strip())
                if fm:
                    call_site = fm.group(1).strip()
                    # Skip native/internal frames (addresses differ per thread, break grouping)
                    if not call_site.startswith("["):
                        current["frames"].append(call_site)
                elif line.strip() == "":
                    in_frames = False

        if current:
            threads.append(current)

        return threads

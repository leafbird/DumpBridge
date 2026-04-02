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

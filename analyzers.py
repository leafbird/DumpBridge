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

    _ROW_RE = re.compile(r"^\s*([0-9a-fA-F]+)\s+(\d+)\s+(\d+)\s+(.+)$")
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
                    "count": int(m.group(2)),
                    "size": int(m.group(3)),
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
    """Placeholder - will be implemented in Task 3."""

    def query(self, args: list[str], execute_fn) -> str:
        return "[ERROR] @stack-groups not yet implemented."

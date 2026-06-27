"""DumpBridge TCP 클라이언트 유틸리티."""

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from protocol import recv_frame, TruncatedFrame


def main():
    parser = argparse.ArgumentParser(description="DumpBridge client")
    parser.add_argument("command", help="Command to send to DumpBridge")
    parser.add_argument("--port", type=int, default=9999, help="TCP port (default: 9999)")
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    args = parser.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((args.host, args.port))
        s.sendall(args.command.encode("utf-8"))
        s.shutdown(socket.SHUT_WR)

        result = recv_frame(s.recv)   # 길이 프레임 — 절단되면 TruncatedFrame

        sys.stdout.buffer.write(result)
        if result and not result.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
    except ConnectionRefusedError:
        print("ERROR: Cannot connect to DumpBridge. Is the server running?", file=sys.stderr)
        sys.exit(1)
    except TruncatedFrame as e:
        print(f"ERROR: truncated response: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        s.close()


if __name__ == "__main__":
    main()

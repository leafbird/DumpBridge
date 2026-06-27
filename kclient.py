import socket, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from protocol import recv_frame, TruncatedFrame

host, port, cmd = "127.0.0.1", 9999, sys.argv[1]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
try:
    s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30000, 5000))
except Exception:
    pass
s.settimeout(7200)            # 2h app timeout
s.connect((host, port))
s.sendall(cmd.encode("utf-8"))
s.shutdown(socket.SHUT_WR)
try:
    payload = recv_frame(s.recv)   # 길이 프레임 — 절단되면 TruncatedFrame
except TruncatedFrame as e:
    sys.stderr.write(f"[kclient] TRUNCATED RESPONSE: {e}\n")
    sys.exit(2)
finally:
    s.close()
sys.stdout.buffer.write(payload)
sys.stdout.buffer.flush()

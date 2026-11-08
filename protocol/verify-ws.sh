#!/bin/bash
# 校验 OneBot v11 正向 WS 是否能握手（不依赖 websockets 库，手写 HTTP Upgrade）
# 用法: bash verify-ws.sh [token]
set -u
TOKEN="${1:-${QBOT_WS_TOKEN:-change-me-token}}"

python3 - "$TOKEN" <<'PY'
import base64, os, socket, sys

token = sys.argv[1]
host, port = "127.0.0.1", 3001

def handshake(path):
    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.socket(); s.settimeout(5)
    try:
        s.connect((host, port))
    except Exception as e:
        return None, f"TCP 连接失败: {e}"
    try:
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        s.sendall(req.encode())
        data = s.recv(4096).decode("utf8", "replace")
        return data, None
    except Exception as e:
        return None, f"握手中断: {e}"
    finally:
        s.close()

def first_line(resp):
    return resp.split("\r\n", 1)[0] if resp else "(无响应)"

ok = True

resp, err = handshake(f"/?access_token={token}")
if err:
    print(f"[verify] 带 token 握手: 失败 — {err}")
    ok = False
elif " 101 " in first_line(resp):
    print(f"[verify] 带 token 握手: OK — {first_line(resp)}")
    print(f"[verify] 可用地址: ws://{host}:{port}?access_token={token}")
else:
    print(f"[verify] 带 token 握手: 失败 — {first_line(resp)}")
    ok = False

# 无 token 应当被拒（401），用于确认 token 生效
resp2, err2 = handshake("/")
if err2:
    print(f"[verify] 无 token 握手: {err2}（不影响结论）")
else:
    print(f"[verify] 无 token 握手: {first_line(resp2)}（预期 401/403，若 101 说明 token 没生效）")

sys.exit(0 if ok else 1)
PY

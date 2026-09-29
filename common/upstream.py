"""Shared upstream CONNECT logic for pool-worker and hash-gate.

Opens a tunnel to host:port through one HTTP or SOCKS5 proxy node, with the
node's own credentials when present. Plain sockets, stdlib only. Returns the
connected socket; raises OSError when the upstream refuses or times out.
"""

from __future__ import annotations

import base64
import socket


def read_exact(sock: socket.socket, size: int, timeout_s: float) -> bytes:
    data = b""
    sock.settimeout(timeout_s)
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def read_http_head(sock: socket.socket, timeout_s: float) -> str:
    data = b""
    sock.settimeout(timeout_s)
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            raise OSError("upstream http head too large")
    return data.decode("latin-1")


def drain_socks5_address(sock: socket.socket, kind: int, timeout_s: float) -> None:
    if kind == 0x01:
        read_exact(sock, 4 + 2, timeout_s)
    elif kind == 0x03:
        length = read_exact(sock, 1, timeout_s)
        read_exact(sock, (length[0] if length else 0) + 2, timeout_s)
    elif kind == 0x04:
        read_exact(sock, 16 + 2, timeout_s)
    else:
        raise OSError("upstream socks5 bad address type")


def _connect_http(
    sock: socket.socket,
    node: dict[str, object],
    host: str,
    port: int,
    timeout_s: float,
) -> None:
    request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    username = node.get("username")
    password = node.get("password")
    if type(username) is str and type(password) is str:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        request += f"Proxy-Authorization: Basic {token}\r\n"
    request += "\r\n"
    sock.sendall(request.encode())
    response = read_http_head(sock, timeout_s)
    first = response.splitlines()[0] if response else ""
    if not (first.startswith("HTTP/1.1 200") or first.startswith("HTTP/1.0 200")):
        raise OSError(f"upstream http rejected: {first}")


def _connect_socks5(
    sock: socket.socket,
    node: dict[str, object],
    host: str,
    port: int,
    timeout_s: float,
) -> None:
    username = node.get("username")
    password = node.get("password")
    has_creds = type(username) is str and type(password) is str
    sock.sendall(b"\x05\x02\x00\x02" if has_creds else b"\x05\x01\x00")
    choice = read_exact(sock, 2, timeout_s)
    if len(choice) != 2 or choice[0] != 0x05:
        raise OSError("upstream socks5 handshake failed")
    if choice[1] == 0x02:
        user = str(username).encode()
        word = str(password).encode()
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(word)]) + word)
        auth = read_exact(sock, 2, timeout_s)
        if len(auth) != 2 or auth[1] != 0x00:
            raise OSError("upstream socks5 auth failed")
    elif choice[1] != 0x00:
        raise OSError("upstream socks5 needs unsupported auth")
    try:
        packed = socket.inet_aton(host)
        connect = b"\x05\x01\x00\x01" + packed + port.to_bytes(2, "big")
    except OSError:
        encoded = host.encode()
        connect = (
            b"\x05\x01\x00\x03"
            + bytes([len(encoded)])
            + encoded
            + port.to_bytes(2, "big")
        )
    sock.sendall(connect)
    reply = read_exact(sock, 4, timeout_s)
    if len(reply) != 4 or reply[1] != 0x00:
        raise OSError("upstream socks5 connect failed")
    drain_socks5_address(sock, reply[3], timeout_s)


def connect_upstream(
    node: dict[str, object], host: str, port: int, timeout_s: float
) -> socket.socket:
    kind = node.get("type")
    server = node.get("server")
    node_port = node.get("port")
    if kind not in {"http", "socks5"}:
        raise ValueError(f"unsupported upstream type: {kind}")
    if type(server) is not str or not server.strip():
        raise ValueError("upstream node has no server")
    if type(node_port) is not int or not 1 <= node_port <= 65535:
        raise ValueError("upstream node has no port")
    sock = socket.create_connection((server.strip(), node_port), timeout=timeout_s)
    try:
        if kind == "http":
            _connect_http(sock, node, host, port, timeout_s)
        else:
            _connect_socks5(sock, node, host, port, timeout_s)
    except OSError:
        sock.close()
        raise
    return sock

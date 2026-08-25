#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sing-box 配置生成器
解析代理 URL，生成 sing-box 配置文件

支持协议: vless, vmess, trojan, ss, hysteria2 (hy2), tuic, anytls, socks5
"""

import os
import sys
import json
import base64
from urllib.parse import parse_qs, unquote


SUPPORTED_PROTOCOLS = "vless, vmess, trojan, ss, hysteria2, tuic, anytls, socks5"


def mask(s):
    return f"{s[:2]}***{s[-2:]}" if len(s) > 4 else "****"


def parse_vless(content):
    uuid, rest = content.split("@", 1)
    if "?" in rest:
        host_port, params_str = rest.split("?", 1)
    else:
        host_port, params_str = rest, ""

    if ":" in host_port:
        address, port = host_port.rsplit(":", 1)
    else:
        address, port = host_port, "443"

    params = parse_qs(params_str)
    security = params.get("security", ["none"])[0]
    network = params.get("type", ["tcp"])[0]
    sni = params.get("sni", [address])[0]
    fp = params.get("fp", ["chrome"])[0]
    flow = params.get("flow", [""])[0]
    pbk = params.get("pbk", [""])[0]
    sid = params.get("sid", [""])[0]
    host = params.get("host", [sni])[0]
    path = unquote(params.get("path", ["/"])[0])

    print(f"[INFO] VLESS -> {mask(address)}:{port} (security: {security}, network: {network})")

    outbound = {
        "type": "vless",
        "server": address,
        "server_port": int(port),
        "uuid": uuid,
        "flow": flow
    }

    if security == "reality":
        outbound["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp},
            "reality": {
                "enabled": True,
                "public_key": pbk,
                "short_id": sid
            }
        }
    elif security == "tls":
        outbound["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp}
        }

    if network == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": path,
            "headers": {"Host": host}
        }
    elif network == "grpc":
        service_name = params.get("serviceName", [""])[0]
        outbound["transport"] = {
            "type": "grpc",
            "service_name": service_name
        }

    return outbound


def parse_vmess(content):
    try:
        padding = 4 - len(content) % 4
        if padding != 4:
            content += "=" * padding
        decoded = base64.b64decode(content).decode("utf-8")
        vm = json.loads(decoded)
    except Exception as e:
        raise ValueError(f"VMess 解析失败: {e}")

    address = vm.get("add", "")
    port = int(vm.get("port", 443))
    uuid = vm.get("id", "")
    aid = int(vm.get("aid", 0))
    network = vm.get("net", "tcp")
    tls = vm.get("tls", "")
    sni = vm.get("sni", "") or vm.get("host", address)
    host = vm.get("host", address)
    path = vm.get("path", "/")
    fp = vm.get("fp", "chrome")

    print(f"[INFO] VMess -> {mask(address)}:{port} (network: {network}, tls: {tls})")

    outbound = {
        "type": "vmess",
        "server": address,
        "server_port": port,
        "uuid": uuid,
        "alter_id": aid,
        "security": "auto",
        "transport": {}
    }

    if tls == "tls":
        outbound["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp}
        }

    if network == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": path,
            "headers": {"Host": host}
        }
    else:
        outbound["transport"] = {"type": network}

    return outbound


def parse_trojan(content):
    password, rest = content.split("@", 1)
    if "?" in rest:
        host_port, params_str = rest.split("?", 1)
    else:
        host_port, params_str = rest, ""

    if ":" in host_port:
        address, port = host_port.rsplit(":", 1)
    else:
        address, port = host_port, "443"

    params = parse_qs(params_str)
    sni = params.get("sni", [address])[0]
    network = params.get("type", ["tcp"])[0]
    host = params.get("host", [sni])[0]
    path = unquote(params.get("path", ["/"])[0])
    fp = params.get("fp", ["chrome"])[0]

    print(f"[INFO] Trojan -> {mask(address)}:{port} (network: {network})")

    outbound = {
        "type": "trojan",
        "server": address,
        "server_port": int(port),
        "password": password,
        "tls": {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp}
        }
    }

    if network == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": path,
            "headers": {"Host": host}
        }
    else:
        outbound["transport"] = {"type": network}

    return outbound


def parse_shadowsocks(content):
    try:
        if "@" in content:
            encoded, server_part = content.split("@", 1)
            decoded = base64.b64decode(encoded + "==").decode("utf-8")
            method, password = decoded.split(":", 1)
            address, port = server_part.split(":", 1)
        else:
            decoded = base64.b64decode(content + "==").decode("utf-8")
            if "@" in decoded:
                user_part, server_part = decoded.split("@", 1)
                method, password = user_part.split(":", 1)
                address, port = server_part.split(":", 1)
            else:
                raise ValueError("无法解析")
    except Exception as e:
        raise ValueError(f"Shadowsocks 解析失败: {e}")

    print(f"[INFO] Shadowsocks -> {mask(address)}:{port}")

    return {
        "type": "shadowsocks",
        "server": address,
        "server_port": int(port),
        "method": method,
        "password": password
    }


def parse_hysteria2(content):
    auth, rest = content.split("@", 1)
    if "?" in rest:
        host_port, params_str = rest.split("?", 1)
    else:
        host_port, params_str = rest, ""

    if ":" in host_port:
        address, port = host_port.rsplit(":", 1)
    else:
        address, port = host_port, "443"

    port = port.strip("/")

    params = parse_qs(params_str)
    sni = params.get("sni", [address])[0]
    insecure = params.get("insecure", ["0"])[0] == "1"
    alpn = params.get("alpn", ["h3"])[0]
    obfs = params.get("obfs", [""])[0]
    obfs_pass = params.get("obfs_password", [""])[0]

    print(f"[INFO] Hysteria2 -> {mask(address)}:{port} (sni: {sni}, insecure: {insecure})")

    outbound = {
        "type": "hysteria2",
        "server": address,
        "server_port": int(port),
        "password": auth,
        "tls": {
            "enabled": True,
            "server_name": sni,
            "insecure": insecure,
            "alpn": [alpn]
        }
    }

    if obfs:
        outbound["obfs"] = {
            "type": obfs,
            "password": obfs_pass
        }

    return outbound


def parse_tuic(content):
    uuid_password, rest = content.split("@", 1)
    if ":" in uuid_password:
        uuid, password = uuid_password.split(":", 1)
    else:
        uuid, password = uuid_password, uuid_password

    if "?" in rest:
        host_port, params_str = rest.split("?", 1)
    else:
        host_port, params_str = rest, ""

    if ":" in host_port:
        address, port = host_port.rsplit(":", 1)
    else:
        address, port = host_port, "443"

    params = parse_qs(params_str)
    sni = params.get("sni", [address])[0]
    cc = params.get("congestion_control", ["bbr"])[0]
    alpn = params.get("alpn", ["h3"])[0]
    udp_mode = params.get("udp_relay_mode", ["native"])[0]
    insecure = params.get("allow_insecure", ["0"])[0] == "1"

    print(f"[INFO] TUIC -> {mask(address)}:{port} (sni: {sni}, cc: {cc})")

    return {
        "type": "tuic",
        "server": address,
        "server_port": int(port),
        "uuid": uuid,
        "password": password,
        "congestion_control": cc,
        "udp_relay_mode": udp_mode,
        "tls": {
            "enabled": True,
            "server_name": sni,
            "insecure": insecure,
            "alpn": [alpn]
        }
    }


def parse_anytls(content):
    uuid, rest = content.split("@", 1)
    if "?" in rest:
        host_port, params_str = rest.split("?", 1)
    else:
        host_port, params_str = rest, ""

    if ":" in host_port:
        address, port = host_port.rsplit(":", 1)
    else:
        address, port = host_port, "443"

    port = port.strip("/")

    params = parse_qs(params_str)
    sni = params.get("sni", [address])[0]
    fp = params.get("fp", ["chrome"])[0]
    insecure = params.get("allow_insecure", params.get("insecure", ["0"]))[0] == "1"

    print(f"[INFO] AnyTLS -> {mask(address)}:{port} (sni: {sni}, fp: {fp})")

    return {
        "type": "anytls",
        "server": address,
        "server_port": int(port),
        "uuid": uuid,
        "password": uuid,
        "tls": {
            "enabled": True,
            "server_name": sni,
            "insecure": insecure,
            "utls": {"enabled": True, "fingerprint": fp}
        }
    }


def parse_socks5(content):
    """socks5 内容 [user:pass@]host:port → 返回 sing-box socks 出站配置"""
    username = ""
    password = ""
    if "@" in content:
        auth, rest = content.rsplit("@", 1)
        if ":" in auth:
            username, password = auth.split(":", 1)
        else:
            username = auth
    else:
        rest = content

    rest = rest.strip("/")
    if rest.startswith("["):
        host, _, tail = rest[1:].partition("]")
        port = tail.lstrip(":") if tail.startswith(":") else "1080"
    elif ":" in rest:
        host, port = rest.rsplit(":", 1)
    else:
        host, port = rest, "1080"

    try:
        port = int(port)
    except ValueError:
        port = 1080

    print(f"[INFO] SOCKS5 -> {mask(host)}:{port}")

    outbound = {
        "type": "socks",
        "server": host,
        "server_port": port,
    }
    if username:
        outbound["username"] = username
    if password:
        outbound["password"] = password

    with open("use_external_socks.txt", "w") as f:
        f.write(f"socks5://{content.split('#')[0]}")

    return outbound


def generate_config(url):
    """根据 URL 生成 sing-box 配置，返回 (config, protocol) 或抛出 ValueError"""
    if url.startswith("vless://"):
        protocol = "vless"
        content = url[8:]
    elif url.startswith("vmess://"):
        protocol = "vmess"
        content = url[8:]
    elif url.startswith("trojan://"):
        protocol = "trojan"
        content = url[9:]
    elif url.startswith("ss://"):
        protocol = "ss"
        content = url[5:]
    elif url.startswith(("hysteria2://", "hy2://")):
        protocol = "hy2"
        content = url.split("://", 1)[1]
    elif url.startswith("tuic://"):
        protocol = "tuic"
        content = url[7:]
    elif url.startswith("anytls://"):
        protocol = "anytls"
        content = url[9:]
    elif url.startswith(("socks5://", "socks://")):
        protocol = "socks5"
        content = url.split("://", 1)[1]
    else:
        proto = url.split("://")[0] if "://" in url else url[:20]
        raise ValueError(f"不支持的协议: {proto}")

    if "#" in content:
        content = content.rsplit("#", 1)[0]

    parsers = {
        "vless": parse_vless,
        "vmess": parse_vmess,
        "trojan": parse_trojan,
        "ss": parse_shadowsocks,
        "hy2": parse_hysteria2,
        "tuic": parse_tuic,
        "anytls": parse_anytls,
        "socks5": parse_socks5,
    }

    outbound = parsers[protocol](content)

    socks_port = int(os.environ.get("SOCKS_PORT", "10808").strip())

    config = {
        "log": {"level": "warn"},
        "inbounds": [
            {"type": "mixed", "listen": "127.0.0.1", "listen_port": socks_port}
        ],
        "outbounds": [outbound]
    }

    return config, protocol


def setup_proxy(url):
    """
    解析代理 URL，生成 sing-box 配置文件。

    返回:
        {"success": True, "socks5": "socks5://127.0.0.1:10808", "protocol": "hy2"}
        {"success": False, "error": "...", "supported": "..."}
    """
    if not url or not url.strip():
        return {"success": False, "error": "PROXY_CONTENT 未配置或为空", "supported": SUPPORTED_PROTOCOLS}

    url = url.strip()
    socks_port = int(os.environ.get("SOCKS_PORT", "10808").strip())

    try:
        config, protocol = generate_config(url)
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "input": url[:80] + "..." if len(url) > 80 else url,
            "supported": SUPPORTED_PROTOCOLS
        }

    if config is None:
        return {"success": True, "socks5": "", "protocol": "socks5"}

    with open("sing-box_config.json", "w") as f:
        json.dump(config, f, indent=2)

    socks5_addr = f"socks5://127.0.0.1:{socks_port}"
    with open("socks5_proxy.txt", "w") as f:
        f.write(socks5_addr)

    print(f"[INFO] ✅ sing-box 配置已生成 → {socks5_addr}")

    return {"success": True, "socks5": socks5_addr, "protocol": protocol}


def main():
    url = os.environ.get("PROXY_CONTENT", "").strip()
    result = setup_proxy(url)

    with open("proxy_result.json", "w") as f:
        json.dump(result, f)

    if not result["success"]:
        print(f"[ERROR] {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import os
import codecs
import collections
import sys
import json
import time
import uuid
import socket
import subprocess
import signal
import threading
import queue
import secrets
import hmac
import hashlib
import random
import shutil
import mimetypes
import struct
import fcntl
import re
import ipaddress
from urllib.parse import urlparse, parse_qs, unquote, quote
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Settings live in ~/.config/orbit; older installs kept them next to the daemon
ORBIT_CONFIG_DIR = os.path.expanduser('~/.config/orbit')
CONFIG_DIR = ORBIT_CONFIG_DIR if os.path.isdir(ORBIT_CONFIG_DIR) else BASE_DIR
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
def apk_file():
    """The phone app to hand out: Orbit.apk, or an APK left by an older build."""
    for name in ('Orbit.apk', 'PCAuthenticator.apk', 'PCConnect.apk', 'authenticator.apk'):
        path = os.path.join(STATIC_DIR, name)
        if os.path.exists(path):
            return path
    return os.path.join(STATIC_DIR, 'Orbit.apk')
STAGING_DIR = os.path.join(BASE_DIR, 'staging')
os.makedirs(STAGING_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "web_port": 1760,
    "auth_timeout_seconds": 35,
    "failmode": "secure",
    "allowed_usb_serials": [],
    "paired_clients": {}
}

active_phone_state = {
    "ip": None,
    "port": 1761,
    "client_name": "Android Phone",
    "battery_level": None,
    "is_charging": False,
    "last_seen": 0,
    "auth_token": None,
    "model": None,
    "manufacturer": None,
    "android_version": None,
    "storage_free": None,
    "storage_total": None
}

staged_files = {}  # token -> {"filepath": ..., "filename": ..., "size": ..., "created_at": ...}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
                return {**DEFAULT_CONFIG, **cfg}
        except Exception as e:
            sys.stderr.write(f"Error loading config: {e}\n")
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    # Holds every pairing token and HMAC secret: only the owner may read it
    try:
        tmp = CONFIG_FILE + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        sys.stderr.write(f"Error saving config: {e}\n")

def secure_config_permissions():
    for path, mode in ((os.path.dirname(CONFIG_FILE), 0o700), (CONFIG_FILE, 0o600)):
        try:
            os.chmod(path, mode)
        except OSError:
            pass

# Local tools (tray, CLI, send) prove they run as this user by reading a
# 0600 key file; other local processes and sandboxed apps can't.
LOCAL_TOKEN_FILE = os.path.join(os.path.dirname(CONFIG_FILE), 'local_token')
LOCAL_TOKEN = ''

def init_local_token():
    global LOCAL_TOKEN
    LOCAL_TOKEN = secrets.token_hex(32)
    fd = os.open(LOCAL_TOKEN_FILE + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(LOCAL_TOKEN)
    os.replace(LOCAL_TOKEN_FILE + '.tmp', LOCAL_TOKEN_FILE)

def has_local_token(handler):
    if not LOCAL_TOKEN:
        return False
    supplied = handler.headers.get('X-Local-Token', '')
    if not supplied:
        for part in handler.headers.get('Cookie', '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == 'pc_local':
                supplied = value
    return bool(supplied) and hmac.compare_digest(supplied, LOCAL_TOKEN)

def get_laptop_id():
    cfg = load_config()
    if 'device_id' not in cfg:
        cfg['device_id'] = str(uuid.uuid4())
        save_config(cfg)
    return cfg['device_id']

def get_auth_token_from_request(handler):
    auth_header = handler.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:].strip()
    # Never from the URL: URLs end up in logs, the tunnel's request inspector and other apps
    return None

def is_tunnel_request(handler):
    headers = handler.headers
    return bool(
        headers.get('CF-Ray') or
        headers.get('CF-Connecting-IP') or
        headers.get('CDN-Loop') or
        headers.get('ngrok-trace-id') or
        headers.get('ngrok-agent-ips') or
        (headers.get('X-Forwarded-Proto') and headers.get('X-Forwarded-For'))
    )

def peer_uid(handler):
    """Which local account opened this connection (from /proc/net/tcp), or None."""
    try:
        cport = handler.client_address[1]
        sport = handler.server.server_address[1]
    except (AttributeError, IndexError):
        return None
    for fn in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(fn) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if (int(parts[1].rsplit(':', 1)[1], 16) == cport
                            and int(parts[2].rsplit(':', 1)[1], 16) == sport):
                        return int(parts[7])
        except (OSError, ValueError, IndexError, StopIteration):
            continue
    return None

def is_lan_or_wifi_request(handler):
    if is_tunnel_request(handler):
        return False
    client_ip = handler.client_address[0]
    try:
        ip = ipaddress.ip_address(client_ip)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except Exception:
        return False

def get_effective_client_ip(handler):
    if is_tunnel_request(handler):
        for header_name in ('CF-Connecting-IP', 'ngrok-agent-ips', 'X-Forwarded-For', 'X-Real-IP'):
            val = handler.headers.get(header_name)
            if val:
                return val.split(',')[0].strip()
    return handler.client_address[0]

def is_foreign_browser_request(handler):
    """True when a web page from another site sent this request.

    Browsers attach Origin (and Sec-Fetch-Site) to cross-site requests; local tools
    like the tray app and pc-connect CLI send neither. Without this check any website
    open on the laptop could drive the API through localhost.
    """
    origin = handler.headers.get('Origin')
    if origin:
        try:
            u = urlparse(origin)
            host = (u.hostname or '').lower()
            port = u.port or (443 if u.scheme == 'https' else 80)
        except ValueError:
            return True
        own_port = handler.server.server_address[1]
        return not (host in ('localhost', '127.0.0.1', '::1') and port == own_port)
    return handler.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site')

def authenticate_client(handler, cfg):
    client_ip = get_effective_client_ip(handler)
    # Local requests (e.g. from desktop tray or pc-connect CLI) are always authorized,
    # UNLESS they arrived through an external reverse proxy / Cloudflare tunnel,
    # or were sent by a web page from another site.
    if (handler.client_address[0] in ('127.0.0.1', '::1', 'localhost')
            and not is_tunnel_request(handler) and not is_foreign_browser_request(handler)
            and has_local_token(handler)):
        return {"client_name": "Local PC", "secret_key": "", "local": True}, "local"

    paired = cfg.get('paired_clients', {})
    token = get_auth_token_from_request(handler)
    if not token or token not in paired:
        return None, token

    client_info = paired[token]
    client_info['last_seen'] = time.time()
    if not is_tunnel_request(handler):
        # Only a direct connection tells us where the phone is. Through the tunnel this would be
        # the phone's public mobile address, and the computer would then send it the token in clear.
        client_info['ip'] = client_ip
        active_phone_state['ip'] = client_ip
    active_phone_state['client_name'] = client_info.get('client_name', 'Android Phone')
    active_phone_state['last_seen'] = time.time()
    active_phone_state['auth_token'] = token

    return client_info, token

def get_local_ip():
    try:
        out = subprocess.check_output(['ip', '-4', 'addr', 'show'], stderr=subprocess.DEVNULL).decode()
        import re
        m = re.search(r'(?:wlan\d|wlp\w+|eth\d|enp\w+):.*?\sinet\s+([0-9.]+)/', out, re.DOTALL)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('1.1.1.1', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

# ==========================================
# Dolphin Dynamic Service Menu Helpers
# ==========================================

def update_dolphin_servicemenu():
    try:
        cfg = load_config()
        paired = cfg.get('paired_clients', {})
        
        valid_clients = []
        for token, client in paired.items():
            cid = client.get('client_id') or token
            if cid == "phone-auto-test" and len(paired) > 1:
                continue
            name = client.get('client_name') or 'Android Phone'
            valid_clients.append({"token": token, "client_id": cid, "name": name})

        desktop_path = os.path.expanduser('~/.local/share/kio/servicemenus/orbit-send.desktop')
        old_path = os.path.expanduser('~/.local/share/kio/servicemenus/pc_connect.desktop')
        send_bin = os.path.expanduser('~/.local/bin/orbit-send')
        if not os.path.exists(send_bin):
            send_bin = os.path.expanduser('~/.local/bin/pc-connect-send')
        os.makedirs(os.path.dirname(desktop_path), exist_ok=True)

        if len(valid_clients) == 1:
            dev_name = valid_clients[0]['name']
            content = f"""[Desktop Entry]
Type=Service
ServiceTypes=KonqPopupMenu/Plugin
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
MimeType=all/all;all/allfiles;inode/directory;application/octet-stream;
Actions=sendToDevice;
X-KDE-Priority=TopLevel
X-KDE-Submenu=Orbit

[Desktop Action sendToDevice]
Name=Send to {dev_name}
Icon=phone
Exec={send_bin} %U
"""
        elif len(valid_clients) > 1:
            actions_list = ";".join([f"sendDevice{i}" for i in range(len(valid_clients))]) + ";"
            actions_blocks = []
            for i, c in enumerate(valid_clients):
                actions_blocks.append(f"""[Desktop Action sendDevice{i}]
Name=Send to {c['name']}
Icon=phone
Exec={send_bin} --device "{c['client_id']}" %U
""")
            content = f"""[Desktop Entry]
Type=Service
ServiceTypes=KonqPopupMenu/Plugin
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
MimeType=all/all;all/allfiles;inode/directory;application/octet-stream;
Actions={actions_list}
X-KDE-Priority=TopLevel
X-KDE-Submenu=Orbit

""" + "\n".join(actions_blocks)
        else:
            content = f"""[Desktop Entry]
Type=Service
ServiceTypes=KonqPopupMenu/Plugin
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
MimeType=all/all;all/allfiles;inode/directory;application/octet-stream;
Actions=sendViaOrbit;
X-KDE-Priority=TopLevel
X-KDE-Submenu=Orbit

[Desktop Action sendViaOrbit]
Name=Send with Orbit
Icon=phone
Exec={send_bin} %U
"""
        with open(desktop_path, 'w', encoding='utf-8') as f:
            f.write(content)
        try:
            if os.path.exists(old_path):
                os.remove(old_path)
        except Exception:
            pass
        try:
            subprocess.run(['kbuildsycoca6'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
    except Exception as e:
        sys.stderr.write(f"update_dolphin_servicemenu error: {e}\n")

def unpair_device(target_identifier):
    cfg = load_config()
    paired = cfg.get('paired_clients', {})
    found_tokens = []
    device_name = "Android Phone"
    for tok, client in list(paired.items()):
        cid = client.get('client_id')
        if tok == target_identifier or cid == target_identifier or client.get('ip') == target_identifier:
            found_tokens.append(tok)
            device_name = client.get('client_name', device_name)

    if not found_tokens:
        return False, "Device not found"

    for tok in found_tokens:
        if tok in paired:
            del paired[tok]
    cfg['paired_clients'] = paired
    save_config(cfg)

    auth_mgr.broadcast_ndjson(json.dumps({"event": "unpaired", "target": target_identifier, "time": int(time.time())}) + "\n")

    if active_phone_state.get('auth_token') in found_tokens or active_phone_state.get('ip') == target_identifier:
        active_phone_state['ip'] = None
        active_phone_state['auth_token'] = None
        active_phone_state['client_name'] = "Android Phone"
        active_phone_state['battery_level'] = None
        active_phone_state['last_seen'] = 0

    update_dolphin_servicemenu()
    try:
        subprocess.Popen(['notify-send', '-i', 'dialog-warning', '-a', 'Orbit', 'Device Unpaired', f'{device_name} is no longer paired with this computer.'])
    except Exception:
        pass
    return True, device_name

# ==========================================
# Laptop Status, Weather & System Helpers
# ==========================================

_last_cpu_sample = None

def get_cpu_usage_pct():
    global _last_cpu_sample
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
        parts = [float(x) for x in line.split()[1:8]]
        idle = parts[3] + parts[4]
        total = sum(parts)
        if _last_cpu_sample:
            prev_idle, prev_total = _last_cpu_sample
            idle_delta = idle - prev_idle
            total_delta = total - prev_total
            _last_cpu_sample = (idle, total)
            if total_delta > 0:
                return max(0.0, min(100.0, round((1.0 - idle_delta / total_delta) * 100.0, 1)))
        _last_cpu_sample = (idle, total)
    except Exception:
        pass
    return 0.0

def is_pc_locked():
    try:
        out = subprocess.check_output(
            ['qdbus6', 'org.freedesktop.ScreenSaver', '/ScreenSaver', 'org.freedesktop.ScreenSaver.GetActive'],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
        if out.lower() == 'true':
            return True
        if out.lower() == 'false':
            return False
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ['loginctl', 'show-session', 'auto', '-p', 'LockedHint'],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
        if 'yes' in out.lower():
            return True
        if 'no' in out.lower():
            return False
    except Exception:
        pass
    return False

weather_cache = {"data": None, "timestamp": 0}

def get_weather_cached():
    global weather_cache
    now = time.time()
    if weather_cache["data"] and (now - weather_cache["timestamp"] < 900):
        return weather_cache["data"]
    try:
        req = urllib.request.Request('https://wttr.in/?format=j1', headers={'User-Agent': 'curl/8.0'})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            cur = data.get('current_condition', [{}])[0]
            area = data.get('nearest_area', [{}])[0]
            city = area.get('areaName', [{}])[0].get('value', 'Local Area')
            temp_c = cur.get('temp_C', '--')
            desc = cur.get('weatherDesc', [{}])[0].get('value', 'Clear')
            humidity = cur.get('humidity', '--')
            feels_like = cur.get('FeelsLikeC', temp_c)
            desc_clean = desc.strip()
            feels_str = f" (Feels {feels_like}°C)" if feels_like and str(feels_like) != str(temp_c) else ""
            formatted = f"{city} • {temp_c}°C{feels_str}, {desc_clean}"
            parsed = {
                "city": city,
                "temp_c": temp_c,
                "desc": desc_clean,
                "humidity": humidity,
                "feels_like_c": feels_like,
                "formatted": formatted,
                "cached_at": int(now)
            }
            weather_cache["data"] = parsed
            weather_cache["timestamp"] = now
            return parsed
    except Exception:
        if weather_cache["data"]:
            return weather_cache["data"]
        return None

def get_pc_system_status():
    locked = is_pc_locked()

    import glob
    bat_info = {"capacity": None, "percent": None, "status": "Unknown", "charging": False, "ac_online": False}
    for p in glob.glob('/sys/class/power_supply/BAT*'):
        try:
            with open(os.path.join(p, 'capacity')) as f:
                c = int(f.read().strip())
                bat_info['capacity'] = c
                bat_info['percent'] = c
            with open(os.path.join(p, 'status')) as f:
                st = f.read().strip()
                bat_info['status'] = st
                bat_info['charging'] = (st.lower() == 'charging')
            break
        except Exception:
            pass

    for p in glob.glob('/sys/class/power_supply/AC*') + glob.glob('/sys/class/power_supply/ADP*'):
        try:
            with open(os.path.join(p, 'online')) as f:
                bat_info['ac_online'] = (f.read().strip() == '1')
            break
        except Exception:
            pass

    cpu_pct = get_cpu_usage_pct()

    mem_info = {"total_gb": 0, "used_gb": 0, "used_pct": 0}
    try:
        mem = {}
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0])
        tot_kb = mem.get('MemTotal', 0)
        avail_kb = mem.get('MemAvailable', 0)
        used_kb = tot_kb - avail_kb
        mem_info = {
            "total_gb": round(tot_kb / 1048576.0, 1),
            "used_gb": round(used_kb / 1048576.0, 1),
            "used_pct": round((used_kb / tot_kb) * 100.0, 1) if tot_kb else 0
        }
    except Exception:
        pass

    uptime_info = {"seconds": 0, "formatted": "--"}
    try:
        with open('/proc/uptime') as f:
            up_sec = float(f.read().split()[0])
        h = int(up_sec // 3600)
        m = int((up_sec % 3600) // 60)
        uptime_info = {"seconds": int(up_sec), "formatted": f"{h}h {m}m"}
    except Exception:
        pass

    storage_info = {"total_gb": 0, "used_gb": 0, "free_gb": 0, "used_pct": 0, "formatted": "--"}
    try:
        du = shutil.disk_usage(os.path.expanduser('~'))
        storage_info = {
            "total_gb": round(du.total / (1024**3), 1),
            "used_gb": round(du.used / (1024**3), 1),
            "free_gb": round(du.free / (1024**3), 1),
            "used_pct": round((du.used / du.total) * 100.0, 1),
            "formatted": f"{round(du.free / (1024**3), 1)} GB free / {round(du.total / (1024**3), 1)} GB ({round((du.used / du.total) * 100.0)}%)"
        }
    except Exception:
        pass

    weather = get_weather_cached()
    weather_formatted = weather.get("formatted", "Weather Unavailable") if weather else "Weather Unavailable"

    cfg = load_config()
    port = cfg.get('web_port', 1760)
    local_ip = get_local_ip()

    return {
        "status": "ok",
        "hostname": socket.gethostname(),
        "locked": locked,
        "battery": bat_info,
        "cpu": {"usage_pct": cpu_pct},
        "memory": mem_info,
        "uptime": uptime_info,
        "storage": storage_info,
        "weather": weather,
        "weather_formatted": weather_formatted,
        "local_url": f"http://{local_ip}:{port}",
        "internet_url": tunnel_mgr.get_url() if 'tunnel_mgr' in globals() else None,
        "captive_portal_supported": os.path.exists(os.path.expanduser('~/bin/iiser-login.sh'))
    }

def trigger_pc_captive_login(body=None):
    body = body or {}
    script_path = os.path.expanduser('~/bin/iiser-login.sh')
    if os.path.exists(script_path) and not body.get('username'):
        try:
            res = subprocess.run([script_path], capture_output=True, text=True, timeout=8)
            out = res.stdout.strip()
            status = 'LIVE' if 'LIVE' in out or 'signed in' in out else 'OK'
            return {"status": "ok", "login_status": status, "output": out}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    username = body.get('username')
    password = body.get('password')
    # Credentials (possibly read from iiser-login.sh) only ever go to the campus gateway
    default_gateway = 'https://gateway.iisertvm.ac.in:8090/login.xml'
    gateway_url = body.get('gateway_url') or default_gateway
    if urlparse(gateway_url).netloc != urlparse(default_gateway).netloc:
        gateway_url = default_gateway
    if (not username or not password) and os.path.exists(script_path):
        try:
            with open(script_path, 'r') as f:
                content = f.read()
                m_user = re.search(r'USERNAME=["\'](.*?)["\']', content)
                m_pass = re.search(r'PASSWORD=["\'](.*?)["\']', content)
                if not username and m_user: username = m_user.group(1)
                if not password and m_pass: password = m_pass.group(1)
        except Exception:
            pass

    if not username or not password:
        return {"status": "error", "message": "Credentials not configured on PC"}

    try:
        import urllib.request, urllib.parse, ssl
        ctx = ssl.create_default_context()      # the gateway has a public certificate: check it
        data = urllib.parse.urlencode({
            'mode': '191',
            'username': username,
            'password': password,
            'a': str(int(time.time() * 1000)),
            'producttype': '0'
        }).encode('utf-8')
        req = urllib.request.Request(gateway_url, data=data, headers={
            'Origin': 'https://gateway.iisertvm.ac.in:8090',
            'Referer': 'https://gateway.iisertvm.ac.in:8090/httpclient.html',
            'Content-Type': 'application/x-www-form-urlencoded'
        })
        with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
            out = r.read().decode('utf-8', errors='ignore')
            if '{username}' in out and username:
                out = out.replace('{username}', username)
            status = 'LIVE' if 'LIVE' in out or 'signed in' in out else 'OK'
            return {"status": "ok", "login_status": status, "output": out}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def get_display_env():
    env = os.environ.copy()
    runtime_dir = env.get('XDG_RUNTIME_DIR') or f"/run/user/{os.getuid()}"
    env['XDG_RUNTIME_DIR'] = runtime_dir
    if not env.get('WAYLAND_DISPLAY'):
        try:
            for entry in os.listdir(runtime_dir):
                if entry.startswith('wayland-') and not entry.endswith('.lock'):
                    env['WAYLAND_DISPLAY'] = entry
                    break
        except Exception:
            pass
        if not env.get('WAYLAND_DISPLAY') and os.path.exists(os.path.join(runtime_dir, 'wayland-0')):
            env['WAYLAND_DISPLAY'] = 'wayland-0'
    if not env.get('DISPLAY'):
        env['DISPLAY'] = ':0'
    if env.get('WAYLAND_DISPLAY'):
        env['QT_QPA_PLATFORM'] = 'wayland'
    env.setdefault('XDG_CURRENT_DESKTOP', 'KDE')
    if not env.get('DBUS_SESSION_BUS_ADDRESS'):
        dbus_path = os.path.join(runtime_dir, 'bus')
        if os.path.exists(dbus_path):
            env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={dbus_path}'
    return env

def capture_pc_screenshot():
    tmp_out = f"/tmp/pc_screen_{secrets.token_hex(4)}.jpg"
    try:
        env = get_display_env()
        res = subprocess.run(
            ['spectacle', '-b', '-n', '-o', tmp_out],
            env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
        )
        if res.returncode == 0 and os.path.exists(tmp_out):
            with open(tmp_out, 'rb') as f:
                data = f.read()
            try:
                os.remove(tmp_out)
            except Exception:
                pass
            return data
    except Exception as e:
        sys.stderr.write(f"capture_pc_screenshot error: {e}\n")
    if os.path.exists(tmp_out):
        try: os.remove(tmp_out)
        except Exception: pass
    return None

def format_file_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

# Credentials the phone never gets to see or download, even with a valid token: if the token
# ever leaked, it must not be enough to take SSH keys, browser logins or Orbit's own secrets.
_SECRET_PATHS = [os.path.realpath(os.path.expanduser(p)) for p in (
    '~/.ssh', '~/.gnupg', '~/.config/orbit', '~/.config/pc-authenticator', '/etc/orbit',
    '~/.local/share/keyrings', '~/.password-store', '~/.config/ngrok', '~/.netrc',
    '~/.git-credentials', '~/.config/gh', '~/.aws', '~/.kube', '~/.docker', '~/.mozilla',
    '~/.config/google-chrome', '~/.config/chromium', '~/.config/BraveSoftware', '~/.pki',
    '~/.local/share/kwalletd', '~/.claude', '~/.claude.json', '~/bin/iiser-login.sh', '/etc/shadow')]

def _is_secret_path(path):
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except (TypeError, ValueError):
        return True
    return any(real == p or real.startswith(p + os.sep) for p in _SECRET_PATHS)

# One-file links the phone can hand to a video player or browser: a short random ticket in
# the URL instead of the pairing token (which other apps, logs and history would then hold).
_STREAM_TICKETS = {}
_STREAM_TICKETS_LOCK = threading.Lock()
STREAM_TICKET_TTL = 6 * 3600

def make_stream_ticket(path):
    ticket = secrets.token_urlsafe(24)
    now = time.time()
    with _STREAM_TICKETS_LOCK:
        for k, (_, exp) in list(_STREAM_TICKETS.items()):
            if exp < now:
                del _STREAM_TICKETS[k]
        _STREAM_TICKETS[ticket] = (path, now + STREAM_TICKET_TTL)
    return ticket

def stream_ticket_path(ticket):
    with _STREAM_TICKETS_LOCK:
        item = _STREAM_TICKETS.get(ticket or '')
    if not item or item[1] < time.time():
        return None
    return item[0]

def search_laptop_files(query, max_results=50):
    query = (query or '').strip()
    if not query:
        return []

    # 1. Try ultra-fast indexed search via fsearch-cli
    fsearch_bin = shutil.which('fsearch-cli') or os.path.expanduser('~/.local/bin/fsearch-cli')
    if os.path.exists(fsearch_bin) and os.access(fsearch_bin, os.X_OK):
        try:
            res = subprocess.run([fsearch_bin, '--json', query, str(max_results)],
                                 capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                parsed = json.loads(res.stdout)
                if isinstance(parsed, list):
                    return parsed
        except Exception:
            pass

    # 2. Fallback to manual walk if fsearch-cli is unavailable
    query_lower = query.lower()
    base_dirs = [
        os.path.expanduser('~/Downloads'),
        os.path.expanduser('~/Documents'),
        os.path.expanduser('~/Pictures'),
        os.path.expanduser('~/Videos'),
        os.path.expanduser('~/Desktop'),
        os.path.expanduser('~')
    ]
    user = os.environ.get('USER', 'lunarphoton')
    for mdir in [f'/run/media/{user}', f'/media/{user}', '/media', '/mnt']:
        if os.path.exists(mdir):
            try:
                for entry in os.scandir(mdir):
                    if entry.is_dir():
                        base_dirs.append(entry.path)
            except Exception:
                pass
    skip_dirs = {'.git', '.cache', 'node_modules', '.venv', '__pycache__', '.local', '.cargo', '.npm', '.rustup', '.gradle'}
    seen_paths = set()
    results = []

    include_hidden = query.startswith('.')

    for bdir in base_dirs:
        if not os.path.exists(bdir):
            continue
        for root, dirs, files in os.walk(bdir):
            dirs[:] = [d for d in dirs if (include_hidden or not d.startswith('.')) and d not in skip_dirs]
            for fn in files:
                if not include_hidden and fn.startswith('.'):
                    continue
                if query_lower in fn.lower():
                    full_p = os.path.join(root, fn)
                    if full_p in seen_paths:
                        continue
                    seen_paths.add(full_p)
                    try:
                        st = os.stat(full_p)
                        results.append({
                            "name": fn,
                            "path": full_p,
                            "is_dir": False,
                            "size": st.st_size,
                            "size_formatted": format_file_size(st.st_size),
                            "mtime": int(st.st_mtime),
                            "extension": os.path.splitext(fn)[1].lower()
                        })
                    except Exception:
                        pass
                    if len(results) >= max_results:
                        return results
    return results

# ==========================================
# KDE Plasma 6 & MPRIS Helpers
# ==========================================

def get_mpris_players():
    try:
        out = subprocess.check_output(['qdbus6'], stderr=subprocess.DEVNULL).decode('utf-8')
        return [l.strip() for l in out.splitlines() if l.strip().startswith('org.mpris.MediaPlayer2.')]
    except Exception:
        return []

def adjust_system_volume(direction):
    """Adjust system master volume via PipeWire (wpctl), PulseAudio (pactl), or ALSA (amixer)"""
    if direction in ("up", "VolumeUp"):
        if shutil.which('wpctl'):
            try:
                res = subprocess.run(['wpctl', 'set-volume', '-l', '1.0', '@DEFAULT_AUDIO_SINK@', '5%+'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
        if shutil.which('pactl'):
            try:
                res = subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', '+5%'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
        if shutil.which('amixer'):
            try:
                res = subprocess.run(['amixer', '-D', 'pulse', 'sset', 'Master', '5%+'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
    elif direction in ("down", "VolumeDown"):
        if shutil.which('wpctl'):
            try:
                res = subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', '5%-'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
        if shutil.which('pactl'):
            try:
                res = subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', '-5%'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
        if shutil.which('amixer'):
            try:
                res = subprocess.run(['amixer', '-D', 'pulse', 'sset', 'Master', '5%-'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
    elif direction in ("mute", "toggle_mute", "VolumeMute"):
        if shutil.which('wpctl'):
            try:
                res = subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', 'toggle'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
        if shutil.which('pactl'):
            try:
                res = subprocess.run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', 'toggle'], capture_output=True)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
    return False

def get_system_volume():
    try:
        if shutil.which('wpctl'):
            out = subprocess.check_output(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'], stderr=subprocess.DEVNULL).decode().strip()
            parts = out.split()
            if len(parts) >= 2 and parts[0] == 'Volume:':
                vol = int(round(float(parts[1]) * 100))
                is_muted = '[MUTED]' in out
                return vol, is_muted
        if shutil.which('pactl'):
            out = subprocess.check_output(['pactl', 'get-sink-volume', '@DEFAULT_SINK@'], stderr=subprocess.DEVNULL).decode()
            import re
            m = re.search(r'(\d+)%', out)
            if m:
                return int(m.group(1)), False
    except Exception:
        pass
    return 50, False

def mpris_player_name(bus_name):
    """Human name for an MPRIS player ("Firefox", "Spotify"), not its bus id."""
    try:
        identity = subprocess.check_output(['qdbus6', bus_name, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Identity'],
                                           stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        if identity:
            # Firefox forks report e.g. "Mozilla zen"
            identity = identity.removeprefix('Mozilla ').strip() or identity
            return identity[:1].upper() + identity[1:]
    except Exception:
        pass
    # org.mpris.MediaPlayer2.firefox.instance_1_89510 -> Firefox
    short = bus_name.replace('org.mpris.MediaPlayer2.', '').split('.instance')[0].split('.')[0]
    return short[:1].upper() + short[1:]

def get_mpris_status():
    vol, is_muted = get_system_volume()
    players = get_mpris_players()
    if not players:
        return {
            "has_player": False,
            "volume": vol,
            "is_muted": is_muted,
            "title": f"System Volume: {vol}%"
        }
    chosen = players[0]
    chosen_status = "Stopped"
    for p in players:
        try:
            st = subprocess.check_output(['qdbus6', p, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player.PlaybackStatus'], stderr=subprocess.DEVNULL).decode().strip()
            if st == "Playing":
                chosen = p
                chosen_status = st
                break
            elif st == "Paused":
                chosen = p
                chosen_status = st
        except Exception:
            pass

    title, artist, album, art_url = "", "", "", ""
    try:
        lines = subprocess.check_output(['qdbus6', chosen, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player.Metadata'], stderr=subprocess.DEVNULL).decode().splitlines()
        for line in lines:
            if line.startswith('xesam:title:'):
                title = line[len('xesam:title:'):].strip()
            elif line.startswith('xesam:artist:'):
                artist = line[len('xesam:artist:'):].strip()
            elif line.startswith('xesam:album:'):
                album = line[len('xesam:album:'):].strip()
            elif line.startswith('mpris:artUrl:'):
                art_url = line[len('mpris:artUrl:'):].strip()
    except Exception:
        pass

    return {
        "has_player": True,
        "player": mpris_player_name(chosen),
        "status": chosen_status,
        "title": title or "Unknown Title",
        "artist": artist,
        "album": album,
        "art_url": art_url,
        "volume": vol,
        "app_volume": app_volume(),
        "is_muted": is_muted
    }

def active_mpris_player():
    """The player that is playing, else the last paused one, else the first."""
    players = get_mpris_players()
    if not players:
        return None
    chosen = players[0]
    for p in players:
        try:
            st = subprocess.check_output(['qdbus6', p, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player.PlaybackStatus'],
                                         stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        except Exception:
            continue
        if st == "Playing":
            return p
        if st == "Paused":
            chosen = p
    return chosen

def _player_streams():
    """Audio streams of the app that is playing media (empty if none can be matched)."""
    player = active_mpris_player()
    if not player or not shutil.which('pactl'):
        return []
    try:
        out = subprocess.check_output(['busctl', '--user', 'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                       'org.freedesktop.DBus', 'GetConnectionUnixProcessID', 's', player],
                                      stderr=subprocess.DEVNULL, timeout=1).decode().split()
        pid = out[-1]
        streams = json.loads(subprocess.check_output(['pactl', '-f', 'json', 'list', 'sink-inputs'],
                                                     stderr=subprocess.DEVNULL, timeout=2))
    except Exception:
        return []
    mine = [st for st in streams if st.get('properties', {}).get('application.process.id') == pid]
    return [st for st in mine if not st.get('corked')] or mine

def _stream_percent(st):
    levels = [int(ch['value_percent'].rstrip('%')) for ch in st.get('volume', {}).values() if 'value_percent' in ch]
    return max(levels) if levels else 100

def app_volume():
    """Volume (0-100) of the app that is playing, or None if it has no audio stream."""
    streams = _player_streams()
    return max(_stream_percent(st) for st in streams) if streams else None

def set_app_volume(percent):
    """Set the playing app's volume; falls back to the system volume. Returns success."""
    percent = max(0, min(100, int(percent)))
    streams = _player_streams()
    if streams:
        for st in streams:
            subprocess.run(['pactl', 'set-sink-input-volume', str(st['index']), f'{percent}%'], capture_output=True)
        return True
    if shutil.which('pactl'):
        return subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{percent}%'], capture_output=True).returncode == 0
    return False

def adjust_app_volume(direction):
    """Change the volume of the app that is playing media (its own audio streams), not
    the whole system. Returns False when no stream can be matched to the player."""
    streams = _player_streams()
    for st in streams:
        new = max(0, min(100, _stream_percent(st) + (5 if direction == "up" else -5)))
        subprocess.run(['pactl', 'set-sink-input-volume', str(st['index']), f'{new}%'], capture_output=True)
    return bool(streams)

def send_mpris_command(command):
    # 1. Volume commands change the playing app's volume; system volume only as a fallback
    if command in ("VolumeUp", "volup", "volume_up"):
        return adjust_app_volume("up") or adjust_system_volume("up")
    elif command in ("VolumeDown", "voldown", "volume_down"):
        return adjust_app_volume("down") or adjust_system_volume("down")
    elif command in ("VolumeMute", "mute", "toggle_mute"):
        return adjust_system_volume("mute")

    # 2. Playback commands target MPRIS players
    players = get_mpris_players()
    if not players:
        return False
    target = players[0]
    for p in players:
        try:
            st = subprocess.check_output(['qdbus6', p, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player.PlaybackStatus'], stderr=subprocess.DEVNULL).decode().strip()
            if st == "Playing":
                target = p
                break
        except Exception:
            pass

    if command in ("PlayPause", "Play", "Pause", "Next", "Previous", "Stop"):
        subprocess.run(['qdbus6', target, '/org/mpris/MediaPlayer2', f'org.mpris.MediaPlayer2.Player.{command}'], stderr=subprocess.DEVNULL)
        return True
    return False

_last_pc_clipboard = ""
_last_phone_clipboard = ""
_last_phone_clipboard_at = 0.0
_clipboard_lock = threading.Lock()

def get_kde_clipboard():
    try:
        return subprocess.check_output(['qdbus6', 'org.kde.klipper', '/klipper', 'org.kde.klipper.klipper.getClipboardContents'], stderr=subprocess.DEVNULL).decode('utf-8')
    except Exception:
        return ""

def set_kde_clipboard(text):
    try:
        subprocess.run(['qdbus6', 'org.kde.klipper', '/klipper', 'org.kde.klipper.klipper.setClipboardContents', text], stderr=subprocess.DEVNULL)
        try:
            subprocess.Popen(['notify-send', '-i', 'edit-paste', '-a', 'Orbit', 'Clipboard Synced', 'Received clipboard from phone'], stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return True
    except Exception:
        return False

# --- Terminal Management Functions ---
def get_open_terminals():
    terminals = []
    seen_ttys = set()
    try:
        q_res = subprocess.run(['qdbus6'], capture_output=True, text=True, timeout=2)
        konsole_svcs = [l.strip() for l in q_res.stdout.splitlines() if 'org.kde.konsole' in l]
        for svc in konsole_svcs:
            try:
                s_res = subprocess.run(['qdbus6', svc, '/Windows/1', 'org.kde.konsole.Window.sessionList'], capture_output=True, text=True, timeout=1)
                session_ids = [s.strip() for s in s_res.stdout.splitlines() if s.strip().isdigit()]
                if not session_ids:
                    intro = subprocess.run(['qdbus6', svc, '/Sessions', 'org.freedesktop.DBus.Introspectable.Introspect'], capture_output=True, text=True, timeout=1)
                    session_ids = re.findall(r'<node name="(\d+)"', intro.stdout)
                
                for sid in session_ids:
                    spath = f'/Sessions/{sid}'
                    
                    # Verify session is actively alive
                    sh_res = subprocess.run(['qdbus6', svc, spath, 'org.kde.konsole.Session.processId'], capture_output=True, text=True, timeout=1)
                    sh_pid = sh_res.stdout.strip()
                    if not sh_pid or not os.path.exists(f'/proc/{sh_pid}'):
                        continue

                    t_res = subprocess.run(['qdbus6', svc, spath, 'org.kde.konsole.Session.title', '1'], capture_output=True, text=True, timeout=1)
                    title = t_res.stdout.strip()
                    
                    fg_res = subprocess.run(['qdbus6', svc, spath, 'org.kde.konsole.Session.foregroundProcessId'], capture_output=True, text=True, timeout=1)
                    fg_pid = fg_res.stdout.strip()
                    
                    cwd = ''
                    tty = ''
                    if sh_pid:
                        try:
                            cwd = os.readlink(f'/proc/{sh_pid}/cwd')
                        except Exception:
                            pass
                        try:
                            fd0 = os.readlink(f'/proc/{sh_pid}/fd/0')
                            if 'pts' in fd0:
                                tty = os.path.basename(fd0)
                                seen_ttys.add(tty)
                                seen_ttys.add(f'pts/{tty}')
                        except Exception:
                            pass
                            
                    fg_comm = ''
                    if fg_pid and os.path.exists(f'/proc/{fg_pid}/comm'):
                        try:
                            with open(f'/proc/{fg_pid}/comm') as f:
                                fg_comm = f.read().strip()
                        except Exception:
                            pass
                    if not fg_comm and sh_pid and os.path.exists(f'/proc/{sh_pid}/comm'):
                        try:
                            with open(f'/proc/{sh_pid}/comm') as f:
                                fg_comm = f.read().strip()
                        except Exception:
                            pass
                            
                    txt_res = subprocess.run(['qdbus6', svc, spath, 'org.kde.konsole.Session.getDisplayedText', '0', '4'], capture_output=True, text=True, timeout=1)
                    preview = txt_res.stdout.strip()
                    
                    terminals.append({
                        'id': f'{svc}:{sid}',
                        'type': 'konsole',
                        'service': svc,
                        'session_id': sid,
                        'title': title if title else f'Konsole Session {sid}',
                        'command': fg_comm if fg_comm else 'shell',
                        'pid': int(fg_pid) if fg_pid.isdigit() else (int(sh_pid) if sh_pid.isdigit() else 0),
                        'cwd': cwd,
                        'tty': tty,
                        'shell_pid': int(sh_pid) if sh_pid.isdigit() else 0,
                        'preview': preview
                    })
            except Exception:
                pass
    except Exception:
        pass

    # Recorded tabs (shell running under `script`): show what actually runs inside
    for t in terminals:
        pid = t.get('shell_pid', 0)
        t['recorded'] = bool(pid) and os.path.exists(os.path.join(TERM_REC_DIR, f'{pid}.timing'))
        if t['recorded']:
            comm, cwd = recorded_tab_process(pid)
            if comm:
                t['command'] = comm
            if cwd:
                t['cwd'] = cwd
            if comm and cwd:
                t['title'] = f"{os.path.basename(cwd.rstrip('/')) or '/'} : {comm}"
            use_shell_tab_title(t['service'], t['session_id'])

    # tmux: tag Konsole tabs that run inside tmux, and list sessions not shown in any tab
    clients = tmux_client_sessions()
    panes = {s['name']: s for s in tmux_list_sessions()}
    shown = set()
    for t in terminals:
        session = clients.get(f"pts/{t['tty']}") if t.get('tty') else None
        t['tmux'] = session or ''
        if session:
            shown.add(session)
            pane = panes.get(session)
            if pane:
                # Konsole only sees the tmux client: name the tab after what runs inside
                t['command'] = pane['command'] or t.get('command', '')
                t['cwd'] = pane['cwd'] or t.get('cwd', '')
                t['title'] = f"{os.path.basename(t['cwd'].rstrip('/')) or '/'} : {t['command']}"
    for s in panes.values():
        if s['name'] in shown:
            continue
        terminals.append({
            'id': f"tmux:{s['name']}",
            'type': 'tmux',
            'service': '',
            'session_id': s['name'],
            'title': f"tmux: {s['name']}" + ('' if s['attached'] else ' (detached)'),
            'command': s['command'] or 'shell',
            'pid': s['pid'],
            'cwd': s['cwd'],
            'tty': '',
            'tmux': s['name'],
            'preview': s['title'],
        })

    # Recorded terminals in other apps (kitty, VS Code, ssh, ...): orbit-term leaves a
    # <pid>.json beside each recording
    in_konsole = {t.get('shell_pid') for t in terminals if t.get('type') == 'konsole'}
    try:
        names = os.listdir(TERM_REC_DIR)
    except OSError:
        names = []
    for name in names:
        stem, _, ext = name.partition('.')
        if ext != 'json' or not stem.isdigit() or int(stem) in in_konsole or not _is_recorder(int(stem)):
            continue
        pid = int(stem)
        try:
            with open(os.path.join(TERM_REC_DIR, name)) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        comm, cwd = recorded_tab_process(pid)
        app = meta.get('app') or 'terminal'
        terminals.append({
            'id': f'rec:{pid}', 'type': 'rec', 'service': '', 'session_id': str(pid),
            'title': f"{os.path.basename(cwd.rstrip('/')) or '/'} : {comm}" if comm else f'{app} terminal',
            'command': comm or 'shell', 'pid': pid, 'cwd': cwd, 'tty': (meta.get('tty') or '').replace('/dev/', ''),
            'shell_pid': pid, 'recorded': True, 'tmux': '', 'app': app, 'preview': ''})

    # Only terminals the phone can show: recorded ones, and tmux
    return [t for t in terminals
            if t.get('type') in ('tmux', 'rec') or (t.get('type') == 'konsole' and (t.get('recorded') or t.get('tmux')))]


# --- Terminal history without tmux ---
# Konsole tabs run their shell under `script`, which records everything the tab prints
# (plus resize events) to ~/.cache/orbit/term/<pid>.{log,timing}. We replay that stream
# through a terminal emulator (pyte) to rebuild the scrollback, with colours.
try:
    import pyte
except ImportError:  # python-pyte not installed: recorded tabs can't be shown
    pyte = None


def _dim_sgr(attrs):
    """pyte ignores dim (SGR 2), which apps use for suggestions, placeholders and hints, so
    those would look like typed text. Carry dim in the unused blink flag (5/25) instead."""
    out, i = [], 0
    while i < len(attrs):
        a = attrs[i]
        if a in (38, 48, 58) and i + 1 < len(attrs):        # extended colour: pass through whole
            n = 3 if attrs[i + 1] == 5 else 5 if attrs[i + 1] == 2 else 1
            out.extend(attrs[i:i + n])
            i += n
            continue
        if a == 2:
            out.append(5)
        elif a == 22:
            out.extend((22, 25))
        elif a not in (5, 6, 25):                              # real blinking: drop
            out.append(a)
        i += 1
    return out


if pyte is not None:
    def _with_dim(base):
        class DimScreen(base):
            def select_graphic_rendition(self, *attrs, **kwargs):
                mapped = _dim_sgr(attrs) if attrs else []
                if attrs and not mapped:
                    return
                super().select_graphic_rendition(*mapped, **kwargs)

            # Remember which rows only exist because a long line hit the right edge (tmux
            # marks these too), so the phone can rejoin them and wrap at its own width.
            # pyte wraps by line-feeding in the middle of draw(); a newline from the program
            # comes through linefeed() on its own.
            _drawing = False

            def draw(self, data):
                self._drawing = True
                try:
                    super().draw(data)
                finally:
                    self._drawing = False

            def linefeed(self):
                super().linefeed()
                self.buffer[self.cursor.y].cont = self._drawing

            def erase_in_display(self, how=0, *args, **kwargs):
                super().erase_in_display(how, *args, **kwargs)
                rows = range(self.cursor.y + (how == 0), self.lines) if how == 0 else \
                    range(0, self.cursor.y + 1) if how == 1 else range(self.lines)
                for y in rows:
                    if y in self.buffer:
                        self.buffer[y].cont = False
        return DimScreen
    _DimScreen, _DimHistoryScreen = _with_dim(pyte.Screen), _with_dim(pyte.HistoryScreen)

TERM_REC_DIR = os.path.expanduser('~/.cache/orbit/term')
TERM_REC_HISTORY = 20000

_ALT_SCREEN = re.compile(r'\x1b\[\?(?:1049|1047|47)([hl])')
# Terminal-mode requests with a private marker (e.g. ESC[>4;2m keyboard mode, ESC[<u);
# they draw nothing, but pyte would misread ESC[>4;2m as "underline".
_PRIVATE_CSI = re.compile(r'\x1b\[[<>=][0-9;:]*[A-Za-z]')
_INCOMPLETE_ESC = re.compile(r'\x1b(?:\[[0-9;?]*)?$')
_SGR_FG = {'black': 30, 'red': 31, 'green': 32, 'brown': 33, 'yellow': 33, 'blue': 34, 'magenta': 35, 'cyan': 36, 'white': 37,
           'brightblack': 90, 'brightred': 91, 'brightgreen': 92, 'brightbrown': 93, 'brightyellow': 93,
           'brightblue': 94, 'brightmagenta': 95, 'brightcyan': 96, 'brightwhite': 97}


def _sgr_color(color, background):
    if color == 'default':
        return None
    if color in _SGR_FG:
        return str(_SGR_FG[color] + (10 if background else 0))
    if re.fullmatch(r'[0-9a-fA-F]{6}', color):
        r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
        return f'{48 if background else 38};2;{r};{g};{b}'
    return None


_SGR_CACHE = {}

def _sgr_for(attrs):
    """ANSI code for a cell's (fg, bg, bold, italics, underscore, reverse, dim); memoised."""
    code = _SGR_CACHE.get(attrs)
    if code is None:
        fg_name, bg_name, bold, italics, underscore, reverse, dim = attrs
        codes = ['0']
        fg, bg = _sgr_color(fg_name, False), _sgr_color(bg_name, True)
        if fg: codes.append(fg)
        if bg: codes.append(bg)
        if bold: codes.append('1')
        if dim: codes.append('2')
        if italics: codes.append('3')
        if underscore: codes.append('4')
        if reverse: codes.append('7')
        code = _SGR_CACHE[attrs] = '\x1b[' + ';'.join(codes) + 'm'
    return code


def _render_row(line, width):
    """One pyte line -> (plain text, text with ANSI colours), trailing blanks dropped.
    One pass over the cells that were actually written. Blanks that only carry a background
    colour are dropped too, all but one: apps pad highlighted lines (Claude Code's own
    messages) out to the computer's full width, and on the narrower phone that padding
    wraps into empty coloured rows."""
    cols = min(max(line.keys(), default=-1) + 1, width)
    out, plain, current, last_text, last_plain, pad_end = [], [], None, 0, 0, 0
    for x in range(cols):
        ch = line[x]
        data = ch.data
        if data == '':          # second cell of a wide character
            continue
        attrs = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.reverse, ch.blink)   # blink = dim, see _dim_sgr
        if attrs != current:
            out.append(_sgr_for(attrs))
            current = attrs
        out.append(data)
        plain.append(data)
        if data != ' ':
            last_plain = len(plain)
        if data != ' ' or ch.reverse:          # a reverse-video blank is a cursor: keep it
            last_text = pad_end = len(out)
        elif attrs[1] != 'default' and pad_end == last_text:
            pad_end = len(out)                  # the one coloured blank kept after the text
    text = ''.join(out[:max(last_text, pad_end)])
    return ''.join(plain[:last_plain]), (text + '\x1b[0m' if '\x1b[' in text else text)


def _render_line(line, width):
    return _render_row(line, width)[1]


_RULE_LINE = re.compile(r'^\s*─{20,}')


class TermRecording:
    """Incrementally replays one `script` recording into a pyte screen."""

    def __init__(self, base):
        self.log_path, self.timing_path = base + '.log', base + '.timing'
        self.timing_pos = self.log_pos = 0
        self.size = [0, 0]           # columns, lines from the header
        self.main = self.alt = None
        self.revision = 0            # bumps whenever the screen may have changed
        self._render_cache = (None, None)
        self.carry = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.lock = threading.Lock()          # guards the screens
        self.update_lock = threading.Lock()   # one reader of the files at a time

    def _new_screen(self, history):
        cols, rows = self.size[0] or 80, self.size[1] or 24
        screen = _DimHistoryScreen(cols, rows, history=TERM_REC_HISTORY) if history else _DimScreen(cols, rows)
        return screen, pyte.Stream(screen)

    def _feed_text(self, text):
        text = self.carry + text
        self.carry = ''
        tail = _INCOMPLETE_ESC.search(text)
        if tail:
            self.carry, text = text[tail.start():], text[:tail.start()]
        pos = 0
        for m in _ALT_SCREEN.finditer(text):
            self._feed_plain(text[pos:m.start()])
            # Full-screen apps (Claude Code, vim) draw on a separate screen with no scrollback;
            # the phone gets that screen exactly as it is, like tmux would show it
            self.alt = self._new_screen(False) if m.group(1) == 'h' else None
            pos = m.end()
        self._feed_plain(text[pos:])

    def _feed_plain(self, text):
        text = _PRIVATE_CSI.sub('', text)
        if text:
            self.revision += 1
            (self.alt or self.main)[1].feed(text)

    def update(self):
        """Read whatever the tab printed since last time. Returns False if the files are gone."""
        with self.update_lock:
            return self._update()

    def _update(self):
        try:
            with open(self.timing_path, 'rb') as tf, open(self.log_path, 'rb') as lf:
                if self.log_pos == 0:
                    # the log starts with a "Script started on ..." banner line that the timing doesn't count
                    head = lf.read(4096)
                    if head.startswith(b'Script started on'):
                        if b'\n' not in head:
                            return True
                        self.log_pos = head.index(b'\n') + 1
                tf.seek(self.timing_pos)
                chunk = tf.read()
                if not chunk:
                    return True
                lines = chunk.split(b'\n')
                consumed = 0
                for raw in lines[:-1]:        # last element is an unfinished line
                    parts = raw.decode('ascii', 'replace').split(' ')
                    kind = parts[0]
                    if kind == 'H' and len(parts) >= 4 and parts[2] in ('COLUMNS', 'LINES'):
                        self.size[0 if parts[2] == 'COLUMNS' else 1] = int(parts[3])
                    elif kind == 'O' and len(parts) >= 3:
                        n = int(parts[2])
                        lf.seek(self.log_pos)
                        data = lf.read(n)
                        if len(data) < n:     # log not flushed yet: retry this entry next time
                            break
                        self.log_pos += n
                        with self.lock:
                            if self.main is None:
                                self.main = self._new_screen(True)
                            self._feed_text(self.decoder.decode(data))
                    elif kind == 'S' and len(parts) >= 5 and parts[2] == 'SIGWINCH':
                        dims = dict(p.split('=', 1) for p in parts[3:] if '=' in p)
                        rows, cols = int(dims.get('ROWS', 0)), int(dims.get('COLS', 0))
                        if rows and cols:
                            self.size = [cols, rows]
                            with self.lock:
                                for scr in (self.main, self.alt):
                                    if scr:
                                        scr[0].resize(lines=rows, columns=cols)
                    consumed += len(raw) + 1
                self.timing_pos += consumed
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True

    def input_line(self):
        """The text typed at the prompt under the cursor (shell or an app like Claude Code),
        without the prompt itself or dim suggestions. None if no prompt is in view."""
        with self.lock:
            return self._input_line()

    def _input_line(self):
        if True:
            if self.main is None:
                return None
            scr = (self.alt or self.main)[0]
            cy = min(max(scr.cursor.y, 0), scr.lines - 1)

            def cells(y):
                row = scr.buffer[y]
                return [(x, row[x]) for x in range(scr.columns) if row[x].data != '']

            start = None
            for y in range(cy, max(-1, cy - 12), -1):
                chars = cells(y)
                line = ''.join(c.data for _, c in chars)
                limit = len(chars) if y < cy else sum(1 for x, _ in chars if x < scr.cursor.x)
                i = _prompt_end(line, limit)
                if i >= 0:
                    start = (y, chars[i][0] + 1)
                    break
                # a shell's input only continues upward through wrapped (full-width) rows
                if not self.alt and (y == 0 or len(''.join(c.data for _, c in cells(y - 1)).rstrip()) < scr.columns - 1):
                    break
            if not start:
                return None
            y0, x0 = start
            end = cy
            if self.alt:     # the input may continue below the cursor, up to the box's edge
                while end + 1 < scr.lines:
                    nxt = ''.join(c.data for _, c in cells(end + 1))
                    if not nxt.strip() or _RULE_LINE.match(nxt) or nxt.lstrip()[:1] in '╰└' or not nxt.startswith(' '):
                        break
                    end += 1
            parts, prev_full = [], False
            for y in range(y0, end + 1):
                row = cells(y)
                text = ''.join(c.data for x, c in row if x >= (x0 if y == y0 else 0) and not c.blink)
                if not self.alt and y == cy:
                    # a right-hand prompt sits after a wide gap past the cursor: not part of the input
                    gap = re.search(r' {8,}', text[max(0, sum(1 for x, _ in row if x0 <= x < scr.cursor.x) if y == y0 else 0):])
                    if gap:
                        text = text[:len(text) - len(gap.string) + gap.start()]
                text = text.replace('\xa0', ' ').replace('\u200b', '').rstrip()
                if text.endswith('│'):
                    text = text[:-1].rstrip()
                if y > y0:
                    text = text[min(x0, len(text) - len(text.lstrip(' '))):]   # continuation indent
                    parts.append(' ' if prev_full and not text.startswith(' ') else '\n')
                prev_full = len(''.join(c.data for _, c in cells(y)).rstrip()) >= scr.columns - 12
                parts.append(text)
            return ''.join(parts).strip('\n')

    def render(self, max_lines, join=False):
        """(lines, total, has_more) for the newest max_lines lines, as ANSI text. With `join`,
        rows that a long line wrapped onto are joined back into it."""
        with self.lock:
            key = (self.revision, max_lines, join)
            if self._render_cache[0] == key:
                return self._render_cache[1]
            result = self._render(max_lines, join)
            self._render_cache = (key, result)
            return result

    def _render(self, max_lines, join=False):
        if self.main is None:
            return [], 0, False
        screen = (self.alt or self.main)[0]
        width = screen.columns
        history = list(screen.history.top) if hasattr(screen, 'history') else []
        rows = [screen.buffer[y] for y in range(screen.lines)]
        # drop empty rows below the cursor
        last = max(screen.cursor.y, 0)
        for y in range(screen.lines - 1, last, -1):
            if any(rows[y][x].data.strip() for x in rows[y].keys()):
                last = y
                break
        rows = rows[:last + 1]
        total = len(history) + len(rows)
        take = rows[-max_lines:] if len(rows) >= max_lines else history[-(max_lines - len(rows)):] + rows
        if not join:
            lines = [_render_line(l, width) for l in take]
            return lines, total, total > len(lines)
        out, last_row_len = [], 0
        for l in take:
            plain, ansi = _render_row(l, width)
            if out and getattr(l, 'cont', False):
                # the row above filled the width and continued here: its trailing blanks
                # were part of the text (a space between words), so put them back
                out[-1] += ' ' * max(0, width - last_row_len) + ansi
            else:
                out.append(ansi)
            last_row_len = len(plain)
        return out, total, total > len(take)


_PROMPT_MARKS = ('❯', '➜', '›', '»', '$', '%', '#', '>', '', '', '\ue285', '\ueab6')


def _prompt_end(line, limit):
    """Index of the last character of the prompt in `line` (only looking before `limit`, the
    cursor), or -1. Themed prompts (Oh My Posh, Starship) end in a Nerd Font icon, a
    private-use character that typed text almost never has; plain ones end in ❯ $ % # >."""
    head = line[:limit]
    for i in range(len(head) - 1, -1, -1):
        if '\ue000' <= head[i] <= '\uf8ff' and line[i + 1:i + 2] in (' ', '\xa0'):
            return i + 1
    for mark in _PROMPT_MARKS:
        for sep in (' ', '\xa0'):
            i = head.find(mark + sep)
            if i >= 0:
                return i + 1
    return -1

TERM_RECORDINGS = {}            # script pid -> TermRecording
TERM_RECORDINGS_LOCK = threading.Lock()

def recording_for(pid):
    """The replayed recording of the Konsole tab whose shell process is `pid`, if it records."""
    if pyte is None or not pid:
        return None
    base = os.path.join(TERM_REC_DIR, str(pid))
    if not os.path.exists(base + '.timing'):
        return None
    with TERM_RECORDINGS_LOCK:
        rec = TERM_RECORDINGS.get(pid)
        if rec is None:
            rec = TERM_RECORDINGS[pid] = TermRecording(base)
    rec.update()
    return rec

_PALETTE_CACHE = {}

def konsole_palette():
    """Colours of the default Konsole profile's scheme, so the phone draws terminals the same way."""
    try:
        rc = os.path.expanduser('~/.config/konsolerc')
        profile = ''
        with open(rc) as f:
            for line in f:
                if line.startswith('DefaultProfile='):
                    profile = line.split('=', 1)[1].strip()
        scheme = ''
        for base in (os.path.expanduser('~/.local/share/konsole'), '/usr/share/konsole'):
            path = os.path.join(base, profile)
            if profile and os.path.exists(path):
                with open(path) as f:
                    for line in f:
                        if line.startswith('ColorScheme='):
                            scheme = line.split('=', 1)[1].strip()
                break
        scheme = scheme or 'Breeze'
        path = next((p for p in (os.path.expanduser(f'~/.local/share/konsole/{scheme}.colorscheme'),
                                 f'/usr/share/konsole/{scheme}.colorscheme') if os.path.exists(p)), None)
        if not path:
            return None
        key = (path, os.path.getmtime(path))
        if key in _PALETTE_CACHE:
            return _PALETTE_CACHE[key]
        colors, section = {}, None
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    section = line[1:-1]
                elif line.startswith('Color=') and section:
                    rgb = [int(v) for v in line[6:].split(',')[:3]]
                    colors[section] = '#%02x%02x%02x' % tuple(rgb)
        palette = {'name': scheme, 'fg': colors.get('Foreground'), 'bg': colors.get('Background'),
                   'normal': [colors.get(f'Color{i}') for i in range(8)],
                   'bright': [colors.get(f'Color{i}Intense') for i in range(8)]}
        if not palette['fg'] or not palette['bg'] or None in palette['normal'] + palette['bright']:
            return None
        _PALETTE_CACHE.clear()
        _PALETTE_CACHE[key] = palette
        return palette
    except (OSError, ValueError):
        return None

def recorded_tab_process(pid):
    """(foreground command, cwd) inside a recorded tab: Konsole only sees `script` itself."""
    try:
        with open(f'/proc/{pid}/task/{pid}/children') as f:
            kids = f.read().split()
        shell = int(kids[0])
        with open(f'/proc/{shell}/stat') as f:
            raw = f.read()
        tpgid = int(raw[raw.rindex(')') + 2:].split()[5])
        fg = tpgid if tpgid > 0 and os.path.exists(f'/proc/{tpgid}') else shell
        with open(f'/proc/{fg}/comm') as f:
            comm = f.read().strip()
        return comm, os.readlink(f'/proc/{fg}/cwd')
    except (OSError, ValueError, IndexError):
        return '', ''

_TITLED_TABS = set()

def use_shell_tab_title(svc, sid):
    """Konsole names tabs after the process it sees (`script` here); let the shell's own
    title ("folder : command", set by the fish block) show instead. Once per tab."""
    key = f'{svc}:{sid}'
    if key in _TITLED_TABS:
        return
    res = subprocess.run(['qdbus6', svc, f'/Sessions/{sid}', 'org.kde.konsole.Session.setTabTitleFormat', '0', '%w'],
                         capture_output=True, timeout=1)
    if res.returncode == 0:
        _TITLED_TABS.add(key)

def _is_recorder(pid):
    """Is `pid` a live process recording a terminal (`script`, or orbit-term)?"""
    try:
        with open(f'/proc/{pid}/comm') as f:
            if f.read().strip() == 'script':
                return True
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return b'orbit-term' in f.read()
    except OSError:
        return False

def prune_term_recordings():
    """Delete recordings whose tab has closed."""
    try:
        names = os.listdir(TERM_REC_DIR)
    except OSError:
        return
    for name in names:
        stem = name.split('.', 1)[0]
        if not stem.isdigit():
            continue
        pid = int(stem)
        alive = _is_recorder(pid)
        if not alive:
            try:
                os.remove(os.path.join(TERM_REC_DIR, name))
            except OSError:
                pass
            with TERM_RECORDINGS_LOCK:
                TERM_RECORDINGS.pop(pid, None)


# --- Keep awake: a desktop sleep + screen-lock block held while enabled ---
_keep_awake_proc = None

# Holds KDE's block (the one the battery applet uses) under Orbit's own name, so the
# applet lists "Orbit: Kept awake from your phone". It lasts as long as this process lives.
_KEEP_AWAKE_HELPER = """
import sys
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtDBus import QDBusConnection, QDBusInterface
app = QCoreApplication(sys.argv)
bus = QDBusConnection.sessionBus()
for service, path, iface in (("org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit",
                              "org.freedesktop.PowerManagement.Inhibit"),
                             ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver")):
    QDBusInterface(service, path, iface, bus).call("Inhibit", "Orbit", "Kept awake from your phone")
app.exec()
"""

def _keep_awake_command():
    try:
        import importlib.util
        if importlib.util.find_spec('PyQt6.QtDBus'):
            return [sys.executable, '-c', _KEEP_AWAKE_HELPER]
    except (ImportError, ValueError):
        pass
    if shutil.which('kde-inhibit'):
        return ['kde-inhibit', '--power', '--screenSaver', 'sleep', 'infinity']
    return ['systemd-inhibit', '--what=sleep:idle', '--who=Orbit', '--why=Kept awake from your phone',
            '--mode=block', 'sleep', 'infinity']

def keep_awake_active():
    return _keep_awake_proc is not None and _keep_awake_proc.poll() is None

def set_keep_awake(enabled):
    global _keep_awake_proc
    if enabled and not keep_awake_active():
        # KDE's own block (same as the battery applet's "block sleep and screen locking"):
        # stops sleep, dimming and screen locking, and shows in the tray. Elsewhere, logind.
        _keep_awake_proc = subprocess.Popen(_keep_awake_command(), stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
    elif not enabled and keep_awake_active():
        # end the whole group (kde-inhibit, if used, holds the block while its child runs)
        try:
            os.killpg(_keep_awake_proc.pid, signal.SIGTERM)
        except OSError:
            _keep_awake_proc.terminate()
        try:
            _keep_awake_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _keep_awake_proc.kill()
        _keep_awake_proc = None

# --- Task manager: this user's processes grouped by program ---
_INTERPRETERS = {'python', 'python3', 'node', 'bash', 'sh', 'fish', 'zsh', 'java', 'ruby', 'perl', 'electron'}

def _program_name(pid, comm):
    """Readable program name: the executable, except version-named binaries (use the
    process name) and interpreters (use the script they run)."""
    try:
        name = os.path.basename(os.readlink(f'/proc/{pid}/exe'))
    except OSError:
        return comm
    if re.fullmatch(r'[\d.]+', name):
        return comm
    base = re.sub(r'[\d.]+$', '', name)
    if base in _INTERPRETERS:
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                args = [a.decode(errors='replace') for a in f.read().split(b'\0') if a]
            script = next((a for a in args[1:] if not a.startswith('-')), '')
            if script:
                return os.path.basename(script)
        except OSError:
            pass
    return name

def _proc_snapshot(uid):
    page = os.sysconf('SC_PAGE_SIZE')
    snap = {}
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            if os.stat(f'/proc/{d}').st_uid != uid:
                continue
            with open(f'/proc/{d}/stat') as f:
                raw = f.read()
            comm = raw[raw.index('(') + 1:raw.rindex(')')]
            fields = raw[raw.rindex(')') + 2:].split()
            state, ticks, rss = fields[0], int(fields[11]) + int(fields[12]), int(fields[21]) * page
            snap[int(d)] = (_program_name(d, comm), state, ticks, rss)
        except (OSError, ValueError, IndexError):
            continue
    return snap

def list_apps():
    """Programs this user is running, with CPU (% of the whole machine over ~0.5 s) and memory."""
    uid = os.getuid()
    hz = os.sysconf('SC_CLK_TCK')
    ncpu = os.cpu_count() or 1
    before, t0 = _proc_snapshot(uid), time.time()
    time.sleep(0.5)
    after, dt = _proc_snapshot(uid), time.time() - t0
    groups = {}
    for pid, (name, state, ticks, rss) in after.items():
        prev = before.get(pid)
        cpu = (ticks - prev[2]) / hz / dt * 100 / ncpu if prev else 0.0
        g = groups.setdefault(name, {'name': name, 'pids': [], 'cpu': 0.0, 'memory_mb': 0.0, 'stuck': 0, 'zombie': 0})
        g['pids'].append(pid)
        g['cpu'] += max(cpu, 0.0)
        g['memory_mb'] += rss / 1048576
        if state == 'D':
            g['stuck'] += 1
        elif state == 'Z':
            g['zombie'] += 1
    apps = sorted(groups.values(), key=lambda g: (g['cpu'], g['memory_mb']), reverse=True)
    for g in apps:
        g['cpu'] = round(g['cpu'], 1)
        g['memory_mb'] = round(g['memory_mb'], 1)
    mem = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':', 1)
                mem[k] = int(v.split()[0])
    except (OSError, ValueError):
        pass
    return {'apps': apps[:80], 'cpu_count': ncpu,
            'memory_total_mb': round(mem.get('MemTotal', 0) / 1024),
            'memory_available_mb': round(mem.get('MemAvailable', 0) / 1024)}

def kill_processes(pids, force=False):
    """Signal this user's processes. Never the daemon itself or the user's systemd manager."""
    uid, me = os.getuid(), os.getpid()
    protected = {me, os.getppid()}
    killed, refused = [], []
    for raw in pids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        try:
            with open(f'/proc/{pid}/comm') as f:
                comm = f.read().strip()
            if pid in protected or os.stat(f'/proc/{pid}').st_uid != uid or comm == 'systemd':
                refused.append(pid)
                continue
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            killed.append(pid)
        except (OSError, ProcessLookupError):
            refused.append(pid)
    return {'killed': killed, 'refused': refused}

def start_terminal_recorder():
    """Keep every recorded Konsole tab replayed (so a read is instant), and remove the
    recordings of tabs that have closed."""
    def _loop():
        recorded, last_scan = [], 0.0
        while True:
            now = time.time()
            try:
                if now - last_scan > 5:
                    found = [t for t in get_open_terminals() if t.get('type') == 'konsole' and t.get('recorded')]
                    recorded = [t['shell_pid'] for t in found]
                    prune_term_recordings()
                    last_scan = now
                for pid in recorded:
                    recording_for(pid)
            except Exception:
                pass
            time.sleep(1.0)

    # history the old screen-stitching kept (it's gone now)
    try:
        os.remove(os.path.expanduser('~/.cache/orbit/terminal-history.json'))
    except OSError:
        pass
    threading.Thread(target=_loop, daemon=True).start()

# --- tmux backing ---
# A Konsole tab running inside tmux is read straight from tmux (exact screen, colours).

def _tmux(*args, timeout=2):
    try:
        return subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None

# tmux escapes tabs in -F output, so fields are joined with '|~|'
_TMUX_CLIENTS_CACHE = [0.0, {}]

def tmux_client_sessions():
    """Map client tty basename (e.g. 'pts/3') -> tmux session name. Cached for 2 s: a held
    read of a tmux tab asks several times a second."""
    if time.time() - _TMUX_CLIENTS_CACHE[0] < 2:
        return dict(_TMUX_CLIENTS_CACHE[1])
    res = _tmux('list-clients', '-F', '#{client_tty}|~|#{session_name}')
    mapping = {}
    if res and res.returncode == 0:
        for line in res.stdout.splitlines():
            parts = line.split('|~|', 1)
            if len(parts) == 2 and parts[0].startswith('/dev/'):
                mapping[parts[0][len('/dev/'):]] = parts[1]
    _TMUX_CLIENTS_CACHE[:] = [time.time(), mapping]
    return dict(mapping)

def tmux_list_sessions():
    """Active pane of every tmux session."""
    fmt = '|~|'.join(['#{session_name}', '#{session_attached}', '#{pane_current_command}', '#{pane_current_path}', '#{pane_pid}', '#{pane_title}'])
    res = _tmux('list-panes', '-a', '-f', '#{&&:#{window_active},#{pane_active}}', '-F', fmt)
    sessions = []
    if res and res.returncode == 0:
        for line in res.stdout.splitlines():
            p = line.split('|~|')
            if len(p) >= 6:
                sessions.append({'name': p[0], 'attached': p[1] not in ('', '0'), 'command': p[2],
                                 'cwd': p[3], 'pid': int(p[4]) if p[4].isdigit() else 0, 'title': p[5]})
    return sessions

def read_tmux_session(session, max_lines):
    target = f'={session}:'
    info = _tmux('display-message', '-p', '-t', target, '#{history_size}|~|#{pane_height}')
    if not info or info.returncode != 0:
        return None
    try:
        hist, height = (int(x) for x in info.stdout.strip().split('|~|'))
    except ValueError:
        hist, height = 0, 0
    total = hist + height
    history_lines = max(0, max_lines - height)
    start_arg = f'-{min(hist, history_lines)}' if hist > 0 else '0'

    # -E - ensures the capture ALWAYS ends at the bottom of the active pane (latest prompt),
    # completely independent of whether the laptop user is scrolled up in copy-mode
    cap = _tmux('capture-pane', '-p', '-e', '-J', '-S', start_arg, '-E', '-', '-t', target)
    if not cap or cap.returncode != 0:
        return None
    lines = cap.stdout.split('\n')
    while lines and not re.sub(r'\x1b\[[0-9;:]*m', '', lines[-1]).strip():
        lines.pop()
    take = lines[-max_lines:] if len(lines) > max_lines else lines
    return {
        'text': '\n'.join(take),
        'total_lines': total,
        'returned_lines': len(take),
        'has_more': min(hist, history_lines) < hist,
        'ansi': True,
    }

def write_tmux_session(session, text):
    target = f'={session}:'
    mode = _tmux('display-message', '-p', '-t', target, '#{pane_in_mode}')
    if mode and mode.stdout.strip() == '1':
        _tmux('send-keys', '-t', target, '-X', 'cancel')
    data = text.encode('utf-8')
    for i in range(0, len(data), 512):
        chunk = data[i:i + 512]
        res = _tmux('send-keys', '-t', target, '-H', *[f'{b:02x}' for b in chunk])
        if not res or res.returncode != 0:
            return {'status': 'error', 'message': (res.stderr.strip() if res else 'tmux not available')}
    return {'status': 'ok'}

_SESSION_TTY_CACHE = {}

def konsole_session_tty(svc, sid):
    """The tty of a Konsole tab (cached: it never changes while the tab exists)."""
    key = (svc, sid)
    hit = _SESSION_TTY_CACHE.get(key)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    tty_name = _konsole_session_tty(svc, sid)
    _SESSION_TTY_CACHE[key] = (time.time(), tty_name)
    return tty_name

def _konsole_session_tty(svc, sid):
    res = subprocess.run(['qdbus6', svc, f'/Sessions/{sid}', 'org.kde.konsole.Session.processId'], capture_output=True, text=True, timeout=1)
    pid = res.stdout.strip()
    if not pid:
        return ''
    try:
        fd0 = os.readlink(f'/proc/{pid}/fd/0')
    except Exception:
        return ''
    return fd0[len('/dev/'):] if fd0.startswith('/dev/pts/') else ''

_SESSION_INFO = {}

def konsole_session_info(svc, sid):
    """(title, shell pid) of a Konsole tab; cached briefly since the phone reads several times a second."""
    key = f'{svc}:{sid}'
    hit = _SESSION_INFO.get(key)
    if hit and time.time() - hit[0] < 3:
        return hit[1], hit[2]
    spath = f'/Sessions/{sid}'
    title = subprocess.run(['qdbus6', svc, spath, 'org.kde.konsole.Session.title', '1'], capture_output=True, text=True, timeout=1).stdout.strip()
    pid = subprocess.run(['qdbus6', svc, spath, 'org.kde.konsole.Session.processId'], capture_output=True, text=True, timeout=1).stdout.strip()
    _SESSION_INFO[key] = (time.time(), title, pid)
    return title, pid

def _recorded_reply(term_id, title, rec, max_lines, join):
    lines, total, more = rec.render(max_lines, join)
    if not total:
        return None
    # columns: the terminal's width, so the phone can lay tables out the same way
    return {'status': 'ok', 'id': term_id, 'title': title, 'text': '\n'.join(lines),
            'total_lines': total, 'returned_lines': len(lines), 'has_more': more, 'ansi': True,
            'columns': rec.size[0] or None, 'lines_independent': True,
            'fullscreen': rec.alt is not None}   # an app like Claude Code: its bottom is the screen's bottom

def read_terminal(term_id, max_lines=35, join=False):
    if term_id.startswith('rec:'):
        rec = _recording_for_term(term_id)
        if rec is None:
            return {'status': 'error', 'message': 'This terminal has closed'}
        comm, cwd = recorded_tab_process(int(term_id[4:]))
        title = f"{os.path.basename(cwd.rstrip('/')) or '/'} : {comm}" if comm else 'Terminal'
        return _recorded_reply(term_id, title, rec, max_lines, join) or {
            'status': 'ok', 'id': term_id, 'title': title, 'text': '', 'total_lines': 0, 'returned_lines': 0, 'has_more': False}
    if term_id.startswith('tmux:'):
        session = term_id[len('tmux:'):]
        res = read_tmux_session(session, max_lines)
        if res:
            pane = next((p for p in tmux_list_sessions() if p['name'] == session), None)
            title = session
            if pane and pane['command']:
                title = f"{os.path.basename(pane['cwd'].rstrip('/')) or '/'} : {pane['command']}"
            return {'status': 'ok', 'id': term_id, 'title': title, **res}
        return {'status': 'error', 'message': 'tmux session not found'}
    if ':' in term_id and 'org.kde.konsole' in term_id:
        svc, sid = term_id.split(':', 1)
        spath = f'/Sessions/{sid}'
        title, sh = konsole_session_info(svc, sid)

        # Konsole tab running tmux: read exact scrollback from tmux
        session = tmux_client_sessions().get(konsole_session_tty(svc, sid))
        if session:
            res = read_tmux_session(session, max_lines)
            if res:
                pane = next((p for p in tmux_list_sessions() if p['name'] == session), None)
                if pane and pane['command']:
                    title = f"{os.path.basename(pane['cwd'].rstrip('/')) or '/'} : {pane['command']}"
                return {'status': 'ok', 'id': term_id, 'title': title, **res}

        # Recorded tab: full scrollback from its `script` recording
        rec = recording_for(int(sh)) if sh.isdigit() else None
        if rec is not None:
            reply = _recorded_reply(term_id, title, rec, max_lines, join)
            if reply:
                return reply

        return {'status': 'error', 'message': 'This tab started before Orbit could follow it. Open a new tab.'}
    return {'status': 'error', 'message': 'Terminal not found or inaccessible'}

# What the phone typed into terminals recently (memory only), to debug input problems
TERMINAL_INPUT_LOG = collections.deque(maxlen=200)

# Wrong pairing PINs across all requests: each request gets a fresh PIN, so without an overall
# cap someone on the network could keep asking for new PINs and guessing.
PAIR_FAILURE_LIMIT = 10
_pair_failures = collections.deque()
_pair_lock_notified = [0.0]

def pairing_locked():
    now = time.time()
    while _pair_failures and now - _pair_failures[0] > 3600:
        _pair_failures.popleft()
    return len(_pair_failures) >= PAIR_FAILURE_LIMIT

def record_pairing_failure():
    _pair_failures.append(time.time())
    if pairing_locked() and time.time() - _pair_lock_notified[0] > 3600:
        _pair_lock_notified[0] = time.time()
        auth_mgr.pending_pairings.clear()
        try:
            subprocess.Popen(['notify-send', '-u', 'critical', '-a', 'Orbit', 'Pairing paused',
                              'Too many wrong pairing PINs were entered. Pairing is paused for an hour.'],
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass

_KEY_NAMES = {'\r': '<enter>', '\n': '<enter>', '\t': '<tab>', '\x7f': '<bs>', '\x1b': '<esc>',
              '\x1b[A': '<up>', '\x1b[B': '<down>', '\x1b[C': '<right>', '\x1b[D': '<left>', '\x1b[F': '<end>'}

def _redact_input(text):
    """What the debug log keeps of something typed from the phone: the keys and the shape,
    never the text itself (it can be a password typed at a sudo prompt)."""
    if text in _KEY_NAMES:
        return _KEY_NAMES[text]
    if len(text) == 1 and ord(text) < 32:
        return f'<ctrl-{chr(ord(text) + 96)}>'
    body = text.rstrip('\r\n')
    return f'<{len(body)} chars>' + ('<enter>' if body != text else '')

# What each phone was last sent, per terminal, so the next reply can be just the change:
# terminals change at the bottom, and resending (and recolouring, and relaying out) 300 lines
# for one typed letter is what made recorded tabs feel slower than tmux.
_TERM_SENT = {}
_TERM_SENT_LOCK = threading.Lock()

def terminal_delta(term_id, max_lines, since, stamp, res, wanted):
    """Remember this reply; if the phone (which asked with `since`) can take a delta, return
    {'base', 'drop', 'keep', 'tail', 'old_count'}: drop lines from the top, keep the next ones,
    then the tail lines (a list)."""
    lines = res.get('text', '').split('\n')
    total = res.get('total_lines') or len(lines)
    key = (term_id, max_lines)
    with _TERM_SENT_LOCK:
        sent = _TERM_SENT.setdefault(key, collections.OrderedDict())
        old = sent.get(since) if wanted and since else None
        sent[stamp] = (lines, total)
        sent.move_to_end(stamp)
        while len(sent) > 6:
            sent.popitem(last=False)
        if len(_TERM_SENT) > 32:
            _TERM_SENT.pop(next(iter(_TERM_SENT)))
    if not old:
        return None
    old_lines, old_total = old
    drop = (total - len(lines)) - (old_total - len(old_lines))   # how far the window slid down
    if drop < 0 or drop > len(old_lines):
        return None
    rest = old_lines[drop:]
    keep = 0
    for a, b in zip(rest, lines):
        if a != b:
            break
        keep += 1
    tail = lines[keep:]
    if len(tail) * 10 > len(lines) * 7:          # most of it changed: a full reply is as good
        return None
    return {'base': since, 'drop': drop, 'keep': keep, 'tail': tail, 'old_count': len(old_lines)}

TERMINAL_WAIT_SECONDS = 20

def _terminal_stamp(res, palette):
    return hashlib.sha1((res.get('text', '') + f"|{res.get('total_lines')}|{palette}").encode('utf-8', 'replace')).hexdigest()[:16]

def wait_for_terminal_change(term_id, max_lines, since, palette, join=False):
    """Hold until the terminal shows something other than version `since`. Recorded tabs are
    watched by their recording file growing; others are re-read every 0.3 s. Returns
    (res, stamp) of the new screen, or None if nothing changed in time."""
    rec = _recording_for_term(term_id)
    deadline = time.time() + TERMINAL_WAIT_SECONDS
    size = -1
    while time.time() < deadline:
        if rec is not None:
            try:
                now = os.path.getsize(rec.timing_path)
            except OSError:
                return None
            if now == size:
                time.sleep(0.03)
                continue
            size = now
        else:
            time.sleep(0.5)          # tmux: re-read (capture-pane) twice a second
        res = read_terminal(term_id, max_lines, join)
        if res.get('status') != 'ok':
            return None
        stamp = _terminal_stamp(res, palette)
        if stamp != since:
            return res, stamp
    return None

def _recorder_pid(term_id):
    """The recording process behind a terminal id: a Konsole tab's shell (which became the
    recorder), or rec:<pid> for a recorded terminal in any other app."""
    if term_id.startswith('rec:'):
        pid = term_id[4:]
        return int(pid) if pid.isdigit() else None
    if ':' in term_id and 'org.kde.konsole' in term_id:
        svc, sid = term_id.split(':', 1)
        sh = konsole_session_info(svc, sid)[1]
        return int(sh) if sh.isdigit() else None
    return None

def _recording_for_term(term_id):
    pid = _recorder_pid(term_id)
    return recording_for(pid) if pid else None

def _send_to_recorder(pid, text):
    """Type into a terminal through orbit-term's input socket (works in any terminal app).
    None if this terminal has no socket (recorded by plain `script`)."""
    path = os.path.join(TERM_REC_DIR, f'{pid}.sock')
    if not os.path.exists(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(path)
            s.sendall(text.encode('utf-8'))
        return {'status': 'ok'}
    except OSError as e:
        return {'status': 'error', 'message': f'Could not type into the terminal: {e}'}

def terminal_input_line(term_id):
    """What is typed at the terminal's prompt right now, so the phone can edit it."""
    session = None
    if term_id.startswith('tmux:'):
        session = term_id[len('tmux:'):]
    elif ':' in term_id and 'org.kde.konsole' in term_id:
        svc, sid = term_id.split(':', 1)
        session = tmux_client_sessions().get(konsole_session_tty(svc, sid))
    if session:
        target = f'={session}:'
        info = _tmux('display-message', '-p', '-t', target, '#{cursor_x}|~|#{cursor_y}')
        if info and info.returncode == 0 and '|~|' in info.stdout:
            try:
                cx, cy = (int(x) for x in info.stdout.strip().split('|~|'))
                cap = _tmux('capture-pane', '-p', '-t', target, '-S', '0', '-E', '-').stdout.splitlines()
                if cy < len(cap):
                    line = cap[cy]
                    limit = min(cx, len(line))
                    end = _prompt_end(line, limit)
                    if end is not None and end >= 0:
                        return {'status': 'ok', 'text': line[end:limit]}
            except Exception:
                pass
        return {'status': 'error', 'message': 'No prompt on screen'}

    rec = _recording_for_term(term_id)
    if rec is None:
        return {'status': 'error', 'message': 'Only recorded Konsole tabs can share their input line'}
    text = rec.input_line()
    if text is None:
        return {'status': 'error', 'message': 'No prompt on screen'}
    return {'status': 'ok', 'text': text}

def replace_terminal_input(term_id, count):
    """Delete `count` characters before the cursor (after jumping to the end of the line),
    so text edited on the phone replaces what was typed on the computer."""
    count = max(0, min(int(count), 4000))
    if not count:
        return {'status': 'ok'}
    res = _send_to_terminal(term_id, '\x1b[F')          # End
    if res['status'] != 'ok':
        return res
    rec = _recording_for_term(term_id)
    if (rec is not None and rec.alt is None) or rec is None:
        # a shell's line editor (or tmux) takes a burst of backspaces fine
        return _send_to_terminal(term_id, '\x7f' * count)
    # full-screen apps (Claude Code) would read a burst as a paste: one keypress at a time
    for _ in range(count):
        res = _send_to_terminal(term_id, '\x7f')
        if res['status'] != 'ok':
            return res
        time.sleep(0.004)
    return res

def edit_terminal_input(term_id, move=0, backspace=0):
    """Live editing from the phone: move the cursor `move` characters (negative = left), then
    delete `backspace` characters before it, mirroring an edit made in the phone's input box."""
    move = max(-4000, min(int(move), 4000))
    backspace = max(0, min(int(backspace), 4000))
    keys = ['\x1b[D' if move < 0 else '\x1b[C'] * abs(move) + ['\x7f'] * backspace
    if not keys:
        return {'status': 'ok'}
    rec = _recording_for_term(term_id)
    if rec is not None and rec.alt is None:
        return _send_to_terminal(term_id, ''.join(keys))     # a shell takes a burst fine
    res = {'status': 'ok'}
    for k in keys:                                           # full-screen apps: one key at a time
        res = _send_to_terminal(term_id, k)
        if res['status'] != 'ok':
            break
        time.sleep(0.004)
    return res

def write_terminal(term_id, text):
    """Type `text` into a terminal. A trailing Enter goes as its own keypress: TUIs such as
    Claude Code treat "text + Enter" arriving in one chunk as a paste, so the Enter would
    become a newline instead of submitting."""
    TERMINAL_INPUT_LOG.append((time.strftime('%H:%M:%S'), term_id, _redact_input(text)))
    # In terminal PTY and raw-mode TUIs (Ink, CLI agents, Node), '\r' submits input.
    if text.endswith('\n'):
        text = text[:-1] + '\r'
    if len(text) > 1 and text.endswith('\r'):
        res = _send_to_terminal(term_id, text[:-1])
        if res['status'] != 'ok':
            return res
        time.sleep(0.05)
        text = '\r'
    return _send_to_terminal(term_id, text)

def _send_to_terminal(term_id, text):
    if term_id.startswith('tmux:'):
        return write_tmux_session(term_id[len('tmux:'):], text)
    if ':' in term_id and 'org.kde.konsole' in term_id:
        svc, sid = term_id.split(':', 1)
        session = tmux_client_sessions().get(konsole_session_tty(svc, sid))
        if session:
            return write_tmux_session(session, text)
    pid = _recorder_pid(term_id)
    if pid:
        res = _send_to_recorder(pid, text)
        if res is not None:
            return res
    if term_id.startswith('rec:'):
        return {'status': 'error', 'message': 'This terminal has closed'}
    if ':' in term_id and 'org.kde.konsole' in term_id:
        svc, sid = term_id.split(':', 1)
        res = subprocess.run(['qdbus6', svc, f'/Sessions/{sid}', 'org.kde.konsole.Session.sendText', text],
                             capture_output=True, text=True, timeout=2)
        if res.returncode != 0:
            err = (res.stdout.strip() or res.stderr.strip()).replace('\n', ' ')
            return {'status': 'error', 'message': err}
        return {'status': 'ok'}
    return {'status': 'error', 'message': 'Terminal not found or inaccessible'}

def send_terminal_key(term_id, key_name):
    k = key_name.lower().strip()
    if k.startswith('ctrl_') and len(k) == 6:
        letter = k[5]
        if 'a' <= letter <= 'z':
            return write_terminal(term_id, chr(ord(letter) - ord('a') + 1))
    KEY_MAP = {
        'ctrl_c': '\x03',
        'ctrl_d': '\x04',
        'ctrl_z': '\x1a',
        'tab': '\t',
        'up': '\x1b[A',
        'down': '\x1b[B',
        'left': '\x1b[D',
        'right': '\x1b[C',
        'enter': '\r',
        'escape': '\x1b',
        'backspace': '\x7f'
    }
    char = KEY_MAP.get(k)
    if char:
        return write_terminal(term_id, char)
    return {'status': 'error', 'message': f'Unsupported key {key_name}'}

def spawn_new_terminal(workdir=''):
    try:
        q_res = subprocess.run(['qdbus6'], capture_output=True, text=True, timeout=1)
        konsole_svcs = [l.strip() for l in q_res.stdout.splitlines() if 'org.kde.konsole' in l]
        if konsole_svcs:
            svc = konsole_svcs[0]
            if workdir:
                subprocess.run(['qdbus6', svc, '/Windows/1', 'org.kde.konsole.Window.newSession', '', workdir], capture_output=True, text=True, timeout=2)
            else:
                subprocess.run(['qdbus6', svc, '/Windows/1', 'org.kde.konsole.Window.newSession'], capture_output=True, text=True, timeout=2)
            return {'status': 'ok', 'action': 'new_tab'}
        else:
            args = ['konsole']
            if workdir:
                args += ['--workdir', workdir]
            subprocess.Popen(args)
            return {'status': 'ok', 'action': 'new_window'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def start_clipboard_monitor(auth_mgr):
    def _monitor():
        global _last_pc_clipboard
        try:
            _last_pc_clipboard = get_kde_clipboard()
        except Exception:
            _last_pc_clipboard = ""

        while True:
            try:
                time.sleep(1.0)
                curr = get_kde_clipboard()
                if curr:
                    with _clipboard_lock:
                        if curr != _last_pc_clipboard:
                            _last_pc_clipboard = curr
                            # Something new was copied on the computer: sync it to the phone.
                            # Not what the phone itself just sent (it can come back with a
                            # trailing newline), so the phone's own text never bounces back.
                            from_phone = (curr.strip() == _last_phone_clipboard.strip()
                                          or time.time() - _last_phone_clipboard_at < 3)
                            if not from_phone:
                                auth_mgr.broadcast_ndjson(json.dumps({"event": "clipboard", "text": curr}) + "\n")
            except Exception:
                pass

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()

def ping_pc():
    def _play_and_notify():
        try:
            for s in ['/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga', '/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga']:
                if os.path.exists(s):
                    subprocess.run(['paplay', s], stderr=subprocess.DEVNULL)
                    break
        except Exception:
            pass
    threading.Thread(target=_play_and_notify, daemon=True).start()
    try:
        subprocess.Popen(['notify-send', '-i', 'phone', '-u', 'critical', '-a', 'Orbit', 'Phone is calling', 'Your phone is pinging this computer'], stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ==========================================
# Telephony / Call Event Management
# ==========================================

telephony_state = {
    "call_active": False,
    "paused_players": [],
    "last_state": "idle"
}
telephony_lock = threading.Lock()

def handle_telephony_call_state(state):
    global telephony_state
    state = (state or '').strip().lower()
    with telephony_lock:
        if state in ("ringing", "talking"):
            if not telephony_state["call_active"]:
                telephony_state["call_active"] = True
                players = get_mpris_players()
                playing = []
                for p in players:
                    try:
                        st = subprocess.check_output(['qdbus6', p, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player.PlaybackStatus'], stderr=subprocess.DEVNULL).decode().strip()
                        if st == "Playing":
                            playing.append(p)
                            subprocess.run(['qdbus6', p, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player.Pause'], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                telephony_state["paused_players"] = playing
                if playing:
                    try:
                        subprocess.Popen(['notify-send', '-i', 'call-start', '-a', 'Orbit', 'Incoming call', f'Paused playback on {len(playing)} media player(s)'], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                elif state == "ringing":
                    try:
                        subprocess.Popen(['notify-send', '-i', 'call-start', '-a', 'Orbit', 'Incoming call', 'Phone is ringing'], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
        elif state == "idle":
            if telephony_state["call_active"]:
                resumed = 0
                for p in telephony_state["paused_players"]:
                    try:
                        subprocess.run(['qdbus6', p, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player.Play'], stderr=subprocess.DEVNULL)
                        resumed += 1
                    except Exception:
                        pass
                if resumed > 0:
                    try:
                        subprocess.Popen(['notify-send', '-i', 'audio-volume-medium', '-a', 'Orbit', 'Call ended', 'Resumed media playback'], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                telephony_state["call_active"] = False
                telephony_state["paused_players"] = []
        telephony_state["last_state"] = state

def get_phone_target():
    ip = active_phone_state.get('ip')
    port = active_phone_state.get('port', 1761)
    token = active_phone_state.get('auth_token')
    cfg = load_config()
    if not ip or ip in ('127.0.0.1', 'localhost', '::1') or not token:
        for t, client in cfg.get('paired_clients', {}).items():
            client_ip = client.get('ip')
            if client_ip and client_ip not in ('127.0.0.1', 'localhost', '::1'):
                ip = client_ip
                token = t
                active_phone_state['ip'] = ip
                active_phone_state['auth_token'] = token
                active_phone_state['client_name'] = client.get('client_name', 'Android Phone')
                break
    return ip, port, token

def ring_phone(phone_ip=None, port=1761):
    # 1. Broadcast via NDJSON stream
    auth_mgr.broadcast_ndjson(json.dumps({"event": "ring", "title": "Find My Phone", "time": int(time.time())}) + "\n")
    # 2. Direct HTTP to phone if IP is known
    target_ip, target_port, target_token = get_phone_target()
    target_ip = phone_ip or target_ip
    target_port = port or target_port
    if target_ip:
        def _direct_ring():
            try:
                headers = {'Content-Type': 'application/json'}
                if target_token:
                    headers['Authorization'] = f'Bearer {target_token}'
                req = urllib.request.Request(f"http://{target_ip}:{target_port}/api/ring", data=b'{}', headers=headers, method='POST')
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
        threading.Thread(target=_direct_ring, daemon=True).start()

def unring_phone(phone_ip=None, port=1761):
    # 1. Broadcast unring via NDJSON stream
    auth_mgr.broadcast_ndjson(json.dumps({"event": "unring", "title": "Stop Alarm", "time": int(time.time())}) + "\n")
    # 2. Direct HTTP to phone to silence
    target_ip, target_port, target_token = get_phone_target()
    target_ip = phone_ip or target_ip
    target_port = port or target_port
    if target_ip:
        def _direct_unring():
            try:
                headers = {'Content-Type': 'application/json'}
                if target_token:
                    headers['Authorization'] = f'Bearer {target_token}'
                req = urllib.request.Request(f"http://{target_ip}:{target_port}/api/unring", data=b'{}', headers=headers, method='POST')
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
        threading.Thread(target=_direct_unring, daemon=True).start()

def notify_file_received(filename, filepath):
    try:
        for sound in ['/usr/share/sounds/freedesktop/stereo/complete.oga', '/usr/share/sounds/freedesktop/stereo/message.oga']:
            if os.path.exists(sound):
                subprocess.Popen(['paplay', sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
    except Exception:
        pass

    downloads_dir = os.path.expanduser('~/Downloads')
    cmd = [
        'notify-send',
        '-i', 'document-save',
        '-a', 'Orbit',
        '📁 File Received from Phone',
        f'{filename}\nSaved in ~/Downloads',
        '-A', 'open=Open File',
        '-A', 'folder=Open Downloads'
    ]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        out, _ = p.communicate(timeout=45)
        chosen = out.strip()
        if chosen == 'open':
            subprocess.Popen(['xdg-open', filepath])
        elif chosen == 'folder':
            subprocess.Popen(['xdg-open', downloads_dir])
    except Exception:
        pass

# ==========================================
# Virtual Mouse & Input Controller (/dev/uinput)
# ==========================================

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02

REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08
REL_HWHEEL = 0x06

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

KEY_MAP = {
    'esc': 1, 'escape': 1,
    '1': 2, '2': 3, '3': 4, '4': 5, '5': 6, '6': 7, '7': 8, '8': 9, '9': 10, '0': 11,
    '-': 12, '=': 13, 'backspace': 14, 'bksp': 14,
    'tab': 15,
    'q': 16, 'w': 17, 'e': 18, 'r': 19, 't': 20, 'y': 21, 'u': 22, 'i': 23, 'o': 24, 'p': 25,
    '[': 26, ']': 27, 'enter': 28, 'return': 28,
    'ctrl': 29, 'leftctrl': 29,
    'a': 30, 's': 31, 'd': 32, 'f': 33, 'g': 34, 'h': 35, 'j': 36, 'k': 37, 'l': 38,
    ';': 39, "'": 40, '`': 41,
    'shift': 42, 'leftshift': 42,
    '\\': 43,
    'z': 44, 'x': 45, 'c': 46, 'v': 47, 'b': 48, 'n': 49, 'm': 50,
    ',': 51, '.': 52, '/': 53,
    'rightshift': 54,
    'alt': 56, 'leftalt': 56,
    'space': 57, ' ': 57,
    'capslock': 58,
    'f1': 59, 'f2': 60, 'f3': 61, 'f4': 62, 'f5': 63, 'f6': 64,
    'f7': 65, 'f8': 66, 'f9': 67, 'f10': 68, 'f11': 87, 'f12': 88,
    'home': 102, 'up': 103, 'pageup': 104, 'prev': 104,
    'left': 105, 'right': 106,
    'end': 107, 'down': 108, 'pagedown': 109, 'next': 109,
    'insert': 110, 'delete': 111, 'del': 111,
    'super': 125, 'win': 125, 'meta': 125,
}

SHIFT_MAP = {
    '!': (2, True), '@': (3, True), '#': (4, True), '$': (5, True), '%': (6, True),
    '^': (7, True), '&': (8, True), '*': (9, True), '(': (10, True), ')': (11, True),
    '_': (12, True), '+': (13, True), '{': (26, True), '}': (27, True), '|': (43, True),
    ':': (39, True), '"': (40, True), '~': (41, True), '<': (51, True), '>': (52, True),
    '?': (53, True)
}

class VirtualMouse:
    def __init__(self):
        self.lock = threading.Lock()
        self.fd = None
        self._last_try = 0.0
        self._open()

    def _ready(self):
        """Open the device if it isn't yet. At boot the service starts before you log in, when
        /dev/uinput isn't yours yet (access comes with the login), so keep trying on use."""
        if self.fd is None and time.time() - self._last_try > 2:
            self._open()
        return self.fd is not None

    def _open(self):
        self._last_try = time.time()
        try:
            self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for btn in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, btn)
            # Register full keyboard keycodes 1..248
            for keycode in range(1, 249):
                try:
                    fcntl.ioctl(self.fd, UI_SET_KEYBIT, keycode)
                except Exception:
                    pass

            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            for rel in (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL):
                fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)

            name = b"Orbit Virtual Input".ljust(80, b'\x00')
            input_id = struct.pack('HHHH', 0x03, 0x1234, 0x5678, 1)
            user_dev = name + input_id + struct.pack('I', 0) + b'\x00' * (64 * 4 * 4)
            os.write(self.fd, user_dev)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
            sys.stderr.write("  Virtual Input Keyboard & Mouse (/dev/uinput) initialized successfully\n")
            sys.stderr.flush()
        except Exception as e:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
            if not getattr(self, '_warned', False):
                sys.stderr.write(f"Warning: Cannot initialize /dev/uinput yet ({e}); will retry when input arrives\n")
                self._warned = True
            self.fd = None


    def emit(self, ev_type, ev_code, val):
        if not self._ready():
            return
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1000000)
        data = struct.pack('qqHHi', sec, usec, ev_type, ev_code, int(val))
        os.write(self.fd, data)

    def move(self, dx, dy):
        if not self._ready():
            return
        with self.lock:
            try:
                idx = int(round(float(dx)))
                idy = int(round(float(dy)))
            except Exception:
                return
            if idx != 0:
                self.emit(EV_REL, REL_X, idx)
            if idy != 0:
                self.emit(EV_REL, REL_Y, idy)
            if idx != 0 or idy != 0:
                self.emit(EV_SYN, 0, 0)

    def click(self, button="left"):
        if not self._ready():
            return
        with self.lock:
            code = BTN_RIGHT if button == "right" else (BTN_MIDDLE if button == "middle" else BTN_LEFT)
            self.emit(EV_KEY, code, 1)
            self.emit(EV_SYN, 0, 0)
            self.emit(EV_KEY, code, 0)
            self.emit(EV_SYN, 0, 0)

    def mouse_down(self, button="left"):
        if not self._ready():
            return
        with self.lock:
            code = BTN_RIGHT if button == "right" else (BTN_MIDDLE if button == "middle" else BTN_LEFT)
            self.emit(EV_KEY, code, 1)
            self.emit(EV_SYN, 0, 0)

    def mouse_up(self, button="left"):
        if not self._ready():
            return
        with self.lock:
            code = BTN_RIGHT if button == "right" else (BTN_MIDDLE if button == "middle" else BTN_LEFT)
            self.emit(EV_KEY, code, 0)
            self.emit(EV_SYN, 0, 0)

    def scroll(self, dy, dx=0):
        if not self._ready():
            return
        with self.lock:
            if dy != 0:
                self.emit(EV_REL, REL_WHEEL, dy)
            if dx != 0:
                self.emit(EV_REL, REL_HWHEEL, dx)
            self.emit(EV_SYN, 0, 0)

    def press_key(self, key_name):
        if not self._ready():
            return
        key_str = str(key_name).strip().lower()
        if not key_str:
            return

        with self.lock:
            if '+' in key_str:
                parts = [p.strip() for p in key_str.split('+')]
                mod_codes = []
                for p in parts[:-1]:
                    if p in ('ctrl', 'control'): mod_codes.append(29)
                    elif p in ('alt',): mod_codes.append(56)
                    elif p in ('shift',): mod_codes.append(42)
                    elif p in ('super', 'win', 'meta'): mod_codes.append(125)
                last_p = parts[-1]
                main_code = KEY_MAP.get(last_p)
                if main_code:
                    for m in mod_codes:
                        self.emit(EV_KEY, m, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, main_code, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, main_code, 0)
                    self.emit(EV_SYN, 0, 0)
                    for m in reversed(mod_codes):
                        self.emit(EV_KEY, m, 0)
                    self.emit(EV_SYN, 0, 0)
                    return

            code = KEY_MAP.get(key_str)
            if code:
                self.emit(EV_KEY, code, 1)
                self.emit(EV_SYN, 0, 0)
                self.emit(EV_KEY, code, 0)
                self.emit(EV_SYN, 0, 0)

    def type_text(self, text):
        if not text or not self._ready():
            return
        with self.lock:
            for ch in str(text):
                if ch == '\n':
                    self.emit(EV_KEY, 28, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, 28, 0)
                    self.emit(EV_SYN, 0, 0)
                elif ch == '\t':
                    self.emit(EV_KEY, 15, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, 15, 0)
                    self.emit(EV_SYN, 0, 0)
                elif ch == '\b':
                    self.emit(EV_KEY, 14, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, 14, 0)
                    self.emit(EV_SYN, 0, 0)
                elif ch in SHIFT_MAP:
                    code, _ = SHIFT_MAP[ch]
                    self.emit(EV_KEY, 42, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, code, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, code, 0)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, 42, 0)
                    self.emit(EV_SYN, 0, 0)
                elif ch.isupper():
                    lower_code = KEY_MAP.get(ch.lower())
                    if lower_code:
                        self.emit(EV_KEY, 42, 1)
                        self.emit(EV_SYN, 0, 0)
                        self.emit(EV_KEY, lower_code, 1)
                        self.emit(EV_SYN, 0, 0)
                        self.emit(EV_KEY, lower_code, 0)
                        self.emit(EV_SYN, 0, 0)
                        self.emit(EV_KEY, 42, 0)
                        self.emit(EV_SYN, 0, 0)
                elif ch.lower() in KEY_MAP:
                    code = KEY_MAP[ch.lower()]
                    self.emit(EV_KEY, code, 1)
                    self.emit(EV_SYN, 0, 0)
                    self.emit(EV_KEY, code, 0)
                    self.emit(EV_SYN, 0, 0)
                time.sleep(0.005)

    def close(self):
        if self.fd:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

virtual_mouse = VirtualMouse()

def trigger_laser_overlay(data):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if isinstance(data, dict):
            msg = json.dumps(data).encode('utf-8')
        else:
            msg = json.dumps({"laser": bool(data)}).encode('utf-8')
        sock.sendto(msg, ('127.0.0.1', 1763))
        sock.close()
    except Exception:
        pass

def start_udp_mouse_listener(port=1762):
    last_cfg_check = [0]
    cached_tokens = [set()]

    def is_valid_token(tok):
        if not tok or not isinstance(tok, str):
            return False
        now = time.time()
        if now - last_cfg_check[0] > 3:
            try:
                cfg = load_config()
                cached_tokens[0] = set(cfg.get('paired_clients', {}).keys())
                last_cfg_check[0] = now
            except Exception:
                pass
        return tok in cached_tokens[0]

    def mouse_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', port))
            sys.stderr.write(f"  UDP Mouse Trackpad Listener active on port {port}\n")
            sys.stderr.flush()
            while True:
                data, addr = sock.recvfrom(2048)
                try:
                    # keyboard and mouse are LAN-only, like their HTTP routes
                    src = ipaddress.ip_address(addr[0])
                    if not (src.is_private or src.is_loopback or src.is_link_local):
                        continue
                    payload = json.loads(data.decode('utf-8'))
                    tok = payload.get('token')
                    if not is_valid_token(tok):
                        continue
                    mtype = payload.get('type', '')
                    if mtype == 'move':
                        virtual_mouse.move(payload.get('dx', 0), payload.get('dy', 0))
                    elif mtype == 'pointer':
                        trigger_laser_overlay(payload)
                    elif mtype == 'laser_state':
                        trigger_laser_overlay(payload)
                    elif mtype == 'key':
                        virtual_mouse.press_key(payload.get('key', ''))
                    elif mtype == 'text':
                        virtual_mouse.type_text(payload.get('text', ''))
                    elif mtype == 'click':
                        virtual_mouse.click(payload.get('button', 'left'))
                    elif mtype == 'down':
                        virtual_mouse.mouse_down(payload.get('button', 'left'))
                    elif mtype == 'up':
                        virtual_mouse.mouse_up(payload.get('button', 'left'))
                    elif mtype == 'scroll':
                        virtual_mouse.scroll(payload.get('dy', 0), payload.get('dx', 0))
                except Exception:
                    pass
        except Exception as e:
            sys.stderr.write(f"UDP mouse listener error: {e}\n")

    t = threading.Thread(target=mouse_loop, daemon=True)
    t.start()

# ==========================================
# Camera Streaming Helpers
# ==========================================

def get_camera_devices():
    devices = []
    v4l_dir = '/sys/class/video4linux'
    if os.path.exists(v4l_dir):
        for entry in sorted(os.listdir(v4l_dir)):
            dev_path = f"/dev/{entry}"
            if not os.path.exists(dev_path):
                continue
            name_file = os.path.join(v4l_dir, entry, 'name')
            name = entry
            if os.path.exists(name_file):
                try:
                    with open(name_file, 'r', encoding='utf-8') as f:
                        name = f.read().strip()
                except Exception:
                    pass
            index_file = os.path.join(v4l_dir, entry, 'index')
            idx = 0
            if os.path.exists(index_file):
                try:
                    with open(index_file, 'r') as f:
                        idx = int(f.read().strip())
                except Exception:
                    pass
            if idx == 0:
                devices.append({"device": dev_path, "name": name})
    if not devices and os.path.exists('/dev/video0'):
        devices.append({"device": "/dev/video0", "name": "Default Camera"})
    return devices

def get_best_camera(preferred=''):
    if preferred and os.path.exists(preferred):
        return preferred
    devices = get_camera_devices()
    for d in devices:
        if 'hd webcam' in d['name'].lower() or 'webcam' in d['name'].lower() or 'camera' in d['name'].lower():
            if 'iriun' not in d['name'].lower():
                return d['device']
    if devices:
        return devices[0]['device']
    return '/dev/video1' if os.path.exists('/dev/video1') else '/dev/video0'

# ==========================================
# Auth Manager
# ==========================================

class AuthManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.current_session = None
        self.sse_clients = []
        self.ndjson_clients = []
        self.pending_pairings = {}

    def add_sse_client(self, client_queue):
        with self.lock:
            self.sse_clients.append(client_queue)

    def remove_sse_client(self, client_queue):
        with self.lock:
            if client_queue in self.sse_clients:
                self.sse_clients.remove(client_queue)

    def add_ndjson_client(self, client_queue):
        with self.lock:
            self.ndjson_clients.append(client_queue)

    def remove_ndjson_client(self, client_queue):
        with self.lock:
            if client_queue in self.ndjson_clients:
                self.ndjson_clients.remove(client_queue)

    def broadcast_sse(self, event_name, data):
        with self.lock:
            dead_clients = []
            msg = f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
            for q in self.sse_clients:
                try:
                    q.put(msg)
                except Exception:
                    dead_clients.append(q)
            for d in dead_clients:
                if d in self.sse_clients:
                    self.sse_clients.remove(d)

    def broadcast_ndjson(self, msg_str):
        with self.lock:
            dead_clients = []
            for q in self.ndjson_clients:
                try:
                    q.put(msg_str)
                except Exception:
                    dead_clients.append(q)
            for d in dead_clients:
                if d in self.ndjson_clients:
                    self.ndjson_clients.remove(d)

    def build_app_notification(self, session, topic="login"):
        local_ip = get_local_ip()
        cfg = load_config()
        port = cfg.get('web_port', 1760)
        sid = session["session_id"]
        user = session["user"]
        hostname = session["hostname"]
        
        return {
            "id": sid,
            "time": int(session.get("created_at", time.time())),
            "event": "message",
            "topic": topic,
            "title": "Sign-in request",
            "message": f"Login requested for {user} on {hostname}. Tap Approve to unlock.",
            "priority": 5,
            "tags": ["warning", "lock"],
            "actions": [
                {
                    "id": f"act_approve_{sid[:8]}",
                    "action": "http",
                    "label": "Approve",
                    "url": f"http://{local_ip}:{port}/api/approve_current",
                    "method": "POST",
                    "clear": True
                },
                {
                    "id": f"act_deny_{sid[:8]}",
                    "action": "http",
                    "label": "Deny",
                    "url": f"http://{local_ip}:{port}/api/deny_current",
                    "method": "POST",
                    "clear": True
                }
            ]
        }

    def create_request(self, user, duration=35):
        now = time.time()
        with self.lock:
            if self.current_session and self.current_session.get("status") == "pending":
                if now < self.current_session.get("expires_at", 0) - 2:
                    # Reuse active valid session so rapid PAM invocations attach to the same challenge
                    return self.current_session

            session_id = str(uuid.uuid4())
            hostname = socket.gethostname()
            cfg = load_config()

            self.current_session = {
                "session_id": session_id,
                "user": user,
                "hostname": hostname,
                "created_at": now,
                "duration": duration,
                "expires_at": now + duration,
                "status": "pending"
            }
        
        self.broadcast_sse("auth_request", self.current_session)
        app_notification = self.build_app_notification(self.current_session, topic="login")
        self.broadcast_ndjson(json.dumps(app_notification) + "\n")
        return self.current_session

    def approve(self, session_id):
        with self.lock:
            if self.current_session and self.current_session["session_id"] == session_id:
                if time.time() <= self.current_session["expires_at"]:
                    self.current_session["status"] = "approved"
                    self.broadcast_sse("auth_cancelled", {"reason": "approved"})
                    self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "approved", "id": session_id}) + "\n")
                    return True
        return False

    def approve_current(self):
        with self.lock:
            if self.current_session and self.current_session["status"] == "pending":
                if time.time() <= self.current_session["expires_at"]:
                    self.current_session["status"] = "approved"
                    sid = self.current_session.get("session_id", "")
                    self.broadcast_sse("auth_cancelled", {"reason": "approved"})
                    self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "approved", "id": sid}) + "\n")
                    return True
        return False

    def deny(self, session_id):
        with self.lock:
            if self.current_session and self.current_session["session_id"] == session_id:
                self.current_session["status"] = "denied"
                self.broadcast_sse("auth_cancelled", {"reason": "denied"})
                self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "denied", "id": session_id}) + "\n")
                return True
        return False

    def deny_current(self):
        with self.lock:
            if self.current_session and self.current_session["status"] == "pending":
                self.current_session["status"] = "denied"
                sid = self.current_session.get("session_id", "")
                self.broadcast_sse("auth_cancelled", {"reason": "denied"})
                self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "denied", "id": sid}) + "\n")
                return True
        return False

    def wait_for_result(self, session_id, timeout=35):
        start = time.time()
        while time.time() - start < timeout:
            with self.lock:
                if not self.current_session or self.current_session["session_id"] != session_id:
                    return "expired"
                if self.current_session["status"] != "pending":
                    return self.current_session["status"]
            time.sleep(0.2)
        with self.lock:
            if self.current_session and self.current_session["session_id"] == session_id:
                self.current_session["status"] = "timeout"
        self.broadcast_sse("auth_cancelled", {"reason": "timeout"})
        self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "timeout", "id": session_id}) + "\n")
        return "timeout"

auth_mgr = AuthManager()

# ==========================================
# High-Speed Internet Tunnel Manager (Ngrok & Cloudflare)
# ==========================================

class TunnelManager:
    def __init__(self, port=1760):
        self.port = port
        self.tunnel_url = None
        self.process = None
        self.lock = threading.Lock()
        self.running = False
        self._thread = None
        self.provider = "none"

    def _find_ngrok(self):
        p = shutil.which('ngrok')
        if p and os.path.isfile(p):
            return p
        cand = os.path.expanduser('~/.local/bin/ngrok')
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        return None

    def _has_ngrok_token(self):
        cfg_path = os.path.expanduser('~/.config/ngrok/ngrok.yml')
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r') as f:
                    content = f.read()
                    if 'authtoken:' in content and 'authtoken: ""' not in content:
                        return True
            except Exception:
                pass
        return False

    def _find_cloudflared(self):
        p = shutil.which('cloudflared')
        if p and os.path.isfile(p):
            return p
        candidates = [
            os.path.expanduser('~/.local/bin/cloudflared'),
            '/usr/local/bin/cloudflared',
            '/usr/bin/cloudflared',
            os.path.join(BASE_DIR, 'cloudflared')
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return None

    def get_provider(self):
        with self.lock:
            return self.provider

    def start(self):
        cfg = load_config()
        if not cfg.get("enable_internet_tunnel", True):
            sys.stderr.write("🌐 Internet tunnel is disabled in config.json\n")
            return

        if self.running and self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_supervisor_loop, daemon=True)
        self._thread.start()

    def _run_supervisor_loop(self):
        while self.running:
            cfg = load_config()
            pref = cfg.get("tunnel_provider", "both").lower()
            if pref == "none":
                time.sleep(5)
                continue

            ngrok_bin = self._find_ngrok()
            has_ngrok = bool(ngrok_bin and self._has_ngrok_token())
            cf_bin = self._find_cloudflared()

            if pref in ("both", "ngrok") and has_ngrok:
                with self.lock:
                    self.provider = "ngrok"
                success = self._run_ngrok_loop(ngrok_bin)
                if not success and pref == "both" and cf_bin and self.running:
                    sys.stderr.write("⚠️ Ngrok failed or exited quickly; falling back to Cloudflare...\n")
                    with self.lock:
                        self.provider = "cloudflare"
                    self._run_cloudflare_loop(cf_bin)
            elif pref in ("both", "cloudflare") and cf_bin:
                with self.lock:
                    self.provider = "cloudflare"
                self._run_cloudflare_loop(cf_bin)
            else:
                sys.stderr.write("⚠️ No tunnel provider found (Ngrok or Cloudflare); retrying in 10s...\n")
                time.sleep(10)

    def _run_ngrok_loop(self, bin_path):
        import re
        cfg = load_config()
        static_url = cfg.get("ngrok_url")
        start_time = time.time()
        had_connection = False
        while self.running:
            sys.stderr.write(f"🌐 Starting Ngrok High-Speed Tunnel on port {self.port}...\n")
            cmd = [bin_path, 'http', str(self.port), '--log=stdout']
            if static_url:
                cmd.extend(['--url', static_url])
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                for line in iter(self.process.stdout.readline, ''):
                    if not self.running:
                        break
                    line_clean = line.strip()
                    m = re.search(r'url=(https://[^\s]+)', line_clean)
                    if m:
                        had_connection = True
                        new_url = m.group(1).rstrip('/')
                        with self.lock:
                            changed = (self.tunnel_url != new_url)
                            self.tunnel_url = new_url
                        sys.stderr.write(f"\n==================================================\n")
                        sys.stderr.write(f"🌐 Ngrok Internet Tunnel ONLINE (Provider: ngrok):\n")
                        sys.stderr.write(f"   👉 {new_url}\n")
                        sys.stderr.write(f"==================================================\n\n")
                        sys.stderr.flush()
                        if changed:
                            try:
                                auth_mgr.broadcast_ndjson(json.dumps({
                                    "event": "tunnel_update",
                                    "internet_url": new_url,
                                    "provider": "ngrok"
                                }) + "\n")
                            except Exception:
                                pass

                self.process.wait()
            except Exception as e:
                sys.stderr.write(f"Ngrok process error: {e}\n")

            if not self.running:
                break
            with self.lock:
                self.tunnel_url = None
            if not had_connection and (time.time() - start_time) < 15:
                # Exited quickly without connection -> allow supervisor fallback
                return False
            sys.stderr.write("🌐 Ngrok tunnel disconnected, restarting in 5s...\n")
            time.sleep(5)
        return had_connection

    def _run_cloudflare_loop(self, bin_path):
        import re
        url_regex = re.compile(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)')
        while self.running:
            sys.stderr.write(f"🌐 Starting Cloudflare Quick Tunnel on port {self.port}...\n")
            cmd = [bin_path, 'tunnel', '--url', f'http://127.0.0.1:{self.port}']
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                for line in iter(self.process.stdout.readline, ''):
                    if not self.running:
                        break
                    line_clean = line.strip()
                    m = url_regex.search(line_clean)
                    if m:
                        new_url = m.group(1)
                        with self.lock:
                            changed = (self.tunnel_url != new_url)
                            self.tunnel_url = new_url
                        sys.stderr.write(f"\n==================================================\n")
                        sys.stderr.write(f"🌐 Cloudflare Internet Tunnel ONLINE (Provider: cloudflare):\n")
                        sys.stderr.write(f"   👉 {new_url}\n")
                        sys.stderr.write(f"==================================================\n\n")
                        sys.stderr.flush()
                        if changed:
                            try:
                                auth_mgr.broadcast_ndjson(json.dumps({
                                    "event": "tunnel_update",
                                    "internet_url": new_url,
                                    "provider": "cloudflare"
                                }) + "\n")
                            except Exception:
                                pass

                self.process.wait()
            except Exception as e:
                sys.stderr.write(f"Tunnel process error: {e}\n")

            if not self.running:
                break
            with self.lock:
                self.tunnel_url = None
            sys.stderr.write("🌐 Cloudflare tunnel disconnected, restarting in 5s...\n")
            time.sleep(5)

    def get_url(self):
        with self.lock:
            return self.tunnel_url

    def stop(self):
        self.running = False
        with self.lock:
            self.tunnel_url = None
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

tunnel_mgr = TunnelManager(port=1760)

class AuthenticatorHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {format % args}\n")
        sys.stderr.flush()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 1. Health & Config
        if path in ('/v1/health', '/health'):
            self.send_json({"healthy": True, "schema": 1})
            return
        elif path in ('/config', '/v1/config'):
            cfg = load_config()
            port = cfg.get('web_port', 1760)
            local_ip = get_local_ip()
            self.send_json({
                "base_url": f"http://{local_ip}:{port}",
                "local_url": f"http://{local_ip}:{port}",
                "internet_url": tunnel_mgr.get_url(),
                "app_root": "/"
            })
            return
        elif path.endswith('/auth'):
            topic_name = path.strip('/').split('/')[0] if '/' in path.strip('/') else "login"
            self.send_json({"topic": topic_name, "read": True, "write": True})
            return

        # 2. APK Download
        elif path in ('/apk', '/PCAuthenticator.apk', '/authenticator.apk', '/download'):
            self.serve_apk()
            return

        # 3. Static Files
        elif path in ('/', '/index.html'):
            self.serve_file(os.path.join(STATIC_DIR, 'index.html'), 'text/html; charset=utf-8')
            return
        elif path == '/browse' and qs.get('ltok'):
            # The tray/CLI open /browse?ltok=<local key>; trade it for an HttpOnly cookie
            ltok = qs.get('ltok', [''])[0]
            if (self.client_address[0] in ('127.0.0.1', '::1') and not is_tunnel_request(self)
                    and LOCAL_TOKEN and hmac.compare_digest(ltok, LOCAL_TOKEN)):
                self.send_response(303)
                self.send_header('Set-Cookie', f'pc_local={LOCAL_TOKEN}; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Location', '/browse')
                self.end_headers()
            else:
                self.send_error(403)
            return
        elif path == '/browse':
            browse_file = os.path.join(STATIC_DIR, 'browse.html')
            if os.path.exists(browse_file):
                self.serve_file(browse_file, 'text/html; charset=utf-8')
            else:
                self.send_error(404, "Browse File Not Found")
            return
        elif path == '/manifest.json':
            self.serve_file(os.path.join(STATIC_DIR, 'manifest.json'), 'application/manifest+json')
            return
        elif path == '/sw.js':
            self.serve_file(os.path.join(STATIC_DIR, 'sw.js'), 'application/javascript')
            return
        elif path in ('/icon-192.png', '/icon-512.png'):
            fn = path.strip('/')
            self.serve_file(os.path.join(STATIC_DIR, fn), 'image/png')
            return

        # 4. Core APIs
        elif path == '/api/info':
            if is_tunnel_request(self):
                # from the internet: just enough to say "this is an Orbit computer"
                self.send_json({"status": "ready"})
                return
            cfg = load_config()
            port = cfg.get('web_port', 1760)
            local_ip = get_local_ip()
            self.send_json({
                "hostname": socket.gethostname(),
                "device_id": get_laptop_id(),
                "status": "ready",
                "smart_dual_mode": True,
                "offline_apk_ready": True,
                "paired_clients_count": len(cfg.get('paired_clients', {})),
                "local_url": f"http://{local_ip}:{port}",
                "internet_url": tunnel_mgr.get_url(),
                "tunnel_provider": tunnel_mgr.get_provider()
            })
            return
        elif path == '/api/events':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.handle_sse()
            return
        elif path == '/api/wait_auth':
            is_local = ((self.client_address[0] in ('127.0.0.1', '::1', 'localhost'))
                        and not is_tunnel_request(self) and not is_foreign_browser_request(self))
            if not is_local:
                cfg = load_config()
                client_info, token = authenticate_client(self, cfg)
                if not client_info:
                    self.send_json({"error": "unauthorized"}, status=401)
                    return
            session_id = qs.get('session_id', [None])[0]
            if not session_id:
                self.send_json({"error": "missing session_id"}, status=400)
                return
            result = auth_mgr.wait_for_result(session_id)
            self.send_json({"status": result})
            return
        elif path == '/api/current_challenge':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized", "message": "Pairing required"}, status=401)
                return
            with auth_mgr.lock:
                if auth_mgr.current_session and auth_mgr.current_session["status"] == "pending":
                    now = time.time()
                    if now <= auth_mgr.current_session["expires_at"]:
                        remaining = int(auth_mgr.current_session["expires_at"] - now)
                        session_copy = dict(auth_mgr.current_session)
                        session_copy["id"] = session_copy.get("session_id")
                        session_copy["remaining_seconds"] = max(1, remaining)
                        self.send_json({"has_challenge": True, "challenge": session_copy})
                        return
            self.send_json({"has_challenge": False})
            return

        # 5. KDE Connect APIs
        elif path == '/api/phone/status':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            is_connected = (time.time() - active_phone_state["last_seen"] < 90) or bool(auth_mgr.ndjson_clients)
            # Fallback to config for phone IP if not seen recently
            if not active_phone_state['ip']:
                for client in cfg.get('paired_clients', {}).values():
                    if client.get('ip') and client.get('ip') != '127.0.0.1':
                        active_phone_state['ip'] = client['ip']
                        active_phone_state['client_name'] = client.get('client_name', 'Android Phone')
                        break

            # Deduplicate paired devices by unique client_id - NEVER expose auth token!
            devices_by_id = {}
            for tok, client in cfg.get('paired_clients', {}).items():
                cid = client.get('client_id') or ("id_" + hashlib.sha256(tok.encode()).hexdigest()[:12])
                if cid == "phone-auto-test" and len(cfg.get('paired_clients', {})) > 1:
                    continue
                is_active = (active_phone_state.get('auth_token') == tok) or (active_phone_state.get('ip') == client.get('ip'))
                devices_by_id[cid] = {
                    "client_id": cid,
                    "client_name": client.get('client_name', 'Android Phone'),
                    # the active phone's address as it is now (the stored one may be from another network)
                    "ip": (active_phone_state.get('ip') or client.get('ip')) if is_active else client.get('ip'),
                    "connected": bool(is_active and is_connected)
                }
            device_list = list(devices_by_id.values())

            self.send_json({
                "connected": is_connected,
                "phone": {
                    "client_name": active_phone_state.get("client_name", "Android Phone"),
                    "ip": active_phone_state.get("ip"),
                    "battery_level": active_phone_state.get("battery_level"),
                    "last_seen": active_phone_state.get("last_seen", 0)
                },
                "clients_count": len(device_list),
                "devices": device_list
            })
            return

        elif path == '/api/devices':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            is_connected = (time.time() - active_phone_state["last_seen"] < 90) or bool(auth_mgr.ndjson_clients)
            devices_by_id = {}
            for tok, client in cfg.get('paired_clients', {}).items():
                cid = client.get('client_id') or ("id_" + hashlib.sha256(tok.encode()).hexdigest()[:12])
                if cid == "phone-auto-test" and len(cfg.get('paired_clients', {})) > 1:
                    continue
                is_active = (active_phone_state.get('auth_token') == tok) or (active_phone_state.get('ip') == client.get('ip'))
                devices_by_id[cid] = {
                    "client_id": cid,
                    "client_name": client.get('client_name', 'Android Phone'),
                    # the active phone's address as it is now (the stored one may be from another network)
                    "ip": (active_phone_state.get('ip') or client.get('ip')) if is_active else client.get('ip'),
                    "connected": bool(is_active and is_connected)
                }
            self.send_json({"devices": list(devices_by_id.values())})
            return

        elif path == '/api/clipboard':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", "text": get_kde_clipboard()})
            return

        elif path == '/api/media/status':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json(get_mpris_status())
            return

        elif path == '/api/pc/status':
            is_local = ((self.client_address[0] in ('127.0.0.1', '::1', 'localhost'))
                        and not is_tunnel_request(self) and not is_foreign_browser_request(self))
            if not is_local:
                cfg = load_config()
                client_info, token = authenticate_client(self, cfg)
                if not client_info:
                    self.send_json({"error": "unauthorized"}, status=401)
                    return
            self.send_json(get_pc_system_status())
            return

        elif path == '/api/keep_awake':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", "enabled": keep_awake_active()})
            return

        elif path == '/api/terminals/input_log':
            # Only for this computer's own tools (debugging what the phone typed)
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info or not client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", "inputs": [list(x) for x in TERMINAL_INPUT_LOG]})
            return

        elif path == '/api/processes':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", **list_apps()})
            return

        elif path == '/api/terminals/list':
            # Viewing is allowed over the internet tunnel; input stays LAN-only
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", "terminals": get_open_terminals(), "can_write": is_lan_or_wifi_request(self)})
            return

        elif path == '/api/terminals/read':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            term_id = qs.get('id', [''])[0]
            lines = qs.get('lines', ['120'])[0]
            try:
                max_lines = int(lines)
            except Exception:
                max_lines = 120
            max_lines = min(max(max_lines, 1), 5000)
            join = qs.get('join', [''])[0] == '1'       # the phone wraps lines itself
            res = read_terminal(term_id, max_lines, join)
            can_write = is_lan_or_wifi_request(self)
            if res.get('status') == 'ok':
                palette = konsole_palette()
                stamp = _terminal_stamp(res, palette)
                since = qs.get('v', [''])[0]
                if since == stamp and qs.get('wait', [''])[0] == '1':
                    # Like tmux's control mode pushing output: instead of the phone asking again
                    # and again, hold the request until the tab prints something (or ~20 s pass)
                    res, stamp = wait_for_terminal_change(term_id, max_lines, since, palette, join) or (res, stamp)
                if since == stamp:
                    # The phone already shows this exact screen
                    self.send_json({'status': 'ok', 'unchanged': True, 'version': stamp, 'can_write': can_write, 'wait': True})
                    return
                res['version'] = stamp
                res['palette'] = palette
                res['wait'] = True              # this computer can hold requests until output changes
                if res.pop('lines_independent', False):
                    delta = terminal_delta(term_id, max_lines, since, stamp, res, wanted=qs.get('delta', [''])[0] == '1')
                    if delta:
                        res.pop('text', None)
                        res['delta'] = delta
            res['can_write'] = can_write
            self.send_json(res)
            return

        elif path == '/api/terminals/input_line':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json(terminal_input_line(qs.get('id', [''])[0]))
            return

        elif path == '/api/screen/screenshot':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            data = capture_pc_screenshot()
            if data:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Disposition', 'inline; filename="pc_screenshot.jpg"')
                self.send_header('Cache-Control', 'no-cache, no-store')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(500, "Screenshot capture failed")
            return

        elif path == '/api/weather':
            is_local = ((self.client_address[0] in ('127.0.0.1', '::1', 'localhost'))
                        and not is_tunnel_request(self) and not is_foreign_browser_request(self))
            if not is_local:
                cfg = load_config()
                client_info, token = authenticate_client(self, cfg)
                if not client_info:
                    self.send_json({"error": "unauthorized"}, status=401)
                    return
            w = get_weather_cached()
            self.send_json({"status": "ok", "weather": w} if w else {"status": "error", "message": "Weather unavailable"})
            return

        elif path == '/api/files/search':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            q = qs.get('q', [''])[0]
            results = [r for r in search_laptop_files(q)
                       if not (isinstance(r, dict) and _is_secret_path(r.get('path', '')))]
            self.send_json({"status": "ok", "query": q, "count": len(results), "results": results})
            return

        elif path == '/api/laptop/files/list':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            req_path = unquote(qs.get('path', ['shortcuts'])[0])
            home = os.path.realpath(os.path.expanduser('~'))
            user = os.environ.get('USER', 'lunarphoton')

            # Discover mounted drives
            mounted_drives = []
            seen_mounts = set()
            for mdir in [f'/run/media/{user}', f'/media/{user}', '/media', '/mnt']:
                if os.path.exists(mdir):
                    try:
                        for entry in os.scandir(mdir):
                            if entry.is_dir():
                                rp = os.path.realpath(entry.path)
                                if rp not in seen_mounts:
                                    seen_mounts.add(rp)
                                    drive_size = "Mounted Drive"
                                    try:
                                        du = shutil.disk_usage(rp)
                                        drive_size = f"{round(du.free / (1024**3), 1)} GB free / {round(du.total / (1024**3), 1)} GB"
                                    except Exception:
                                        pass
                                    mounted_drives.append({
                                        "name": f"💾 {entry.name}",
                                        "path": entry.path,
                                        "is_dir": True,
                                        "icon": "💾",
                                        "size_formatted": drive_size
                                    })
                    except Exception:
                        pass

            try:
                du_root = shutil.disk_usage('/')
                root_size = f"{round(du_root.free / (1024**3), 1)} GB free / {round(du_root.total / (1024**3), 1)} GB"
            except Exception:
                root_size = "Root Filesystem"
            mounted_drives.append({
                "name": "💽 System Root (/)",
                "path": "/",
                "is_dir": True,
                "icon": "💽",
                "size_formatted": root_size
            })

            storage_info = {"total_gb": 0, "used_gb": 0, "free_gb": 0, "used_pct": 0, "formatted": "--"}

            if req_path in ('', 'shortcuts'):
                try:
                    du = shutil.disk_usage(home)
                    storage_info = {
                        "total_gb": round(du.total / (1024**3), 1),
                        "used_gb": round(du.used / (1024**3), 1),
                        "free_gb": round(du.free / (1024**3), 1),
                        "used_pct": round((du.used / du.total) * 100.0, 1),
                        "formatted": f"{round(du.free / (1024**3), 1)} GB free / {round(du.total / (1024**3), 1)} GB ({round((du.used / du.total) * 100.0)}%)"
                    }
                except Exception:
                    pass

                shortcuts = [
                    {"name": "🏠 Home", "path": home, "is_dir": True, "icon": "🏠", "size_formatted": "Folder"},
                    {"name": "📥 Downloads", "path": os.path.join(home, "Downloads"), "is_dir": True, "icon": "📥", "size_formatted": "Folder"},
                    {"name": "📄 Documents", "path": os.path.join(home, "Documents"), "is_dir": True, "icon": "📄", "size_formatted": "Folder"},
                    {"name": "🖼️ Pictures", "path": os.path.join(home, "Pictures"), "is_dir": True, "icon": "🖼️", "size_formatted": "Folder"},
                    {"name": "🎬 Videos", "path": os.path.join(home, "Videos"), "is_dir": True, "icon": "🎬", "size_formatted": "Folder"},
                    {"name": "💻 Desktop", "path": os.path.join(home, "Desktop"), "is_dir": True, "icon": "💻", "size_formatted": "Folder"},
                ]
                existing_shortcuts = [s for s in shortcuts if os.path.exists(s["path"])]
                all_items = existing_shortcuts + mounted_drives

                self.send_json({
                    "status": "ok",
                    "current_path": "shortcuts",
                    "parent_path": None,
                    "storage": storage_info,
                    "items": all_items
                })
                return

            req_path = os.path.realpath(os.path.expanduser(req_path))
            if not os.path.exists(req_path) or not os.path.isdir(req_path) or _is_secret_path(req_path):
                self.send_json({"status": "error", "message": "Directory not found"}, status=404)
                return

            try:
                du = shutil.disk_usage(req_path)
                storage_info = {
                    "total_gb": round(du.total / (1024**3), 1),
                    "used_gb": round(du.used / (1024**3), 1),
                    "free_gb": round(du.free / (1024**3), 1),
                    "used_pct": round((du.used / du.total) * 100.0, 1),
                    "formatted": f"{round(du.free / (1024**3), 1)} GB free / {round(du.total / (1024**3), 1)} GB ({round((du.used / du.total) * 100.0)}%)"
                }
            except Exception:
                pass

            mount_roots = {os.path.realpath(m['path']) for m in mounted_drives}
            if req_path in ('/', home) or req_path in mount_roots:
                parent_path = "shortcuts"
            else:
                parent_path = os.path.dirname(req_path)

            show_hidden = qs.get('hidden', ['0'])[0] in ('1', 'true', 'True')
            items = []
            try:
                with os.scandir(req_path) as it:
                    for entry in it:
                        if not show_hidden and entry.name.startswith('.'):
                            continue
                        if _is_secret_path(entry.path):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            st = entry.stat(follow_symlinks=False)
                            size_bytes = st.st_size if not is_dir else 0
                            size_str = format_file_size(size_bytes) if not is_dir else "Folder"
                            items.append({
                                "name": entry.name,
                                "path": entry.path,
                                "is_dir": is_dir,
                                "is_hidden": entry.name.startswith('.'),
                                "size": size_bytes,
                                "size_formatted": size_str,
                                "mtime": int(st.st_mtime)
                            })
                        except Exception:
                            continue
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, status=500)
                return

            items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

            self.send_json({
                "status": "ok",
                "current_path": req_path,
                "parent_path": parent_path,
                "storage": storage_info,
                "items": items
            })
            return

        elif path in ('/api/files/download_pc', '/api/laptop/files/stream'):
            ticket_path = stream_ticket_path(qs.get('ticket', [''])[0]) if 'ticket' in qs else None
            if ticket_path:
                req_path = ticket_path                      # a one-file link made for a player app
            else:
                cfg = load_config()
                client_info, token = authenticate_client(self, cfg)
                if not client_info:
                    self.send_json({"error": "unauthorized"}, status=401)
                    return
                req_path = unquote(qs.get('path', [''])[0])
            req_path = os.path.realpath(os.path.expanduser(req_path))
            if not os.path.exists(req_path) or os.path.isdir(req_path) or _is_secret_path(req_path):
                self.send_error(404, "File not found")
                return
            try:
                fn = os.path.basename(req_path)
                total_size = os.path.getsize(req_path)
                ctype, _ = mimetypes.guess_type(fn)
                ctype = ctype or 'application/octet-stream'

                range_header = self.headers.get('Range')
                start = 0
                end = total_size - 1

                if range_header and range_header.startswith('bytes='):
                    ranges = range_header.replace('bytes=', '').split('-')
                    if ranges[0]:
                        start = int(ranges[0])
                    if len(ranges) > 1 and ranges[1]:
                        end = int(ranges[1])
                    if end >= total_size:
                        end = total_size - 1

                    if start > end or start >= total_size:
                        self.send_response(416, "Requested Range Not Satisfiable")
                        self.send_header('Content-Range', f'bytes */{total_size}')
                        self.end_headers()
                        return

                    length = end - start + 1
                    self.send_response(206, "Partial Content")
                    self.send_header('Content-Range', f'bytes {start}-{end}/{total_size}')
                    self.send_header('Content-Length', str(length))
                else:
                    self.send_response(200, "OK")
                    self.send_header('Content-Length', str(total_size))

                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Disposition', f'inline; filename="{quote(fn)}"')
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()

                with open(req_path, 'rb') as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk_size = min(remaining, 65536)
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try: self.send_error(500, str(e))
                except Exception: pass
            return

        elif path == '/api/files/stage/status':
            token = qs.get('token', [''])[0]
            staged = staged_files.get(token)
            if not staged:
                self.send_json({"error": "not_found", "status": "not_found"}, status=404)
                return
            self.send_json({
                "status": "ok",
                "token": token,
                "filename": staged.get("filename"),
                "downloaded": staged.get("downloaded", False),
                "downloaded_at": staged.get("downloaded_at"),
                "created_at": staged.get("created_at"),
                "phone_connected": bool(auth_mgr.ndjson_clients)
            })
            return

        # 6. File Staging Download (PC to Phone pull)
        elif path.startswith('/api/files/staging/'):
            token = path.replace('/api/files/staging/', '').strip('/')
            staged = staged_files.get(token)
            if not staged or not os.path.exists(staged['filepath']):
                self.send_error(404, "Staged file not found or expired")
                return
            cfg = load_config()
            client_info, client_token = authenticate_client(self, cfg)
            if not client_info:
                self.send_error(401, "Unauthorized: Pairing required to download staged file")
                return
            try:
                size = os.path.getsize(staged['filepath'])
                fn = quote(staged['filename'])
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(size))
                self.send_header('Content-Disposition', f'attachment; filename="{fn}"')
                self.end_headers()
                with open(staged['filepath'], 'rb') as f:
                    while chunk := f.read(65536):
                        self.wfile.write(chunk)
                self.wfile.flush()
                staged['downloaded'] = True
                staged['downloaded_at'] = time.time()
                sys.stderr.write(f"📥 Staged file '{staged['filename']}' successfully downloaded by phone\n")
            except Exception as e:
                try: self.send_error(500, str(e))
                except Exception: pass
            return

        # 7. Phone File Browser Proxies (Linux Desktop -> Phone)
        elif path == '/api/phone/files/list':
            cfg = load_config()
            client_info, client_token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            phone_ip, phone_port, phone_token = get_phone_target()
            if not phone_ip:
                self.send_json({"error": "phone_not_connected", "message": "Phone is not connected or IP unknown"}, status=503)
                return
            req_path = qs.get('path', ['/storage/emulated/0'])[0]
            try:
                target_url = f"http://{phone_ip}:{phone_port}/api/files/list?path={quote(req_path)}"
                headers = {}
                if phone_token:
                    headers['Authorization'] = f'Bearer {phone_token}'
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = resp.read()
                    self.send_response(resp.status)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(data)
            except Exception as e:
                self.send_json({"error": "phone_unreachable", "message": str(e)}, status=502)
            return

        elif path in ('/api/phone/files/download', '/api/phone/files/preview'):
            cfg = load_config()
            client_info, client_token = authenticate_client(self, cfg)
            if not client_info:
                self.send_error(401, "Unauthorized")
                return
            phone_ip, phone_port, phone_token = get_phone_target()
            if not phone_ip:
                self.send_error(503, "Phone is not connected")
                return
            req_path = qs.get('path', [''])[0]
            is_preview = (path == '/api/phone/files/preview') or (qs.get('preview', ['0'])[0] in ('1', 'true'))
            try:
                target_url = f"http://{phone_ip}:{phone_port}/api/files/download?path={quote(req_path)}"
                headers = {}
                if phone_token:
                    headers['Authorization'] = f'Bearer {phone_token}'
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.send_response(200)
                    fn = os.path.basename(req_path)
                    content_length = resp.headers.get('Content-Length')
                    
                    if is_preview:
                        ctype, _ = mimetypes.guess_type(fn)
                        ctype = ctype or 'application/octet-stream'
                        self.send_header('Content-Type', ctype)
                        self.send_header('Content-Disposition', f'inline; filename="{fn}"')
                    else:
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Disposition', f'attachment; filename="{fn}"')

                    if content_length:
                        self.send_header('Content-Length', content_length)
                    self.end_headers()
                    while chunk := resp.read(65536):
                        self.wfile.write(chunk)
            except Exception as e:
                try: self.send_error(502, f"Download error: {e}")
                except Exception: pass
            return

        # 7b. Camera Endpoints
        elif path == '/api/camera/devices':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            devices = get_camera_devices()
            self.send_json({"devices": devices})
            return

        elif path == '/api/camera/snapshot':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            device = qs.get('device', [''])[0]
            self.handle_camera_snapshot(device)
            return

        elif path == '/api/camera/stream':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            device = qs.get('device', [''])[0]
            quality = qs.get('quality', ['smooth'])[0]
            self.handle_camera_stream(device, quality)
            return

        elif path == '/camera':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.handle_camera_web_page()
            return

        # 8. Topic Streaming / Polling (ntfy Android protocol)
        is_poll = qs.get('poll', ['0'])[0] in ('1', 'true')
        parts = [p for p in path.strip('/').split('/') if p]
        if parts:
            topic = parts[0]
            if is_poll:
                self.handle_poll(topic)
                return
            elif len(parts) == 1 or parts[1] == 'json':
                self.handle_ndjson_stream(topic)
                return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == '/api/files/stream_ticket':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            body = self.read_json()
            req_path = os.path.realpath(os.path.expanduser(str(body.get('path', '') if isinstance(body, dict) else '')))
            if not os.path.isfile(req_path) or _is_secret_path(req_path):
                self.send_json({"error": "not_found"}, status=404)
                return
            self.send_json({"status": "ok", "ticket": make_stream_ticket(req_path)})
            return

        # 1. File Upload (Phone -> PC)
        if path == '/api/files/upload':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            raw_fn = self.headers.get('X-Filename', '')
            if raw_fn:
                filename = os.path.basename(unquote(raw_fn))
            else:
                filename = f"received_file_{int(time.time())}"

            downloads_dir = os.path.expanduser('~/Downloads')
            os.makedirs(downloads_dir, exist_ok=True)

            target_path = os.path.join(downloads_dir, filename)
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(target_path):
                target_path = os.path.join(downloads_dir, f"{base} ({counter}){ext}")
                counter += 1

            try:
                length = int(self.headers.get('Content-Length', 0))
                # never let an upload fill the disk: keep 2 GB free
                if length < 0 or length > shutil.disk_usage(downloads_dir).free - 2 * 1024 ** 3:
                    self.send_json({"status": "error", "message": "Not enough free space on the computer"}, status=507)
                    return
                remaining = length
                with open(target_path, 'wb') as f:
                    while remaining > 0:
                        chunk_size = min(65536, remaining)
                        chunk = self.rfile.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                if remaining > 0:
                    # the phone's connection dropped mid-way: don't keep (or announce) half a file
                    try:
                        os.remove(target_path)
                    except OSError:
                        pass
                    self.send_json({"status": "error", "message": f"Transfer cut off ({length - remaining} of {length} bytes)"}, status=500)
                    return

                final_fn = os.path.basename(target_path)
                threading.Thread(target=notify_file_received, args=(final_fn, target_path), daemon=True).start()
                self.send_json({"status": "ok", "filename": final_fn, "path": target_path})
            except Exception as e:
                self.send_json({"error": "upload_failed", "message": str(e)}, status=500)
            return

        # 2. File Proxy Upload (PC -> Phone)
        elif path == '/api/phone/files/upload':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            phone_ip, phone_port, phone_token = get_phone_target()
            if not phone_ip:
                self.send_json({"error": "phone_not_connected"}, status=503)
                return
            target_path = qs.get('path', ['/storage/emulated/0/Download'])[0]
            raw_fn = self.headers.get('X-Filename', '')
            target_url = f"http://{phone_ip}:{phone_port}/api/files/upload?path={quote(target_path)}"
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self.rfile.read(length)
                headers = {
                    'Content-Type': 'application/octet-stream',
                    'X-Filename': raw_fn
                }
                if phone_token:
                    headers['Authorization'] = f'Bearer {phone_token}'
                req = urllib.request.Request(
                    target_url,
                    data=data,
                    headers=headers,
                    method='POST'
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    res_body = resp.read()
                    self.send_response(resp.status)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(res_body)
            except Exception as e:
                self.send_json({"error": "upload_proxy_failed", "message": str(e)}, status=502)
            return

        # Read JSON body for standard API calls
        body = self.read_json()

        # 3. Core PC Authenticator APIs (Local only)
        if path == '/api/request_auth':
            is_local = ((self.client_address[0] in ('127.0.0.1', '::1', 'localhost'))
                        and not is_tunnel_request(self) and not is_foreign_browser_request(self))
            if not is_local or not (has_local_token(self) or peer_uid(self) in (os.getuid(), 0)):
                # Only this account's lock screen or the login screen (root) may ask the phone to
                # approve; another user on the computer must not be able to spam approval prompts
                self.send_json({"error": "forbidden", "message": "Auth requests can only be initiated locally."}, status=403)
                return
            user = body.get('user', os.environ.get('USER', 'lunarphoton'))
            cfg = load_config()
            timeout = cfg.get('auth_timeout_seconds', 35)
            session = auth_mgr.create_request(user, duration=timeout)
            self.send_json(session)

        elif path == '/api/auth/prove':
            # Lets a paired phone check that a new address really is this laptop before
            # it sends its token there: we answer HMAC(secret, nonce + our own IP). Binding
            # the IP stops a fake laptop from relaying the phone's nonce to us.
            client_id = body.get('client_id', '') if isinstance(body, dict) else ''
            nonce = body.get('nonce', '') if isinstance(body, dict) else ''
            if not client_id or not (16 <= len(nonce) <= 128):
                self.send_json({"error": "bad_request"}, status=400)
                return
            cfg = load_config()
            secret = next((c.get('secret_key', '') for c in cfg.get('paired_clients', {}).values()
                           if c.get('client_id') == client_id), '')
            if not secret:
                self.send_json({"error": "unknown_client"}, status=404)
                return
            # the address the phone actually reached (the computer may have several)
            own_ip = self.connection.getsockname()[0] if not is_tunnel_request(self) else get_local_ip()
            mac = hmac.new(secret.encode('utf-8'), f"prove:{nonce}:{own_ip}".encode('utf-8'), hashlib.sha256).hexdigest()
            self.send_json({"status": "ok", "ip": own_ip, "mac": mac})

        elif path == '/api/pair/request':
            if is_tunnel_request(self):
                self.send_json({"error": "forbidden", "message": "Pairing is only allowed over local Wi-Fi / LAN."}, status=403)
                return

            now = time.time()
            if pairing_locked():
                self.send_json({"error": "locked", "message": "Pairing is paused for an hour after too many wrong PINs."}, status=429)
                return
            with auth_mgr.lock:
                if not hasattr(auth_mgr, 'pair_rate_limits'):
                    auth_mgr.pair_rate_limits = []
                auth_mgr.pair_rate_limits = [t for t in auth_mgr.pair_rate_limits if now - t < 60]
                if len(auth_mgr.pair_rate_limits) >= 5:
                    self.send_json({"error": "rate_limited", "message": "Too many pairing attempts. Please wait 1 minute."}, status=429)
                    return
                auth_mgr.pair_rate_limits.append(now)

            client_id = body.get('client_id', '')
            client_name = body.get('client_name', 'Android Phone')
            if not client_id:
                client_id = str(uuid.uuid4())
            pin = f"{secrets.randbelow(900000) + 100000}"
            with auth_mgr.lock:
                auth_mgr.pending_pairings[client_id] = {
                    "pin": pin,
                    "name": client_name,
                    "expires_at": time.time() + 60,
                    "attempts": 0
                }
            try:
                formatted_pin = f"{pin[:3]} {pin[3:]}"
                msg = f"Pairing PIN for {client_name}:\n\n👉  {formatted_pin}  👈\n\nEnter this PIN in your phone app to authorize."
                subprocess.Popen(['notify-send', '-a', 'Orbit', 'Pairing PIN', msg, '-u', 'critical', '-i', 'dialog-password'])
            except Exception as e:
                sys.stderr.write(f"notify-send error: {e}\n")
            sys.stderr.write(f"\n==================================================\n")
            sys.stderr.write(f"  PAIRING REQUEST from {client_name}\n")
            sys.stderr.write(f"  👉 PIN: {pin[:3]} {pin[3:]} 👈 (valid 60s)\n")
            sys.stderr.write(f"==================================================\n")
            sys.stderr.flush()
            self.send_json({
                "status": "pin_generated",
                "expires_in": 60,
                "client_id": client_id,
                "device_id": get_laptop_id(),
                "hostname": socket.gethostname()
            })

        elif path == '/api/pair/confirm':
            if is_tunnel_request(self):
                self.send_json({"error": "forbidden", "message": "Pairing confirmation is only allowed over local Wi-Fi / LAN."}, status=403)
                return
            client_id = body.get('client_id', '')
            pin = str(body.get('pin', '')).replace(' ', '').strip()
            client_name = body.get('client_name', 'Android Phone')
            if pairing_locked():
                self.send_json({"status": "error", "message": "Pairing is paused for an hour after too many wrong PINs."}, status=429)
                return
            with auth_mgr.lock:
                pending = auth_mgr.pending_pairings.get(client_id)
                if not pending:
                    self.send_json({"status": "error", "message": "No pairing request pending or expired"}, status=400)
                    return
                if time.time() > pending["expires_at"]:
                    del auth_mgr.pending_pairings[client_id]
                    self.send_json({"status": "error", "message": "PIN expired"}, status=400)
                    return
                pending["attempts"] += 1
                if pending["attempts"] > 3:
                    del auth_mgr.pending_pairings[client_id]
                    self.send_json({"status": "error", "message": "Too many failed attempts"}, status=403)
                    return
                if not hmac.compare_digest(pending["pin"], pin):
                    record_pairing_failure()
                    self.send_json({"status": "error", "message": "Incorrect PIN", "attempts_left": 3 - pending["attempts"]}, status=403)
                    return

                del auth_mgr.pending_pairings[client_id]
                auth_token = secrets.token_hex(32)
                secret_key = secrets.token_hex(32)
                cfg = load_config()
                if "paired_clients" not in cfg:
                    cfg["paired_clients"] = {}
                # Deduplicate by client_id so same device doesn't create duplicate tokens
                cfg["paired_clients"] = {
                    tok: c for tok, c in cfg["paired_clients"].items()
                    if c.get("client_id") != client_id
                }
                cfg["paired_clients"][auth_token] = {
                    "client_id": client_id,
                    "client_name": client_name,
                    "secret_key": secret_key,
                    "paired_at": time.time(),
                    "ip": get_effective_client_ip(self)
                }
                save_config(cfg)
                update_dolphin_servicemenu()
                active_phone_state['ip'] = get_effective_client_ip(self)
                active_phone_state['client_name'] = client_name
                active_phone_state['auth_token'] = auth_token
                try:
                    subprocess.Popen(['notify-send', '-a', 'Orbit', 'Paired', f'Paired with {client_name}', '-u', 'normal'])
                except Exception:
                    pass
                sys.stderr.write(f"✅ Successfully paired with {client_name} ({auth_token[:8]}...)\n")
                sys.stderr.flush()
                self.send_json({
                    "status": "ok",
                    "auth_token": auth_token,
                    "secret_key": secret_key,
                    "device_id": get_laptop_id(),
                    "hostname": socket.gethostname(),
                    "local_url": f"http://{get_local_ip()}:{cfg.get('web_port', 1760)}",
                    "internet_url": tunnel_mgr.get_url()
                })

        elif path in ('/api/approve_current', '/api/approve'):
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"status": "unauthorized", "message": "Device not paired"}, status=401)
                return

            if not client_info.get('local'):
                sig = self.headers.get('X-Auth-Signature', '')
                ts_str = self.headers.get('X-Auth-Timestamp', '')
                try:
                    ts = int(ts_str)
                    if abs(int(time.time()) - ts) > 45:
                        self.send_json({"status": "forbidden", "message": "Timestamp expired"}, status=403)
                        return
                except Exception:
                    self.send_json({"status": "forbidden", "message": "Invalid timestamp"}, status=403)
                    return

                with auth_mgr.lock:
                    current_sid = auth_mgr.current_session.get("session_id", "") if auth_mgr.current_session else ""
                secret_key = client_info.get('secret_key', '')
                req_sid = self.headers.get('X-Session-ID', '')
                if not req_sid and isinstance(body, dict):
                    req_sid = body.get('session_id', '')

                # Bind the signature to the pending session and refuse replays
                if not current_sid or (req_sid and req_sid != current_sid):
                    self.send_json({"status": "forbidden", "message": "No matching login request"}, status=403)
                    return
                with auth_mgr.lock:
                    now_t = time.time()
                    used = {k: v for k, v in getattr(auth_mgr, 'used_signatures', {}).items() if now_t - v < 120}
                    auth_mgr.used_signatures = used
                    if sig.lower() in used:
                        self.send_json({"status": "forbidden", "message": "Signature already used"}, status=403)
                        return
                candidates = [f"{current_sid}:{ts}:approve"]
                valid = False
                for cand in candidates:
                    expected_sig = hmac.new(secret_key.encode('utf-8'), cand.encode('utf-8'), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(sig.lower(), expected_sig.lower()):
                        valid = True
                        break
                if not valid:
                    self.send_json({"status": "forbidden", "message": "Cryptographic signature mismatch"}, status=403)
                    return
                with auth_mgr.lock:
                    auth_mgr.used_signatures[sig.lower()] = time.time()

            if path == '/api/approve_current':
                ok = auth_mgr.approve_current()
            else:
                session_id = body.get('session_id') if isinstance(body, dict) else ""
                ok = auth_mgr.approve(session_id)
            self.send_json({"status": "ok" if ok else "error"})

        elif path in ('/api/deny_current', '/api/deny'):
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"status": "unauthorized", "message": "Device not paired"}, status=401)
                return

            if not client_info.get('local'):
                sig = self.headers.get('X-Auth-Signature', '')
                ts_str = self.headers.get('X-Auth-Timestamp', '')
                try:
                    ts = int(ts_str)
                    if abs(int(time.time()) - ts) > 45:
                        self.send_json({"status": "forbidden", "message": "Timestamp expired"}, status=403)
                        return
                except Exception:
                    self.send_json({"status": "forbidden", "message": "Invalid timestamp"}, status=403)
                    return

                with auth_mgr.lock:
                    current_sid = auth_mgr.current_session.get("session_id", "") if auth_mgr.current_session else ""
                secret_key = client_info.get('secret_key', '')
                req_sid = self.headers.get('X-Session-ID', '')
                if not req_sid and isinstance(body, dict):
                    req_sid = body.get('session_id', '')
                candidates = [f"{current_sid}:{ts}:deny", f":{ts}:deny"]
                if req_sid:
                    candidates.append(f"{req_sid}:{ts}:deny")
                valid = False
                for cand in candidates:
                    expected_sig = hmac.new(secret_key.encode('utf-8'), cand.encode('utf-8'), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(sig.lower(), expected_sig.lower()):
                        valid = True
                        break
                if not valid:
                    self.send_json({"status": "forbidden", "message": "Cryptographic signature mismatch"}, status=403)
                    return

            if path == '/api/deny_current':
                ok = auth_mgr.deny_current()
            else:
                session_id = body.get('session_id') if isinstance(body, dict) else ""
                ok = auth_mgr.deny(session_id)
            self.send_json({"status": "ok" if ok else "error"})

        # 4. KDE Connect Phone Status Updates
        elif path == '/api/phone/status':
            # Only the paired phone may say where it is; otherwise anyone on the LAN
            # could redirect the token-bearing requests we send to the phone.
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            if 'battery' in body:
                active_phone_state['battery_level'] = body.get('battery')
            if 'charging' in body:
                active_phone_state['is_charging'] = bool(body.get('charging'))
            if 'port' in body:
                try:
                    port_val = int(body.get('port'))
                    if 1024 <= port_val <= 65535:
                        active_phone_state['port'] = port_val
                except (TypeError, ValueError):
                    pass
            if 'device_name' in body:
                active_phone_state['client_name'] = body.get('device_name')
            if 'model' in body:
                active_phone_state['model'] = body.get('model')
            if 'manufacturer' in body:
                active_phone_state['manufacturer'] = body.get('manufacturer')
            if 'android_version' in body:
                active_phone_state['android_version'] = body.get('android_version')
            if 'storage_free' in body:
                active_phone_state['storage_free'] = body.get('storage_free')
            if 'storage_total' in body:
                active_phone_state['storage_total'] = body.get('storage_total')
            # Only a direct LAN connection tells us where the phone is
            if not is_tunnel_request(self):
                active_phone_state['ip'] = self.client_address[0]
            active_phone_state['auth_token'] = token
            active_phone_state['last_seen'] = time.time()
            self.send_json({"status": "ok"})

        # 5. Clipboard Sync
        elif path == '/api/clipboard':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            global _last_phone_clipboard, _last_pc_clipboard, _last_phone_clipboard_at
            text = body.get('text', '')
            is_local = client_info.get('local', False)
            if not text and is_local:
                text = get_kde_clipboard()
                if text:
                    with _clipboard_lock:
                        _last_pc_clipboard = text
                    auth_mgr.broadcast_ndjson(json.dumps({"event": "clipboard", "text": text}) + "\n")
            elif text:
                with _clipboard_lock:
                    _last_phone_clipboard = text
                    _last_phone_clipboard_at = time.time()
                    _last_pc_clipboard = text
                set_kde_clipboard(text)
                # If triggered from local PC, broadcast to phone
                if is_local:
                    auth_mgr.broadcast_ndjson(json.dumps({"event": "clipboard", "text": text}) + "\n")
            self.send_json({"status": "ok"})

        # 6. Media Control
        elif path == '/api/media/command':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            cmd = body.get('command', '')
            success = send_mpris_command(cmd)
            self.send_json({"status": "ok" if success else "failed"})

        elif path == '/api/media/volume':
            # Slider on the phone: set the playing app's volume (system volume if none)
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            try:
                ok = set_app_volume(body.get('percent'))
            except (TypeError, ValueError):
                self.send_json({"status": "error", "message": "percent must be a number"}, status=400)
                return
            self.send_json({"status": "ok" if ok else "failed", "app_volume": app_volume()})

        # Terminal Endpoints
        elif path == '/api/terminals/write':
            if not is_lan_or_wifi_request(self):
                self.send_json({"error": "forbidden", "message": "Terminal is only accessible over local Wi-Fi / LAN for security."}, status=403)
                return
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            term_id = body.get('id', '')
            text = body.get('text', '')
            res = {'status': 'ok'}
            if body.get('move') or body.get('backspace'):
                try:
                    res = edit_terminal_input(term_id, body.get('move', 0), body.get('backspace', 0))
                except (TypeError, ValueError):
                    res = {'status': 'error', 'message': 'bad edit'}
            if res['status'] == 'ok' and body.get('replace'):
                # the phone loaded the line typed on the computer and edited it: remove the old text first
                try:
                    res = replace_terminal_input(term_id, body.get('replace'))
                except (TypeError, ValueError):
                    res = {'status': 'error', 'message': 'bad replace count'}
            if res['status'] == 'ok' and text:
                res = write_terminal(term_id, text)
            self.send_json(res)
            return

        elif path == '/api/terminals/key':
            if not is_lan_or_wifi_request(self):
                self.send_json({"error": "forbidden", "message": "Terminal is only accessible over local Wi-Fi / LAN for security."}, status=403)
                return
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            term_id = body.get('id', '')
            key_name = body.get('key', '')
            res = send_terminal_key(term_id, key_name)
            self.send_json(res)
            return

        elif path == '/api/terminals/new':
            if not is_lan_or_wifi_request(self):
                self.send_json({"error": "forbidden", "message": "Terminal is only accessible over local Wi-Fi / LAN for security."}, status=403)
                return
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            workdir = body.get('cwd', '')
            res = spawn_new_terminal(workdir)
            self.send_json(res)
            return

        # 7. Ping (Ring PC)
        elif path == '/api/ping':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            ping_pc()
            self.send_json({"status": "ok"})

        # 8. Ring (Find My Phone)
        elif path == '/api/ring':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            ring_phone()
            self.send_json({"status": "ok"})

        # 8b. Stop Ringing (Find My Phone)
        elif path == '/api/unring':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            unring_phone()
            self.send_json({"status": "ok"})

        # 9. Remote Action (Lock, Suspend, Screen off)
        elif path == '/api/action':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            act = body.get('action', '')
            if act in ('reboot', 'shutdown', 'suspend') and not is_lan_or_wifi_request(self):
                # Over the internet a leaked token must not be able to switch the computer off
                self.send_json({"status": "error", "message": "Restart, shut down and sleep only work over Wi-Fi."}, status=403)
                return
            if act == 'lock':
                subprocess.Popen(['loginctl', 'lock-session'])
                try:
                    subprocess.run(['qdbus6', 'org.freedesktop.ScreenSaver', '/ScreenSaver', 'org.freedesktop.ScreenSaver.Lock'], stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                self.send_json({"status": "ok", "action": "lock"})
            elif act == 'unlock':
                pwd = body.get('password', '')
                user = os.environ.get('USER', 'lunarphoton')
                # The account password is required (checked with unix_chkpwd). Works over the
                # internet too, so wrong guesses are rate-limited: 5 per 10 minutes.
                if not pwd:
                    self.send_json({"status": "error", "message": "Password required"}, status=401)
                    return
                now_t = time.time()
                with auth_mgr.lock:
                    fails = [t for t in getattr(auth_mgr, 'unlock_failures', []) if now_t - t < 600]
                    auth_mgr.unlock_failures = fails
                if len(fails) >= 5:
                    self.send_json({"status": "error", "message": "Too many wrong passwords. Try again in 10 minutes."}, status=429)
                    return
                try:
                    p = subprocess.Popen(['unix_chkpwd', user, 'nullok'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    out, err = p.communicate(pwd.encode('utf-8') + b'\x00', timeout=3)
                    if p.returncode != 0:
                        with auth_mgr.lock:
                            auth_mgr.unlock_failures.append(now_t)
                        self.send_json({"status": "error", "message": "Incorrect password"}, status=401)
                        return
                except Exception as e:
                    self.send_json({"status": "error", "message": f"Verification error: {str(e)}"}, status=500)
                    return

                # Unlock session via loginctl and ScreenSaver D-Bus
                subprocess.Popen(['loginctl', 'unlock-session'])
                try:
                    subprocess.run(['qdbus6', 'org.freedesktop.ScreenSaver', '/ScreenSaver', 'org.freedesktop.ScreenSaver.SetActive', 'false'], stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                try:
                    subprocess.Popen(['notify-send', '-i', 'system-lock-screen', '-a', 'Orbit', 'Unlocked', 'Screen unlocked from your phone'], stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                self.send_json({"status": "ok", "action": "unlock"})
            elif act == 'suspend':
                subprocess.Popen(['systemctl', 'suspend'])
                self.send_json({"status": "ok", "action": "suspend"})
            elif act == 'screen_off':
                subprocess.Popen(['kscreen-doctor', '--dpms', 'off'])
                self.send_json({"status": "ok", "action": "screen_off"})
            elif act in ('reboot', 'shutdown'):
                subprocess.Popen(['systemctl', 'reboot' if act == 'reboot' else 'poweroff'])
                self.send_json({"status": "ok", "action": act})
            elif act == 'restart_desktop':
                # Recovery when the panel/desktop has frozen
                subprocess.Popen(['systemctl', '--user', 'restart', 'plasma-plasmashell.service'])
                self.send_json({"status": "ok", "action": act})
            else:
                self.send_json({"status": "unknown_action"}, status=400)

        elif path == '/api/keep_awake':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            set_keep_awake(bool(body.get('enabled')))
            self.send_json({"status": "ok", "enabled": keep_awake_active()})

        elif path == '/api/processes/kill':
            if not is_lan_or_wifi_request(self):
                self.send_json({"error": "forbidden", "message": "Ending apps only works over Wi-Fi."}, status=403)
                return
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            pids = body.get('pids') or []
            if not isinstance(pids, list):
                pids = []
            res = kill_processes(pids, force=bool(body.get('force')))
            self.send_json({"status": "ok" if res['killed'] else "error", **res})

        # 9b. Telephony / Call Event (Auto-pause media on call)
        elif path == '/api/telephony/call':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            state = body.get('state', '').lower()
            handle_telephony_call_state(state)
            self.send_json({"status": "ok", "state": state})

        # 10. File Staging for PC-to-Phone send (Local PC Only)
        elif path == '/api/files/stage':
            is_local = ((self.client_address[0] in ('127.0.0.1', '::1', 'localhost'))
                        and not is_tunnel_request(self) and not is_foreign_browser_request(self))
            # Staging serves any readable file, so only this user's own tools may do it
            if not is_local or not has_local_token(self):
                self.send_json({"error": "forbidden", "message": "File staging is only allowed from local PC."}, status=403)
                return
            filepath = body.get('filepath', '')
            if not filepath or not os.path.exists(filepath):
                self.send_json({"error": "file_not_found"}, status=404)
                return

            now = time.time()
            # Prune expired staged files older than 1 hour
            for tok, item in list(staged_files.items()):
                if now - item.get('created_at', 0) > 3600:
                    staged_files.pop(tok, None)

            token = secrets.token_urlsafe(16)
            fn = os.path.basename(filepath)
            size = os.path.getsize(filepath)
            staged_files[token] = {
                "token": token,
                "filepath": filepath,
                "filename": fn,
                "size": size,
                "created_at": now,
                "downloaded": False,
                "downloaded_at": None
            }
            local_ip = get_local_ip()
            cfg = load_config()
            port = cfg.get('web_port', 1760)
            dl_url = f"http://{local_ip}:{port}/api/files/staging/{token}"
            tunnel_url = tunnel_mgr.get_url()
            internet_dl_url = f"{tunnel_url}/api/files/staging/{token}" if tunnel_url else None
            auth_mgr.broadcast_ndjson(json.dumps({
                "event": "incoming_file",
                "filename": fn,
                "download_url": dl_url,
                "internet_download_url": internet_dl_url,
                "staging_path": f"/api/files/staging/{token}",
                "size": size
            }) + "\n")
            has_listeners = bool(auth_mgr.ndjson_clients)
            self.send_json({
                "status": "ok",
                "token": token,
                "download_url": dl_url,
                "internet_download_url": internet_dl_url,
                "staging_path": f"/api/files/staging/{token}",
                "phone_connected": has_listeners
            })

        # 11. Phone Filesystem Mutations (Proxy)
        elif path == '/api/phone/files/delete':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            phone_ip, phone_port, phone_token = get_phone_target()
            if not phone_ip:
                self.send_json({"error": "phone_not_connected"}, status=503)
                return
            target_path = body.get('path', '')
            try:
                target_url = f"http://{phone_ip}:{phone_port}/api/files/delete?path={quote(target_path)}"
                headers = {'Content-Type': 'application/json'}
                if phone_token:
                    headers['Authorization'] = f'Bearer {phone_token}'
                req = urllib.request.Request(target_url, data=b'{}', headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.send_json({"status": "ok"})
            except Exception as e:
                self.send_json({"error": "delete_failed", "message": str(e)}, status=502)

        elif path == '/api/phone/files/mkdir':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            phone_ip, phone_port, phone_token = get_phone_target()
            if not phone_ip:
                self.send_json({"error": "phone_not_connected"}, status=503)
                return
            parent_path = body.get('path', '')
            name = body.get('name', '')
            try:
                target_url = f"http://{phone_ip}:{phone_port}/api/files/mkdir?path={quote(parent_path)}&name={quote(name)}"
                headers = {'Content-Type': 'application/json'}
                if phone_token:
                    headers['Authorization'] = f'Bearer {phone_token}'
                req = urllib.request.Request(target_url, data=b'{}', headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.send_json({"status": "ok"})
            except Exception as e:
                self.send_json({"error": "mkdir_failed", "message": str(e)}, status=502)

        # 12. Open Webpage / URL in default browser
        elif path == '/api/open_url':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            url = body.get('url', '').strip()
            if url and (url.startswith('http://') or url.startswith('https://')):
                try:
                    subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        subprocess.Popen(['notify-send', '-i', 'applications-internet', '-a', 'Orbit', 'Link from phone', url], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    self.send_json({"status": "ok", "url": url})
                except Exception as e:
                    self.send_json({"error": str(e)}, status=500)
            else:
                self.send_json({"error": "invalid_url", "message": "URL must start with http:// or https://"}, status=400)

        # 13. Virtual Mouse & Keyboard / Trackpad (HTTP fallback)
        elif path == '/api/mouse':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            # Keyboard/mouse is as powerful as a terminal, so it is LAN-only too
            if not is_lan_or_wifi_request(self):
                self.send_json({"error": "forbidden", "message": "Remote input is only available on the same Wi-Fi."}, status=403)
                return
            mtype = body.get('type', '')
            if mtype == 'move':
                virtual_mouse.move(body.get('dx', 0), body.get('dy', 0))
            elif mtype == 'pointer':
                trigger_laser_overlay(body)
            elif mtype == 'laser_state':
                trigger_laser_overlay(body)
            elif mtype == 'key':
                virtual_mouse.press_key(body.get('key', ''))
            elif mtype == 'text':
                virtual_mouse.type_text(body.get('text', ''))
            elif mtype == 'click':
                virtual_mouse.click(body.get('button', 'left'))
            elif mtype == 'down':
                virtual_mouse.mouse_down(body.get('button', 'left'))
            elif mtype == 'up':
                virtual_mouse.mouse_up(body.get('button', 'left'))
            elif mtype == 'scroll':
                virtual_mouse.scroll(body.get('dy', 0), body.get('dx', 0))
            self.send_json({"status": "ok"})

        elif path == '/api/keyboard/text':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            # Keyboard/mouse is as powerful as a terminal, so it is LAN-only too
            if not is_lan_or_wifi_request(self):
                self.send_json({"error": "forbidden", "message": "Remote input is only available on the same Wi-Fi."}, status=403)
                return
            text = body.get('text', '')
            if text:
                virtual_mouse.type_text(text)
            self.send_json({"status": "ok"})

        elif path == '/api/keyboard/key':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            # Keyboard/mouse is as powerful as a terminal, so it is LAN-only too
            if not is_lan_or_wifi_request(self):
                self.send_json({"error": "forbidden", "message": "Remote input is only available on the same Wi-Fi."}, status=403)
                return
            key = body.get('key', '')
            if key:
                virtual_mouse.press_key(key)
            self.send_json({"status": "ok"})

        # 14. Unpair Device API (Local PC or Phone)
        elif path in ('/api/devices/unpair', '/api/unpair'):
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            is_local = client_info.get('local', False)
            if is_local:
                ident = body.get('token') or body.get('client_id') or body.get('id') or body.get('ip')
            else:
                ident = token
            if not ident:
                self.send_json({"error": "missing_device_id"}, status=400)
                return
            ok, msg = unpair_device(ident)
            self.send_json({"status": "ok" if ok else "error", "message": msg})

        # 16. Captive Portal / Campus Auto-Login
        elif path == '/api/network/captive_login':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            res = trigger_pc_captive_login(body)
            self.send_json(res)
            return

        else:
            self.send_error(404, "Not Found")

    def handle_poll(self, topic):
        cfg = load_config()
        client_info, token = authenticate_client(self, cfg)
        if not client_info:
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized", "message": "Pairing required"}).encode('utf-8'))
            return

        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()

        with auth_mgr.lock:
            if auth_mgr.current_session and auth_mgr.current_session["status"] == "pending":
                if time.time() <= auth_mgr.current_session["expires_at"]:
                    msg = auth_mgr.build_app_notification(auth_mgr.current_session, topic=topic)
                    try:
                        self.wfile.write((json.dumps(msg) + "\n").encode('utf-8'))
                        self.wfile.flush()
                    except Exception:
                        pass

    def handle_ndjson_stream(self, topic):
        cfg = load_config()
        client_info, token = authenticate_client(self, cfg)
        if not client_info:
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized", "message": "Pairing required"}).encode('utf-8'))
            return

        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        q = queue.Queue()
        auth_mgr.add_ndjson_client(q)

        # 1. Handshake conforming to ntfy spec
        open_event = {
            "id": str(uuid.uuid4())[:10],
            "time": int(time.time()),
            "event": "open",
            "topic": topic
        }
        try:
            self.wfile.write((json.dumps(open_event) + "\n").encode('utf-8'))
            self.wfile.flush()
        except Exception:
            auth_mgr.remove_ndjson_client(q)
            return

        # 2. Push active challenge if pending
        with auth_mgr.lock:
            if auth_mgr.current_session and auth_mgr.current_session["status"] == "pending":
                if time.time() <= auth_mgr.current_session["expires_at"]:
                    pending_msg = auth_mgr.build_app_notification(auth_mgr.current_session, topic=topic)
                    try:
                        self.wfile.write((json.dumps(pending_msg) + "\n").encode('utf-8'))
                        self.wfile.flush()
                    except Exception:
                        pass

        # 3. Push pending un-downloaded staged files from the last 15 minutes
        now = time.time()
        local_ip = get_local_ip()
        cfg = load_config()
        port = cfg.get('web_port', 1760)
        tunnel_url = tunnel_mgr.get_url()
        for s_token, s_info in list(staged_files.items()):
            if not s_info.get("downloaded") and (now - s_info.get("created_at", 0) < 900):
                if os.path.exists(s_info.get("filepath", "")):
                    staged_event = {
                        "event": "incoming_file",
                        "filename": s_info["filename"],
                        "download_url": f"http://{local_ip}:{port}/api/files/staging/{s_token}",
                        "internet_download_url": f"{tunnel_url}/api/files/staging/{s_token}" if tunnel_url else None,
                        "staging_path": f"/api/files/staging/{s_token}",
                        "size": s_info["size"]
                    }
                    try:
                        self.wfile.write((json.dumps(staged_event) + "\n").encode('utf-8'))
                        self.wfile.flush()
                        sys.stderr.write(f"🔄 Replayed pending staged file '{s_info['filename']}' to reconnected phone\n")
                    except Exception:
                        pass

        # 4. Stream loop. A keepalive wakes the phone's radio, so send it rarely: every ~170 s
        # on Wi-Fi (nothing in between drops an idle connection); through the tunnel every
        # 50 s, since proxies close idle connections sooner
        keepalive_every = 50 if is_tunnel_request(self) else 170
        try:
            while True:
                try:
                    msg = q.get(timeout=keepalive_every)
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    keepalive = {
                        "id": str(uuid.uuid4())[:10],
                        "time": int(time.time()),
                        "event": "keepalive",
                        "topic": topic
                    }
                    self.wfile.write((json.dumps(keepalive) + "\n").encode('utf-8'))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            auth_mgr.remove_ndjson_client(q)

    def handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        q = queue.Queue()
        auth_mgr.add_sse_client(q)

        init_data = {"hostname": socket.gethostname()}
        try:
            self.wfile.write(f"event: info\ndata: {json.dumps(init_data)}\n\n".encode('utf-8'))
            self.wfile.flush()
        except Exception:
            auth_mgr.remove_sse_client(q)
            return

        with auth_mgr.lock:
            if auth_mgr.current_session and auth_mgr.current_session["status"] == "pending":
                try:
                    self.wfile.write(f"event: auth_request\ndata: {json.dumps(auth_mgr.current_session)}\n\n".encode('utf-8'))
                    self.wfile.flush()
                except Exception:
                    pass

        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            auth_mgr.remove_sse_client(q)

    def serve_apk(self):
        apk = apk_file()
        if not os.path.exists(apk):
            self.send_error(404, "APK Not Found")
            return
        try:
            size = os.path.getsize(apk)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.android.package-archive')
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', 'attachment; filename="Orbit.apk"')
            self.end_headers()
            with open(apk, 'rb') as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try: self.send_error(500, str(e))
            except Exception: pass

    def serve_file(self, filepath, content_type):
        if not os.path.exists(filepath):
            self.send_error(404, "File Not Found")
            return
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try: self.send_error(500, str(e))
            except Exception: pass

    def handle_camera_snapshot(self, device=''):
        cam = get_best_camera(device)
        cmd = [
            'ffmpeg', '-nostdin', '-loglevel', 'error', '-y',
            '-f', 'v4l2', '-input_format', 'mjpeg',
            '-video_size', '1280x720',
            '-i', cam,
            '-vframes', '1',
            '-f', 'image2', 'pipe:1'
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
            if res.returncode == 0 and res.stdout:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(res.stdout)))
                self.send_header('Cache-Control', 'no-cache, no-store')
                self.end_headers()
                self.wfile.write(res.stdout)
                return
        except Exception:
            pass
        self.send_error(500, "Failed to capture camera snapshot")

    def handle_camera_stream(self, device='', quality='smooth'):
        cam = get_best_camera(device)
        size = '1280x720' if quality == 'hd' else '640x480'
        fps = '20' if quality == 'hd' else '30'

        cmd = [
            'ffmpeg', '-nostdin', '-loglevel', 'error',
            '-f', 'v4l2', '-input_format', 'mjpeg',
            '-video_size', size, '-framerate', fps,
            '-i', cam,
            '-c:v', 'copy',
            '-f', 'mjpeg', 'pipe:1'
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            self.send_error(500, f"Cannot access camera: {e}")
            return

        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=--frame')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()

        buffer = b''
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start = buffer.find(b'\xff\xd8')
                    if start == -1:
                        buffer = buffer[-2:]
                        break
                    end = buffer.find(b'\xff\xd9', start)
                    if end == -1:
                        buffer = buffer[start:]
                        break
                    frame = buffer[start:end+2]
                    buffer = buffer[end+2:]
                    header = f"\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode('ascii')
                    self.wfile.write(header + frame)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                proc.kill()

    def handle_camera_web_page(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PC Webcam Live Viewer</title>
    <style>
        body { margin: 0; background: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; }
        .card { background: #1e293b; border-radius: 16px; padding: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); max-width: 720px; width: 90%; text-align: center; }
        h1 { margin-top: 0; font-size: 20px; color: #06b6d4; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .stream-container { position: relative; border-radius: 12px; overflow: hidden; background: #000; width: 100%; aspect-ratio: 4/3; display: flex; align-items: center; justify-content: center; }
        .stream-container img { width: 100%; height: 100%; object-fit: contain; }
        .actions { margin-top: 16px; display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
        button, a.btn { background: #0ea5e9; color: white; border: none; padding: 10px 18px; border-radius: 8px; font-weight: bold; cursor: pointer; text-decoration: none; font-size: 14px; transition: background 0.2s; }
        button:hover, a.btn:hover { background: #0284c7; }
        .btn-green { background: #10b981; }
        .btn-green:hover { background: #059669; }
    </style>
</head>
<body>
    <div class="card">
        <h1>📹 PC Camera Live Feed</h1>
        <div class="stream-container">
            <img src="/api/camera/stream" alt="Live Camera Feed">
        </div>
        <div class="actions">
            <a class="btn btn-green" href="/api/camera/snapshot" target="_blank" download="snapshot.jpg">📸 Save Snapshot</a>
            <button onclick="location.reload()">🔄 Reconnect</button>
            <a class="btn" href="/browse">📁 File Explorer</a>
        </div>
    </div>
</body>
</html>"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 0:
                return json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            pass
        return {}

    def send_json(self, data, status=200):
        content = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

# ==========================================
# Network Discovery & Beaconing
# ==========================================

def get_default_gateway():
    try:
        out = subprocess.check_output(['ip', 'route', 'show', 'default'], stderr=subprocess.DEVNULL).decode()
        import re
        m = re.search(r'default\s+via\s+([0-9.]+)', out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

def start_udp_discovery_server(port):
    def udp_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', port))
            sys.stderr.write(f"  UDP Discovery Listener active on port {port}\n")
            sys.stderr.flush()
            while True:
                data, addr = sock.recvfrom(1024)
                local_ip = get_local_ip()
                reply = {
                    "type": "pc_auth_discovery_reply",
                    "device_id": get_laptop_id(),
                    "hostname": socket.gethostname(),
                    "ip": local_ip,
                    "port": port,
                    "user": os.environ.get("USER", "lunarphoton"),
                    "local_url": f"http://{local_ip}:{port}",
                    "internet_url": tunnel_mgr.get_url()
                }
                try:
                    sock.sendto(json.dumps(reply).encode('utf-8'), addr)
                except Exception:
                    pass
        except Exception as e:
            sys.stderr.write(f"UDP discovery error: {e}\n")
            sys.stderr.flush()

    t = threading.Thread(target=udp_loop, daemon=True)
    t.start()

def start_udp_beacon(port):
    def beacon_loop():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            try:
                local_ip = get_local_ip()
                gw = get_default_gateway()
                beacon_data = json.dumps({
                    "type": "pc_auth_discovery_reply",
                    "device_id": get_laptop_id(),
                    "hostname": socket.gethostname(),
                    "ip": local_ip,
                    "port": port,
                    "user": os.environ.get("USER", "lunarphoton"),
                    "local_url": f"http://{local_ip}:{port}"
                }).encode('utf-8')

                try:
                    sock.sendto(beacon_data, ('255.255.255.255', port))
                except Exception:
                    pass

                if gw and gw != '127.0.0.1':
                    try:
                        sock.sendto(beacon_data, (gw, port))
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(3)

    t = threading.Thread(target=beacon_loop, daemon=True)
    t.start()

def cli_pair():
    cfg = load_config()
    port = cfg.get('web_port', 1760)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/pair/request",
            data=b'{"client_name":"Terminal CLI"}',
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            print("==================================================")
            print("  Orbit - pair a phone")
            print("==================================================")
            print("  A pairing PIN has been triggered.")
            print("  Check your desktop notifications or system logs.")
            print(f"  Laptop ID: {data.get('device_id')}")
            print(f"  Hostname:  {data.get('hostname')}")
            print("==================================================")
    except Exception as e:
        print(f"Error requesting pairing PIN: {e}")

def main():
    secure_config_permissions()
    init_local_token()
    cfg = load_config()
    port = cfg.get('web_port', 1760)
    local_ip = get_local_ip()

    update_dolphin_servicemenu()
    start_udp_discovery_server(port)
    start_udp_beacon(port)
    start_udp_mouse_listener(1762)
    start_clipboard_monitor(auth_mgr)
    start_terminal_recorder()

    tunnel_mgr.port = port
    tunnel_mgr.start()

    server = ThreadingHTTPServer(('0.0.0.0', port), AuthenticatorHandler)
    sys.stderr.write("==================================================\n")
    sys.stderr.write(f"  Orbit running on port {port}\n")
    sys.stderr.write(f"  Device ID: {get_laptop_id()}\n")
    sys.stderr.write(f"  Web Explorer: http://{local_ip}:{port}/browse\n")
    sys.stderr.write(f"  Download APK: http://{local_ip}:{port}/apk\n")
    sys.stderr.write(f"  Topic Stream: http://{local_ip}:{port}/login/json\n")
    sys.stderr.write("==================================================\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        tunnel_mgr.stop()
        server.server_close()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('--pair', 'pair'):
        cli_pair()
    else:
        main()

#!/usr/bin/env python3
import os
import codecs
import collections
import sys
import json
import time
import uuid
import socket
import select
import subprocess
import signal
import threading
import queue
import secrets
import hmac
import hashlib
import glob
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
_APK_PRINT = {}


def apk_print():
    """(sha256, size) of the app build handed out; the phone updates itself when it differs
    from the one it runs (the version number never changes between builds)."""
    path = apk_file()
    try:
        st = os.stat(path)
    except OSError:
        return None, 0
    key = (path, st.st_mtime, st.st_size)
    if _APK_PRINT.get('key') != key:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        _APK_PRINT.update(key=key, sha=h.hexdigest())
    return _APK_PRINT['sha'], st.st_size


def apk_note():
    """The signed release note beside the app build (Orbit.apk.json: version, SHA-256, signature),
    or None. The phone checks its signature itself; this only hands it over."""
    try:
        with open(apk_file() + '.json') as f:
            note = json.load(f)
        return note if isinstance(note, dict) else None
    except (OSError, ValueError):
        return None


def app_update_watch():
    """A new app build installed here: tell the phone, which fetches and installs it."""
    last = None
    while True:
        try:
            sha, size = apk_print()
            if sha and sha != last:
                last = sha
                sys.stderr.write(f"Phone app build ({size // 1024} KB): telling the phone\n")
                auth_mgr.broadcast_ndjson(json.dumps({"event": "app_update", "sha256": sha, "size": size}) + "\n")
        except Exception as e:
            sys.stderr.write(f"App update watch: {e}\n")
        time.sleep(15)


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
    WALK_AWAY.contact(is_tunnel_request(handler))
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

_GRAPHICAL_SESSION = [0.0, None]

def graphical_session():
    """This user's desktop login session (its loginctl id), or None while the computer sits
    at the login screen. This service runs outside that session (it starts at boot, before
    anyone logs in), so loginctl's "auto" session never means anything here: look it up."""
    if time.time() - _GRAPHICAL_SESSION[0] < 3:
        return _GRAPHICAL_SESSION[1]
    found = None
    try:
        out = subprocess.run(['loginctl', 'list-sessions', '--no-legend'], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[1] != str(os.getuid()):
                continue
            info = subprocess.run(['loginctl', 'show-session', parts[0], '-p', 'Class', '-p', 'Type', '-p', 'State'],
                                  capture_output=True, text=True, timeout=2).stdout
            props = dict(l.split('=', 1) for l in info.splitlines() if '=' in l)
            if props.get('Class') == 'user' and props.get('Type') in ('wayland', 'x11') and props.get('State') != 'closing':
                found = parts[0]
                if props.get('State') == 'active':
                    break
    except (OSError, subprocess.SubprocessError):
        pass
    _GRAPHICAL_SESSION[:] = [time.time(), found]
    return found

def current_challenge_reply():
    """The sign-in waiting for approval on this computer, if any."""
    with auth_mgr.lock:
        if auth_mgr.current_session and auth_mgr.current_session["status"] == "pending":
            now = time.time()
            if now <= auth_mgr.current_session["expires_at"]:
                session_copy = dict(auth_mgr.current_session)
                session_copy["id"] = session_copy.get("session_id")
                session_copy["remaining_seconds"] = max(1, int(auth_mgr.current_session["expires_at"] - now))
                return {"has_challenge": True, "challenge": session_copy}
    return {"has_challenge": False}

def is_pc_locked():
    """True or False, or None when nobody is logged in (the login screen isn't "unlocked")."""
    session = graphical_session()
    if session is None:
        return None
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
            ['loginctl', 'show-session', session, '-p', 'LockedHint'],
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
        "locked": bool(locked),
        "logged_in": locked is not None,     # False: the computer is at its login screen
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


class _PyteScreen:
    """A pyte screen behind the interface TermRecording uses; the fallback when the C++
    emulator (liborbitvt.so, see _VtScreen) isn't built."""

    def __init__(self, cols, rows, history):
        self.history = history
        self.screen = _DimHistoryScreen(cols, rows, history=history) if history else _DimScreen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    columns = property(lambda self: self.screen.columns)
    lines = property(lambda self: self.screen.lines)

    def cursor(self):
        return self.screen.cursor.x, self.screen.cursor.y

    def feed(self, text):
        self.stream.feed(text)

    def resize(self, lines, columns):
        self.screen.resize(lines=lines, columns=columns)

    def history_len(self):
        return len(self.screen.history.top) if self.history else 0

    history_serial = history_len    # changes when lines scroll into the history

    def take_dirty(self):
        """Rows changed since the last call (pyte tracks them in `dirty`)."""
        scr = self.screen
        dirty = [y for y in scr.dirty if 0 <= y < scr.lines]
        scr.dirty.clear()
        return dirty

    def rows(self, start=0, end=None):
        """(plain, ansi, wrapped-from-above) for screen rows [start, end)."""
        scr = self.screen
        end = scr.lines if end is None else min(end, scr.lines)
        return [_render_row(scr.buffer[y], scr.columns) + (getattr(scr.buffer[y], 'cont', False),)
                for y in range(start, end)]

    def history_rows(self, count):
        """The same for the newest `count` lines of history."""
        if not self.history or count <= 0:
            return []
        return [_render_row(l, self.screen.columns) + (getattr(l, 'cont', False),)
                for l in list(self.screen.history.top)[-count:]]

    def cells(self, y):
        """(x, text, dim) for every cell of row y, the second halves of wide characters left out."""
        row = self.screen.buffer[y]
        return [(x, row[x].data, row[x].blink) for x in range(self.screen.columns) if row[x].data != '']


# The C++ emulator: native/orbitvt.cpp, built by the installer into liborbitvt.so next to this
# file. It does pyte's job (and renders rows) many times faster: replaying a long recording
# takes a fraction of a second instead of seconds of CPU. ORBIT_VT=pyte forces the fallback.
import ctypes
import operator
import time
_first = operator.itemgetter(0)
_VT_ABI = 1


def _load_vt():
    if os.environ.get('ORBIT_VT') == 'pyte':
        return None
    here = os.path.dirname(os.path.abspath(globals().get('__file__') or 'x'))
    for path in (os.environ.get('ORBIT_VT_LIB'), os.path.join(here, 'liborbitvt.so'),
                 os.path.expanduser('~/.local/share/orbit/liborbitvt.so')):
        if not path or not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
            if lib.ovt_abi() != _VT_ABI:
                continue
        except (OSError, AttributeError):
            continue
        P, I, L, N = ctypes.c_void_p, ctypes.c_int, ctypes.c_long, ctypes.POINTER(ctypes.c_size_t)
        sigs = {'ovt_new': (P, [I, I, I]), 'ovt_free': (None, [P]), 'ovt_feed': (None, [P, ctypes.c_char_p, ctypes.c_size_t]),
                'ovt_resize': (None, [P, I, I]), 'ovt_columns': (I, [P]), 'ovt_lines': (I, [P]),
                'ovt_cursor_x': (I, [P]), 'ovt_cursor_y': (I, [P]), 'ovt_history_len': (L, [P]),
                'ovt_history_serial': (ctypes.c_ulonglong, [P]), 'ovt_take_dirty': (I, [P, ctypes.POINTER(I), I]),
                'ovt_rows': (P, [P, I, L, L, N]), 'ovt_cells': (P, [P, I, N])}
        for name, (res, args) in sigs.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = res, args
        return lib
    return None


_VT = _load_vt()


class _VtScreen:
    """A screen in the C++ emulator, with _PyteScreen's interface."""

    def __init__(self, cols, rows, history):
        self._vt = _VT
        self._h = _VT.ovt_new(cols, rows, history)
        self._len = ctypes.c_size_t()

    def __del__(self):
        if getattr(self, '_h', None):
            self._vt.ovt_free(self._h)
            self._h = None

    columns = property(lambda self: self._vt.ovt_columns(self._h))
    lines = property(lambda self: self._vt.ovt_lines(self._h))

    def cursor(self):
        return self._vt.ovt_cursor_x(self._h), self._vt.ovt_cursor_y(self._h)

    def feed(self, text):
        data = text.encode('utf-8', 'replace')
        self._vt.ovt_feed(self._h, data, len(data))

    def resize(self, lines, columns):
        self._vt.ovt_resize(self._h, lines, columns)

    def history_len(self):
        return self._vt.ovt_history_len(self._h)

    def history_serial(self):
        return self._vt.ovt_history_serial(self._h)

    def take_dirty(self):
        ys = (ctypes.c_int * max(1, self.lines))()
        return ys[:self._vt.ovt_take_dirty(self._h, ys, len(ys))]

    def _records(self, ptr):
        text = ctypes.string_at(ptr, self._len.value).decode('utf-8', 'replace')
        return text.split('\x1e')[:-1]

    def rows(self, start=0, end=None):
        end = self.lines if end is None else end
        recs = self._records(self._vt.ovt_rows(self._h, 0, start, end, ctypes.byref(self._len)))
        return [(p, a, c == '1') for p, a, c in (r.split('\x1f') for r in recs)]

    def history_rows(self, count):
        n = self.history_len()
        if count <= 0 or not n:
            return []
        recs = self._records(self._vt.ovt_rows(self._h, 1, max(0, n - count), n, ctypes.byref(self._len)))
        return [(p, a, c == '1') for p, a, c in (r.split('\x1f') for r in recs)]

    def cells(self, y):
        raw = ctypes.string_at(self._vt.ovt_cells(self._h, y, ctypes.byref(self._len)), self._len.value)
        n = int.from_bytes(raw[:4], 'little')
        xs = memoryview(raw)[4:4 + 2 * n].cast('H').tolist()
        texts = raw[4 + 3 * n:].decode('utf-8', 'replace').split('\x1e')
        return list(zip(xs, texts, map(bool, raw[4 + 2 * n:4 + 3 * n])))


def _make_screen(cols, rows, history):
    return _VtScreen(cols, rows, history) if _VT else _PyteScreen(cols, rows, history)

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


ALT_FRAME_INTERVAL = 0.3      # seconds of recorded time between snapshots of a full-screen app
_SCROLL_ANSWER = 0.4          # seconds an app has to redraw after a scroll input
ALT_HISTORY_MAX = 5000


_ANSI_SGR = re.compile(r'\x1b\[[0-9;:]*m')
_RULE_LINE = re.compile(r'^\s*─{20,}')


def _split_footer(frame):
    """Split a full-screen app's frame into (body, footer). The footer is fixed chrome at the
    bottom between rules, like Claude Code's input box and status bar; it must not pile up in
    the history, so only the current one is shown, once, at the end."""
    rules = [i for i, (plain, _) in enumerate(frame) if _RULE_LINE.match(plain)]
    bottom = [i for i in rules if i >= len(frame) - 14]
    if len(bottom) >= 2:
        return frame[:bottom[-2]], frame[bottom[-2]:]
    return frame, []


_VOLATILE = re.compile(r'[0-9|█▉▊▋▌▍▎▏▓▒░#=*.\-:/%]+')


def _same_layout(a, b):
    """Do two frames show the same screen with values updated (a dashboard like htop)?
    Compares lines with numbers and bar characters masked out."""
    if not a or not b or abs(len(a) - len(b)) > 3:
        return False
    same = sum(1 for x, y in zip(a, b) if _VOLATILE.sub('#', x[0]) == _VOLATILE.sub('#', y[0]))
    return same * 5 >= max(len(a), len(b)) * 3          # 60% of lines


_STATUS_MAX = 6
# Hints an app shows while you've scrolled back inside it (Claude Code's fullscreen view)
_SCROLLED_HINT = re.compile(r'jump to bottom|ctrl\+end\b', re.I)

def _shift(before, now):
    """How far a screen's text moved between two frames (lists of plain rows): d > 0 when it
    moved up d rows (newer text came in below), d < 0 when it moved down, 0 if it stayed,
    None if nothing lines up."""
    best, best_hits = None, 2
    for d in range(-40, 41):
        hits = sum(1 for i, p in enumerate(now) if p.strip() and 0 <= i + d < len(before) and before[i + d] == p)
        if hits > best_hits:
            best, best_hits = d, hits
    return best


def _scrolled_back(rows):
    """Does the bottom of the screen (just above the app's own input/status area) show it?"""
    end = len(rows)
    while end and not rows[end - 1][0].strip():
        end -= 1
    return any(_SCROLLED_HINT.search(plain) for plain, _ in rows[max(0, end - 12):end])
_STATUS_CONFIRM = 3     # scrolls a row must stay pinned through to count as status

def _status_mask(plain):
    """A row with its moving parts masked: numbers, bars, a leading spinner glyph and the
    spinner's word ("Misting…", "Swooping…")."""
    t = re.sub(r'^\W+', '', _VOLATILE.sub('#', plain.strip()))
    t = re.sub(r'^[\w-]+…', '…', t)
    # a progress row ("Misting… (1m 5s · ↓ 4k tokens · …)") is one kind, whatever its details say
    return '…(' if t.startswith('… (') else t

def _pinned_rows(prev, body):
    """How many rows at the bottom of a full-screen frame stayed put while the content above
    scrolled up between two frames (0 if it didn't scroll)."""
    if not prev or not body:
        return 0
    def bottom_same(n):
        # exactly the same: a log's "line 41" / "line 42" only differ in numbers, and that's content
        return n <= min(len(prev), len(body)) and all(body[-1 - j][0] == prev[-1 - j][0] for j in range(n))
    upper = max(0, len(body) - _STATUS_MAX)
    # for each shift d, how many text rows above the status area moved up by exactly d
    where = {}
    for k, (plain, _) in enumerate(prev[:upper + 40]):
        where.setdefault(plain, []).append(k)
    hits, texts = [0] * 40, [0]
    for i in range(upper):
        plain = body[i][0]
        texts.append(texts[-1] + (1 if plain.strip() else 0))     # text rows among body[:i+1]
        if plain.strip():
            for k in where.get(plain, ()):
                if 1 <= k - i < 40:
                    hits[k - i] += 1
    scrolled = False
    for d in range(1, min(40, len(prev))):
        pairs = texts[max(0, min(upper, len(prev) - d))]
        if hits[d] >= 3 and hits[d] * 2 >= pairs:
            scrolled = True
            break
    if not scrolled:
        return 0
    n = 0
    while n < min(_STATUS_MAX, len(body) - 1, len(prev) - 1) and bottom_same(n + 1):
        n += 1
    return n

def _merge_frames(old, new):
    """Stitch a full-screen app's new frame body onto the history so far. Items are
    (plain, ansi) lines; matching uses the plain text. The frame is placed where its lines
    best line up anywhere in the history (counting matches, so a ticking timer doesn't break
    it): content that scrolled up stays, a redraw replaces what it covers, and a view of an
    older part updates that part without cutting off what came after it.
    Returns (history, index where the new frame starts)."""
    if not old:
        return list(new), 0
    if not new:
        return list(old), len(old)
    ok, nk = list(map(_first, old)), list(map(_first, new))
    n_old, n_new = len(ok), len(nk)
    anchors = [j for j, line in enumerate(nk) if line.strip()][:4]
    best_s, best_score = None, 0
    newest_first = ok[::-1]
    for j in anchors:
        hits = at = 0
        while hits < 6:                          # newest first; a few candidates per anchor
            try:
                at = newest_first.index(nk[j], at) + 1
            except ValueError:
                break
            p = n_old - at
            hits += 1
            s = p - j
            if s >= 0:
                overlap = min(n_new, n_old - s)
                score = sum(1 for k in range(overlap) if ok[s + k] == nk[k] and nk[k].strip())
                if score > best_score or (score == best_score and best_s is not None and s > best_s):
                    best_s, best_score = s, score
    if best_s is not None:
        overlap = min(n_new, n_old - best_s)
        content = sum(1 for k in range(overlap) if nk[k].strip())
        if best_score >= 2 and best_score * 3 >= content:
            end = best_s + n_new
            return old[:best_s] + list(new) + (old[end:] if end < n_old else []), best_s
    return old + list(new), n_old              # nothing in common: genuinely new screen


# The key hints a menu or picker shows at its bottom (Claude Code's resume list, a dialog):
# a screen that goes away. Capitalised: in the conversation itself, Claude Code's status lines
# say "esc to cancel" in lower case, and those must stay
_MENU_HINT = re.compile(r'(^|·)\s*Esc to (cancel|go back|close|exit)\b')
# Claude Code's "Resume session" over "Resuming conversation…", which has no hint
_MENU_TITLE = re.compile(r'^\s*Resume session\s*$')

def _replaced_screen(old_screen, new):
    """Did the app swap its screen for another instead of scrolling it? Called when nothing
    lined up. Yes when the top stayed and only the layout below changed (a list appearing
    under the same banner), or when the old screen was a menu that has now closed: then it
    goes, as on the computer, instead of staying in the history above what replaced it."""
    def top(rows):
        return [plain for plain, _ in rows if plain.strip()][:3]
    if len(top(old_screen)) == 3 and top(old_screen) == top(new):
        return True
    shown = [plain for plain, _ in old_screen if plain.strip()]
    return (any(_MENU_HINT.search(plain) for plain in shown[-3:])
            or any(_MENU_TITLE.match(plain) for plain in shown[:8]))


def _collapse_blank(items):
    """(plain, ansi) lines with each run of blank lines cut down to one."""
    out, blank = [], True          # True also drops blank lines at the very top
    for item in items:
        if item[0].strip():
            out.append(item)
            blank = False
        elif not blank:
            out.append(item)
            blank = True
    return out


class TermRecording:
    """Incrementally replays one `script` recording into a terminal screen (see _make_screen)."""

    def __init__(self, base):
        self.log_path, self.timing_path = base + '.log', base + '.timing'
        self.timing_pos = self.log_pos = 0
        self.size = [0, 0]           # columns, lines from the header
        self.main = self.alt = None
        self.alt_frames = []         # stitched history of the current full-screen app
        self.alt_footer = []         # its fixed bottom chrome (input box), shown once at the end
        self.alt_prev_body = []      # last frame body, to spot in-place redraws
        self.alt_pos = 0             # index in alt_frames where the current screen starts
        self.view, self.views = 'main', {}   # apps with several views (Claude Code agents): a history each
        self.alt_prev_raw = []        # last full frame body, to see which rows stay put
        self.alt_rows_src = None      # the screen alt_rows was rendered from
        self.main_tui = False         # an app redraws the normal screen in place (see _tui)
        self.main_homes = 0           # jumps to the top seen since text last scrolled
        self.main_hist_len = 0
        self.alt_latest_rows = []     # the last screen showing the latest part (not scrolled back)
        self.alt_scrolled = False     # the app shows an older part (you scrolled back on the computer)
        self.alt_last_seen = []       # the previous screen's body, scrolled or not
        self.input_marks = False      # the recording notes scroll inputs (orbit-term)
        self.scroll_input_until = -1.0   # recorded time until which a redraw follows a scroll input
        self.scroll_probe = None      # (recorded time, screen before, direction) of an unanswered scroll input
        self.hint_seen = False        # the app says when it's scrolled back ("Jump to bottom")
        self.mid_redraw = False       # the last output filled a whole read: more of it follows
        self.last_data_at = 0.0       # wall time the recording last grew
        self.alt_status_seen = {}     # masked row -> scrolls it stayed pinned through
        self.alt_rows, self.alt_rows_width = None, 0   # rendered rows of the current screen
        self.log_backed = False       # Claude Code with its log: the phone needs no stitched history
        self.revision = 0            # bumps whenever the screen may have changed
        self._render_cache = (None, None)
        self.clock = self.last_snap = 0.0
        self.carry = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.lock = threading.Lock()          # guards the screens
        self.update_lock = threading.Lock()   # one reader of the files at a time

    def _new_screen(self, history):
        return _make_screen(self.size[0] or 80, self.size[1] or 24, TERM_REC_HISTORY if history else 0)

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
            # we rebuild one by stitching its frames together (see _snapshot_alt)
            if m.group(1) == 'h':
                self._snapshot_alt()
                self.alt = self._new_screen(False)
                self._reset_stitching()
            else:
                self.alt = None
                if self.main_tui:
                    self._reset_stitching()
            pos = m.end()
        self._feed_plain(text[pos:])

    def _reset_stitching(self):
        self.alt_frames, self.alt_footer, self.alt_prev_body, self.alt_pos = [], [], [], 0
        self.alt_latest_rows = []
        self.alt_scrolled = False
        self.scroll_probe = None
        self.alt_last_seen = []
        self.view, self.views = 'main', {}
        self.alt_prev_raw, self.alt_status_seen = [], {}
        self.alt_rows = None

    def _tui(self):
        """The screen a full-screen app draws on: the alternate screen, or the normal one when
        the app redraws it in place (Claude Code does this after /resume: it jumps to the top
        and rewrites everything, so nothing ever scrolls into history)."""
        if self.alt:
            return self.alt
        return self.main if self.main_tui else None

    def _watch_main_redraws(self, text):
        """Notice an app redrawing the normal screen in place, and notice it stop (text scrolls
        normally again, e.g. back at the shell prompt)."""
        scr = self.main
        hist = scr.history_serial()
        if hist != self.main_hist_len:
            self.main_hist_len = hist
            if self.main_tui:
                self.main_tui = False            # lines scroll into history: ordinary output again
                self._reset_stitching()
            self.main_homes = 0
            return
        self.main_homes += text.count('\x1b[H')
        if not self.main_tui and self.main_homes >= 3:
            rows = [r[:2] for r in scr.rows()]
            while rows and not rows[-1][0].strip():
                rows.pop()
            if _split_footer(rows)[1]:           # an input box between rules at the bottom
                self.main_tui = True
                self._reset_stitching()
            else:
                self.main_homes = 0

    def _alt_rows(self):
        """(plain, ansi) for every row of the full-screen app; only rows that changed since
        last time are re-rendered."""
        scr = self._tui()
        lines, cols = scr.lines, scr.columns
        dirty = scr.take_dirty()
        if (self.alt_rows is None or len(self.alt_rows) != lines or self.alt_rows_width != cols
                or self.alt_rows_src is not scr or len(dirty) > lines // 2):
            self.alt_rows_src, self.alt_rows_width = scr, cols
            self.alt_rows = [r[:2] for r in scr.rows()]
        else:
            for y in dirty:
                self.alt_rows[y] = scr.rows(y, y + 1)[0][:2]
        return self.alt_rows

    # Claude Code with its log isn't stitched, but you can still scroll back in it on the
    # computer; the phone then keeps the latest screen instead of following. Claude Code says
    # so itself: "Jump to bottom (ctrl+End) ↓" above its input box for exactly as long as it's
    # scrolled back. Without that hint (another version), the recorded scroll inputs and the
    # screen's answer tell: scrolling up moves it, scrolling down that moves nothing means
    # you're at the bottom again.
    def _screen_body(self):
        return [plain for plain, _ in _split_footer(list(self._alt_rows()))[0]]

    def _scroll_input(self, direction):
        if not self.log_backed or not self._tui():
            return
        if self.scroll_probe and self.clock - self.scroll_probe[0] > _SCROLL_ANSWER:
            self._scroll_answered(False)
        if self.scroll_probe:                  # several wheel steps before the screen answers
            self.scroll_probe = (self.scroll_probe[0], self.scroll_probe[1], direction)
        else:
            self.scroll_probe = (self.clock, self._screen_body(), direction)

    def _check_scroll(self, late=False):
        """After output (or a quiet spell, `late`): settle an unanswered scroll input."""
        if not self.scroll_probe or not self._tui():
            self.scroll_probe = None
            return
        if late or self.clock - self.scroll_probe[0] > _SCROLL_ANSWER:
            self._scroll_answered(0)
            return
        before, now = self.scroll_probe[1], self._screen_body()
        # a scroll moves (nearly) every row; a spinner or timer changes one or two
        if sum(1 for a, b in zip(before, now) if a != b) + abs(len(before) - len(now)) > 3:
            self._scroll_answered(_shift(before, now))

    def _scroll_answered(self, shift):
        """shift: how the text moved (_shift): > 0 up (newer text below: scrolled down, or new
        output), < 0 down (older text above: scrolled up), None jumped, 0 didn't move."""
        direction = self.scroll_probe[2]
        self.scroll_probe = None
        if direction == 'bottom' or (direction == 'down' and shift == 0):
            self.alt_scrolled = False          # ctrl+End, or down and nothing moved: at the bottom
        elif direction in ('up', 'top') and (shift is None or shift < 0):
            self.alt_scrolled = True

    def _caught_up(self):
        """Scrolled back, is the screen at (or past) the latest one again? Yes when it shows the
        end of that screen's text (left out: the last rows, where a spinner or timer ticks)."""
        latest = [plain for plain, _ in _split_footer(self.alt_latest_rows)[0]][:-4]
        text_rows = [i for i, p in enumerate(latest) if p.strip()][-3:]
        if len(text_rows) < 3:
            return False
        tail, at = [latest[i] for i in text_rows], text_rows[0]
        now = self._screen_body()
        # where it was or higher up (new text pushed it up); scrolled back, it shows lower down
        return any([now[i + k - at] for k in text_rows if 0 <= i + k - at < len(now)] == tail
                   for i in range(0, at + 1))

    def caught_up_with(self, newest):
        """Claude Code: the screen shows the end of the newest message in the log (`newest`,
        normalised words): you're back at the bottom."""
        with self.lock:
            if not self.alt_scrolled or self.hint_seen or not newest or not self._tui():
                return
            for plain in self._screen_body():
                words = _norm_words(plain)
                if len(words) >= 20 and newest.endswith(words):
                    self.alt_scrolled = False
                    return

    def _snapshot_alt(self):
        if not self._tui():
            return
        if self.log_backed:
            rows = list(self._alt_rows())
            if _scrolled_back(rows):
                self.hint_seen = self.alt_scrolled = True
                self.scroll_probe = None
            elif self.hint_seen:
                if not self.mid_redraw:            # half a redraw may not have drawn the hint yet
                    self.alt_scrolled = False
                self.scroll_probe = None
            elif self.alt_scrolled and not self.scroll_probe and self._caught_up():
                self.alt_scrolled = False
            if not self.alt_scrolled and not self.scroll_probe:
                rows = list(self._alt_rows())
                while rows and not rows[-1][0]:
                    rows.pop()
                self.alt_latest_rows = rows
            self.last_snap = self.clock
            return
        frame = list(self._alt_rows())
        while frame and not frame[-1][0]:
            frame.pop()
        if frame and _scrolled_back(frame):
            # You're reading further up inside the app (it shows "Jump to bottom"): that's an
            # old part of the conversation, not new content. Leave the history alone; the
            # phone keeps showing the latest screen until you're back at the bottom.
            self.last_snap = self.clock
            return
        if frame:
            body, self.alt_footer = _split_footer(frame)
            # Rows pinned to the bottom (a spinner, "Auto-update failed") aren't content: like a
            # real terminal's scrollback, history only gets lines that scroll up, so leave them out
            # (they still show once, in the live screen)
            raw_body = body
            # a row counts as status once it has stayed put through a few scrolls
            for plain, _ in raw_body[len(raw_body) - _pinned_rows(self.alt_prev_raw, raw_body):]:
                m = _status_mask(plain)
                if len(m) >= 6 or m == '…(':
                    self.alt_status_seen[m] = self.alt_status_seen.pop(m, 0) + 1
            while len(self.alt_status_seen) > 200:
                self.alt_status_seen.pop(next(iter(self.alt_status_seen)))
            n = seen = 0
            while n < min(_STATUS_MAX, len(raw_body) - 1):
                plain = raw_body[-1 - n][0]
                if plain.strip():
                    if self.alt_status_seen.get(_status_mask(plain), 0) < _STATUS_CONFIRM:
                        break
                    seen = n + 1
                n += 1
            if seen:
                body = body[:len(body) - seen]
            self.alt_prev_raw = raw_body
            view = self._view_of(self.alt_footer)
            if view != self.view:
                # The app shows another view (e.g. an agent's transcript): park this history
                # and continue the other one, so the phone shows only what the computer does
                self.views[self.view] = (self.alt_frames, self.alt_pos, self.alt_prev_body)
                self.alt_frames, self.alt_pos, self.alt_prev_body = self.views.pop(view, ([], 0, []))
                self.view = view
            merged, pos = _merge_frames(self.alt_frames, body)
            # Scrolling moves a few lines at a time, so the screen shares most lines with the one
            # just before it; a view switch (a panel, and back) replaces it all at once
            seen = {plain for plain, _ in self.alt_last_seen if plain.strip()}
            shown = [plain for plain, _ in body if plain.strip()]
            continuous = bool(shown) and sum(1 for p in shown if p in seen) * 3 >= len(shown)
            self.alt_last_seen = body
            nothing_new = (bool(self.alt_frames) and pos < self.alt_pos and len(merged) == len(self.alt_frames)
                           and sum(1 for a, b in zip(merged, self.alt_frames) if a != b) <= 2)
            if self.input_marks:
                # The recording says when you scrolled: an older part after a scroll input (or
                # while still scrolled back) is scrolling; otherwise follow the screen as it is
                scrolling = nothing_new and (self.clock <= self.scroll_input_until or self.alt_scrolled)
            else:
                scrolling = nothing_new and continuous      # older recordings: judge by the screens
            if scrolling:
                # Everything on screen is already in the history, and it's an older part than
                # the latest: you're scrolling back in the app on the computer, nothing new was
                # printed. Change nothing, so the phone doesn't move; new lines resume it.
                self.alt_scrolled = True
                self.last_snap = self.clock
                return
            self.alt_scrolled = False
            self.alt_latest_rows = frame
            prev = self.alt_prev_body
            if (len(merged) == len(self.alt_frames) + len(body) and prev
                    and self.alt_frames[-len(prev):] == prev and _same_layout(prev, body)):
                # nothing lined up, but it's the same screen with new values: update in place
                pos = len(self.alt_frames) - len(prev)
                merged = self.alt_frames[:pos] + body
            elif (self.alt_frames and pos == len(self.alt_frames)
                    and _replaced_screen(self.alt_frames[self.alt_pos:], body)):
                pos = self.alt_pos
                merged = self.alt_frames[:pos] + body
            cut = max(0, len(merged) - ALT_HISTORY_MAX)
            self.alt_frames = merged[cut:]
            self.alt_pos = max(0, pos - cut)       # where the current screen sits in the history
            self.alt_prev_body = body
        self.last_snap = self.clock

    def _view_of(self, footer):
        """Which view a full-screen app shows, from its input box: Claude Code's agent view
        says "Message @agent…", its views list marks the open one with ●, and the main
        conversation has a plain prompt. Unknown (e.g. while typing): the current one."""
        text = '\n'.join(plain for plain, _ in footer)
        if not text:
            return self.view
        m = _VIEW_AGENT.search(text)
        if m:
            return m.group(1)
        if '◯' in text:
            for plain, _ in footer:
                m = _VIEW_LISTED.match(plain)
                if m:
                    return m.group(1)
        if '❯' in text and self._input_line() == '':
            return 'main'
        return self.view

    def _feed_plain(self, text):
        text = _PRIVATE_CSI.sub('', text)
        if text:
            self.revision += 1
            (self.alt or self.main).feed(text)
            if not self.alt:
                self._watch_main_redraws(text)

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
                    if self.scroll_probe and time.time() - self.last_data_at > _SCROLL_ANSWER:
                        with self.lock:
                            self._check_scroll(late=True)    # the app never redrew: nothing moved
                    return True
                self.last_data_at = time.time()
                lines = chunk.split(b'\n')
                consumed = 0
                for raw in lines[:-1]:        # last element is an unfinished line
                    parts = raw.decode('ascii', 'replace').split(' ')
                    kind = parts[0]
                    if kind == 'H' and len(parts) >= 4 and parts[2] in ('COLUMNS', 'LINES'):
                        self.size[0 if parts[2] == 'COLUMNS' else 1] = int(parts[3])
                    elif kind == 'H' and len(parts) >= 3 and parts[2] == 'ORBIT_INPUT_MARKS':
                        self.input_marks = True          # orbit-term notes scroll inputs (below)
                    elif kind == 'X' and len(parts) >= 4 and parts[2] == 'SCROLL':
                        # you scrolled the app's view (wheel, Page Up/Down, Ctrl+Home/End): what
                        # it redraws next is an older part, not new output
                        try:
                            self.clock += float(parts[1])
                        except ValueError:
                            pass
                        self.scroll_input_until = self.clock + 1.0
                        with self.lock:
                            self._scroll_input(parts[3])
                    elif kind == 'O' and len(parts) >= 3:
                        n = int(parts[2])
                        try:
                            self.clock += float(parts[1])
                        except ValueError:
                            pass
                        lf.seek(self.log_pos)
                        data = lf.read(n)
                        if len(data) < n:     # log not flushed yet: retry this entry next time
                            break
                        self.log_pos += n
                        self.mid_redraw = n >= 4095
                        with self.lock:
                            if self.main is None:
                                self.main = self._new_screen(True)
                            if self.scroll_probe and self.clock - self.scroll_probe[0] > _SCROLL_ANSWER:
                                self._check_scroll(late=True)       # this output came too late to be the answer
                            self._feed_text(self.decoder.decode(data))
                            if self.scroll_probe:
                                self._check_scroll()
                            if self._tui() and self.clock - self.last_snap >= ALT_FRAME_INTERVAL:
                                self._snapshot_alt()
                    elif kind == 'S' and len(parts) >= 5 and parts[2] == 'SIGWINCH':
                        dims = dict(p.split('=', 1) for p in parts[3:] if '=' in p)
                        rows, cols = int(dims.get('ROWS', 0)), int(dims.get('COLS', 0))
                        if rows and cols:
                            self.size = [cols, rows]
                            with self.lock:
                                for scr in (self.main, self.alt):
                                    if scr:
                                        scr.resize(lines=rows, columns=cols)
                    consumed += len(raw) + 1
                self.timing_pos += consumed
                if consumed and self._tui():
                    with self.lock:
                        self._snapshot_alt()
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
            scr = self.alt or self.main
            cx, cy = scr.cursor()
            cy = min(max(cy, 0), scr.lines - 1)
            cols = scr.columns
            cached = {}

            def cells(y):          # (x, text, dim) per cell
                if y not in cached:
                    cached[y] = scr.cells(y)
                return cached[y]

            def text_of(y):
                return ''.join(d for _, d, _ in cells(y))

            start = None
            for y in range(cy, max(-1, cy - 12), -1):
                chars = cells(y)
                line = ''.join(d for _, d, _ in chars)
                limit = len(chars) if y < cy else sum(1 for x, _, _ in chars if x < cx)
                i = _prompt_end(line, limit)
                if i >= 0:
                    start = (y, chars[i][0] + 1)
                    break
                # a shell's input only continues upward through wrapped (full-width) rows
                if not self.alt and (y == 0 or len(text_of(y - 1).rstrip()) < cols - 1):
                    break
            if not start:
                return None
            y0, x0 = start
            end = cy
            if self.alt:     # the input may continue below the cursor, up to the box's edge
                while end + 1 < scr.lines:
                    nxt = text_of(end + 1)
                    if not nxt.strip() or _RULE_LINE.match(nxt) or nxt.lstrip()[:1] in '╰└' or not nxt.startswith(' '):
                        break
                    end += 1
            parts, prev_full = [], False
            for y in range(y0, end + 1):
                row = cells(y)
                text = ''.join(d for x, d, dim in row if x >= (x0 if y == y0 else 0) and not dim)
                if not self.alt and y == cy:
                    # a right-hand prompt sits after a wide gap past the cursor: not part of the input
                    gap = re.search(r' {8,}', text[max(0, sum(1 for x, _, _ in row if x0 <= x < cx) if y == y0 else 0):])
                    if gap:
                        text = text[:len(text) - len(gap.string) + gap.start()]
                text = text.replace('\xa0', ' ').replace('\u200b', '').rstrip()
                if text.endswith('│'):
                    text = text[:-1].rstrip()
                if y > y0:
                    text = text[min(x0, len(text) - len(text.lstrip(' '))):]   # continuation indent
                    parts.append(' ' if prev_full and not text.startswith(' ') else '\n')
                prev_full = len(text_of(y).rstrip()) >= cols - 12
                parts.append(text)
            return ''.join(parts).strip('\n')

    def phone_screen(self):
        """live_screen(), but while you're reading back in the app on the computer, the latest
        screen it showed (with the live input box), so the phone doesn't scroll along."""
        rows = self.live_screen()
        with self.lock:
            if rows is not None and self.log_backed:
                hint = _scrolled_back(rows)
                if hint or (self.hint_seen and not self.mid_redraw):
                    self.hint_seen, self.alt_scrolled = True, hint
            if rows is None or not self.alt_scrolled or not self.alt_latest_rows:
                return rows
            footer = _split_footer(rows)[1]
            return _split_footer(self.alt_latest_rows)[0] + footer

    def live_screen(self):
        """(plain, ansi) rows of the full-screen app's current screen, trailing blanks dropped."""
        with self.lock:
            if not self._tui():
                return None
            rows = list(self._alt_rows())
        while rows and not rows[-1][0]:
            rows.pop()
        return rows

    def render(self, max_lines, join=False):
        """(lines, total, has_more) for the newest max_lines lines, as ANSI text. With `join`,
        rows that a long line wrapped onto are joined back into it."""
        with self.lock:
            key = (self.revision, max_lines, len(self.alt_frames), join)
            if self._render_cache[0] == key:
                return self._render_cache[1]
            result = self._render(max_lines, join)
            self._render_cache = (key, result)
            return result

    def _render(self, max_lines, join=False):
        if self.main is None:
            return [], 0, False
        if self._tui():
            # The bottom is always the app's current screen, read live; above it, what has
            # scrolled off (stitched history). If the app shows an older part, newer lines
            # stay hidden until it comes back, as on the real terminal.
            rows = self._alt_rows()
            if (self.alt_scrolled or _scrolled_back(rows)) and self.alt_latest_rows:
                rows = self.alt_latest_rows        # scrolled back on the computer: keep the latest
            end = len(rows)
            while end and not rows[end - 1][0]:
                end -= 1
            # The app's screen is a fixed-size canvas, so it has blank stretches (above the
            # input box, an empty start screen) that stitching piles up; on the phone they're
            # wasted space, so any run of blank lines shows as one.
            above = self.alt_frames[:self.alt_pos]
            known = {m for m, c in self.alt_status_seen.items() if c >= _STATUS_CONFIRM}
            if known:
                # status rows that got into the history before they were recognised as status
                above = [f for f in above if not (f[0].strip() and _status_mask(f[0]) in known)]
            frames = [ansi for _, ansi in _collapse_blank(above + rows[:end])]
            if not self.alt:
                # redrawing the normal screen: what the shell printed before it is still above
                frames = [ansi for _, ansi, _ in self.main.history_rows(2000)] + frames
            take = frames[-max_lines:]
            return take, len(frames), len(frames) > len(take)
        screen = self.alt or self.main
        width = screen.columns
        rows = screen.rows()               # (plain, ansi, wrapped-from-above)
        # drop empty rows below the cursor
        last = min(max(screen.cursor()[1], 0), len(rows) - 1)
        for y in range(len(rows) - 1, last, -1):
            if rows[y][0].strip():
                last = y
                break
        rows = rows[:last + 1]
        total = screen.history_len() + len(rows)
        take = rows[-max_lines:] if len(rows) >= max_lines else screen.history_rows(max_lines - len(rows)) + rows
        if not join:
            lines = [ansi for _, ansi, _ in take]
            return lines, total, total > len(lines)
        out, last_row_len = [], 0
        for plain, ansi, cont in take:
            if out and cont:
                # the row above filled the width and continued here: its trailing blanks
                # were part of the text (a space between words), so put them back
                out[-1] += ' ' * max(0, width - last_row_len) + ansi
            else:
                out.append(ansi)
            last_row_len = len(plain)
        return out, total, total > len(take)


_PROMPT_MARKS = ('❯', '➜', '›', '»', '$', '%', '#', '>')
_VIEW_AGENT = re.compile(r'Message @([\w.-]+)')
_VIEW_LISTED = re.compile(r'^\s*(?:❯\s*)?●\s+([\w.-]+)')


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
    if (pyte is None and _VT is None) or not pid:
        return None
    base = os.path.join(TERM_REC_DIR, str(pid))
    if not os.path.exists(base + '.timing'):
        return None
    with TERM_RECORDINGS_LOCK:
        rec = TERM_RECORDINGS.get(pid)
        if rec is None:
            rec = TERM_RECORDINGS[pid] = TermRecording(base)
            # Claude Code shows its own log: no need to stitch its screens on the first replay
            rec.log_backed = bool(_claude_pid_under(pid))
    rec.update()
    return rec

# ---- Claude Code: its history from its own log ----
# Claude Code's fullscreen mode only ever draws one screen and keeps the conversation in its
# own memory, so the terminal never holds the older part. It writes every message to
# ~/.claude/projects/<dir>/<session>.jsonl, and ~/.claude/sessions/<pid>.json says which
# session a running claude has. For a tab running Claude Code the phone shows that log and,
# below it, the part of the screen that isn't in the log yet (a reply being written, a
# question, the input box, a dashboard): no text that isn't in the log or on the screen now.
CLAUDE_DIR = os.path.expanduser('~/.claude')
_CLAUDE_PROC_CACHE = {}       # recorder pid -> (checked at, claude pid or None)
_CLAUDE_LOGS = {}             # log path -> ClaudeLog
_CLAUDE_LOGS_LOCK = threading.Lock()

# Claude Code's own colours, so the phone looks like the computer
_C_USER = '\x1b[0;38;2;153;153;153;48;2;55;55;55m'
_C_USER_TEXT = '\x1b[0;38;2;255;255;255;48;2;55;55;55m'
_C_DIM = '\x1b[0;38;2;153;153;153m'
_C_RESET = '\x1b[0m'
_MD_BOLD = re.compile(r'\*\*(.+?)\*\*')


def _claude_pid_under(recorder_pid):
    """The claude process running in this terminal (below its recorder), or None."""
    now = time.time()
    hit = _CLAUDE_PROC_CACHE.get(recorder_pid)
    if hit and now - hit[0] < 3:
        return hit[1]
    children = collections.defaultdict(list)
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/stat') as f:
                stat = f.read()
            ppid = int(stat[stat.rindex(')') + 2:].split()[1])
        except (OSError, ValueError):
            continue
        children[ppid].append(int(name))
    found, todo = None, list(children.get(recorder_pid, []))
    while todo:
        pid = todo.pop()
        if os.path.exists(os.path.join(CLAUDE_DIR, 'sessions', f'{pid}.json')):
            found = max(found or 0, pid)
        todo += children.get(pid, [])
    _CLAUDE_PROC_CACHE[recorder_pid] = (now, found)
    return found


def _claude_log_path(claude_pid):
    try:
        with open(os.path.join(CLAUDE_DIR, 'sessions', f'{claude_pid}.json')) as f:
            session = json.load(f).get('sessionId', '')
    except (OSError, ValueError, AttributeError):
        return None
    if not re.fullmatch(r'[0-9a-f-]{36}', session or ''):
        return None
    projects = os.path.join(CLAUDE_DIR, 'projects')
    try:
        for d in os.listdir(projects):
            path = os.path.join(projects, d, session + '.jsonl')
            if os.path.exists(path):
                return path
    except OSError:
        pass
    return None


_MD_CODE = re.compile(r'`([^`]+)`')
_MD_LIST = re.compile(r'^(\s*)([-*]|\d+[.)])\s+')


def _wrap_words(text, width, first, rest):
    """Word-wrap like Claude Code does (at spaces, long words split), with a first-line and a
    continuation prefix; returns plain rows."""
    import textwrap
    return textwrap.wrap(text, width=width, initial_indent=first, subsequent_indent=rest,
                         break_on_hyphens=False, drop_whitespace=True, replace_whitespace=False) or [first.rstrip()]


def _claude_item_lines(kind, text, width=None):
    """One message as ANSI lines, laid out as Claude Code draws it: wrapped at the screen's
    width (unwrapped when it isn't known), with its markdown shown, not its ** and `."""
    rows = text.split('\n')
    if kind == 'user':
        out = []
        for n, r in enumerate(rows):
            lead = '❯ ' if n == 0 else '  '
            wrapped = _wrap_words(r, width, lead, '  ') if width else [lead + r]
            for w in wrapped:
                if w.startswith('❯ '):
                    out.append(f'{_C_USER}❯ {_C_USER_TEXT}{w[2:]} {_C_RESET}')
                else:
                    out.append(f'{_C_USER_TEXT}{w} {_C_RESET}')
        return out
    if kind == 'tool':
        return [f'{_C_DIM}● {text}{_C_RESET}']
    if kind != 'text':
        return [f'{_C_DIM}──── {text} ────{_C_RESET}']
    out, code = [], False
    for n, r in enumerate(rows):
        lead = '● ' if n == 0 else '  '
        if r.strip().startswith('```'):
            code = not code
            continue
        if code or not width:
            out.append(f'{_C_RESET}{lead}{_claude_md(r)}{_C_RESET}' if r or n == 0 else '')
            continue
        if not r.strip():
            out.append('')
            continue
        bold = [b for b in _MD_BOLD.findall(r)]
        plain = _MD_CODE.sub(r'\1', _MD_BOLD.sub(r'\1', r))
        heading = re.match(r'^#{1,6}\s+', plain)
        if heading:
            plain = plain[heading.end():]
            bold = [plain]
        m = _MD_LIST.match(plain)
        if m:
            # "- item" sits two in, and its wrapped lines line up under the item's text
            marker = m.group(2)
            indent = '  ' + m.group(1)
            body = plain[m.end():]
            first = (lead if n == 0 else indent) + marker + ' '
            wrapped = _wrap_words(body, width, first, ' ' * len(indent + marker + ' '))
        else:
            wrapped = _wrap_words(plain, width, lead, '  ')
        for w in wrapped:
            for b in bold:
                b = _MD_CODE.sub(r'\1', b)
                if b and b in w:
                    w = w.replace(b, f'\x1b[1m{b}\x1b[22m', 1)
            out.append(f'{_C_RESET}{w}{_C_RESET}')
    return out


def _claude_md(line):
    return _MD_BOLD.sub('\x1b[1m\\1\x1b[22m', line)


def _tool_summary(name, inp):
    """One line for a tool call, like Claude Code's own "● Bash(…)"."""
    if not isinstance(inp, dict):
        inp = {}
    arg = ''
    if name == 'Bash':
        arg = inp.get('description') or (inp.get('command') or '').strip().split('\n')[0]
    elif name in ('Read', 'Write', 'Edit', 'NotebookEdit'):
        arg = (inp.get('file_path') or inp.get('notebook_path') or '').replace(os.path.expanduser('~'), '~')
    elif name in ('Grep', 'Glob'):
        arg = inp.get('pattern') or ''
    elif name in ('Agent', 'Task'):
        arg = inp.get('description') or ''
    elif name == 'WebFetch':
        arg = inp.get('url') or ''
    elif name == 'WebSearch':
        arg = inp.get('query') or ''
    elif name == 'Skill':
        arg = inp.get('skill') or ''
    arg = ' '.join(str(arg).split())
    if len(arg) > 160:
        arg = arg[:159] + '…'
    return f'{name}({arg})' if arg else name


def _user_text(content):
    """What you typed, from a user entry, or None for entries that aren't a typed message."""
    if isinstance(content, list):
        content = '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
    if not isinstance(content, str) or not content.strip():
        return None
    s = content.strip()
    if s.startswith('<'):
        m = re.match(r'<command-name>(.*?)</command-name>(?:.*?<command-args>(.*?)</command-args>)?', s, re.S)
        if m:
            return (m.group(1) + ' ' + (m.group(2) or '')).strip()
        m = re.match(r'<bash-input>(.*?)</bash-input>', s, re.S)
        if m:
            return '! ' + m.group(1).strip()
        if s.startswith('<pasted_content'):
            return s
        return None                  # command output, task notices, reminders: not typed
    return s


def _norm_words(s):
    return ' '.join(re.sub(r'[^\w]+', ' ', s.lower()).split())


class ClaudeLog:
    """Reads one Claude Code conversation log as it grows, keeping what the phone shows:
    your messages, Claude's replies, and one line per tool call."""

    def __init__(self, path):
        self.path, self.pos, self.carry = path, 0, b''
        self.items = []              # (kind, text): 'user', 'text', 'tool', 'note'
        self.version = 0
        self._lines = self._recent = (None, None)      # (version, value) caches
        self.lock = threading.Lock()

    def update(self):
        with self.lock:
            try:
                with open(self.path, 'rb') as f:
                    f.seek(self.pos)
                    data = f.read()
            except OSError:
                return
            if not data:
                return
            self.pos += len(data)
            lines = (self.carry + data).split(b'\n')
            self.carry = lines.pop()
            for raw in lines:
                self._add(raw)
            self.version += 1

    def _add(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict) or d.get('isSidechain'):
            return
        kind = d.get('type')
        msg = d.get('message') if isinstance(d.get('message'), dict) else {}
        if kind == 'user' and not d.get('isMeta') and not d.get('isCompactSummary'):
            text = _user_text(msg.get('content'))
            if text and text.startswith('[Request interrupted'):
                self.items.append(('note', 'Interrupted'))     # Claude Code's marker, not typed
            elif text:
                self.items.append(('user', text))
        elif kind == 'attachment':
            a = d.get('attachment') or {}
            # a message you sent while Claude was working
            if a.get('type') == 'queued_command' and (a.get('origin') or {}).get('kind') == 'human':
                text = _user_text(a.get('prompt'))
                if text:
                    self.items.append(('user', text))
        elif kind == 'assistant':
            if d.get('isApiErrorMessage') or msg.get('model') == '<synthetic>':
                return       # "You've hit your limit" and the like: notes on screen, not replies
            for block in msg.get('content') or []:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and block.get('text', '').strip():
                    self.items.append(('text', block['text'].strip()))
                elif block.get('type') == 'tool_use':
                    self.items.append(('tool', _tool_summary(block.get('name', ''), block.get('input'))))
        elif kind == 'system' and d.get('subtype') == 'compact_boundary':
            self.items.append(('note', 'Conversation compacted'))

    def lines_upto(self, n, width=None):
        """The first n messages as ANSI lines, wrapped at `width` like the screen (cached)."""
        with self.lock:
            key = (self.version, n, width)
            if self._lines[0] == key:
                return list(self._lines[1])
            out = []
            for kind, text in self.items[:n]:
                if out:
                    out.append('')
                out += _claude_item_lines(kind, text, width)
            self._lines = (key, out)
            return list(out)


def claude_log_for(recorder_pid):
    """The conversation log of the Claude Code running in this terminal, read up to now."""
    cpid = _claude_pid_under(recorder_pid)
    path = _claude_log_path(cpid) if cpid else None
    if not path:
        return None
    with _CLAUDE_LOGS_LOCK:
        log = _CLAUDE_LOGS.get(path)
        if log is None:
            if len(_CLAUDE_LOGS) > 8:
                _CLAUDE_LOGS.pop(next(iter(_CLAUDE_LOGS)))
            log = _CLAUDE_LOGS[path] = ClaudeLog(path)
    log.update()
    return log


def claude_reply_lines(rec, log):
    """The current screen exactly as it is, and above it the earlier messages from the log:
    every message from the log up to the first one the screen shows. If the screen starts
    partway into a message, that message comes from the log too, so none of it is lost."""
    newest = next((t for k, t in reversed(log.items) if k in ('user', 'text')), None)
    rec.caught_up_with(_norm_words(newest) if newest else None)
    rows = rec.phone_screen()
    if rows is None:
        return None
    items = log.items
    if not items:
        return None
    # the earliest logged message the screen shows (searching the latest ones)
    first, first_row = None, None
    lo = max(0, len(items) - 60)
    norms = [(i, _norm_words(t)) for i, (k, t) in enumerate(items) if i >= lo and k in ('user', 'text')]
    for r, (plain, _) in enumerate(rows):
        words = _norm_words(plain)
        if len(words) < 12:
            continue
        hit = next((i for i, w in norms if words in w), None)
        if hit is not None and (first is None or hit < first):
            first, first_row = hit, r
    if first is None:
        upto = len(items)                     # nothing logged on screen (a dashboard, a menu)
    else:
        # does the screen show that message from its start ("❯ " or "● ", then indented
        # lines)? If it begins above the screen, take it from the log too
        r = first_row
        while r >= 0 and not rows[r][0].startswith(('❯', '●')) and (rows[r][0].startswith(' ') or not rows[r][0].strip()):
            r -= 1
        upto = first if r >= 0 and rows[r][0].startswith(('❯', '●')) else first + 1
    # Claude Code wraps its text one column short of the terminal's width
    width = (rec.size[0] - 1) if rec.size[0] and rec.size[0] > 20 else None
    lines = log.lines_upto(upto, width)
    # the screen's top may be the tail of the last message above, already shown in full
    if upto and items[upto - 1][0] in ('user', 'text'):
        above = _norm_words(items[upto - 1][1])
        start = 0
        while start < len(rows) and not rows[start][0].startswith(('❯', '●')) \
                and (not rows[start][0].strip() or _norm_words(rows[start][0]) in above):
            start += 1
        rows = rows[start:]
    if lines:
        lines.append('')
    return lines + [ansi for _, ansi in rows]

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
# applet lists "Orbit: Kept awake from your phone", once: one request for both sleep and
# screen dimming (1 | 4). The power manager may not be running yet (at login this service
# starts first) or may restart and forget every block, so the helper asks again whenever it
# (re)appears, and checks every few seconds until a request holds. Without KDE's power
# manager, the freedesktop calls. The block lasts as long as this process lives.
_KEEP_AWAKE_HELPER = """
import sys, time
from PyQt6.QtCore import QCoreApplication, QMetaType, QTimer
from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusArgument, QDBusServiceWatcher
WHO, WHY = "Orbit", "Kept awake from your phone"
KDE = "org.kde.Solid.PowerManagement"
FD = (("org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit",
       "org.freedesktop.PowerManagement.Inhibit"),
      ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver"))
app = QCoreApplication(sys.argv)
bus = QDBusConnection.sessionBus()
started = time.monotonic()
held = [False]

def hold():
    if held[0]:
        return
    if bus.interface().isServiceRegistered(KDE).value():
        kde = QDBusInterface(KDE, "/org/kde/Solid/PowerManagement/PolicyAgent", KDE + ".PolicyAgent", bus)
        reply = kde.call("AddInhibition", QDBusArgument(1 | 4, QMetaType.Type.UInt.value), WHO, WHY)
        held[0] = not reply.errorMessage()
    elif time.monotonic() - started > 60 and bus.interface().isServiceRegistered(FD[0][0]).value():
        # no KDE power manager after a minute: another desktop
        for service, path, iface in FD:
            QDBusInterface(service, path, iface, bus).call("Inhibit", WHO, WHY)
        held[0] = True

def gone(name):
    held[0] = False            # a restarted power manager has forgotten every block

watcher = QDBusServiceWatcher(KDE, bus, QDBusServiceWatcher.WatchModeFlag.WatchForRegistration
                              | QDBusServiceWatcher.WatchModeFlag.WatchForUnregistration)
watcher.serviceRegistered.connect(lambda name: QTimer.singleShot(1000, hold))
watcher.serviceUnregistered.connect(gone)
timer = QTimer()
timer.timeout.connect(hold)
timer.start(5000)
hold()
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
    start = max(0, max_lines - height)
    cap = _tmux('capture-pane', '-p', '-e', '-J', '-S', f'-{start}', '-t', target)
    if not cap or cap.returncode != 0:
        return None
    lines = cap.stdout.split('\n')
    while lines and not re.sub(r'\x1b\[[0-9;:]*m', '', lines[-1]).strip():
        lines.pop()
    # history_size counts wrapped rows while -J joins them, so only report a
    # larger total when there really is unread history above
    total = hist + height if start < hist else len(lines)
    return {
        'text': '\n'.join(lines),
        'total_lines': total,
        'returned_lines': len(lines),
        'has_more': start < hist,
        'ansi': True,
    }

def write_tmux_session(session, text):
    data = text.encode('utf-8')
    for i in range(0, len(data), 512):
        chunk = data[i:i + 512]
        res = _tmux('send-keys', '-t', f'={session}:', '-H', *[f'{b:02x}' for b in chunk])
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
    if rec._tui() is not None:
        pid = _recorder_pid(term_id)
        log = claude_log_for(pid) if pid else None
        rec.log_backed = log is not None          # its screens needn't be stitched
        lines = claude_reply_lines(rec, log) if log else None
        if lines:
            take = lines[-max_lines:]
            return {'status': 'ok', 'id': term_id, 'title': title, 'text': '\n'.join(take),
                    'total_lines': len(lines), 'returned_lines': len(take), 'has_more': len(lines) > len(take),
                    'ansi': True, 'columns': rec.size[0] or None, 'lines_independent': True, 'fullscreen': True}
    lines, total, more = rec.render(max_lines, join)
    if not total:
        return None
    # columns: the terminal's width, so the phone can lay tables out the same way
    return {'status': 'ok', 'id': term_id, 'title': title, 'text': '\n'.join(lines),
            'total_lines': total, 'returned_lines': len(lines), 'has_more': more, 'ansi': True,
            'columns': rec.size[0] or None, 'lines_independent': True,
            'fullscreen': rec._tui() is not None}   # an app like Claude Code: its bottom is the screen's bottom

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
            return {'status': 'ok', 'id': term_id, 'title': session, **res}
        return {'status': 'error', 'message': 'tmux session not found'}
    if ':' in term_id and 'org.kde.konsole' in term_id:
        svc, sid = term_id.split(':', 1)
        spath = f'/Sessions/{sid}'
        title, sh = konsole_session_info(svc, sid)

        # Recorded tab: full scrollback from its `script` recording
        rec = recording_for(int(sh)) if sh.isdigit() else None
        if rec is not None:
            reply = _recorded_reply(term_id, title, rec, max_lines, join)
            if reply:
                return reply

        # Konsole tab running tmux: read exact scrollback from tmux
        session = tmux_client_sessions().get(konsole_session_tty(svc, sid))
        if session:
            res = read_tmux_session(session, max_lines)
            if res:
                pane = next((p for p in tmux_list_sessions() if p['name'] == session), None)
                if pane and pane['command']:
                    title = f"{os.path.basename(pane['cwd'].rstrip('/')) or '/'} : {pane['command']}"
                return {'status': 'ok', 'id': term_id, 'title': title, **res}

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
    if rec is not None and rec.alt is None:
        # a shell's line editor takes a burst of backspaces fine
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

# Phone notifications on the desktop, with Reply (KDE's inline reply) and Mark as read. A small
# helper talks to the desktop's notification service over D-Bus (notify-send can't receive a
# typed reply): commands in on stdin, what you did out on stdout, one JSON object per line.
_NOTIFY_HELPER = r"""
import sys, json
from PyQt6.QtCore import QCoreApplication, QMetaType, QObject, QSocketNotifier, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusArgument, QDBusMessage
app = QCoreApplication(sys.argv)
bus = QDBusConnection.sessionBus()
SVC, PATH = "org.freedesktop.Notifications", "/org/freedesktop/Notifications"
notes = QDBusInterface(SVC, PATH, SVC, bus)
ids, keys = {}, {}

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

def show(c):
    actions = []
    if c.get("reply"):
        actions += ["inline-reply", "Reply"]
    if c.get("mark_read"):
        actions += ["mark_read", "Mark as read"]
    hints = {"urgency": QDBusArgument(int(c.get("urgency", 1)), QMetaType.Type.UChar.value),
             "x-kde-display-appname": c.get("app", "Phone")}
    if c.get("reply"):
        hints["x-kde-reply-placeholder-text"] = "Reply to " + (c.get("title") or c.get("app") or "")
        hints["x-kde-reply-submit-button-text"] = "Send"
    old = ids.get(c["key"], 0)
    r = notes.call("Notify", c.get("app", "Phone"), QDBusArgument(old, QMetaType.Type.UInt.value),
                   c.get("icon", "smartphone"), c.get("title", ""), c.get("text", ""),
                   QDBusArgument(actions, QMetaType.Type.QStringList.value), hints, -1 if c.get("urgency", 1) < 2 else 0)
    if r.arguments():
        nid = int(r.arguments()[0])
        ids[c["key"]] = nid
        keys[nid] = c["key"]

def close(c):
    nid = ids.pop(c.get("key"), None)
    if nid:
        keys.pop(nid, None)
        notes.call("CloseNotification", QDBusArgument(nid, QMetaType.Type.UInt.value))

class Signals(QObject):
    @pyqtSlot(QDBusMessage)
    def replied(self, m):
        nid, text = m.arguments()
        if int(nid) in keys:
            out({"event": "reply", "key": keys[int(nid)], "text": text})
    @pyqtSlot(QDBusMessage)
    def action(self, m):
        nid, act = m.arguments()
        if int(nid) in keys and act != "inline-reply":
            out({"event": "action", "key": keys[int(nid)], "action": act})
    @pyqtSlot(QDBusMessage)
    def closed(self, m):
        nid = int(m.arguments()[0])
        key = keys.pop(nid, None)
        if key:
            ids.pop(key, None)

sig = Signals()
bus.connect(SVC, PATH, SVC, "NotificationReplied", sig.replied)
bus.connect(SVC, PATH, SVC, "ActionInvoked", sig.action)
bus.connect(SVC, PATH, SVC, "NotificationClosed", sig.closed)

buf = b""
def readable():
    global buf
    import os
    data = os.read(0, 65536)
    if not data:
        app.quit(); return
    buf += data
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        try:
            c = json.loads(line)
            (show if c.get("cmd") == "show" else close)(c)
        except Exception as e:
            sys.stderr.write("notify helper: %s\n" % e)
watch = QSocketNotifier(0, QSocketNotifier.Type.Read)
watch.activated.connect(readable)
app.exec()
"""

_PHONE_NOTE_TIMES = collections.deque(maxlen=30)

def _markup_safe(text, limit):
    text = str(text or '')[:limit]
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

class PhoneNotes:
    """Runs the notification helper and passes what you do with a phone notification on the
    desktop (a reply, Mark as read) back to the phone over the live link."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def _helper(self):
        if self.proc and self.proc.poll() is None:
            return self.proc
        try:
            import importlib.util
            if not importlib.util.find_spec('PyQt6.QtDBus'):
                return None
        except (ImportError, ValueError):
            return None
        self.proc = subprocess.Popen([sys.executable, '-c', _NOTIFY_HELPER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        threading.Thread(target=self._listen, args=(self.proc,), daemon=True, name='phone-notes').start()
        return self.proc

    def _listen(self, proc):
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get('event') == 'reply' and ev.get('text'):
                auth_mgr.broadcast_ndjson(json.dumps({"event": "notification_reply", "key": ev['key'],
                                                      "text": str(ev['text'])[:2000]}) + "\n")
            elif ev.get('event') == 'action' and ev.get('action') == 'mark_read':
                auth_mgr.broadcast_ndjson(json.dumps({"event": "notification_action", "key": ev['key'],
                                                      "action": "mark_read"}) + "\n")

    def send(self, cmd):
        with self.lock:
            proc = self._helper()
            if not proc:
                return False
            try:
                proc.stdin.write((json.dumps(cmd) + "\n").encode())
                return True
            except OSError:
                self.proc = None
                return False

PHONE_NOTES = PhoneNotes()

def show_phone_notification(note):
    """A notification from the phone, as a desktop notification: a message (with Reply and Mark
    as read when the phone's app offers them), or who is calling (stays until the call ends).
    `removed` closes the one shown for that key."""
    key = str(note.get('key', ''))[:300]
    try:
        late = time.time() - float(note.get('posted_at', 0)) / 1000
        if note.get('posted_at') and late > 2:
            sys.stderr.write(f"Phone notification arrived {late:.0f} s after the phone got it "
                             f"({str(note.get('app', ''))[:30]})\n")
    except (TypeError, ValueError):
        pass
    if note.get('removed'):
        PHONE_NOTES.send({"cmd": "close", "key": key})
        return {"status": "ok"}
    now = time.time()
    if len(_PHONE_NOTE_TIMES) == _PHONE_NOTE_TIMES.maxlen and now - _PHONE_NOTE_TIMES[0] < 60:
        return {"status": "error", "message": "Too many notifications"}      # 30 a minute at most
    _PHONE_NOTE_TIMES.append(now)
    app = str(note.get('app') or 'Phone')[:60]
    title = _markup_safe(note.get('title'), 200)
    text = _markup_safe(note.get('text'), 1000)
    call = note.get('category') == 'call'
    shown = PHONE_NOTES.send({"cmd": "show", "key": key, "app": f"{app} (phone)", "title": title or app,
                              "text": text, "icon": 'call-start' if call else 'smartphone',
                              "urgency": 2 if call else 1, "reply": bool(note.get('can_reply')) and not call,
                              "mark_read": bool(note.get('can_mark_read')) and not call})
    if not shown:
        # no helper (PyQt6 missing): a plain notification, without Reply
        try:
            subprocess.run(['notify-send', '-a', f'{app} (phone)', '-i', 'call-start' if call else 'smartphone']
                           + (['-u', 'critical'] if call else []) + [title or app, text],
                           capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return {"status": "error", "message": "No notification service (nobody logged in?)"}
    return {"status": "ok"}

# ---- Orbit Cam: the phone's camera as this computer's webcam ----
# The camera has one format while apps use it, set by its mode (config.json orbit_cam_format,
# switched from the tray):
#   sharp       MJPEG 1920x1080: what real webcams send at 1080p. Each frame is a ~100 KB JPEG
#               made by the GPU instead of a 3 MB raw frame, so moving frames costs little
#   raw         NV12 1280x720: uncompressed, what the GPU decoder makes
#   compatible  I420 1280x720: for an older app that takes nothing else
WEBCAM_MODES = {'sharp': ('mjpeg', (1920, 1080)), 'raw': ('nv12', (1280, 720)),
                'compatible': ('yuv420p', (1280, 720))}

def _mode_from_config():
    m = str(load_config().get('orbit_cam_format', '')).lower()
    return {'i420': 'compatible', 'yuv420p': 'compatible', 'nv12': 'raw', 'mjpeg': 'sharp'}.get(m, m if m in WEBCAM_MODES else 'sharp')

WEBCAM_MODE = _mode_from_config()
STANDBY_COUNT, STANDBY_FPS = 10, 4

def _cam_format():
    return WEBCAM_MODES[WEBCAM_MODE]

def webcam_device():
    """A virtual camera to write the phone's video into (v4l2loopback), or None. One named
    Orbit Cam is preferred; any other v4l2loopback camera works the same way."""
    found = []
    base = '/sys/devices/virtual/video4linux'
    try:
        for name in sorted(os.listdir(base)):
            try:
                with open(os.path.join(base, name, 'name')) as f:
                    label = f.read().strip()
            except OSError:
                label = ''
            dev = '/dev/' + name
            if os.access(dev, os.W_OK):
                found.append(('orbit' not in label.lower(), dev, label))
    except OSError:
        return None
    return sorted(found)[0][1:] if found else None


def _standby_frames():
    """What the camera shows while the phone isn't streaming: soft blurred colour with a
    moving film grain and the name. A few frames, made once per mode and then looped, so
    showing it costs next to nothing. A list of frames (bytes), or None."""
    pix, (w, h) = _cam_format()
    base = os.path.expanduser(f'~/.cache/orbit/orbit-cam-standby-{WEBCAM_MODE}')
    frame_size = w * h * 3 // 2
    if not os.path.exists(base + ('.0' if pix == 'mjpeg' else '.raw')):
        os.makedirs(os.path.dirname(base), exist_ok=True)
        bold, regular = '/usr/share/fonts/noto/NotoSans-Bold.ttf', '/usr/share/fonts/noto/NotoSans-Regular.ttf'
        fb = f":fontfile={bold}" if os.path.exists(bold) else ''
        fr = f":fontfile={regular}" if os.path.exists(regular) else ''
        k = w / 1280                            # the text scales with the frame
        src = (f"gradients=s={w}x{h}:c0=0x1b1446:c1=0x0b3a4a:c2=0x3a1240:c3=0x0b0d12:nb_colors=4"
               f":x0={int(200*k)}:y0={int(120*k)}:x1={int(1100*k)}:y1={int(650*k)}:type=radial:speed=0")
        vf = (f"boxblur={int(40*k)}:2,noise=alls=22:allf=t+u,"
              f"drawtext=text='Orbit Cam'{fb}:fontsize={int(56*k)}:fontcolor=0xf2f3f7:x=(w-tw)/2:y=h/2-{int(60*k)},"
              f"drawtext=text='Off for now · start it from Orbit on your phone'{fr}:fontsize={int(24*k)}"
              f":fontcolor=0xb8bccb:x=(w-tw)/2:y=h/2+{int(18*k)}")
        out = ['-pix_fmt', 'yuvj420p', '-c:v', 'mjpeg', '-q:v', '4', '-f', 'image2', base + '.%d'] if pix == 'mjpeg' else \
              ['-pix_fmt', pix, '-f', 'rawvideo', base + '.raw']
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', src,
                        '-frames:v', str(STANDBY_COUNT), '-vf', vf, '-start_number', '0'] + out,
                       capture_output=True, timeout=90)
    frames = []
    try:
        if pix == 'mjpeg':
            for n in range(STANDBY_COUNT):
                with open(f'{base}.{n}', 'rb') as f:
                    frames.append(f.read())
        else:
            with open(base + '.raw', 'rb') as f:
                data = f.read()
            frames = [data[k:k + frame_size] for k in range(0, len(data) - frame_size + 1, frame_size)]
    except OSError:
        pass
    return frames or None

def _request_chunks(handler):
    """The request body as it arrives (chunked, or up to Content-Length)."""
    rfile = handler.rfile
    if 'chunked' in handler.headers.get('Transfer-Encoding', '').lower():
        while True:
            line = rfile.readline(64)
            if not line:
                return
            size = int(line.split(b';')[0].strip() or b'0', 16)
            if size == 0:
                rfile.readline(64)
                return
            data = rfile.read(size)
            rfile.readline(64)                 # the CRLF after each chunk
            if not data:
                return
            yield data
    else:
        left = int(handler.headers.get('Content-Length', '0') or 0)
        while left > 0:
            data = rfile.read(min(65536, left))
            if not data:
                return
            left -= len(data)
            yield data

_VAAPI = {'checked': False, 'device': None}

def _vaapi_device():
    """A GPU that decodes H.264 itself (VA-API), checked once with a tiny test stream, or None.
    Decoding, turning and resizing 720p on the CPU takes most of a core; on the GPU about a
    fifth of that."""
    if _VAAPI['checked']:
        return _VAAPI['device']
    _VAAPI['checked'] = True
    nodes = []
    try:
        for name in sorted(os.listdir('/sys/class/drm')):
            if name.startswith('renderD'):
                try:
                    with open(f'/sys/class/drm/{name}/device/vendor') as f:
                        vendor = f.read().strip()
                except OSError:
                    vendor = ''
                if vendor in ('0x8086', '0x1002'):          # Intel, AMD: VA-API drivers
                    nodes.append('/dev/dri/' + name)
    except OSError:
        return None
    for node in nodes:
        try:
            test = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                                   'testsrc2=size=320x240:rate=10', '-t', '1', '-c:v', 'libx264', '-f', 'h264', '-'],
                                  capture_output=True, timeout=20).stdout
            r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-hwaccel', 'vaapi',
                                '-hwaccel_device', node, '-hwaccel_output_format', 'vaapi', '-f', 'h264', '-i', 'pipe:0',
                                '-vf', 'transpose_vaapi=dir=clock,scale_vaapi=w=640:h=360:force_original_aspect_ratio=decrease,'
                                       'hwdownload,format=nv12', '-f', 'null', '-'],
                               input=test, capture_output=True, timeout=20)
            if r.returncode == 0 and test:
                _VAAPI['device'] = node
                sys.stderr.write(f"Orbit Cam: decoding on the GPU ({node})\n")
                return node
        except (OSError, subprocess.SubprocessError):
            continue
    sys.stderr.write("Orbit Cam: no GPU decoding, using the CPU\n")
    return None


class OrbitCam:
    """The virtual camera, fed by one writer that never lets go of it: closing and reopening
    the device between pictures fails ("Invalid argument") and makes apps lose the camera.
    It shows the standby picture, or the phone's video while the phone streams."""

    def __init__(self):
        self.lock = threading.Lock()          # one frame (or piece of stream) at a time
        self.sink = None
        self.live = False                      # the phone's video is showing
        self.streaming = threading.Lock()      # one phone stream at a time

    def _sink_ready(self):
        if self.sink and self.sink.poll() is None:
            return True
        dev = webcam_device()
        if not dev:
            return False
        pix, (w, h) = _cam_format()
        if pix == 'mjpeg':
            # JPEG frames, copied in as they are (no decoding here)
            # no look-ahead: probing would queue frames for good, a lag that never drains
            inp = ['-probesize', '32', '-analyzeduration', '0', '-fflags', 'nobuffer', '-flags', 'low_delay',
                   '-f', 'mjpeg', '-i', 'pipe:0', '-c:v', 'copy']
        else:
            inp = ['-f', 'rawvideo', '-pix_fmt', pix, '-video_size', f'{w}x{h}', '-framerate', '30',
                   '-i', 'pipe:0', '-c:v', 'rawvideo']
        self.sink = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error'] + inp + ['-f', 'v4l2', dev[0]],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     bufsize=0)
        return True

    def _write_all(self, data):
        view = memoryview(data)
        while view:
            n = self.sink.stdin.write(view)
            view = view[n:]

    def write(self, frame, standby=False):
        with self.lock:
            if standby and self.live:          # the phone's video took over meanwhile
                return False
            if not self._sink_ready():
                return False
            try:
                self._write_all(frame)
                return True
            except (OSError, ValueError):
                self.sink = None               # it died: a new one next frame
                return False

    def pass_through(self, src_fd):
        """Move the decoder's output into the camera without copying it through Python
        (splice): raw frames a whole frame at a time, JPEGs as they come. False at the end."""
        pix, (w, h) = _cam_format()
        with self.lock:
            if not self._sink_ready():
                return False
            dst = self.sink.stdin.fileno()
            try:
                if pix == 'mjpeg':
                    return os.splice(src_fd, dst, 1 << 20) > 0
                left = w * h * 3 // 2
                while left:
                    n = os.splice(src_fd, dst, left)
                    if n == 0:
                        return False
                    left -= n
                return True
            except OSError:
                self.sink = None
                return False

    def users(self):
        """Programs that have the camera open now (not our own writer)."""
        dev = webcam_device()
        if not dev:
            return []
        mine = self.sink.pid if self.sink and self.sink.poll() is None else None
        names = []
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or int(pid) in (mine, os.getpid()):
                continue
            try:
                for fd in os.listdir(f'/proc/{pid}/fd'):
                    if os.readlink(f'/proc/{pid}/fd/{fd}') == dev[0]:
                        with open(f'/proc/{pid}/comm') as f:
                            names.append(f.read().strip())
                        break
            except OSError:
                continue
        return sorted(set(names))

    def switch_mode(self, mode):
        """Sharp / raw / compatible. A camera's format can't change while an app has it open,
        so this refuses then and says which apps."""
        global WEBCAM_MODE
        if mode not in WEBCAM_MODES:
            return {"status": "error", "message": "Unknown mode"}
        if mode == WEBCAM_MODE:
            return {"status": "ok", "mode": mode}
        if self.live:
            return {"status": "error", "message": "Stop Orbit Cam on the phone first."}
        busy = self.users()
        if busy:
            return {"status": "error", "message": "Close " + ", ".join(busy) + " first: the camera is in use."}
        with self.lock:
            WEBCAM_MODE = mode
            if self.sink and self.sink.poll() is None:
                self.sink.terminate()
                try:
                    self.sink.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.sink.kill()
            self.sink = None                   # the next frame starts it in the new format
        cfg = load_config()
        cfg['orbit_cam_format'] = mode
        save_config(cfg)
        return {"status": "ok", "mode": mode}

    def standby(self):
        """While the phone isn't streaming: the standby frames, STANDBY_FPS a second."""
        frames, n, mode = [], 0, WEBCAM_MODE
        while True:
            try:
                if mode != WEBCAM_MODE:        # switched: that mode's own standby frames
                    frames, mode = [], WEBCAM_MODE
                if not frames and webcam_device():
                    frames = _standby_frames() or []
                if frames and not self.live:
                    if self.write(frames[n % len(frames)], standby=True):
                        n += 1
            except Exception as e:
                sys.stderr.write(f"Orbit Cam standby: {e}\n")
            time.sleep(1 / STANDBY_FPS if frames else 15)

ORBIT_CAM = OrbitCam()

def start_webcam_standby():
    threading.Thread(target=ORBIT_CAM.standby, daemon=True, name='orbit-cam').start()
    threading.Thread(target=_vaapi_device, daemon=True, name='orbit-cam-gpu').start()    # checked ahead of the first stream

def _decode_ladder(fmt, rot):
    """Ways to turn the phone's stream into camera frames, cheapest first. The first that
    produces a picture is kept (stream_phone_webcam tries them in turn), so any phone works:
      gpu         decode, turn, fit and (Sharp) make JPEGs on the GPU
      gpu-range   the same, with the stream's colour labels set to plain video ones: some
                  phones send full-range colour that the GPU chain won't take as labelled
      cpu         everything on the CPU: works with anything, costs the most
    Each is (name, ffmpeg command)."""
    pix, (w, h) = _cam_format()
    gpu = _vaapi_device()
    # Fast start, and no waiting: without low_delay the decoder holds ~17 frames (~0.6 s)
    # before passing one on. (nobuffer makes HEVC drop frames, so H.264 only.)
    start = ['-probesize', '32', '-analyzeduration', '0', '-flags', 'low_delay'] + \
            (['-fflags', 'nobuffer'] if fmt == 'h264' else [])
    # Raw H.264 and HEVC have no clock: frames pass straight through as they come
    def cmd(hw, vf, enc):
        return (['ffmpeg', '-hide_banner', '-loglevel', 'warning'] + hw + start + ['-f', fmt, '-i', 'pipe:0',
                '-vf', vf] + enc[:-2] + ['-fps_mode', 'passthrough'] + enc[-2:] + ['pipe:1'])
    ladder = []
    if gpu:
        turn = {90: 'transpose_vaapi=dir=clock,', 180: 'transpose_vaapi=dir=reversal,',
                270: 'transpose_vaapi=dir=cclock,'}.get(rot, '')
        hw = ['-init_hw_device', f'vaapi=va:{gpu}', '-filter_hw_device', 'va',
              '-hwaccel', 'vaapi', '-hwaccel_device', 'va', '-hwaccel_output_format', 'vaapi']
        # upright, then fitted into the camera's one size (a portrait picture gets side bars).
        # Frames the CPU decoded instead (an unusual stream) are uploaded first
        fit = (f'format=nv12|vaapi,hwupload,{turn}scale_vaapi=w={w}:h={h}:force_original_aspect_ratio=decrease'
               f':format={"nv12" if pix == "mjpeg" else pix},pad_vaapi=w={w}:h={h}:x=(ow-iw)/2:y=(oh-ih)/2')
        if pix == 'mjpeg':
            # 95: the camera adds no visible loss of its own (~53 dB over what the phone sent)
            enc, tail = ['-c:v', 'mjpeg_vaapi', '-global_quality', '95', '-f', 'mjpeg'], ''
        else:
            enc, tail = ['-pix_fmt', pix, '-f', 'rawvideo'], f',hwdownload,format={pix}'
        relabel = 'setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709,'
        ladder += [('gpu', cmd(hw, fit + tail, enc)), ('gpu-range', cmd(hw, relabel + fit + tail, enc))]
    turn = {90: 'transpose=1,', 180: 'transpose=1,transpose=1,', 270: 'transpose=2,'}.get(rot, '')
    fit = f'{turn}scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2'
    enc = ['-pix_fmt', 'yuvj420p', '-c:v', 'mjpeg', '-q:v', '2', '-f', 'mjpeg'] if pix == 'mjpeg' \
        else ['-pix_fmt', pix, '-f', 'rawvideo']
    ladder.append(('cpu', cmd([], fit, enc)))
    return ladder

def _stream_range(data, fmt):
    """'pc' (full) or 'tv' (limited) range, from the start of a phone's stream; '' if unknown."""
    try:
        res = subprocess.run(['ffprobe', '-v', 'error', '-f', fmt, '-i', 'pipe:0', '-select_streams', 'v:0',
                              '-show_entries', 'stream=color_range', '-of', 'csv=p=0'],
                             input=data, capture_output=True, timeout=5)
        return res.stdout.decode(errors='replace').strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def stream_phone_webcam(handler, rot, codec='h264'):
    """Decode the phone's stream (H.264 or HEVC) into the camera until the phone stops. What
    arrives is kept until a way of decoding it produces a picture; if one fails first, the next
    is tried with all of it, so nothing the phone sent is lost."""
    dev = webcam_device()
    if dev is None:
        return {"status": "error", "message": "No virtual camera on this computer (install v4l2loopback)."}
    device, label = dev
    if not ORBIT_CAM.streaming.acquire(blocking=False):
        return {"status": "error", "message": "Orbit Cam is already streaming."}
    fmt = 'hevc' if codec == 'hevc' else 'h264'
    ladder = _decode_ladder(fmt, rot)
    errpath = os.path.expanduser('~/.cache/orbit/orbit-cam.log')
    state = {'decoder': None, 'copier': None, 'picture': threading.Event(), 'way': None}
    held = bytearray()                         # everything so far, until a picture comes out
    head = bytearray()                         # the stream's start, to read its colour range

    def launch(name, cmd):
        with open(errpath, 'wb') as errlog:
            dec = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog, bufsize=0)
        picture = threading.Event()

        def copy():
            fd = dec.stdout.fileno()
            first = True
            me = threading.current_thread()
            while True:
                select.select([fd], [], [])       # wait for output before taking the camera
                ORBIT_CAM.live = me               # this decoder owns the camera now
                if not ORBIT_CAM.pass_through(fd):
                    # it ended without a picture: standby again, unless the next way of
                    # decoding has the camera already (clearing it then let a standby frame
                    # into the middle of its first JPEG)
                    if first and ORBIT_CAM.live is me:
                        ORBIT_CAM.live = False
                    return
                first = False
                picture.set()                     # a real frame went in: this way works

        t = threading.Thread(target=copy, daemon=True, name='orbit-cam-live')
        t.start()
        state.update(decoder=dec, copier=t, picture=picture, way=name)
        sys.stderr.write(f"Orbit Cam: decoding with '{name}'\n")

    def next_way():
        """The current way failed before showing anything: the next one, fed all that came."""
        dec = state['decoder']
        try:
            dec.kill()
        except OSError:
            pass
        while ladder:
            name, cmd = ladder.pop(0)
            launch(name, cmd)
            try:
                state['decoder'].stdin.write(bytes(held))
                return True
            except OSError:
                continue
        return False

    started, received = time.time(), 0
    pix, (w, h) = _cam_format()
    try:
        handler.connection.settimeout(10)      # a phone that went quiet ends the stream
        name, cmd = ladder.pop(0)
        launch(name, cmd)
        subprocess.Popen(['notify-send', '-i', 'camera-web', '-a', 'Orbit', 'Orbit Cam on',
                          f'Your phone is the camera "{label}"'], stderr=subprocess.DEVNULL)
        sys.stderr.write(f"Orbit Cam: stream started ({fmt}, turn {rot}, {WEBCAM_MODE} {w}x{h})\n")
        # the start of the latest stream, kept privately, to see why a phone's video won't
        # decode (opened once video arrives, so an empty attempt doesn't wipe the last one)
        sample = None
        for chunk in _request_chunks(handler):
            if received < 2 << 20:
                if sample is None:
                    sample = open(os.open(os.path.expanduser('~/.cache/orbit/orbit-cam-last.' + fmt),
                                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb')
                sample.write(chunk)
            received += len(chunk)
            confirmed = state['picture'].is_set()
            if not confirmed:
                held.extend(chunk)
            if head is not None and pix == 'mjpeg':
                head.extend(chunk)
                if len(head) >= 48 << 10:
                    # A JPEG is full range, and the graphics chip can't stretch limited-range
                    # video to it (blacks and whites come out grey): limited range goes the
                    # CPU's way, which converts it
                    start, head = bytes(head), None
                    if _stream_range(start, fmt) == 'tv' and state['way'] != 'cpu':
                        sys.stderr.write("Orbit Cam: limited-range video, converting on the CPU\n")
                        ladder[:] = [x for x in ladder if x[0] == 'cpu']
                        held[:] = start                # all so far, this chunk included
                        if not next_way():
                            return {"status": "error", "message": "This computer can't decode the phone's video."}
                        continue
            dec = state['decoder']
            ok = True
            try:
                dec.stdin.write(chunk)
            except OSError:
                ok = False
            if not confirmed and (not ok or dec.poll() is not None):
                if not next_way():
                    return {"status": "error", "message": "This computer can't decode the phone's video."}
            elif confirmed and held:
                held.clear()                   # a picture: nothing more to keep
            elif not ok:
                return {"status": "error", "message": "The decoder stopped."}
        if sample:
            sample.close()
        return {"status": "ok"}
    except (OSError, ValueError) as e:
        return {"status": "error", "message": f"Stream ended: {e}"}
    finally:
        took = time.time() - started
        sys.stderr.write(f"Orbit Cam: stream ended after {took:.0f} s, {received // 1024} KB "
                         f"({received * 8 / max(took, 0.1) / 1e6:.1f} Mbit/s), decoded with '{state['way']}'\n")
        dec = state['decoder']
        if dec:
            try:
                dec.stdin.close()
            except OSError:
                pass
            try:
                dec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                dec.kill()
        if state['copier']:
            state['copier'].join(timeout=3)
        ORBIT_CAM.live = False                 # the standby picture again
        ORBIT_CAM.streaming.release()
        try:
            with open(errpath, 'rb') as f:
                err = f.read()[-600:].decode('utf-8', 'replace').strip()
            if err:
                sys.stderr.write(f"Orbit Cam: ffmpeg said: {err}\n")
        except OSError:
            pass
        subprocess.Popen(['notify-send', '-i', 'camera-web', '-a', 'Orbit', 'Orbit Cam off',
                          'Your phone is no longer the camera'], stderr=subprocess.DEVNULL)

def handle_telephony_call_state(state, generic_notice=True):
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
                elif state == "ringing" and generic_notice:
                    # without notification forwarding, at least say that the phone rings
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

def _phone_answers(ip, port, timeout=0.8):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False

_PHONE_WAKE_LOCK = threading.Lock()

def wake_phone(ip=None, wait=5.0):
    """The phone doesn't answer where we last saw it (its address changed, it was dozing, or
    this service restarted and forgot it): ask it to check in. Over the live link it keeps open
    to us, and with a UDP note to its last address and the network (in case the link is down).
    The phone answers by reporting its address (/api/phone/status). True once it has."""
    with _PHONE_WAKE_LOCK:
        before = active_phone_state.get('last_seen', 0)
        auth_mgr.broadcast_ndjson(json.dumps({"event": "check_in", "time": int(time.time())}) + "\n")
        note = json.dumps({"type": "pc_auth_wake", "device_id": get_laptop_id(), "hostname": socket.gethostname()}).encode()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for target in ([ip] if ip else []) + ['255.255.255.255']:
                    try:
                        u.sendto(note, (target, 1760))
                    except OSError:
                        pass
        except OSError:
            pass
        deadline = time.time() + wait
        while time.time() < deadline:
            if active_phone_state.get('last_seen', 0) > before:
                return True
            time.sleep(0.1)
        return False

def get_phone_target(wake=True):
    """(ip, port, token) to reach the phone's own server. With `wake`, a phone that doesn't
    answer there is asked to check in first, so a stale address heals by itself."""
    ip, port, token = _phone_target_known()
    if wake and ip and not _phone_answers(ip, port):
        if wake_phone(ip):
            ip, port, token = _phone_target_known()
    return ip, port, token

def _phone_target_known():
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
    # 2. Direct HTTP to phone if IP is known (no waiting: the live link already carries it)
    target_ip, target_port, target_token = get_phone_target(wake=False)
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
# Find My: where the phone is, and where this computer is
# ==========================================
# The phone reports its location to this computer and to nothing else: when it moves, every
# 15 minutes, when asked, and once more when its battery is nearly flat. Each report also
# lists the Wi-Fi networks around it, so this computer learns, privately, where those networks
# are. If the computer itself is taken somewhere the phone has been, the networks it sees there
# place it on the map, with no outside location service (beaconDB has no coverage here).
# Everything stays in ~/.local/share/orbit/findmy/ (this user only).
import sqlite3
import math
from contextlib import closing

FINDMY_DIR = os.path.expanduser('~/.local/share/orbit/findmy')
FINDMY_DB = os.path.join(FINDMY_DIR, 'findmy.sqlite')
FINDMY_PHOTOS = os.path.join(FINDMY_DIR, 'photos')
FINDMY_KEEP_DAYS = 30
_FINDMY_LOCK = threading.Lock()


def _findmy_db():
    os.makedirs(FINDMY_DIR, mode=0o700, exist_ok=True)
    db = sqlite3.connect(FINDMY_DB, timeout=5)
    try:
        os.chmod(FINDMY_DB, 0o600)
    except OSError:
        pass
    db.executescript('''
        CREATE TABLE IF NOT EXISTS fixes (t REAL, lat REAL, lng REAL, acc REAL, alt REAL, speed REAL,
            provider TEXT, reason TEXT, battery INTEGER, charging INTEGER, net TEXT);
        CREATE INDEX IF NOT EXISTS fixes_t ON fixes(t);
        CREATE TABLE IF NOT EXISTS wifi (bssid TEXT PRIMARY KEY, ssid TEXT, lat REAL, lng REAL,
            weight REAL, seen INTEGER, last REAL, moving INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events (t REAL, kind TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS laptop (t REAL, lat REAL, lng REAL, acc REAL, networks INTEGER, ssid TEXT);
        CREATE TABLE IF NOT EXISTS fingerprints (t REAL, lat REAL, lng REAL, acc REAL, scan TEXT);
    ''')
    return db


def _num(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi and v == v else None


def _metres(lat1, lng1, lat2, lng2):
    """Distance between two points (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(a)))


def _bssid(v):
    v = str(v or '').strip().lower()
    return v if re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}', v) else None


def _signal_weight(dbm):
    """How much a sighting counts: a strong signal means the access point was close."""
    return 10 ** ((max(-100.0, min(-30.0, dbm)) + 100) / 20)


def findmy_record(report):
    """Store a report from the phone: its fixes, and what the Wi-Fi around it says about where
    those networks are. Returns how many fixes were kept."""
    now = time.time()
    fixes = []
    for f in (report.get('fixes') or [])[:200]:
        lat, lng = _num(f.get('lat'), -90, 90), _num(f.get('lng'), -180, 180)
        t = _num(f.get('t'), now - 40 * 86400, now + 600)
        if lat is None or lng is None or t is None:
            continue
        fixes.append((t, lat, lng, _num(f.get('acc'), 0, 1e6), _num(f.get('alt'), -1e4, 1e5),
                      _num(f.get('speed'), 0, 1e4), str(f.get('provider') or '')[:16],
                      str(f.get('reason') or '')[:24]))
    battery = _num(report.get('battery'), 0, 100)
    charging = 1 if report.get('charging') else 0
    net = str(report.get('net') or '')[:64]
    with _FINDMY_LOCK, closing(_findmy_db()) as db:
        for t, lat, lng, acc, alt, speed, provider, reason in fixes:
            db.execute('INSERT INTO fixes VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (t, lat, lng, acc, alt, speed, provider, reason,
                        None if battery is None else int(battery), charging, net))
        # Learn the Wi-Fi around the phone, from a fix taken close in time to the scan and good
        # to 100 m. A network seen far from where it was before moves around (a hotspot, a bus):
        # it can't place anything, so it's marked and left out.
        anchor = report.get('wifi_at') or {}
        alat, alng = _num(anchor.get('lat'), -90, 90), _num(anchor.get('lng'), -180, 180)
        aacc = _num(anchor.get('acc'), 0, 1e6)
        # a fingerprint: the whole Wi-Fi picture at a well-known spot (see fingerprint_locate)
        fp = phone_scan(report.get('wifi'))
        if alat is not None and alng is not None and aacc is not None and aacc <= 60 and len(fp) >= 3:
            db.execute('INSERT INTO fingerprints VALUES (?,?,?,?,?)', (now, alat, alng, aacc, json.dumps(fp)))
            db.execute('DELETE FROM fingerprints WHERE rowid IN (SELECT rowid FROM fingerprints ORDER BY t DESC LIMIT -1 OFFSET 6000)')
            _FINGERPRINTS['stale'] = True
        if alat is not None and alng is not None and aacc is not None and aacc <= 100:
            for ap in (report.get('wifi') or [])[:120]:
                b = _bssid(ap.get('bssid'))
                rssi = _num(ap.get('rssi'), -127, 0)
                if not b or rssi is None or int(b[:2], 16) & 0x02:   # randomised / hotspot address
                    continue
                w = _signal_weight(rssi) / max(aacc, 8) ** 2
                row = db.execute('SELECT lat, lng, weight, seen, moving FROM wifi WHERE bssid=?', (b,)).fetchone()
                ssid = str(ap.get('ssid') or '')[:64]
                if row is None:
                    db.execute('INSERT INTO wifi VALUES (?,?,?,?,?,?,?,0)', (b, ssid, alat, alng, w, 1, now))
                elif row[4]:
                    continue
                elif _metres(row[0], row[1], alat, alng) > 1500:
                    db.execute('UPDATE wifi SET moving=1, last=? WHERE bssid=?', (now, b))
                else:
                    total = row[2] + w
                    db.execute('UPDATE wifi SET lat=?, lng=?, weight=?, seen=?, last=?, ssid=? WHERE bssid=?',
                               ((row[0] * row[2] + alat * w) / total, (row[1] * row[2] + alng * w) / total,
                                total, row[3] + 1, now, ssid or None, b))
        if fixes and random.random() < 0.05:
            cut = now - FINDMY_KEEP_DAYS * 86400
            db.execute('DELETE FROM fixes WHERE t < ?', (cut,))
            db.execute('DELETE FROM laptop WHERE t < ?', (cut,))
            # photos (a wrong unlock on the phone, a wrong password here) stay until you delete them
            db.execute("DELETE FROM events WHERE t < ? AND kind NOT IN ('wrong_unlock', 'laptop_wrong_password', 'lost_report')", (cut,))
        db.commit()
    if fixes:
        latest = max(fixes)
        if latest[7] == 'low_battery':
            findmy_event('low_battery', {'battery': battery}, notify=False)
    return len(fixes)


def findmy_event(kind, detail, notify=True):
    """Something the phone reports that you should know about (a new SIM, a wrong unlock)."""
    with _FINDMY_LOCK, closing(_findmy_db()) as db:
        db.execute('INSERT INTO events VALUES (?,?,?)', (time.time(), kind, json.dumps(detail)[:2000]))
        db.commit()
    if not notify:
        return
    titles = {
        'sim_changed': ("Your phone's SIM card changed",
                        f"Now: {detail.get('operator') or 'unknown network'}. If you didn't do this, open Find my phone."),
        'sim_removed': ("Your phone's SIM card was removed", "If you didn't do this, open Find my phone."),
        'wrong_unlock': ("Someone tried to unlock your phone", "A photo was taken. Open Find my phone to see it."),
    }
    title, text = titles.get(kind, ('Find my phone', kind))
    icon = detail.get('photo') if kind == 'wrong_unlock' and detail.get('photo') else 'find-location'
    try:
        subprocess.Popen(['notify-send', '-u', 'critical', '-i', icon, '-a', 'Orbit', title, text],
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


def findmy_save_photo(data, reason):
    """A photo the phone took (the front camera, after wrong unlock attempts)."""
    if not data.startswith(b'\xff\xd8') or len(data) > 6 << 20:
        return None
    os.makedirs(FINDMY_PHOTOS, mode=0o700, exist_ok=True)
    name = time.strftime('%Y%m%d-%H%M%S') + f'-{secrets.token_hex(3)}.jpg'
    path = os.path.join(FINDMY_PHOTOS, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    findmy_event('wrong_unlock', {'photo': path, 'name': name, 'reason': reason[:40]})
    return name


def findmy_phone_view():
    """What the Find my phone page shows: the latest fix, the last day's trail, alerts, photos."""
    with _FINDMY_LOCK, closing(_findmy_db()) as db:
        last = db.execute('SELECT t, lat, lng, acc, provider, reason, battery, charging, net FROM fixes '
                          'ORDER BY t DESC LIMIT 1').fetchone()
        trail = db.execute('SELECT t, lat, lng, acc FROM fixes WHERE t > ? ORDER BY t',
                           (time.time() - 86400,)).fetchall()[-500:]
        events = db.execute("SELECT t, kind, detail FROM events WHERE kind != 'laptop_wrong_password' "
                            "ORDER BY t DESC LIMIT 40").fetchall()
        known = db.execute('SELECT COUNT(*) FROM wifi WHERE moving=0').fetchone()[0]
    keys = ('t', 'lat', 'lng', 'acc', 'provider', 'reason', 'battery', 'charging', 'net')
    out_events = []
    for t, kind, detail in events:
        try:
            d = json.loads(detail)
        except ValueError:
            d = {}
        d.pop('photo', None)                   # a path on this computer; the page uses the name
        out_events.append({'t': t, 'kind': kind, **d})
    return {'status': 'ok', 'last': dict(zip(keys, last)) if last else None,
            'trail': [{'t': t, 'lat': a, 'lng': b, 'acc': c} for t, a, b, c in trail],
            'events': out_events, 'networks_known': known, 'laptop': laptop_last(),
            'bluetooth': ORBIT_BLE.phone_reading(),
            'nearby': {'on': time.time() < NEARBY['until'],
                       'score': NEARBY['score'] if time.time() - NEARBY['t'] < 20 else None,
                       'word': likeness_word(NEARBY['score']) if time.time() - NEARBY['t'] < 20 else None},
            'now': time.time()}


# ---- this computer's own location ----
_NMCLI_SPLIT = re.compile(r'(?<!\\):')


def laptop_wifi_scan(rescan=True):
    """[(bssid, ssid, dBm, connected)] of the Wi-Fi networks this computer sees."""
    try:
        out = subprocess.run(['nmcli', '-t', '-f', 'BSSID,SSID,SIGNAL,IN-USE', 'dev', 'wifi', 'list',
                              '--rescan', 'yes' if rescan else 'no'],
                             capture_output=True, text=True, timeout=25).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    aps = []
    for line in out.splitlines():
        parts = [p.replace('\\:', ':') for p in _NMCLI_SPLIT.split(line)]
        if len(parts) < 4:
            continue
        b = _bssid(parts[0])
        try:
            dbm = int(parts[2]) / 2 - 100       # nmcli gives 0-100
        except ValueError:
            continue
        if b:
            aps.append((b, parts[1], dbm, parts[3].strip() == '*'))
    return aps


# ---- the Wi-Fi map: where something is, from the Wi-Fi it sees (fingerprinting) ----
# Each location report from the phone, when its fix is good, leaves a fingerprint: every network
# it saw and how strongly, at that spot. Anything that sees the Wi-Fi (this computer, or the phone
# indoors where GPS is poor) is placed where its own picture best matches stored ones: the few
# closest by signal, averaged. This is how indoor positioning works (RADAR-style k-nearest
# neighbours); much finer than guessing where each access point stands, and the phone and this
# computer are placed the same way, so their errors largely cancel in how far apart they are.
_FINGERPRINTS = {'stale': True, 'rows': []}


def _fingerprints():
    if _FINGERPRINTS['stale']:
        with _FINDMY_LOCK, closing(_findmy_db()) as db:
            rows = db.execute('SELECT lat, lng, acc, scan FROM fingerprints').fetchall()
        parsed = []
        for lat, lng, acc, scan in rows:
            try:
                parsed.append((lat, lng, acc, json.loads(scan)))
            except ValueError:
                pass
        _FINGERPRINTS.update(stale=False, rows=parsed)
    return _FINGERPRINTS['rows']


def fingerprint_locate(scan):
    """{lat, lng, acc, matches} from the Wi-Fi map for a scan ({bssid: dBm}), or None."""
    query = {b: v for b, v in scan.items() if v > -92}
    if len(query) < 2:
        return None
    scored = []
    for lat, lng, acc, fp in _fingerprints():
        common = sum(1 for b in query if b in fp)
        if common < min(3, len(query)):
            continue
        keys = set(query) | {b for b, v in fp.items() if v > -92}
        # signal distance: missing networks count as barely heard
        d = math.sqrt(sum((query.get(k, -100) - fp.get(k, -100)) ** 2 for k in keys) / len(keys))
        scored.append((d, lat, lng, acc))
    if not scored:
        return None
    scored.sort()
    best = scored[:4]
    ws = [1 / (d + 2) ** 2 for d, _, _, _ in best]
    total = sum(ws)
    lat = sum(w * a for w, (_, a, _, _) in zip(ws, best)) / total
    lng = sum(w * b for w, (_, _, b, _) in zip(ws, best)) / total
    spread = sum(w * _metres(lat, lng, a, b) for w, (_, a, b, _) in zip(ws, best)) / total
    fix = sum(w * c for w, (_, _, _, c) in zip(ws, best)) / total
    acc = max(6.0, spread + 0.5 * fix + (10.0 if len(best) == 1 else 0.0))
    return {'lat': lat, 'lng': lng, 'acc': round(acc), 'matches': len(best), 'signal_gap': round(best[0][0], 1)}


def laptop_locate(rescan=True):
    """Where this computer is, from the networks it sees and what the phone has learned about
    them. Stored when found, so the phone can still see where it was last."""
    aps = laptop_wifi_scan(rescan)
    seen = len(aps)
    connected = next((ssid for _, ssid, _, on in aps if on), '')
    found = None
    fp = fingerprint_locate({b: dbm for b, _, dbm, _ in aps}) if aps else None
    if fp:
        found = {'t': time.time(), 'lat': fp['lat'], 'lng': fp['lng'], 'acc': fp['acc'],
                 'networks': len(aps), 'ssid': connected, 'method': 'wifi_map'}
        aps = []                         # placed: the rough per-network guess isn't needed
    if aps:
        with _FINDMY_LOCK, closing(_findmy_db()) as db:
            marks = ','.join('?' * len(aps))
            known = {b: (lat, lng) for b, lat, lng in db.execute(
                f'SELECT bssid, lat, lng FROM wifi WHERE moving=0 AND bssid IN ({marks})', [a[0] for a in aps])}
        pts = [(known[b][0], known[b][1], _signal_weight(dbm)) for b, _, dbm, _ in aps if b in known]
        if pts:
            total = sum(w for _, _, w in pts)
            lat = sum(a * w for a, _, w in pts) / total
            lng = sum(b * w for _, b, w in pts) / total
            spread = math.sqrt(sum(w * _metres(lat, lng, a, b) ** 2 for a, b, w in pts) / total)
            acc = round(max(25.0, spread + (60.0 if len(pts) == 1 else 20.0)))
            found = {'t': time.time(), 'lat': lat, 'lng': lng, 'acc': acc, 'networks': len(pts), 'ssid': connected,
                     'method': 'networks'}
    if found:
        lat, lng, acc = found['lat'], found['lng'], found['acc']
        with _FINDMY_LOCK, closing(_findmy_db()) as db:
            last = db.execute('SELECT t, lat, lng FROM laptop ORDER BY t DESC LIMIT 1').fetchone()
            # one row per place (or per hour in one place), not one per check
            if not last or _metres(last[1], last[2], lat, lng) > 50 or found['t'] - last[0] > 3600:
                db.execute('INSERT INTO laptop VALUES (?,?,?,?,?,?)',
                           (found['t'], lat, lng, acc, found['networks'], connected))
                db.commit()
    return found, connected, seen


def laptop_last():
    with _FINDMY_LOCK, closing(_findmy_db()) as db:
        row = db.execute('SELECT t, lat, lng, acc, networks, ssid FROM laptop ORDER BY t DESC LIMIT 1').fetchone()
    return dict(zip(('t', 'lat', 'lng', 'acc', 'networks', 'ssid'), row)) if row else None


def laptop_locator_loop():
    """Every 10 minutes: where is this computer now (no fresh scan: what Wi-Fi already saw)."""
    while True:
        try:
            laptop_locate(rescan=False)
        except Exception as e:
            sys.stderr.write(f"Find my: locating this computer failed: {e}\n")
        time.sleep(600)


# ---- ring this computer (from the phone) ----
class LaptopRinger:
    """A loud, repeating sound until the phone stops it (or 2 minutes pass). The volume goes
    up and unmutes for it, and comes back afterwards."""

    def __init__(self):
        self.stop = threading.Event()
        self.thread = None

    def ringing(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        if self.ringing():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._ring, daemon=True, name='find-my-ring')
        self.thread.start()
        try:
            subprocess.Popen(['notify-send', '-u', 'critical', '-i', 'find-location', '-a', 'Orbit',
                              'Your phone is looking for this computer', 'Ringing until it stops.'],
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _ring(self):
        before = None
        try:
            before = subprocess.run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'],
                                    capture_output=True, text=True, timeout=3).stdout
            subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'], timeout=3)
            subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', '0.8'], timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        sound = next((s for s in ('/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga',
                                  '/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga',
                                  '/usr/share/sounds/freedesktop/stereo/bell.oga') if os.path.exists(s)), None)
        end = time.time() + 120
        while sound and not self.stop.is_set() and time.time() < end:
            try:
                p = subprocess.Popen(['paplay', sound], stderr=subprocess.DEVNULL)
                while p.poll() is None and not self.stop.is_set():
                    time.sleep(0.1)
                if p.poll() is None:
                    p.terminate()
            except OSError:
                break
        if before:
            m = re.search(r'Volume:\s*([0-9.]+)', before)
            try:
                if m:
                    subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', m.group(1)], timeout=3)
                subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1' if 'MUTED' in before else '0'], timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def silence(self):
        self.stop.set()


LAPTOP_RINGER = LaptopRinger()


# ---- near or far, by the Wi-Fi around (no Bluetooth needed) ----
# The phone and this computer each see a set of Wi-Fi networks, each at some strength. Side by
# side they see the same ones, about equally strong; rooms apart the strengths differ; far apart
# they share nothing. That likeness (0 to 1) drives the nearby finder on both sides and the
# walk-away lock.
_LAPTOP_SCAN = {'t': 0.0, 'aps': {}, 'details': {}}
_LAPTOP_SCAN_LOCK = threading.Lock()


def laptop_scan_now(max_age=15):
    """{bssid: dBm} this computer sees, rescanned when older than max_age seconds."""
    with _LAPTOP_SCAN_LOCK:
        if time.time() - _LAPTOP_SCAN['t'] > max_age:
            aps = laptop_wifi_scan(rescan=True)
            if aps:
                _LAPTOP_SCAN.update(
                    t=time.time(),
                    aps={b: dbm for b, _, dbm, _ in aps},
                    details={b: {'ssid': ssid, 'dbm': dbm, 'connected': conn} for b, ssid, dbm, conn in aps}
                )
        return dict(_LAPTOP_SCAN['aps'])


def laptop_scan_details():
    with _LAPTOP_SCAN_LOCK:
        return dict(_LAPTOP_SCAN.get('details', {}))


def _wifi_identity_match(phone_raw, theirs, mine):
    """Matches phone Wi-Fi scan with laptop Wi-Fi scan to identify shared networks and signal gap."""
    phone_ssids = {}
    for ap in (phone_raw or [])[:120]:
        b = _bssid(ap.get('bssid'))
        if b and ap.get('ssid'):
            phone_ssids[b] = str(ap.get('ssid'))

    laptop_details = laptop_scan_details()
    common = []
    gap_sum = 0.0
    for b, p_rssi in theirs.items():
        if b in mine:
            l_rssi = mine[b]
            diff = abs(p_rssi - l_rssi)
            gap_sum += diff
            ssid = phone_ssids.get(b) or (laptop_details.get(b, {}).get('ssid') if laptop_details else '') or ''
            common.append({
                'bssid': b,
                'ssid': ssid,
                'phone_rssi': p_rssi,
                'laptop_rssi': l_rssi,
                'diff': round(diff, 1)
            })
    common.sort(key=lambda x: x['phone_rssi'], reverse=True)
    signal_gap = (gap_sum / len(common)) if common else None
    return common, signal_gap


def last_known_gps_distance():
    """Returns (distance_metres, phone_lat, phone_lng, lap_lat, lap_lng) from latest GPS fixes, or None."""
    with _FINDMY_LOCK, closing(_findmy_db()) as db:
        fix = db.execute('SELECT lat, lng, acc, t FROM fixes ORDER BY t DESC LIMIT 1').fetchone()
        lap = db.execute('SELECT lat, lng, acc, t FROM laptop ORDER BY t DESC LIMIT 1').fetchone()
    if fix and lap and fix[0] is not None and lap[0] is not None:
        return round(_metres(fix[0], fix[1], lap[0], lap[1])), fix[0], fix[1], lap[0], lap[1]
    return None, None, None, None, None


def wifi_distance_estimate(score, signal_gap, common_count, apart=None):
    """Estimates distance in metres between phone and laptop from Wi-Fi identity recognition."""
    if common_count and common_count > 0 and score is not None and score >= 0.15:
        gap_factor = (signal_gap * 0.35) if signal_gap is not None else 0.0
        dist = 0.8 + 25.0 * math.pow(max(0.0, 1.0 - score), 1.8) + gap_factor
        return round(max(0.4, dist), 1)
    if apart is not None and apart >= 0:
        return float(apart)
    return None


def wifi_likeness(a, b):
    """How alike two scans ({bssid: dBm}) are: 1 side by side, 0 nothing in common, None if
    either saw nothing. Networks count by how strong they are: a strong one seen by only one
    side says more than a faint one."""
    keys = {k for k, v in a.items() if v > -88} | {k for k, v in b.items() if v > -88}
    if not a or not b or not keys:
        return None
    num = den = 0.0
    for k in keys:
        pa, pb = a.get(k), b.get(k)
        w = _signal_weight(max(v for v in (pa, pb) if v is not None))
        den += w
        if pa is not None and pb is not None:
            num += w * max(0.0, 1 - abs(pa - pb) / 25)
    return num / den if den else None


def likeness_word(s):
    if s is None:
        return 'No Wi-Fi to compare'
    return 'Very close' if s > 0.55 else 'Close' if s > 0.35 else 'Nearby' if s > 0.15 else 'Far'


def phone_scan(wifi):
    out = {}
    for ap in (wifi or [])[:120]:
        b, rssi = _bssid(ap.get('bssid')), _num(ap.get('rssi'), -127, 0)
        if b and rssi is not None:
            out[b] = rssi
    return out


NEARBY = {'until': 0.0, 't': 0.0, 'score': None}     # the computer's page asked to find the phone


# ---- the nearby finder, by Bluetooth: how strong each one's signal is at the other ----
# Optional (the phone's Bluetooth may be off: then sound and Wi-Fi do it). Our own two devices
# only: each advertises Orbit's mark while it's being looked for, and the other measures how
# strongly it hears it. Stronger means nearer: a metre or two up close, coarser further away.
ORBIT_BLE_UUID = 'f1d0c0de-4f72-6269-7420-46696e644d79'

class OrbitBluetooth:
    """Bluetooth for finding each other up close. This computer advertises Orbit's mark so the
    phone can tell how near it is, and listens for the phone's mark to tell how near the phone
    is. Bluetooth is switched on for it (unblocked, powered) and back off afterwards if it was
    off. Driven through bluetoothctl; each session ends by itself after 5 minutes."""

    SESSION = 300

    def __init__(self):
        self.lock = threading.Lock()
        self.adv = None                 # bluetoothctl advertising
        self.scan = None                # bluetoothctl scanning for the phone
        self.adv_until = self.scan_until = 0.0
        self.readings = collections.deque(maxlen=20)   # (time, dBm) of the phone
        self.restore = None             # how Bluetooth was before we switched it on
        self.expiring = False

    def _ctl(self):
        return subprocess.Popen(['bluetoothctl'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def _send(self, p, *lines):
        try:
            for line in lines:
                p.stdin.write(line + '\n')
            p.stdin.flush()
        except (OSError, ValueError):
            pass

    def _power_on(self):
        try:
            self._power_on_now()
        except (OSError, subprocess.TimeoutExpired) as e:
            sys.stderr.write(f"Bluetooth didn't switch on: {e}\n")

    def _power_on_now(self):
        if self.restore is not None:
            return
        blocked = 'Soft blocked: yes' in subprocess.run(['rfkill', 'list', 'bluetooth'], capture_output=True,
                                                         text=True, timeout=5).stdout
        powered = 'Powered: yes' in subprocess.run(['bluetoothctl', 'show'], capture_output=True,
                                                   text=True, timeout=5).stdout
        self.restore = (blocked, powered)
        if blocked:
            subprocess.run(['rfkill', 'unblock', 'bluetooth'], timeout=5)
            time.sleep(1.5)
        if not powered:
            subprocess.run(['bluetoothctl', 'power', 'on'], capture_output=True, timeout=10)

    def _power_restore(self):
        if self.restore is None or self.adv or self.scan:
            return
        blocked, powered = self.restore
        self.restore = None
        try:
            if not powered:
                subprocess.run(['bluetoothctl', 'power', 'off'], capture_output=True, timeout=10)
            if blocked:
                subprocess.run(['rfkill', 'block', 'bluetooth'], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def advertise(self, on):
        """So the phone can find this computer nearby."""
        with self.lock:
            if on:
                self.adv_until = time.time() + self.SESSION
                if self.adv and self.adv.poll() is None:
                    return True
                self._power_on()
                self.adv = self._ctl()
                self._send(self.adv, 'menu advertise', f'uuids {ORBIT_BLE_UUID}', 'name on', 'back',
                           'advertise on')
                self._start_expiry()
                return True
            self._stop_adv()
            self._power_restore()
            return False

    def _stop_adv(self):
        if self.adv:
            self._send(self.adv, 'advertise off', 'quit')
            try:
                self.adv.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.adv.kill()
            self.adv = None

    def listen(self, on):
        """Measure how near the phone is (it advertises Orbit's mark meanwhile)."""
        with self.lock:
            if on:
                self.scan_until = time.time() + self.SESSION
                if self.scan and self.scan.poll() is None:
                    return True
                self._power_on()
                self.readings.clear()
                self.scan = self._ctl()
                self._send(self.scan, 'menu scan', f'uuids {ORBIT_BLE_UUID}', 'transport le', 'duplicate-data on',
                           'back', 'scan on')
                threading.Thread(target=self._read, args=(self.scan,), daemon=True).start()
                self._start_expiry()
                return True
            self._stop_scan()
            self._power_restore()
            return False

    def _stop_scan(self):
        if self.scan:
            self._send(self.scan, 'scan off', 'quit')
            try:
                self.scan.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.scan.kill()
            self.scan = None

    def _read(self, p):
        rssi = re.compile(r'RSSI:\s*(?:0x[0-9a-f]+\s*)?\(?(-\d+)\)?', re.I)
        for line in p.stdout:
            m = rssi.search(line)
            if m:
                self.readings.append((time.time(), int(m.group(1))))

    def _start_expiry(self):
        if not self.expiring:
            self.expiring = True
            threading.Thread(target=self._expire, daemon=True, name='bluetooth-expiry').start()

    def _expire(self):
        while True:
            time.sleep(5)
            with self.lock:
                now = time.time()
                if self.adv and now > self.adv_until:
                    self._stop_adv()
                if self.scan and now > self.scan_until:
                    self._stop_scan()
                if not self.adv and not self.scan:
                    self._power_restore()
                    self.expiring = False
                    return

    def phone_reading(self):
        """The phone's signal, smoothed over the last few seconds (None if not heard)."""
        recent = [(t, v) for t, v in list(self.readings) if time.time() - t < 6]
        on = bool(self.scan and self.scan.poll() is None)
        if not recent:
            return {'listening': on, 'dbm': None}
        vals = sorted(v for _, v in recent)
        return {'listening': on, 'dbm': vals[len(vals) // 2], 'age': round(time.time() - recent[-1][0], 1)}


ORBIT_BLE = OrbitBluetooth()


# ---- how far, by sound (two-way acoustic ranging, "BeepBeep") ----
# Wi-Fi goes through walls; sound doesn't. While the phone's Nearby finder is open, both record
# for ~3 s; this computer plays a rising chirp (16.5-19 kHz, which most adults can't hear) and the
# phone a falling one. Each device then measures, on its own clock, the time between the two
# chirps in its own recording; the difference of those two intervals is twice the sound's travel
# time between them, whatever each device's audio delays are. To a few centimetres in a room;
# not heard at all through a wall ("not in this room").
SOUND_SR = 48000
SOUND_WINDOW = 2.8           # seconds this computer records
SPEED_OF_SOUND = 343.0


def _chirp(f0, f1, dur=0.08):
    import numpy as np
    t = np.arange(int(SOUND_SR * dur)) / SOUND_SR
    k = (f1 - f0) / dur
    return np.sin(2 * np.pi * (f0 * t + 0.5 * k * t * t)) * np.hanning(len(t)) ** 0.5   # soft edges: no click


def _find_chirp(rec, tmpl, skip=None):
    """(seconds, strength) of the template's best match in the recording, to a fraction of a
    sample. `skip` (seconds): leave out 0.15 s around it, where the device's own loud chirp is:
    the rising and falling chirps aren't perfectly unlike, and a loud one of either shape gives
    the other's template a match of its own (that read as "heard each other" when far apart)."""
    import numpy as np
    n = len(rec) + len(tmpl)
    size = 1 << (n - 1).bit_length()
    c = np.abs(np.fft.irfft(np.fft.rfft(rec, size) * np.conj(np.fft.rfft(tmpl, size)), size)[:len(rec)])
    noise = np.median(c) + 1e-12
    if skip is not None:
        a, b = int((skip - 0.15) * SOUND_SR), int((skip + 0.15) * SOUND_SR)
        c = c.copy()
        c[max(0, a):max(0, b)] = 0
    i = int(np.argmax(c))
    strength = float(c[i] / noise)
    if 0 < i < len(c) - 1:                       # parabola through the peak: sub-sample timing
        a, b, d = c[i - 1], c[i], c[i + 1]
        den = a - 2 * b + d
        i = i + (0.5 * (a - d) / den if den else 0.0)
    return i / SOUND_SR, strength


class SoundRanger:
    def __init__(self):
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.path = None

    def _chirp_wav(self):
        import numpy as np, wave
        path = os.path.join(os.path.expanduser('~/.cache/orbit'), 'chirp-up.wav')
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            x = np.concatenate([_chirp(16500, 19000), np.zeros(int(0.03 * SOUND_SR))])
            with wave.open(path, 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(SOUND_SR)
                f.writeframes((x * 0.95 * 32767).astype('<i2').tobytes())
        return path

    def start(self):
        """Record SOUND_WINDOW seconds, playing this computer's chirp near the start."""
        with self.lock:
            self.done.clear()
            self.path = os.path.join(os.path.expanduser('~/.cache/orbit'), f'range-{secrets.token_hex(4)}.wav')
            threading.Thread(target=self._run, args=(self.path,), daemon=True, name='sound-range').start()

    def _run(self, path):
        rec = None
        before = None
        try:
            chirp = self._chirp_wav()
            rec = subprocess.Popen(['pw-record', '--rate', str(SOUND_SR), '--channels', '1', '--format', 's16', path],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = time.time()
            # loud enough to be heard across a room (back to how it was afterwards)
            before = subprocess.run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'], capture_output=True, text=True, timeout=2).stdout
            m = re.search(r'Volume:\s*([0-9.]+)', before or '')
            if 'MUTED' in (before or '') or (m and float(m.group(1)) < 0.6):
                subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'], timeout=2)
                subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', '0.6'], timeout=2)
            else:
                before = None
            time.sleep(max(0.0, 0.35 - (time.time() - started)))
            subprocess.run(['pw-play', chirp], timeout=3)
            time.sleep(max(0.0, SOUND_WINDOW - (time.time() - started)))
        except Exception as e:
            sys.stderr.write(f"Sound ranging: {e}\n")
        finally:
            if rec:
                rec.terminate()
                try:
                    rec.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    rec.kill()
            if before:
                m = re.search(r'Volume:\s*([0-9.]+)', before)
                try:
                    if m:
                        subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', m.group(1)], timeout=2)
                    if 'MUTED' in before:
                        subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1'], timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self.done.set()

    def finish(self, phone_pcm):
        """The phone's recording (16-bit mono, 48 kHz): the distance, or why there's none."""
        import numpy as np, wave
        if not self.done.wait(timeout=SOUND_WINDOW + 3) or not self.path:
            return {"status": "error", "message": "This computer didn't record."}
        try:
            with wave.open(self.path) as f:
                mine = np.frombuffer(f.readframes(f.getnframes()), '<i2').astype(float)
        except Exception:
            return {"status": "error", "message": "This computer's microphone didn't record."}
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        phone = np.frombuffer(phone_pcm[:len(phone_pcm) // 2 * 2], '<i2').astype(float)
        up, down = _chirp(16500, 19000), _chirp(19000, 16500)
        l_up, sl_up = _find_chirp(mine, up)                  # this computer's own chirp, heard here
        p_dn, sp_dn = _find_chirp(phone, down)               # the phone's own chirp
        l_dn, sl_dn = _find_chirp(mine, down, skip=l_up)     # the phone's chirp, heard here
        p_up, sp_up = _find_chirp(phone, up, skip=p_dn)      # this computer's chirp, heard by the phone
        strengths = {"here_own": round(sl_up), "here_phone": round(sl_dn),
                     "phone_laptop": round(sp_up), "phone_own": round(sp_dn)}
        try:                                                   # the last pair, privately, to look into a bad reading
            dbg = os.path.expanduser('~/.cache/orbit/range-last')
            os.makedirs(dbg, mode=0o700, exist_ok=True)
            np.save(os.path.join(dbg, 'laptop.npy'), mine.astype('<i2'))
            np.save(os.path.join(dbg, 'phone.npy'), phone.astype('<i2'))
        except Exception:
            pass
        # each own chirp loud and clear; the other's well out of the noise; and each other's chirp
        # where it can be: the phone chirps 0.3-1.5 s after this computer does
        gap_here, gap_phone = l_dn - l_up, p_dn - p_up
        heard = (min(sl_up, sp_dn) > 40 and min(sl_dn, sp_up) > 30
                 and 0.25 < gap_here < 1.8 and 0.25 < gap_phone < 1.8)
        d = SPEED_OF_SOUND / 2 * (gap_here - gap_phone) + 0.07       # + each one's speaker-to-mic gap
        sys.stderr.write(f"Nearby by sound: {'heard' if heard else 'not heard'}, {strengths}, "
                         f"gaps {gap_here:.3f}/{gap_phone:.3f} s, {d:.2f} m\n")
        if not heard:
            return {"status": "ok", "heard": False, "strengths": strengths}
        if not -0.5 < d < 25:
            return {"status": "ok", "heard": False, "strengths": strengths, "odd": round(float(d), 2)}
        return {"status": "ok", "heard": True, "distance": round(float(max(0.0, d)), 2), "strengths": strengths}


SOUND_RANGER = SoundRanger()


# ---- the walk-away lock ----
class WalkAway:
    """Locks this computer when the phone walks away: when the phone, after being on this
    computer's network, is now only reachable through the internet; or when two Wi-Fi scans
    from it in a row look nothing like what this computer sees (a big network like a campus,
    where the phone stays on the same network far away). It never unlocks."""
    GRACE = 90            # seconds off the local network before it counts (a brief detour isn't leaving)

    def __init__(self):
        self.lan_at = self.tunnel_at = 0.0
        self.far = 0
        self.locked_episode = False

    def contact(self, tunnel):
        if tunnel:
            self.tunnel_at = time.time()
        else:
            self.lan_at = time.time()
            if self.far == 0:
                self.locked_episode = False

    def scan(self, likeness):
        if likeness is None:
            return
        if likeness < 0.08:
            self.far += 1
        elif likeness > 0.2:
            self.far = 0
            if self.lan_at > self.tunnel_at:
                self.locked_episode = False

    def away(self):
        left_network = self.lan_at > 0 and self.tunnel_at > self.lan_at + self.GRACE
        return left_network or self.far >= 2

    def loop(self):
        while True:
            time.sleep(15)
            try:
                if not findmy_settings().get('walk_away_lock') or self.locked_episode or not self.away():
                    continue
                if is_pc_locked() is not False:          # locked already, or nobody logged in
                    self.locked_episode = True
                    continue
                session = graphical_session()
                subprocess.run(['loginctl', 'lock-session', session], timeout=5)
                self.locked_episode = True
                sys.stderr.write("Find my: the phone walked away, locked this computer\n")
                findmy_event('walk_away_lock', {}, notify=False)
                auth_mgr.broadcast_ndjson(json.dumps({"event": "laptop_alert", "kind": "walk_away_lock",
                                                      "t": time.time()}) + "\n")
            except Exception as e:
                sys.stderr.write(f"Find my: walk-away lock: {e}\n")


WALK_AWAY = WalkAway()


def findmy_settings():
    cfg = load_config()
    return {'walk_away_lock': bool(cfg.get('findmy_walk_away_lock', True)),
            'wrong_password_photo': bool(cfg.get('findmy_wrong_password_photo', True))}


def set_findmy_settings(body):
    cfg = load_config()
    if 'walk_away_lock' in body:
        cfg['findmy_walk_away_lock'] = bool(body['walk_away_lock'])
    if 'wrong_password_photo' in body:
        cfg['findmy_wrong_password_photo'] = bool(body['wrong_password_photo'])
    save_config(cfg)
    return findmy_settings()


# ---- a photo when someone types a wrong password here ----
def builtin_camera():
    """This computer's own camera (not a virtual one like Orbit Cam), or None."""
    for d in sorted(glob.glob('/sys/class/video4linux/video*')):
        try:
            if 'virtual' in os.path.realpath(os.path.join(d, 'device')):
                continue
            with open(os.path.join(d, 'index')) as f:
                if f.read().strip() != '0':        # a camera's other nodes carry metadata
                    continue
        except OSError:
            continue
        return '/dev/' + os.path.basename(d)
    return None


def webcam_photo():
    """A photo from this computer's own camera, kept with Find my's photos: its name, or None
    (no camera, or another app has it)."""
    cam = builtin_camera()
    data = b''
    if cam:
        for size in (['-video_size', '1280x720'], []):
            try:
                # the first frames are dark while the exposure settles: keep the 15th
                data = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'v4l2'] + size +
                                      ['-i', cam, '-vf', r'select=gte(n\,15)', '-frames:v', '1', '-q:v', '3',
                                       '-f', 'mjpeg', '-'], capture_output=True, timeout=20).stdout
            except (OSError, subprocess.TimeoutExpired):
                data = b''
            if data.startswith(b'\xff\xd8'):
                break
    name = None
    if data.startswith(b'\xff\xd8'):
        os.makedirs(FINDMY_PHOTOS, mode=0o700, exist_ok=True)
        name = 'laptop-' + time.strftime('%Y%m%d-%H%M%S') + f'-{secrets.token_hex(3)}.jpg'
        fd = os.open(os.path.join(FINDMY_PHOTOS, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
    return name


def take_laptop_photo():
    name = webcam_photo()
    found, connected, _ = laptop_locate(rescan=False)
    where = {'place': {'lat': found['lat'], 'lng': found['lng'], 'acc': found['acc']} if found else None,
             'connected': connected}
    findmy_event('laptop_wrong_password', {'name': name, **where}, notify=False)
    sys.stderr.write(f"Find my: wrong password on this computer, {'photo taken' if name else 'no photo (camera busy?)'}\n")
    auth_mgr.broadcast_ndjson(json.dumps({"event": "laptop_alert", "kind": "wrong_password",
                                          "name": name, "t": time.time(), **where}) + "\n")


def wrong_password_watch():
    """Follow the system log for wrong passwords at the lock screen or the login screen."""
    user = re.escape(os.environ.get('USER') or '')
    pat = re.compile(r'pam_unix\((kde|sddm|plasmalogin|login|system-local-login)[^)]*:auth\): authentication failure'
                     r'|password check failed for user \(' + user + r'\)')
    last = 0.0
    while True:
        try:
            p = subprocess.Popen(['journalctl', '-f', '-n', '0', '-q', '-o', 'cat',
                                  '--grep', 'authentication failure|password check failed'],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in p.stdout:
                if pat.search(line) and findmy_settings().get('wrong_password_photo') and time.time() - last > 30:
                    last = time.time()
                    threading.Thread(target=take_laptop_photo, daemon=True).start()
        except Exception as e:
            sys.stderr.write(f"Find my: can't follow the system log: {e}\n")
        time.sleep(10)


def laptop_alerts(limit=10):
    with _FINDMY_LOCK, closing(_findmy_db()) as db:
        rows = db.execute("SELECT t, kind, detail FROM events WHERE kind IN ('laptop_wrong_password', 'walk_away_lock', "
                          "'lost_report') ORDER BY t DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for t, kind, detail in rows:
        try:
            d = json.loads(detail)
        except ValueError:
            d = {}
        a = {'t': t, 'kind': 'wrong_password' if kind == 'laptop_wrong_password' else kind, 'name': d.get('name')}
        a.update({'place': d.get('place'), 'connected': d.get('connected')})
        if kind == 'lost_report':
            a.update({k: d.get(k) for k in ('networks', 'ip', 'isp', 'city', 'region', 'country')})
        out.append(a)
    return out


# ---- lost mode: when this computer is marked lost (from the phone) ----
# A thief won't take it anywhere the phone has been, so the Wi-Fi map can't place it. While it's
# marked lost, every 10 minutes (the first at once) it reports what can be learned anywhere: a
# webcam photo, the names of the Wi-Fi networks around it (people often recognise a cafe, a
# hostel, an office), and its internet provider and city from its public address (ipinfo.io,
# only while lost). It stays lost through restarts, until the phone says it's found.
LOST_EVERY = 600


def lost_state():
    cfg = load_config()
    return {'lost': bool(cfg.get('findmy_lost')), 'since': cfg.get('findmy_lost_since')}


def set_lost(on):
    cfg = load_config()
    was = bool(cfg.get('findmy_lost'))
    cfg['findmy_lost'] = bool(on)
    cfg['findmy_lost_since'] = time.time() if on and not was else (cfg.get('findmy_lost_since') if on else None)
    save_config(cfg)
    if on and not was:
        LOST_WAKE.set()                       # the first report now
    return lost_state()


def public_place():
    """{ip, isp, city, region, country} of this computer's internet address, or {}."""
    try:
        req = urllib.request.Request('https://ipinfo.io/json', headers={'Accept': 'application/json',
                                                                        'User-Agent': 'Orbit'})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        return {}
    org = str(d.get('org') or '')
    return {'ip': d.get('ip'), 'isp': re.sub(r'^AS\d+\s+', '', org) or None, 'city': d.get('city'),
            'region': d.get('region'), 'country': d.get('country')}


def lost_report():
    aps = laptop_wifi_scan(rescan=True)
    names = []
    for _, ssid, dbm, _ in sorted(aps, key=lambda a: -a[2]):
        if ssid and ssid not in names:
            names.append(ssid)
    place = public_place()
    photo = webcam_photo()
    found, connected, _ = laptop_locate(rescan=False)
    detail = {'name': photo, 'networks': names[:12], **place,
              'place': {'lat': found['lat'], 'lng': found['lng'], 'acc': found['acc']} if found else None,
              'connected': connected}
    findmy_event('lost_report', detail, notify=False)
    sys.stderr.write(f"Lost mode: report sent ({place.get('isp')}, {place.get('city')}; {len(names)} networks; "
                     f"{'photo' if photo else 'no photo'})\n")
    auth_mgr.broadcast_ndjson(json.dumps({"event": "laptop_alert", "kind": "lost_report", "t": time.time(),
                                          **{k: v for k, v in detail.items() if k != 'connected'}}) + "\n")


LOST_WAKE = threading.Event()


def lost_mode_loop():
    while True:
        LOST_WAKE.wait(timeout=LOST_EVERY)
        LOST_WAKE.clear()
        try:
            if lost_state()['lost']:
                lost_report()
        except Exception as e:
            sys.stderr.write(f"Lost mode: {e}\n")



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
            if not os.path.exists(dev_path) or _is_virtual_camera(dev_path):
                continue            # Orbit Cam is the phone itself: not a computer camera
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

def _is_virtual_camera(dev):
    return os.path.exists(os.path.join('/sys/devices/virtual/video4linux', os.path.basename(dev)))

def get_best_camera(preferred=''):
    if preferred and os.path.exists(preferred):
        return preferred
    devices = get_camera_devices()
    for d in devices:
        if 'hd webcam' in d['name'].lower() or 'webcam' in d['name'].lower() or 'camera' in d['name'].lower():
            if not _is_virtual_camera(d['device']):      # not Orbit Cam (the phone) or the like
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
        self.current_session = self._restore()
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
            self._persist()
        
        self.broadcast_sse("auth_request", self.current_session)
        app_notification = self.build_app_notification(self.current_session, topic="login")
        self.broadcast_ndjson(json.dumps(app_notification) + "\n")
        return self.current_session

    # The waiting sign-in is kept in a file too, so restarting this service (an update) in the
    # middle of an unlock doesn't lose it: the phone's approval still finds it
    _SESSION_FILE = os.path.expanduser('~/.cache/orbit/pending_auth.json')

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(self._SESSION_FILE), exist_ok=True)
            fd = os.open(self._SESSION_FILE + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(self.current_session, f)
            os.replace(self._SESSION_FILE + '.tmp', self._SESSION_FILE)
        except (OSError, TypeError, ValueError):
            pass

    @classmethod
    def _restore(cls):
        try:
            with open(cls._SESSION_FILE) as f:
                saved = json.load(f)
            if isinstance(saved, dict) and time.time() <= saved.get("expires_at", 0):
                return saved
        except (OSError, ValueError):
            pass
        return None

    def approve(self, session_id):
        with self.lock:
            if self.current_session and self.current_session["session_id"] == session_id:
                if time.time() <= self.current_session["expires_at"]:
                    self.current_session["status"] = "approved"
                    self._persist()
                    self.broadcast_sse("auth_cancelled", {"reason": "approved"})
                    self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "approved", "id": session_id}) + "\n")
                    return True
        return False

    def approve_current(self):
        with self.lock:
            if self.current_session and self.current_session["status"] == "pending":
                if time.time() <= self.current_session["expires_at"]:
                    self.current_session["status"] = "approved"
                    self._persist()
                    sid = self.current_session.get("session_id", "")
                    self.broadcast_sse("auth_cancelled", {"reason": "approved"})
                    self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "approved", "id": sid}) + "\n")
                    return True
        return False

    def deny(self, session_id):
        with self.lock:
            if self.current_session and self.current_session["session_id"] == session_id:
                self.current_session["status"] = "denied"
                self._persist()
                self.broadcast_sse("auth_cancelled", {"reason": "denied"})
                self.broadcast_ndjson(json.dumps({"event": "cancelled", "reason": "denied", "id": session_id}) + "\n")
                return True
        return False

    def deny_current(self):
        with self.lock:
            if self.current_session and self.current_session["status"] == "pending":
                self.current_session["status"] = "denied"
                self._persist()
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
        elif path == '/api/app/latest':
            client_info, token = authenticate_client(self, load_config())
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            sha, size = apk_print()
            self.send_json({"status": "ok", "sha256": sha, "size": size, "note": apk_note()})
            return
        elif path in ('/apk', '/PCAuthenticator.apk', '/authenticator.apk', '/download'):
            self.serve_apk()
            return

        # 3. Static Files
        elif path in ('/', '/index.html'):
            self.serve_file(os.path.join(STATIC_DIR, 'index.html'), 'text/html; charset=utf-8')
            return
        elif path == '/findmy' and qs.get('ltok'):
            # The tray opens /findmy?ltok=<local key>; trade it for an HttpOnly cookie (as /browse)
            ltok = qs.get('ltok', [''])[0]
            if (self.client_address[0] in ('127.0.0.1', '::1') and not is_tunnel_request(self)
                    and LOCAL_TOKEN and hmac.compare_digest(ltok, LOCAL_TOKEN)):
                self.send_response(303)
                self.send_header('Set-Cookie', f'pc_local={LOCAL_TOKEN}; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Location', '/findmy')
                self.end_headers()
            else:
                self.send_error(403)
            return
        elif path == '/findmy':
            self.serve_file(os.path.join(STATIC_DIR, 'findmy.html'), 'text/html; charset=utf-8')
            return
        elif path in ('/leaflet/leaflet.js', '/leaflet/leaflet.css'):
            self.serve_file(os.path.join(STATIC_DIR, path.lstrip('/')),
                            'application/javascript' if path.endswith('.js') else 'text/css')
            return
        elif path == '/api/findmy/phone':
            # Find my phone, on this computer only
            client_info, token = authenticate_client(self, load_config())
            if not client_info or not client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json(findmy_phone_view())
            return
        elif path == '/api/findmy/photo':
            client_info, token = authenticate_client(self, load_config())
            name = qs.get('name', [''])[0]
            if not client_info or not client_info.get('local') or not re.fullmatch(r'[0-9-]+-[0-9a-f]{6}\.jpg', name):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.serve_file(os.path.join(FINDMY_PHOTOS, name), 'image/jpeg')
            return
        elif path == '/api/findmy/laptop':
            # The phone asks where this computer is: a fresh look, and where it was last found
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            found, ssid, seen = laptop_locate(rescan=qs.get('fresh', [''])[0] == '1')
            self.send_json({"status": "ok", "now": found, "last": laptop_last(), "ssid": ssid,
                            "networks_seen": seen, "ringing": LAPTOP_RINGER.ringing(), "time": time.time(),
                            "alerts": laptop_alerts(), "settings": findmy_settings(), "lost": lost_state()})
            return
        elif path == '/api/findmy/laptop/photo':
            # the phone fetches a photo taken here after a wrong password
            client_info, token = authenticate_client(self, load_config())
            name = qs.get('name', [''])[0]
            if not client_info or client_info.get('local') or not re.fullmatch(r'laptop-[0-9-]+-[0-9a-f]{6}\.jpg', name):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.serve_file(os.path.join(FINDMY_PHOTOS, name), 'image/jpeg')
            return
        elif path == '/api/findmy/settings':
            client_info, token = authenticate_client(self, load_config())
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", **findmy_settings()})
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
            self.send_json(current_challenge_reply())
            return

        elif path == '/api/phone/sync':
            # Everything the app's open screen needs, in one request instead of one each:
            # ?parts=challenge,status,media (the phone asks for what its panel shows)
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            parts = set(qs.get('parts', [''])[0].split(','))
            reply = {"status": "ok", "keep_awake": keep_awake_active()}
            if 'challenge' in parts:
                reply['challenge'] = current_challenge_reply()
            if 'status' in parts:
                reply['pc'] = get_pc_system_status()
            if 'media' in parts:
                reply['media'] = get_mpris_status()
            self.send_json(reply)
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

        elif path == '/api/webcam/format':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info or not client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", "mode": WEBCAM_MODE,
                            "live": bool(ORBIT_CAM.live), "users": ORBIT_CAM.users()})
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
            if graphical_session() is None:
                self.send_json({"status": "error", "message": "Nobody is logged in on the computer yet."}, status=409)
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

        # Orbit Cam's format, switched from this computer's own Orbit window (not the phone)
        if path == '/api/webcam/format':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info or not client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            body = self.read_json()
            mode = str((body or {}).get('mode', '')).lower() if isinstance(body, dict) else ''
            self.send_json(ORBIT_CAM.switch_mode(mode))
            return

        # The phone's camera as this computer's webcam: H.264 from the phone, decoded into a
        # virtual camera (v4l2loopback) that video-call apps and browsers can pick
        if path == '/api/webcam/stream':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            if not is_lan_or_wifi_request(self):
                self.send_json({"status": "error", "message": "The phone camera works over Wi-Fi only."}, status=403)
                return
            try:
                rot = int(qs.get('rot', ['0'])[0]) % 360
            except ValueError:
                rot = 0
            codec = 'hevc' if qs.get('codec', [''])[0] == 'hevc' else 'h264'
            self.send_json(stream_phone_webcam(self, rot, codec))
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

        # Find my, the distance by sound: the phone's recording (raw audio, before the JSON read)
        if path in ('/api/findmy/sound/start', '/api/findmy/sound/finish'):
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            if is_tunnel_request(self):
                # the two chirps must land in each other's recording: too slow over the internet
                self.send_json({"status": "error", "message": "Works on the same Wi-Fi only."}, status=403)
                return
            try:
                import numpy  # noqa: F401
            except ImportError:
                self.send_json({"status": "error", "message": "Install python-numpy on the computer."}, status=501)
                return
            if path.endswith('/start'):
                self.read_json()
                SOUND_RANGER.start()
                self.send_json({"status": "ok", "window": SOUND_WINDOW})
            else:
                length = int(self.headers.get('Content-Length', 0) or 0)
                if not 0 < length <= 2 << 20:
                    self.send_json({"error": "too large"}, status=413)
                    return
                self.send_json(SOUND_RANGER.finish(self.rfile.read(length)))
            return

        # Find my: a photo from the phone (a raw JPEG, so before the body is read as JSON)
        if path == '/api/findmy/photo':
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            length = int(self.headers.get('Content-Length', 0) or 0)
            if not 0 < length <= 6 << 20:
                self.send_json({"error": "too large"}, status=413)
                return
            name = findmy_save_photo(self.rfile.read(length), self.headers.get('X-Orbit-Reason', ''))
            self.send_json({"status": "ok" if name else "error"})
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

        # Find my: reports and alerts from the phone
        elif path == '/api/findmy/report':
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            kept = findmy_record(body if isinstance(body, dict) else {})
            # the phone keeps where this computer was last, for when it can't reach it
            self.send_json({"status": "ok", "kept": kept, "laptop": laptop_last()})
            return
        elif path == '/api/findmy/event':
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            kind = str((body or {}).get('kind', '')) if isinstance(body, dict) else ''
            if kind not in ('sim_changed', 'sim_removed'):
                self.send_json({"error": "unknown event"}, status=400)
                return
            detail = body.get('detail') if isinstance(body.get('detail'), dict) else {}
            findmy_event(kind, {k: str(v)[:80] for k, v in list(detail.items())[:10]})
            self.send_json({"status": "ok"})
            return
        elif path == '/api/findmy/laptop/beacon':
            # From the phone's Nearby finder: this computer advertises Orbit's Bluetooth mark
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            on = bool((body if isinstance(body, dict) else {}).get('on'))
            self.send_json({"status": "ok", "advertising": ORBIT_BLE.advertise(on), "uuid": ORBIT_BLE_UUID})
            return
        elif path == '/api/findmy/laptop/ring':
            # From the phone: ring this computer
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            on = bool((body if isinstance(body, dict) else {}).get('on'))
            LAPTOP_RINGER.start() if on else LAPTOP_RINGER.silence()
            self.send_json({"status": "ok", "ringing": on})
            return
        elif path == '/api/findmy/wifi':
            # From the phone: the Wi-Fi it sees now. How alike is it to what this computer sees?
            # (the nearby finder on either side, and the walk-away lock)
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            b = body if isinstance(body, dict) else {}
            purpose = str(b.get('purpose', ''))
            phone_raw = b.get('wifi') or []
            mine = laptop_scan_now(8 if purpose in ('nearby', 'find_phone', 'radar') else 30)
            theirs = phone_scan(phone_raw)
            score = wifi_likeness(theirs, mine)
            extra = {}
            if purpose != 'presence':
                sys.stderr.write(f"Nearby by Wi-Fi: {score if score is None else round(score, 2)}, "
                                 f"{len(set(theirs) & set(mine))} of {len(theirs)}/{len(mine)} networks in common\n")
                # both placed on the Wi-Fi map the same way, and how far apart
                phone_at, laptop_at = fingerprint_locate(theirs), fingerprint_locate(mine)
                extra = {'phone_at': phone_at, 'laptop_at': laptop_at}
                if phone_at and laptop_at:
                    extra['apart'] = round(_metres(phone_at['lat'], phone_at['lng'], laptop_at['lat'], laptop_at['lng']))

            # Wi-Fi identity recognition and distance estimation
            common_aps, signal_gap = _wifi_identity_match(phone_raw, theirs, mine)
            apart_val = extra.get('apart')
            if apart_val is None:
                gps_dist, p_lat, p_lng, l_lat, l_lng = last_known_gps_distance()
                if gps_dist is not None:
                    extra['apart'] = gps_dist
                    apart_val = gps_dist
                    if not extra.get('phone_at') and p_lat is not None:
                        extra['phone_at'] = {'lat': p_lat, 'lng': p_lng, 'acc': 20}
                    if not extra.get('laptop_at') and l_lat is not None:
                        extra['laptop_at'] = {'lat': l_lat, 'lng': l_lng, 'acc': 30}

            est_dist = wifi_distance_estimate(score, signal_gap, len(common_aps), apart_val)

            identity_info = {
                'match_percent': round((score or 0) * 100) if score is not None else 0,
                'common_count': len(common_aps),
                'signal_gap': round(signal_gap, 1) if signal_gap is not None else None,
                'distance': est_dist,
                'total_phone': len(theirs),
                'total_laptop': len(mine),
                'shared_aps': common_aps[:20]
            }

            if purpose == 'presence':
                WALK_AWAY.scan(score)
            elif purpose == 'find_phone':
                NEARBY.update(t=time.time(), score=score, distance=est_dist)
            self.send_json({"status": "ok", "score": None if score is None else round(score, 3),
                            "word": likeness_word(score), "networks": len(mine),
                            "distance": est_dist, "identity": identity_info, **extra})
            return
        elif path == '/api/findmy/settings':
            client_info, token = authenticate_client(self, load_config())
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", **set_findmy_settings(body if isinstance(body, dict) else {})})
            return
        elif path == '/api/findmy/capabilities':
            # what the phone can time distances with (radio time-of-flight); logged to decide how to range
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            b = body if isinstance(body, dict) else {}
            sys.stderr.write("Phone ranging capabilities: " + json.dumps({k: b.get(k) for k in (
                'model', 'android', 'wifi_rtt', 'wifi_rtt_on', 'uwb', 'rtt_access_points', 'access_points_seen')}) + "\n")
            self.send_json({"status": "ok"})
            return
        elif path == '/api/findmy/lost':
            # From the phone: mark this computer lost (or found)
            client_info, token = authenticate_client(self, load_config())
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"status": "ok", **set_lost(bool((body if isinstance(body, dict) else {}).get('on')))})
            return
        elif path in ('/api/findmy/phone/locate', '/api/findmy/phone/ring', '/api/findmy/phone/nearby'):
            # From the Find my phone page on this computer
            client_info, token = authenticate_client(self, load_config())
            if not client_info or not client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            on = bool((body if isinstance(body, dict) else {}).get('on', True))
            if path.endswith('/locate'):
                auth_mgr.broadcast_ndjson(json.dumps({"event": "locate", "time": int(time.time())}) + "\n")
            elif path.endswith('/ring'):
                ring_phone() if on else unring_phone()
            else:
                # the phone sends its Wi-Fi every few seconds meanwhile (find_phone), for 3 minutes
                NEARBY['until'] = time.time() + 180 if on else 0.0
                NEARBY['score'] = None
                auth_mgr.broadcast_ndjson(json.dumps({"event": "wifi_nearby", "on": on}) + "\n")
                # and by Bluetooth, if the phone's is on: it advertises, this computer listens
                ORBIT_BLE.listen(on)
                auth_mgr.broadcast_ndjson(json.dumps({"event": "ble_beacon", "on": on, "uuid": ORBIT_BLE_UUID}) + "\n")
            self.send_json({"status": "ok", "phone_connected": bool(auth_mgr.ndjson_clients)})
            return

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
            session = graphical_session()
            if act in ('lock', 'unlock') and session is None:
                self.send_json({"status": "error", "message": "Nobody is logged in on the computer yet."}, status=409)
                return
            if act == 'lock':
                subprocess.Popen(['loginctl', 'lock-session', session])
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
                subprocess.Popen(['loginctl', 'unlock-session', session])
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
            enabled = bool(body.get('enabled'))
            set_keep_awake(enabled)
            # remembered, so a restart of this service (an update) doesn't quietly let the
            # computer sleep: the block dies with the old process
            cfg = load_config()
            if bool(cfg.get('keep_awake')) != enabled:
                cfg['keep_awake'] = enabled
                save_config(cfg)
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

        # Phone notifications shown on the desktop (and taken away when they go on the phone)
        elif path == '/api/phone/notification':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info or client_info.get('local'):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json(show_phone_notification(body if isinstance(body, dict) else {}))

        # 9b. Telephony / Call Event (Auto-pause media on call)
        elif path == '/api/telephony/call':
            cfg = load_config()
            client_info, token = authenticate_client(self, cfg)
            if not client_info:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            state = body.get('state', '').lower()
            handle_telephony_call_state(state, generic_notice=not body.get('notifications'))
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
    start_webcam_standby()
    threading.Thread(target=laptop_locator_loop, daemon=True, name='find-my-laptop').start()
    threading.Thread(target=app_update_watch, daemon=True, name='app-update').start()
    threading.Thread(target=WALK_AWAY.loop, daemon=True, name='walk-away').start()
    threading.Thread(target=wrong_password_watch, daemon=True, name='wrong-password').start()
    threading.Thread(target=lost_mode_loop, daemon=True, name='lost-mode').start()
    if lost_state()['lost']:
        LOST_WAKE.set()                  # still lost after a restart: report right away
    if load_config().get('keep_awake'):
        set_keep_awake(True)            # it was on before this service restarted

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

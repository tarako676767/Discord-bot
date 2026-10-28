import sys, io, time, json, base64, hmac, hashlib, urllib.parse, secrets, argparse, datetime, re, socket
import requests, msgpack
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization

import os as _os, builtins as _bi
try:
    _DBG_LOG = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tsum_debug.log"),
                    "a", encoding="utf-8", errors="replace")
except Exception:
    _DBG_LOG = None
def print(*a, **k):
    if _DBG_LOG is None:
        return
    k["file"] = _DBG_LOG; k["flush"] = True
    _bi.print(*a, **k)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AUTH_BASE = "https://game-api.line.me"
GAME_BASE = "https://lgtmtm-game.linegame.jp/"
APP_ID    = "LGTMTM"
SDK_VER   = "3.12.1.422"
def _device_id():
    import uuid
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "device_id.json")
    try:
        if _os.path.exists(p):
            v = (json.load(open(p, encoding="utf-8-sig")) or {}).get("device_id")
            if v:
                return v
    except Exception:
        pass
    v = uuid.uuid4().hex
    try:
        _jdump_atomic(p, {"device_id": v})
    except Exception:
        pass
    return v


DEVICE_ID = _device_id()
MCC, MNC  = "440", "00"
UA        = "android;9;PQ3B_190801_04221524;GOOGLEPLAY;ja"
PKG = "com.linecorp.LGTMTM"
# Play ストア取得に失敗したときの保険値。
# 実際に API が通ったバージョンは appver.json の "good" に自動記録され、そちらが優先される。
APPVER_FALLBACK = "12.9.2"
RESVER, MSTVER = "12.6.0", "1.0.0"
RESVER_FALLBACK, MSTVER_FALLBACK = RESVER, MSTVER
_VER_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "appver.json")
_VER_TTL = 86400


def _jdump_atomic(path, obj, **kw):
    """一時ファイルに書いてから置き換える（書き込み中に落ちても元ファイルが壊れない）。"""
    kw.setdefault("ensure_ascii", False)
    folder = _os.path.dirname(path)
    if folder:
        _os.makedirs(folder, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, _os.getpid())   # プロセスごとに別名にして衝突を避ける
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, **kw)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, path)
    except Exception:
        try:
            if _os.path.exists(tmp):
                _os.remove(tmp)      # 失敗したら中途半端な一時ファイルを残さない
        except Exception:
            pass
        raise


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


# getPublicKey は 99.0.0 でも public_key を返すためバージョン検証にならない。
# それに依存していた _server_accepts / _bump_until_ok / _find_floor は
# 誤った判断のもとになるので削除した。実際に通ったバージョンは
# mark_versions_good() で appver.json の "good" に記録される。


def _play_appver():
    import re
    try:
        r = requests.get(
            "https://play.google.com/store/apps/details?id=%s&hl=ja&gl=JP" % PKG,
            headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0.0.0 Safari/537.36"),
                     "Accept-Language": "ja"},
            timeout=12)
        m = re.search(r'\[\[\["(\d+\.\d+\.\d+)"\]\]', r.text)
        return m.group(1) if m else None
    except Exception:
        return None


def _cache_read_all():
    try:
        d = json.load(open(_VER_FILE, encoding="utf-8-sig"))
        if time.time() - float(d.get("ts", 0)) < _VER_TTL and d.get("appver"):
            return d
    except Exception:
        pass
    return None


def _cache_read():
    d = _cache_read_all()
    return d["appver"] if d else None


def _cache_write(v, resver=None, mstver=None):
    try:
        try:
            d = json.load(open(_VER_FILE, encoding="utf-8-sig")) or {}
        except Exception:
            d = {}
        d.update({"appver": v,
                  "resver": resver or RESVER,
                  "mstver": mstver or MSTVER,
                  "ts": time.time()})
        # "good"(実働記録)は消さずに残す
        _jdump_atomic(_VER_FILE, d)
    except Exception:
        pass


def resver_candidates(appver=None):
    """resver をサーバーに弾かれたとき(retcode 201)に試す候補。
    resver は appver と別系統なので、現行値 → appver と同値 → appver の近傍 の順で試す。"""
    av = appver or APPVER
    a, b, c = (list(_ver_tuple(av)) + [0, 0, 0])[:3]
    cands = [RESVER, RESVER_FALLBACK, av, "%d.%d.0" % (a, b)]
    if b > 0:
        cands.append("%d.%d.0" % (a, b - 1))
    cands.append("%d.%d.0" % (a, b + 1))
    out = []
    for v in cands:
        if v and v not in out:
            out.append(v)
    return out


def apply_versions(appver=None, resver=None, mstver=None, cache=True):
    """appver/resver/mstver をまとめて差し替える (cache=True で appver.json に保存)。"""
    global APPVER, RESVER, MSTVER
    if appver:
        APPVER = appver
    if resver:
        RESVER = resver
    if mstver:
        MSTVER = mstver
    if cache:
        _cache_write(APPVER, RESVER, MSTVER)
    return APPVER, RESVER, MSTVER


def current_versions():
    return APPVER, RESVER, MSTVER


def fresh_socks_identity(url):
    """SOCKSプロキシURLに新しいランダムな user:pass を埋め込んで返す。
    Tor は SOCKS認証情報が違うと別の出口回線(≒別IP)を割り当てる
    (IsolateSOCKSAuth、Torの既定で有効)。呼ぶたびに新しい値になるので、
    リトライのたびに呼べば「たまたま繋がりの悪い出口ノードに当たって
    何度リトライしても同じ理由で失敗し続ける」事態を避けられる。
    SOCKS以外のURL(例: 固定のHTTPプロキシ)はそのまま返す(対象外)。"""
    if not url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if scheme not in ("socks5", "socks5h", "socks4", "socks4a"):
        return url
    host = rest.rsplit("@", 1)[-1]      # 既に user:pass@ が付いていれば捨てて新規発行する
    ident = _os.urandom(8).hex()
    return f"{scheme}://{ident}:{ident}@{host}"


def known_good():
    """実際に API が retcode 0 を返した (appver, resver, mstver)。無ければ None。"""
    try:
        d = json.load(open(_VER_FILE, encoding="utf-8-sig")) or {}
    except Exception:
        return None
    g = d.get("good")
    if isinstance(g, dict) and g.get("appver"):
        return g
    return None


def mark_versions_good(appver=None, resver=None, mstver=None):
    """API が実際に通った組み合わせを appver.json に記録する。
    getPublicKey はどんなバージョンでも通ってしまい検証にならないので、
    『本当に通った』証拠はこれだけが頼りになる。"""
    av = appver or APPVER
    rv = resver or RESVER
    mv = mstver or MSTVER
    try:
        try:
            d = json.load(open(_VER_FILE, encoding="utf-8-sig")) or {}
        except Exception:
            d = {}
        g = d.get("good") or {}
        if (g.get("appver"), g.get("resver"), g.get("mstver")) == (av, rv, mv):
            return                      # 変化なしなら書かない
        d["good"] = {"appver": av, "resver": rv, "mstver": mv, "ts": time.time()}
        _jdump_atomic(_VER_FILE, d)
        print(f"[ver] 実働バージョンを記録: appver={av} resver={rv} mstver={mv}")
    except Exception:
        pass


def resolve_appver(force=False):
    global APPVER, RESVER, MSTVER
    if not force:
        d = _cache_read_all()
        if d:
            APPVER = d["appver"]
            RESVER = d.get("resver") or RESVER
            MSTVER = d.get("mstver") or MSTVER
            return APPVER
    # getPublicKey は 99.0.0 でも通ってしまい検証にならないので、
    # 「実際に API が通った値(known_good)」を土台にして、それ以上のものだけ採用する。
    good = known_good() or {}
    base = good.get("appver") or globals().get("APPVER") or APPVER_FALLBACK
    if good.get("resver"):
        RESVER = good["resver"]
    if good.get("mstver"):
        MSTVER = good["mstver"]

    play = _play_appver()
    if play and _ver_tuple(play) >= _ver_tuple(base):
        APPVER = play                       # Play ストアの最新（＝実機と同じ値）
    else:
        if play:
            print(f"[ver] Playストア({play})が実働値({base})より古いため採用しません")
        else:
            print(f"[ver] Playストアの取得に失敗 → 実働値 {base} を使います")
        APPVER = base
    _cache_write(APPVER)
    return APPVER


_VER_CACHE0 = _cache_read_all()
APPVER = (_VER_CACHE0 or {}).get("appver") or APPVER_FALLBACK
RESVER = (_VER_CACHE0 or {}).get("resver") or RESVER
MSTVER = (_VER_CACHE0 or {}).get("mstver") or MSTVER
del _VER_CACHE0
TERMINAL  = DEVICE_ID
TERMS_RESULT = "%7B%22agreements%22%3A%5B%7B%22termsId%22%3A%22LGTMTM_Privacy%22%2C%22country%22%3A%22JP%22%2C%22language%22%3A%22ja%22%2C%22revisionDate%22%3A%2220231001%22%2C%22agree%22%3Atrue%7D%2C%7B%22termsId%22%3A%22LGTMTM_UoI%22%2C%22country%22%3A%22JP%22%2C%22language%22%3A%22ja%22%2C%22revisionDate%22%3A%2220241021%22%2C%22agree%22%3Atrue%7D%2C%7B%22termsId%22%3A%22LGTMTM_term%22%2C%22country%22%3A%22JP%22%2C%22language%22%3A%22ja%22%2C%22revisionDate%22%3A%2220231001%22%2C%22agree%22%3Atrue%7D%5D%7D"


def url_encode_key(s: str) -> str:
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        out.append(c if (c.isalnum() or c in "-._~") else f"%{b:02X}")
    return "".join(out)

def trident_sign(body_bytes: bytes):
    ts_ms = str(int(time.time())) + "000"
    msg   = f"trident&{APP_ID}&{ts_ms}"
    key   = url_encode_key(msg).encode("utf-8")
    sig   = hmac.new(key, body_bytes, hashlib.sha256).digest()
    si    = base64.b64encode(sig).decode()
    return ts_ms, si

def auth_headers(body_bytes: bytes):
    ts_ms, si = trident_sign(body_bytes)
    now = datetime.datetime.now().astimezone()
    return {
        "X-Linegame-DeviceId": DEVICE_ID,
        "X-Linegame-AppId": APP_ID,
        "Content-Type": "application/x-msgpack",
        "X-Linegame-Authorization": f'ts="{ts_ms}", si="{urllib.parse.quote(si, safe="")}"',
        "User-Agent": UA,
        "X-Linegame-Timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.000%z"),
        "X-Linegame-SdkVersion": SDK_VER,
        "X-Linegame-MCC": MCC,
        "X-Linegame-MNC": MNC,
    }


def trident_authenticate(line_jwt: str, use_refresh=False, proxies=None):
    if use_refresh:
        path = "/auth/v3.8/refresh"
        payload = {"providerId": "LINE", "accessToken": line_jwt, "country": "JP"}
    else:
        path = "/auth/v3.8/authentication/LINE"
        payload = {"accessToken": line_jwt, "termsResult": TERMS_RESULT, "country": "JP"}
    body = msgpack.packb(payload, use_bin_type=False)
    hdr = auth_headers(body)
    print(f"\n[auth] POST {path}  (body {len(body)}B)")
    print(f"  X-Linegame-Authorization: {hdr['X-Linegame-Authorization']}")
    r = requests.post(AUTH_BASE + path, data=body, headers=hdr, proxies=proxies if proxies is not None else _creds_proxy(), timeout=20)
    print(f"  -> HTTP {r.status_code} ({len(r.content)}B)  CT={r.headers.get('Content-Type')}")
    text = r.content.decode("utf-8", "replace")
    print(f"  resp: {text[:400]}")
    try:
        j = json.loads(r.content)
        return j
    except Exception:
        try:
            return msgpack.unpackb(r.content, raw=False)
        except Exception:
            return {"_raw": text, "_status": r.status_code}


def growthyinfo():
    ts = datetime.datetime.now().strftime("%Y%m%d %H%M%S")
    return json.dumps({
        "sdkVersion":"3.0","osVer":"9","terminalId":TERMINAL,
        "deviceName":"SM-A805N","country":"JP","language":"ja",
        "networkType":2,"carrier":"440/00","clientTimestamp":ts
    }, separators=(",",":"))

def qenc(v): return urllib.parse.quote(str(v), safe="")
def build_form(pairs): return "&".join(f"{k}={qenc(v)}" for k, v in pairs)
def aes_gcm_encrypt(pt, key):
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, pt.encode("utf-8"), None)
def aes_gcm_decrypt(data, key):
    return AESGCM(key).decrypt(data[:12], data[12:], None).decode("utf-8")
def rsa_oaep(key, pub_b64):
    pub = serialization.load_der_public_key(base64.b64decode(pub_b64))
    enc = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                        algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(enc).decode()

def game_login(user_token: str, proxies=None):
    s = requests.Session()
    s.headers.update({"User-Agent": "tsumtsum/%s" % APPVER})
    _px = proxies if proxies is not None else _creds_proxy()
    if _px: s.proxies.update(_px)
    aeskey = secrets.token_bytes(32)
    r = s.get(GAME_BASE + "api/getPublicKey.nhn?appver=" + APPVER, timeout=15)
    j = r.json()
    if not (j.get("public_key") or j.get("publicKey")) and j.get("retcode") == 201:
        resolve_appver(force=True)
        s.headers.update({"User-Agent": "tsumtsum/%s" % APPVER})
        r = s.get(GAME_BASE + "api/getPublicKey.nhn?appver=" + APPVER, timeout=15)
        j = r.json()
    pub = j.get("public_key") or j.get("publicKey"); kid = str(j.get("kid",""))
    print(f"\n[login] getPublicKey {r.status_code} kid={kid}")
    common = [
        ("userid",""),("appver",APPVER),("resver",RESVER),("mstver",MSTVER),
        ("hash",""),("os","2"),("lang","ja"),("checkval",""),
        ("countrycode","JP"),("growthyinfo",growthyinfo()),
    ]
    pairs = [("rankdt",0),("accesstoken",user_token),("trigger",0)] + common
    body = build_form(pairs)
    enc = aes_gcm_encrypt(body, aeskey)
    hdr = {"Content-Type":"application/tsum",
           "X-Encrypted-Session-Key": rsa_oaep(aeskey, pub), "X-Key-Id": kid}
    r = s.post(GAME_BASE + "login.nhn", data=enc, headers=hdr, timeout=20)
    print(f"[login] login.nhn -> HTTP {r.status_code} ({len(r.content)}B)")
    try:
        dec = aes_gcm_decrypt(r.content, aeskey)
        print(f"[login] resp: {dec[:600]}")
        return json.loads(dec)
    except Exception as e:
        print(f"[login] decrypt fail: {e}  raw={r.content[:120]}")
        return None


def extract_jwt_from_mitm(path):
    from mitmproxy.io import FlowReader
    with open(path, "rb") as f:
        for flow in FlowReader(f).stream():
            if not getattr(flow, "request", None): continue
            if "/auth/v" in flow.request.pretty_url:
                obj = msgpack.unpackb(flow.request.content, raw=False)
                return obj.get("accessToken"), ("/refresh" in flow.request.pretty_url)
    return None, False


LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_UA = "com.linecorp.LGTMTM/12.6.1 ChannelSDK/5.11.1 (Linux; U; Android 9; ja-JP; SM-A805N Build/PQ3B.190801.04221524)"
TOKENS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "line_tokens_latest.json")
LAST_ERROR = ""

# --- Tor プロキシ (PayPay を除く全通信で共有) -----------------------------
# bot_config.json の tor_enabled / tor_socks_url を読む。
# PayPay は tsum_bot.py が別プロセス(python_realip)で叩くため、ここには一切関与しない。
_BOT_CONFIG_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "bot_config.json")
_TOR_CACHE = {"ts": 0.0, "proxies": False}   # False=未取得, None=無効, dict=有効
_TOR_CACHE_TTL = 5.0   # 秒。設定変更後すぐ反映されるよう短めにしつつ、毎回のファイル読みは避ける


def _tor_proxies_from_config():
    env = _os.environ.get("TSUM_PROXY_URL", "").strip()
    if env:
        return {"http": env, "https": env}
    try:
        d = json.load(open(_BOT_CONFIG_FILE, encoding="utf-8-sig"))
    except Exception:
        return None
    if not d.get("tor_enabled"):
        return None
    url = str(d.get("tor_socks_url") or "").strip()
    return {"http": url, "https": url} if url else None


_PROXY_STATE = {"warned_down": False, "warned_up": False}


def _proxy_reachable(url, timeout=3.0):
    """プロキシのポートに実際にTCP接続できるかを見る。
    Tor/Privoxyを入れていないホスティング環境でも動かせるように、
    「設定はされているが起動していない」場合を検出するために使う。"""
    m = re.match(r"^[a-z0-9]+://(?:[^@/]*@)?([^:/]+):(\d+)", url or "", re.I)
    if not m:
        return False
    host, port = m.group(1), int(m.group(2))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _creds_proxy():
    """PayPay 以外の全通信(LINEログイン・ツムツムAPI・引き継ぎ)で使う既定プロキシ。
    明示的に proxy= / proxies= が渡されたときはこちらより優先される
    (呼び出し側で `if proxies is not None else _creds_proxy()` という形になっている)。"""
    now = time.time()
    if _TOR_CACHE["proxies"] is not False and (now - _TOR_CACHE["ts"] < _TOR_CACHE_TTL):
        return _TOR_CACHE["proxies"]
    px = _tor_proxies_from_config()
    if px:
        url = px.get("https") or px.get("http")
        if _proxy_reachable(url):
            if _PROXY_STATE["warned_down"] and not _PROXY_STATE["warned_up"]:
                print(f"[proxy] {url} に接続できるようになりました。プロキシ経由に戻します。")
                _PROXY_STATE["warned_up"] = True
                _PROXY_STATE["warned_down"] = False
        else:
            # 設定はされているが起動していない(ホスティング環境等)。
            # 死んだプロキシを使うと全通信が失敗するので、直接接続にフォールバックする。
            if not _PROXY_STATE["warned_down"]:
                print(f"[proxy] {url} に接続できません。プロキシ無し(直接接続)で動作します。")
                _PROXY_STATE["warned_down"] = True
                _PROXY_STATE["warned_up"] = False
            px = None
    _TOR_CACHE["proxies"] = px
    _TOR_CACHE["ts"] = now
    return px

def line_refresh(refresh_token, client_id="1381161556"):
    r = requests.post(LINE_TOKEN_URL,
                      data={"grant_type":"refresh_token","refresh_token":refresh_token,"client_id":client_id},
                      headers={"User-Agent":LINE_UA, "Content-Type":"application/x-www-form-urlencoded"},
                      proxies=_creds_proxy(), timeout=20)
    if r.status_code != 200:
        print(f"[line_refresh] HTTP {r.status_code}: {r.text[:160]}")
        return None
    return r.json()

def save_tokens(tok):
    _jdump_atomic(TOKENS_FILE, tok, indent=2)

CREDENTIALS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "line_credentials.json")

def idpw_login():
    global LAST_ERROR
    LAST_ERROR = ""
    import os
    if not os.path.exists(CREDENTIALS_FILE):
        LAST_ERROR = "アカウント情報が入力されていません。"
        return False
    try:
        cred = json.load(open(CREDENTIALS_FILE, encoding="utf-8-sig"))
        import line_password_login
        print("[token] ID/PW ログイン(access.line.me)を試行…")
        out = line_password_login.password_login(cred["id"], cred["password"], save=True, verbose=True)
        if not (out and out.get("access_token")):
            LAST_ERROR = getattr(line_password_login, "LAST_ERROR", "") or "ID/PWログインに失敗しました。"
        return bool(out and out.get("access_token"))
    except Exception as e:
        print(f"[token] ID/PWログイン失敗: {e}")
        LAST_ERROR = "ID/PWログインでエラーが発生しました。"
        return False

def obtain_user_token(force_idpw=False):
    global LAST_ERROR
    LAST_ERROR = ""
    import os
    if force_idpw:
        if idpw_login():
            tok2 = json.load(open(TOKENS_FILE, encoding="utf-8-sig"))
            res = trident_authenticate(tok2["access_token"], use_refresh=False)
            if isinstance(res, dict) and res.get("userToken"):
                return res
        else:
            print(f"[token] ID/PWログイン不可({CREDENTIALS_FILE} 無し or 失敗)。")
            if not LAST_ERROR:
                LAST_ERROR = "ID/PWログインに失敗しました。"
        return None

    tok = json.load(open(TOKENS_FILE, encoding="utf-8-sig")) if os.path.exists(TOKENS_FILE) else {}
    at, rt, cid = tok.get("access_token"), tok.get("refresh_token"), tok.get("client_id","1381161556")
    if at:
        res = trident_authenticate(at, use_refresh=False)
        if isinstance(res, dict) and res.get("userToken"):
            print("[token] 保存済み access_token は有効。")
            return res
        print("[token] access_token 失効 → refresh_token で更新を試行…")
    if rt:
        j = line_refresh(rt, cid)
        if j and j.get("access_token"):
            tok["access_token"] = j["access_token"]
            tok["refresh_token"] = j.get("refresh_token", rt)
            tok["captured_ts"] = int(time.time()); tok["expires_in"] = j.get("expires_in")
            save_tokens(tok)
            print("[token] refresh 成功。新トークン保存。再 auth…")
            res = trident_authenticate(j["access_token"], use_refresh=False)
            if isinstance(res, dict) and res.get("userToken"):
                return res
            print("[token] 新 access_token でも auth 失敗。")
        else:
            print("[token] refresh 失効 → ID/PW 再ログインを試行…")
    if idpw_login():
        tok2 = json.load(open(TOKENS_FILE, encoding="utf-8-sig"))
        res = trident_authenticate(tok2["access_token"], use_refresh=False)
        if isinstance(res, dict) and res.get("userToken"):
            return res
    else:
        print(f"[token] ID/PW再ログイン不可({CREDENTIALS_FILE} 無し or 失敗)。")
        if not LAST_ERROR:
            LAST_ERROR = "ID/PW再ログインに失敗しました。"
    return None


SESSION_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tsum_session_headless.json")

_UNSET = object()      # game_proxy 未指定 = proxy と同じものを使う(従来の挙動)


def headless_session_direct(login_id, password, proxy=None, captcha_solver=None, save=False, verbose=True,
                            error_box=None, pin_notice=None, pin_wait_timeout=240, game_proxy=_UNSET):
    global LAST_ERROR
    LAST_ERROR = ""
    if error_box is not None:
        error_box["error"] = ""
    def fail(reason):
        global LAST_ERROR
        LAST_ERROR = reason
        if error_box is not None:
            error_box["error"] = reason
        return None

    # LINE側(ログイン・認証)とゲーム側(ツムツム本体)で経路を分けられるようにする。
    # ゲーム側は1件で数百〜数千回叩くため、Torを通すと極端に遅くなる。
    # 一方、お客様のLINEに届く「ログインしました」通知のIPはLINE側のものなので、
    # LINE側だけTorを通せば、IPを毎回変えるという目的は達成できる。
    proxies = {"http": proxy, "https": proxy} if proxy else None
    _gp = proxy if game_proxy is _UNSET else game_proxy
    game_proxies = {"http": _gp, "https": _gp} if _gp else None

    try:
        import line_password_login
        line_error = {}
        tok = line_password_login.password_login(
            login_id,
            password,
            save=False,
            verbose=verbose,
            proxies=proxies,
            captcha_solver=captcha_solver,
            error_box=line_error,
            pin_notice=pin_notice,
            pin_wait_timeout=pin_wait_timeout,
        )
        if not (tok and tok.get("access_token")):
            return fail(line_error.get("error") or "ID/PWログインに失敗しました。")
    except Exception as e:
        _m = str(e)
        _is_net = isinstance(e, requests.exceptions.RequestException)
        if _is_net:
            # 通信エラーは想定内(Torの出口が弾かれた等)。呼び出し元が別回線で
            # 再試行するので、トレースバックは出さず1行だけ残す。
            print(f"[login] 通信エラー: {type(e).__name__}: {_m[:150]}")
            if "通信エラー" in _m:
                return fail(_m)          # 既に分かりやすい文面ならそのまま使う
            _kind = "接続先に到達できません" if ("proxy" in _m.lower() or "Tunnel connection failed" in _m) else type(e).__name__
            return fail(f"通信エラーでログインできませんでした（{_kind}）")
        # 想定外の例外は原因究明のためトレースバックを残す
        import traceback as _tb
        _tb.print_exc()
        return fail(f"ID/PWログインでエラーが発生しました。（{type(e).__name__}: {_m[:80]}）")

    res = trident_authenticate(tok["access_token"], use_refresh=False, proxies=proxies)
    if not isinstance(res, dict) or not res.get("userToken"):
        return fail("userTokenの取得に失敗しました。")

    j = game_login(res["userToken"], proxies=game_proxies)
    if not isinstance(j, dict) or j.get("retcode") != 0:
        if verbose: print(f"[headless] login.nhn 失敗 retcode={j.get('retcode') if isinstance(j,dict) else j}")
        return fail("ツムツムログインに失敗しました。")

    ui = j.get("userinfo") or {}
    sess = {
        "userid":   ui.get("userid") or j.get("userid"),
        "hash":     j.get("hash") or ui.get("hash"),
        "checkval": ui.get("checkval") or j.get("checkval"),
        "session_id": ui.get("session_id"),
        "userToken": res.get("userToken"),
        "userKey":   res.get("userKey"),
        "ts": int(time.time()),
    }
    if save:
        _jdump_atomic(SESSION_FILE, sess, indent=2)
    return sess

def headless_session(save=True, verbose=True, force_idpw=False):
    global LAST_ERROR
    res = obtain_user_token(force_idpw=force_idpw)
    if not isinstance(res, dict) or not res.get("userToken"):
        if verbose: print("[headless] userToken 取得失敗。mitm 再捕捉が必要。")
        if not LAST_ERROR:
            LAST_ERROR = "userTokenの取得に失敗しました。"
        return None
    j = game_login(res["userToken"])
    if not isinstance(j, dict) or j.get("retcode") != 0:
        if verbose: print(f"[headless] login.nhn 失敗 retcode={j.get('retcode') if isinstance(j,dict) else j}")
        return None
    ui = j.get("userinfo") or {}
    sess = {
        "userid":   ui.get("userid") or j.get("userid"),
        "hash":     j.get("hash") or ui.get("hash"),
        "checkval": ui.get("checkval") or j.get("checkval"),
        "session_id": ui.get("session_id"),
        "userToken": res.get("userToken"),
        "userKey":   res.get("userKey"),
        "ts": int(time.time()),
    }
    if save and sess["userid"] and sess["hash"] and sess["checkval"]:
        json.dump(sess, open(SESSION_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
        if verbose: print(f"[headless] session 保存 -> {SESSION_FILE}")
    return sess


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true",
                    help="保存済み line_tokens_latest.json から自動(access→refresh fallback)。通常はこれ。")
    ap.add_argument("--jwt", help="LINE access JWT (eyJ...) を直接指定")
    ap.add_argument("--from-mitm", help="mitm捕捉ファイルから JWT を抽出してテスト")
    ap.add_argument("--refresh", action="store_true", help="/refresh を使う(既存ユーザ)")
    ap.add_argument("--no-login", action="store_true", help="auth/v3.8 のみ(login.nhnしない)")
    args = ap.parse_args()

    if args.auto:
        if args.no_login:
            res = obtain_user_token()
            if not isinstance(res, dict) or not res.get("userToken"):
                print("\n[中断] userToken 取得失敗。mitm 再捕捉を。"); raise SystemExit(1)
            print(f"\n[result] userKey={res.get('userKey')}")
            print(f"[result] userToken={str(res.get('userToken'))[:60]}...")
            raise SystemExit(0)
        sess = headless_session()
        if not sess:
            print("\n[中断] ヘッドレスログイン失敗。エミュで line_capture_addon.py を使い再捕捉を。")
            raise SystemExit(1)
        print(f"\n[session] userid={sess['userid']}")
        print(f"[session] checkval={sess['checkval']}")
        print(f"[session] hash={sess['hash']}")
        print(f"\n→ tsum_forge.py をそのまま実行すれば、この session を自動で使います。")
        raise SystemExit(0)

    jwt = args.jwt
    use_refresh = args.refresh
    if args.from_mitm and not jwt:
        jwt, use_refresh = extract_jwt_from_mitm(args.from_mitm)
        print(f"[mitm] 抽出 JWT={str(jwt)[:40]}...  refresh={use_refresh}")
    if not jwt:
        print("LINE JWT が必要です (--auto / --jwt / --from-mitm)"); raise SystemExit(1)

    res = trident_authenticate(jwt, use_refresh=use_refresh)
    user_token = res.get("userToken") if isinstance(res, dict) else None
    user_key   = res.get("userKey")   if isinstance(res, dict) else None
    print(f"\n[result] userKey={user_key}")
    print(f"[result] userToken={str(user_token)[:60]}{'...' if user_token else ''}")

    if not user_token:
        print("\n[判定] userToken 取れず。HTTP/応答を確認。")
        print("  - 署名エラー(invalid signature 等)なら HMAC実装の問題")
        print("  - token expired / invalid なら LINE JWT が失効(=HMACは正常、要・新JWT)")
        raise SystemExit(1)

    if args.no_login:
        raise SystemExit(0)

    j = game_login(user_token)
    if isinstance(j, dict):
        ui = j.get("userinfo") or {}
        print(f"\n[session] retcode={j.get('retcode')}")
        print(f"[session] userid={ui.get('userid') or j.get('userid')}")
        print(f"[session] checkval={j.get('checkval') or ui.get('checkval')}")
        print(f"[session] hash候補は login応答/後続通信から。tsum_forge.py に渡してください。")

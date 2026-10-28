import sys, io, os, re, json, time, base64, hashlib, secrets, urllib.parse, argparse, html
import requests
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import builtins as _bi
try:
    _DBG_LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsum_debug.log"),
                    "a", encoding="utf-8", errors="replace")
except Exception:
    _DBG_LOG = None
def _jdump_atomic(path, obj, **kw):
    """一時ファイルに書いてから置き換える（書き込み中に落ちても元ファイルが壊れない）。"""
    kw.setdefault("ensure_ascii", False)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())   # プロセスごとに別名にして衝突を避ける
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, **kw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)      # 失敗したら中途半端な一時ファイルを残さない
        except Exception:
            pass
        raise


def print(*a, **k):
    if _DBG_LOG is None:
        return
    k["file"] = _DBG_LOG; k["flush"] = True
    _bi.print(*a, **k)

CHANNEL_ID = "1381161556"
REDIRECT_URI = "intent://result#Intent;package=com.linecorp.LGTMTM;scheme=lineauth;end"
SCOPE = "friends message.write profile trident.user.key"
UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line_tokens_latest.json")

def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def rsa_pkcs1v15_hex(msg: bytes, n_hex: str, e_hex: str) -> str:
    n = int(n_hex, 16); e = int(e_hex, 16)
    k = (n.bit_length() + 7) // 8
    if len(msg) > k - 11:
        raise ValueError("message too long for RSA modulus")
    ps = bytearray()
    while len(ps) < k - 3 - len(msg):
        b = secrets.token_bytes(1)
        if b != b"\x00": ps += b
    eb = b"\x00\x02" + bytes(ps) + b"\x00" + msg
    c = pow(int.from_bytes(eb, "big"), e, n)
    return format(c, "x").zfill(k * 2)

CAPTCHA_SOLVER = None
LAST_ERROR = ""

def _creds_proxy():
    """PayPay 以外の既定プロキシ(Tor等)。設定の実体は tsum_login.py 側の1箇所にまとめてある。
    tsum_login が本モジュールを遅延importする構造(循環import回避)に合わせ、ここでも遅延importする。"""
    try:
        import tsum_login
        return tsum_login._creds_proxy()
    except Exception:
        return None

def _parse_noauto_config(page_html):
    t = html.unescape(page_html or "")
    i = t.find('"channel-id"')
    if i < 0:
        return {}
    start = t.rfind("{", 0, i + 1)
    depth = 0
    end = None
    for j in range(start, min(len(t), start + 16000)):
        c = t[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    blob = t[start:end] if end else ""
    try:
        return json.loads(blob)
    except Exception:
        return dict(re.findall(r'"([a-z0-9\-]+)"\s*:\s*"([^"]*)"', blob))


def _poll_pin(s, wait_url, pin_verifier, timeout, log):
    from requests import exceptions as _rexc
    full = wait_url if wait_url.startswith("http") else "https://access.line.me" + wait_url
    headers = {
        "X-Line-Login-Verifier": pin_verifier,
        "If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://access.line.me/",
        "Accept": "application/json, text/plain, */*",
    }
    deadline = time.time() + timeout
    polls = 0
    hard_err = 0
    while time.time() < deadline:
        polls += 1
        try:
            r = s.get(full, headers=headers, timeout=(10, 40))
        except (_rexc.ProxyError, _rexc.ConnectionError, _rexc.Timeout, _rexc.ChunkedEncodingError):
            continue
        except Exception as e:
            hard_err += 1
            log(f"   [pin poll] 想定外エラー({hard_err}): {type(e).__name__}")
            if hard_err > 8:
                return False
            time.sleep(2)
            continue
        st = r.status_code
        if st in (408, 410, 504):
            continue
        if 400 <= st <= 499:
            log(f"   [pin poll] HTTP {st} → 中断")
            return False
        if st == 200:
            try:
                j = r.json()
            except Exception:
                j = {}
            data = j.get("data") if isinstance(j, dict) else None
            if (isinstance(j, dict) and j.get("result") is True) or \
               (isinstance(data, dict) and data.get("result") is True):
                log(f"   [pin poll] 承認確認(polls={polls})")
                return True
            time.sleep(1.5)
            continue
        time.sleep(1.5)
    log(f"   [pin poll] タイムアウト(polls={polls})")
    return False


def password_login(login_id, password, dry_run=False, save=True, verbose=True, captcha_text=None, proxies=None, captcha_solver=None, error_box=None, pin_notice=None, pin_wait_timeout=180):
    global LAST_ERROR
    LAST_ERROR = ""
    if error_box is not None:
        error_box["error"] = ""
    def log(*a):
        if verbose: print(*a)
    def fail(reason):
        global LAST_ERROR
        LAST_ERROR = reason
        if error_box is not None:
            error_box["error"] = reason
        return None
    if proxies is None:
        proxies = _creds_proxy()
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ja"})
    if proxies:
        s.proxies.update(proxies); log(f"[proxy] {proxies['https']} 経由")

    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(12))[:16]
    consent = ("/oauth2/v2.1/authorize/consent?response_type=code"
               f"&client_id={CHANNEL_ID}&state={state}"
               f"&code_challenge={challenge}&code_challenge_method=S256"
               f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
               f"&sdk_ver=5.11.1&scope={urllib.parse.quote(SCOPE)}&bot_prompt=normal")
    login_url = ("https://access.line.me/oauth2/v2.1/login?returnUri="
                 f"{urllib.parse.quote(consent, safe='')}"
                 f"&loginChannelId={CHANNEL_ID}&ui_locales=ja")

    html = s.get(login_url, timeout=20).text
    def flag(name):
        m = re.search(re.escape(name) + r'&quot;\s*:\s*(true|false)', html)
        return m.group(1) if m else "?"
    is_captcha = flag("is-captcha"); is_recaptcha = flag("is-recaptcha")
    mcsrf = re.search(r'__csrf&quot;\s*,\s*&quot;value&quot;\s*:\s*&quot;([^&]+)&quot;', html)
    csrf = mcsrf.group(1) if mcsrf else s.cookies.get("X-SCGW-CSRF-Token")
    mcap = re.search(r'captcha-url&quot;\s*:\s*&quot;([^&]+)&quot;', html)
    captcha_key = mcap.group(1).rstrip("/").split("/")[-1] if mcap else ""
    captcha_url = mcap.group(1) if mcap else ""
    log(f"[1] login page: is-captcha={is_captcha} is-recaptcha={is_recaptcha} csrf={csrf} captchaKey={captcha_key}")
    if is_recaptcha == "true":
        log("× reCAPTCHA(Google)要求。これは打鍵では解けない=自動ログイン不可。"
            " 別IP/しばらく時間を置く/mitm捕捉に切替を。")
        return fail("reCAPTCHAが要求されました。別IPに変えるか、時間を置いてください。")
    captcha_relayed = False
    if is_captcha == "true":
        if not captcha_text:
            img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line_captcha.png")
            img_bytes = b""
            _img_err = ""
            for _cap_try in range(3):
                try:
                    img = s.get("https://access.line.me" + captcha_url, headers={"Referer": login_url}, timeout=20)
                    img_bytes = img.content
                    if img_bytes:
                        _img_err = ""
                        break
                    _img_err = "空の応答"
                except Exception as e:
                    img_bytes = b""
                    _img_err = f"{type(e).__name__}: {e}"
                log(f"   CAPTCHA画像の取得失敗(試行{_cap_try+1}/3): {_img_err}")
                if _cap_try < 2:
                    time.sleep(0.5)
            if not img_bytes:
                # 空(0バイト)の画像を人間に見せても読めないので、ここで確実に止める。
                # 一度でも captcha_solver(=人にCAPTCHA入力を頼む処理)を呼んでしまうと
                # 呼び出し元の自動リトライ(別のTor回線への切替)が働かなくなるため、
                # 呼ぶ前に失敗として返す。
                # 「通信エラー」という語を含めておくと、呼び出し元(forge_sync)の
                # 自動リトライ判定にひっかかり、別のTor回線に切り替えて再試行される。
                return fail(f"CAPTCHA画像の取得に失敗しました（通信エラー: {_img_err}）。もう一度お試しください。")
            if not captcha_solver:
                try:
                    open(img_path, "wb").write(img_bytes)
                    log(f"△ 画像CAPTCHA出現。画像を保存 → {img_path}")
                except Exception:
                    pass
            solver = captcha_solver or CAPTCHA_SOLVER
            if solver:
                captcha_relayed = True
                try:
                    captcha_text = (solver(img_bytes) or "").strip()
                except Exception as e:
                    log(f"   CAPTCHA_SOLVER エラー: {e}"); captcha_text = ""
            elif sys.stdin and sys.stdin.isatty():
                log("   その画像を開いて文字を読み、下のプロンプトに入力してください(この実行のまま)。")
                try:
                    captcha_text = input("   CAPTCHAの文字を入力(Enterで中止): ").strip()
                except Exception:
                    captcha_text = ""
            else:
                log("   ※非対話実行では入力できません。自分のターミナル/Discord botで実行してください。")
            if not captcha_text:
                return fail("CAPTCHAが入力されませんでした。")
        log(f"[1c] captcha 文字='{captcha_text}' を送信に含めます。")

    user_id = hashlib.md5(login_id.encode()).hexdigest()
    def lc(x): return bytes([len(x)])

    def _get_json(url, params, what, tries=3):
        """LINEのJSON APIを叩く。Torの出口が弾かれると空応答やHTMLが返り
        .json()がJSONDecodeErrorで落ちるので、数回やり直す。
        それでも駄目なら「通信エラー」を含む文言で失敗させ、呼び出し元
        (forge_sync)が別のTor回線に切り替えて再試行できるようにする。"""
        last = ""
        for i in range(tries):
            body = ""
            try:
                r = s.get(url, params=params, headers={"Referer": login_url}, timeout=20)
                body = (r.text or "")[:80]
                return r.json()
            except Exception as e:
                last = type(e).__name__
                log(f"   {what}の取得に失敗({i+1}/{tries}): {last} body={body!r}")
                if i < tries - 1:
                    time.sleep(0.5)
        raise requests.exceptions.RequestException(
            f"{what}を取得できませんでした（通信エラー: {last}）")

    session_key = _get_json("https://access.line.me/oauth2/v2.1/authn/session",
                            {"_": str(int(time.time()*1000))}, "セッションキー").get("result")
    if not session_key:
        return fail("LINEのセッションキーを取得できませんでした（通信エラー）。もう一度お試しください。")
    rsa_key = _get_json("https://access.line.me/oauth2/v2.1/authn/keys/line",
                        {"sessionId": session_key, "_": str(int(time.time()*1000))},
                        "暗号化キー").get("rsa_key")
    if not rsa_key or "," not in str(rsa_key):
        return fail("LINEの暗号化キーを取得できませんでした（通信エラー）。もう一度お試しください。")
    keyname, n_hex, e_hex = rsa_key.split(",")
    log(f"[2/3] session_key={session_key[:24]}... keyId={keyname} n_len={len(n_hex)//2}B e={e_hex}")
    msg = lc(session_key)+session_key.encode() + lc(login_id)+login_id.encode() + lc(password)+password.encode()
    enc_pw = rsa_pkcs1v15_hex(msg, n_hex, e_hex)
    log(f"[enc] userId=md5(id)={user_id}  password={len(enc_pw)} hex chars")
    loc = ""; body = ""; r = None
    MAX_CAP_ROUNDS = 1
    for _cap_try in range(MAX_CAP_ROUNDS + 1):
        form = {
            "userId": user_id, "encUserId": "", "id": keyname, "password": enc_pw,
            "idProvider": "1", "sessionId": session_key, "encKeyId": keyname,
            "loginChannelId": CHANNEL_ID, "returnUri": consent, "displayType": "M",
            "captchaKey": captcha_key, "__csrf": csrf, "lang": "ja",
        }
        if captcha_text:
            form["captcha"] = captcha_text
        if dry_run:
            log("[dry-run] authenticate は送信しません。form 準備完了。")
            return fail("dry-runのためログイン送信していません。")
        r = s.post("https://access.line.me/oauth2/v2.1/authenticate", data=form,
                   headers={"Referer": login_url, "Origin": "https://access.line.me",
                            "Content-Type": "application/x-www-form-urlencoded"},
                   allow_redirects=False, timeout=20)
        loc = r.headers.get("location", "")
        log(f"[4] authenticate(round {_cap_try}) -> HTTP {r.status_code}  Location: {loc[:130]}")
        if "CAPTCHA_APPLY" in loc and _cap_try < MAX_CAP_ROUNDS and not captcha_relayed:
            solver = captcha_solver or CAPTCHA_SOLVER
            cap_url = ""
            if solver:
                try:
                    na = s.get(loc if loc.startswith("http") else "https://access.line.me" + loc,
                               headers={"Referer": login_url}, timeout=20)
                    cap_url = (_parse_noauto_config(na.text) or {}).get("captcha-url") or ""
                except Exception as _e:
                    log(f"   [446] noauto取得失敗: {_e}")
            if solver and cap_url:
                txt = ""
                try:
                    img = s.get("https://access.line.me" + cap_url, headers={"Referer": login_url}, timeout=20).content
                    captcha_relayed = True
                    txt = (solver(img) or "").strip()
                except Exception as _e:
                    log(f"   [446] CAPTCHA解決失敗: {_e}")
                if txt:
                    captcha_text = txt
                    captcha_key = cap_url.rstrip("/").split("/")[-1]
                    log(f"[446] {_cap_try + 1}枚目 CAPTCHA='{captcha_text}' を付けて再authenticate")
                    continue
            log("   [446] CAPTCHA処理不可(solver/画像なし or 未入力) → そのまま返す")
        break
    body = r.text if (r is not None and r.status_code == 200 and r.headers.get("content-type", "").startswith("text/html")) else ""
    def body_flag(name):
        m = re.search(re.escape(name) + r'&quot;\s*:\s*(true|false)', body)
        return m.group(1) if m else "false"
    pin_in_loc = ("pincode=" in loc and "verifier=" in loc) or "/pincode" in loc
    if pin_in_loc or (body and "pincode" in body.lower()):
        noauto_url = loc if loc.startswith("http") else "https://access.line.me" + loc
        try:
            pr = s.get(noauto_url, headers={"Referer": login_url}, timeout=20)
            cfg = _parse_noauto_config(pr.text)
        except Exception as _e:
            log(f"   [pin] noauto-login取得失敗: {_e}")
            cfg = {}
        q = dict(re.findall(r'[?&]([^=&]+)=([^&]+)', loc))
        pincode = cfg.get("pincode") or urllib.parse.unquote(q.get("pincode", ""))
        pin_verifier = cfg.get("verifier") or urllib.parse.unquote(q.get("verifier", ""))
        wait_url = cfg.get("wait-pin-code-verification-api-url") or "/qrlogin/oauth2/v2.1/authn/pin/wait"
        authorize_url = cfg.get("authorize-page-url") or "/oauth2/v2.1/authenticate"
        if not (pincode and pin_verifier):
            return fail("PIN(端末認証)が要求されましたが、PINコードの取得に失敗しました。")
        log(f"△ PIN(端末認証)要求。pincode={pincode} 提示→承認ポーリング開始。")
        if pin_notice is None:
            return fail(f"PIN認証が必要です。LINEアプリで {pincode} を入力してください。")
        try:
            pin_notice(pincode)
        except Exception as _e:
            log(f"   [pin] pin_notice失敗: {_e}")
        if not _poll_pin(s, wait_url, pin_verifier, pin_wait_timeout, log):
            return fail("PIN認証がタイムアウト/未承認でした。LINEアプリで番号を入力してから、もう一度お試しください。")
        full_auth = authorize_url if authorize_url.startswith("http") else "https://access.line.me" + authorize_url
        _confirm = dict(form)
        _confirm["verifier"] = pin_verifier
        rf = s.post(full_auth, data=_confirm,
                    headers={"Referer": noauto_url, "Origin": "https://access.line.me",
                             "Content-Type": "application/x-www-form-urlencoded"},
                    allow_redirects=False, timeout=20)
        loc = rf.headers.get("location", "")
        log(f"[pin] 承認後 authenticate -> HTTP {rf.status_code} Location: {loc[:130]}")
    else:
        if body and (body_flag("is-recaptcha") == "true"):
            log("△ authenticate後に reCAPTCHA にエスカレーション。打鍵不可。"
                " → VPNを別サーバへ繋ぎ替えて再試行(ロックはされません)。")
            return fail("CAPTCHA送信後にreCAPTCHAへ切り替わりました。別IPに変えてください。")
        if body and (body_flag("is-captcha") == "true"):
            log("△ authenticate後に画像CAPTCHAを要求。--captcha 無しで再試行(対話で打鍵)するか"
                " VPNサーバ変更を。(ロックはされません)")
            return fail("CAPTCHAが正しくないか、LINE側がもう一度画像CAPTCHAを要求しました。")
        if "errorCode" in loc or "noauto-login" in loc or "login_fail" in loc.lower():
            m = re.search(r'errorCode=(\d+)', loc); ec = m.group(1) if m else "?"
            if ec in ("445",):
                log(f"× ログイン失敗 errorCode={ec}(ID/PW誤りの可能性)。連発するとロックの恐れ。一旦停止。")
                return fail("アカウント情報が違う可能性があります。連続試行は避けてください。")
            else:
                log(f"× ログイン中断 errorCode={ec}。")
                if "CAPTCHA_APPLY" in loc:
                    return fail(f"画像認証(CAPTCHA)に失敗しました。もう一度お試しください。errorCode={ec}")
                return fail(f"LINE側でログインが中断されました。追加認証または一時制限の可能性があります。errorCode={ec}")

    def find_code(u):
        m = re.search(r"[?&]code=([^&#]+)", u or "")
        return urllib.parse.unquote(m.group(1)) if m else None

    code = find_code(loc)
    consent_url = loc if loc.startswith("http") else "https://access.line.me" + loc
    if not code:
        rc = s.get(consent_url, headers={"Referer": login_url}, allow_redirects=False, timeout=20)
        if rc.status_code in (301, 302):
            code = find_code(rc.headers.get("location", ""))
        else:
            mc = re.search(r'__csrf&quot;\s*,\s*&quot;value&quot;\s*:\s*&quot;([^&]+)&quot;', rc.text)
            ccsrf = mc.group(1) if mc else (s.cookies.get("X-SCGW-CSRF-Token") or csrf)
            cdata = [("addFriend", "true")]
            for p in ("P", "F", "M", "TUK"):
                cdata += [("allPermission", p), ("approvedPermission", p)]
            cdata += [("channelId", CHANNEL_ID), ("addFriendMode", "ADD_MODE"),
                      ("__csrf", ccsrf), ("lang", "ja"), ("allow", "true")]
            rp = s.post("https://access.line.me/oauth2/v2.1/authorize/consent", data=cdata,
                        headers={"Referer": consent_url, "Origin": "https://access.line.me",
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        allow_redirects=False, timeout=20)
            code = find_code(rp.headers.get("location", ""))
    if not code:
        log("△ consent から code 取得失敗。")
        return fail("LINE認可コードの取得に失敗しました。追加認証または一時制限の可能性があります。")
    log(f"[5] authorization code 取得: {code[:24]}...")

    tok = s.post("https://api.line.me/oauth2/v2.1/token",
                        data={"grant_type": "authorization_code", "code": code,
                              "redirect_uri": REDIRECT_URI, "client_id": CHANNEL_ID,
                              "code_verifier": verifier},
                        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
                        timeout=20)
    log(f"[6] token -> HTTP {tok.status_code}: {tok.text[:160]}")
    if tok.status_code != 200:
        return fail(f"LINEトークン取得に失敗しました。HTTP {tok.status_code}")
    j = tok.json()
    out = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", ""),
           "client_id": CHANNEL_ID, "captured_ts": int(time.time()), "expires_in": j.get("expires_in")}
    if save:
        _jdump_atomic(TOKENS_FILE, out, indent=2)
        log(f"\n★ ID/PWログイン成功! token を {TOKENS_FILE} に保存。")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="LINEログインID (email or phone)")
    ap.add_argument("--password", required=True, help="LINEパスワード")
    ap.add_argument("--dry-run", action="store_true", help="authenticate直前まで(送信しない)")
    ap.add_argument("--captcha", default=None, help="画像CAPTCHAが出た場合の文字(line_captcha.pngを見て入力)")
    ap.add_argument("--proxy", default=None, help="プロキシURL(例 http://host:port)。IP切替用")
    args = ap.parse_args()
    px = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    out = password_login(args.id, args.password, dry_run=args.dry_run, captcha_text=args.captcha, proxies=px)
    if out:
        print("   → python tsum_login.py --auto でゲームログインまで通せます。")

if __name__ == "__main__":
    main()

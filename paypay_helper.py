import sys, os, json, re, asyncio

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

CRED = os.path.join(_HERE, "paypay_credential.json")

_GET_URL = "https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo"
_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def emit(ok, msg, **extra):
    print(json.dumps({"ok": bool(ok), "msg": msg, **extra}, ensure_ascii=True))
    sys.exit(0)


def _extract(raw):
    m = re.search(r"https?://(?:pay\.paypay\.ne\.jp|paypay\.ne\.jp)/[^\s]+", raw or "")
    return m.group(0) if m else (raw or "").strip()


def _code_of(link):
    return (link or "").replace("https://pay.paypay.ne.jp/", "").replace("https://paypay.ne.jp/", "").strip()


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


def _load_creds(token_file):
    if not token_file or not os.path.exists(token_file):
        return None
    try:
        return json.load(open(token_file, encoding="utf-8-sig"))
    except Exception:
        return None


def _direct_getinfo(code, token=None):
    import requests
    h = {"Accept": "application/json, text/plain, */*", "User-Agent": _UA, "Content-Type": "application/json"}
    ck = {"token": token} if token else None
    r = requests.get(_GET_URL, headers=h, params={"verificationCode": code}, cookies=ck, timeout=15)
    try:
        j = r.json()
    except Exception:
        j = None
    return r.status_code, j


def _already_received(link, creds):
    tok = (creds or {}).get("access_token")
    if not tok:
        return False, "no_token", None
    code = _code_of(link)
    if not code:
        return False, "no_code", None
    try:
        status, j = _direct_getinfo(code, token=tok)
    except Exception as e:
        return False, f"exc:{type(e).__name__}", None
    if status != 200 or not isinstance(j, dict):
        return False, f"http:{status}", None
    p = j.get("payload", {}) or {}
    if p.get("orderStatus") != "SUCCESS":
        return False, f"orderStatus:{p.get('orderStatus')}", None
    recv = (p.get("receiver") or {}).get("externalId")
    mine = (creds or {}).get("external_id")
    if mine and recv and recv != mine:
        return False, f"receiver_mismatch:{recv}", None
    amt = (p.get("pendingP2PInfo") or {}).get("amount")
    return True, "already_received", amt


async def _link_info(link):
    import paypayu
    try:
        info = await paypayu.check_link(link)
        if info:
            return info, "PENDING"
    except Exception:
        pass
    code = _code_of(link)
    if not code:
        return None, "INVALID"
    try:
        status, j = await asyncio.to_thread(_direct_getinfo, code)
    except Exception:
        return None, "CHECK_FAILED"
    if status != 200 or not isinstance(j, dict):
        return None, "CHECK_FAILED"
    if j.get("header", {}).get("resultCode") != "S0000":
        return None, "INVALID"
    if j.get("payload", {}).get("orderStatus") != "PENDING":
        return None, "NOT_PENDING"
    return j, "PENDING"


def _amount_of(info):
    return (info.get("payload", {}).get("message", {}).get("data", {}) or {}).get("amount")


async def _link_rev(link, creds, token_file):
    import paypayu
    phone = creds.get("phone")
    password = creds.get("password")
    uuid_str = creds.get("uuid") or creds.get("client_uuid")
    saved = creds.get("access_token")
    new_token = None
    result = None

    if saved:
        try:
            result, returned = await paypayu.link_rev(link, phone, password, uuid_str, access_token=saved)
            if returned:
                new_token = returned
        except Exception:
            result = "TOKEN_EXPIRED"

    if result != "HELD" and result is not True:
        try:
            result, returned = await paypayu.link_rev(link, phone, password, uuid_str)
            if returned:
                new_token = returned
        except Exception:
            return "ERROR"

    if new_token:
        creds["access_token"] = new_token.get("access_token") if isinstance(new_token, dict) else new_token
        if isinstance(new_token, dict) and new_token.get("refresh_token"):
            creds["refresh_token"] = new_token.get("refresh_token")
        try:
            _jdump_atomic(token_file, creds, indent=2)
        except Exception:
            pass
    return result


async def do_check(link, amount):
    link = _extract(link)
    info, state = await _link_info(link)
    if state == "CHECK_FAILED":
        emit(False, "PayPayリンクの確認に失敗しました。少し待って再度お試しください。")
    if state == "INVALID":
        emit(False, "PayPayリンクが無効です。")
    if state == "NOT_PENDING":
        emit(False, "PayPayリンクは期限切れか、すでに受け取り済みです。")
    total = _amount_of(info)
    if total is None:
        emit(False, "PayPayリンクの金額を確認できませんでした。")
    if int(total) < int(amount):
        emit(False, f"金額が足りません。必要額は ¥{int(amount):,} です。")
    emit(True, "")


async def do_receive(link, token_file):
    link = _extract(link)
    creds = _load_creds(token_file)
    if not creds or not creds.get("phone") or not creds.get("password") or not (creds.get("uuid") or creds.get("client_uuid")):
        emit(False, "PayPay受取アカウントが未設定です。")
    got, _why, _amt = await asyncio.to_thread(_already_received, link, creds)
    if got:
        emit(True, f"{int(_amt)}円を受け取りました" if _amt is not None else "")
    result = await _link_rev(link, creds, token_file)
    if result is True:
        emit(True, "")
    if result == "HELD":
        emit(False, "受け取り一時保留です。PayPayアプリで取引を承認してください。", held=True)
    if result == "LOGINERR":
        emit(False, "PayPay受取アカウントの再ログインが必要です。")
    if result == "ERROR":
        emit(False, "PayPay受取に失敗しました。")
    emit(False, "PayPayリンクは期限切れか、すでに受け取り済みです。")


async def do_verify_receive(link, price, token_file):
    link = _extract(link)
    if not link:
        emit(False, "PayPay送金リンクを入力してください")
    creds = _load_creds(token_file)
    if not creds or not creds.get("phone"):
        emit(False, "PayPayの設定がありません（オーナーに連絡してください）")
    got, _why, _amt = await asyncio.to_thread(_already_received, link, creds)
    if got:
        if _amt is None:
            emit(False, "受け取り済みですが金額を確認できませんでした。オーナーにご連絡ください。")
        if int(_amt) < int(price):
            emit(False, f"金額が足りません（必要 {int(price)}円 / 受取 {int(_amt)}円）")
        emit(True, f"{int(_amt)}円を受け取りました")
    info, state = await _link_info(link)
    if state == "CHECK_FAILED":
        emit(False, "PayPayリンクの確認に失敗しました。少し待って再度お試しください。")
    if state == "INVALID":
        emit(False, "PayPayリンクが無効です")
    if state == "NOT_PENDING":
        emit(False, "PayPayリンクは期限切れか、すでに受け取り済みです")
    amount = _amount_of(info)
    if amount is None:
        emit(False, "金額を取得できませんでした")
    if int(amount) < int(price):
        emit(False, f"金額が足りません（必要 {int(price)}円 / 送金 {int(amount)}円）")
    result = await _link_rev(link, creds, token_file)
    if result == "LOGINERR":
        emit(False, "PayPayログインに失敗しました（オーナーに連絡してください）")
    if result == "HELD":
        emit(False, "受け取り一時保留です。PayPayアプリで取引を承認してください。", held=True)
    if result is not True:
        emit(False, "PayPayの受取に失敗しました（リンクが既に使われていないか確認してください）")
    emit(True, f"{int(amount)}円を受け取りました")


def main():
    if len(sys.argv) < 2:
        emit(False, "PayPay処理に失敗しました。")
    cmd = sys.argv[1]

    if len(sys.argv) < 3:
        emit(False, "PayPay処理に失敗しました。")
    link = sys.argv[2]
    if cmd == "check":
        asyncio.run(do_check(link, sys.argv[3] if len(sys.argv) > 3 else "0"))
    elif cmd == "receive":
        asyncio.run(do_receive(link, sys.argv[3] if len(sys.argv) > 3 else ""))
    elif cmd == "verify_receive":
        asyncio.run(do_verify_receive(link,
                                      sys.argv[3] if len(sys.argv) > 3 else "0",
                                      sys.argv[4] if len(sys.argv) > 4 else ""))
    elif cmd in ("login_start", "login_otp"):
        emit(False, "このコマンドは未対応です。再ログインは _pp_relogin_start.py を使ってください。")
    else:
        emit(False, "PayPay処理に失敗しました。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"[helper] fatal: {type(e).__name__}: {e}", file=sys.stderr)
        emit(False, "PayPay処理に失敗しました。")

import os, re, json, base64, hashlib, secrets, urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))

ACCOUNTS_FILE = os.path.join(HERE, "line_accounts.json")
CREDS_FILE    = os.path.join(HERE, "line_credentials.json")
TOKENS_FILE   = os.path.join(HERE, "line_tokens_latest.json")

import tsum_login


def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        return json.load(open(ACCOUNTS_FILE, encoding="utf-8-sig"))
    return {}


def load_creds():
    if os.path.exists(CREDS_FILE):
        try:
            return json.load(open(CREDS_FILE, encoding="utf-8-sig"))
        except Exception:
            return {}
    return {}


def current_label():
    cred = load_creds()
    cid = cred.get("id", "")
    for name, a in load_accounts().items():
        if a.get("id") == cid:
            return a.get("label", name), cid
    return (cid if cid else "(未設定)"), cid


def tsum_status_check_token():
    try:
        if not os.path.exists(TOKENS_FILE):
            return "none", "保存トークンなし"
        tok = json.load(open(TOKENS_FILE, encoding="utf-8-sig"))
        at = tok.get("access_token")
        if not at:
            return "none", "access_token空"
        res = tsum_login.trident_authenticate(at, use_refresh=False)
        if isinstance(res, dict) and res.get("userToken"):
            return "ok", "有効"
        return "expired", "失効"
    except Exception as e:
        return "error", f"確認失敗({type(e).__name__})"


UA_WEB = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")
CHANNEL_ID = "1381161556"
REDIRECT_URI = "intent://result#Intent;package=com.linecorp.LGTMTM;scheme=lineauth;end"
SCOPE = "friends message.write profile trident.user.key"


def _login_url():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.quote
    ret = ("/oauth2/v2.1/authorize/consent?response_type=code&client_id=" + CHANNEL_ID
           + "&state=stchk12345678&code_challenge=" + challenge
           + "&code_challenge_method=S256&redirect_uri=" + q(REDIRECT_URI, safe="")
           + "&sdk_ver=5.11.1&scope=" + q(SCOPE) + "&bot_prompt=normal")
    return ("https://access.line.me/oauth2/v2.1/login?returnUri=" + q(ret, safe="")
            + "&loginChannelId=" + CHANNEL_ID + "&ui_locales=ja")


def check_captcha(timeout=20):
    try:
        r = requests.get(_login_url(),
                         headers={"User-Agent": UA_WEB, "Accept-Language": "ja"},
                         timeout=timeout)
        if r.status_code != 200 or "is-captcha" not in r.text:
            return "?", "?"

        def flag(name):
            m = re.search(re.escape(name) + r"&quot;\s*:\s*(true|false)", r.text)
            return m.group(1) if m else "?"

        return flag("is-captcha"), flag("is-recaptcha")
    except Exception:
        return "?", "?"


def public_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=15).text.strip()
    except Exception:
        return "?"

import sys, json, asyncio
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
import os as _os0
sys.path.insert(0, _os0.path.dirname(_os0.path.abspath(__file__)))
import paypayu

import os as _os
otp = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
_HERE = _os.path.dirname(_os.path.abspath(__file__))
TOKEN_FILE = sys.argv[2] if len(sys.argv) > 2 else _os.path.join(_HERE, "paypay_token.json")
if not _os.path.isabs(TOKEN_FILE):
    TOKEN_FILE = _os.path.join(_HERE, TOKEN_FILE)
_tag = _os.path.splitext(_os.path.basename(TOKEN_FILE))[0]
STATE_FILE = _os.path.join(_HERE, f"_pp_relogin_state_{_tag}.json")
if not _os.path.exists(STATE_FILE):
    print("OTP_FAIL: ログイン保留状態がありません。先にログイン開始をやり直してください"); sys.exit(1)
st = json.load(open(STATE_FILE, encoding="utf-8-sig"))

data = asyncio.run(paypayu.login_otp(st["client_uuid"], otp, st["otp_reference_id"], st["otp_prefix"]))
if data == "ERR" or not isinstance(data, dict) or not data.get("access_token"):
    print(f"OTP_FAIL: コードが違うか期限切れ (resp={str(data)[:160]})"); sys.exit(1)

t = json.load(open(TOKEN_FILE, encoding="utf-8-sig"))
t["phone"] = st["phone"]; t["password"] = st["password"]; t["uuid"] = st["client_uuid"]
t["access_token"] = data["access_token"]
if data.get("refresh_token"): t["refresh_token"] = data["refresh_token"]
import time; t["captured_ts"] = int(time.time())
json.dump(t, open(TOKEN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("RELOGIN_OK: 新トークン保存完了")
print("  refresh_token:", "保存した" if data.get("refresh_token") else "応答に無し")
try: import os; os.remove(STATE_FILE)
except Exception: pass

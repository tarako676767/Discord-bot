import sys, json, asyncio, uuid as _uuid
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
import os as _os0
sys.path.insert(0, _os0.path.dirname(_os0.path.abspath(__file__)))
import paypayu

import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
TOKEN_FILE = sys.argv[1] if len(sys.argv) > 1 else _os.path.join(_HERE, "paypay_token.json")
if not _os.path.isabs(TOKEN_FILE):
    TOKEN_FILE = _os.path.join(_HERE, TOKEN_FILE)
_tag = _os.path.splitext(_os.path.basename(TOKEN_FILE))[0]
STATE_FILE = _os.path.join(_HERE, f"_pp_relogin_state_{_tag}.json")

_arg_phone = sys.argv[2] if len(sys.argv) > 2 else ""
_arg_pw = sys.argv[3] if len(sys.argv) > 3 else ""
if _os.path.exists(TOKEN_FILE):
    t = json.load(open(TOKEN_FILE, encoding="utf-8-sig"))
else:
    t = {}
if _arg_phone and _arg_pw:
    t["phone"], t["password"] = _arg_phone, _arg_pw
    _os.makedirs(_os.path.dirname(TOKEN_FILE) or ".", exist_ok=True)
    json.dump(t, open(TOKEN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
phone, password = t.get("phone"), t.get("password")
if not (phone and password):
    print("LOGIN_START_FAIL: 電話番号/パスワードがありません(初回は引数で渡してください)"); sys.exit(1)
cu = str(_uuid.uuid4())

data = asyncio.run(paypayu.login(phone, password, cu))
if not isinstance(data, dict):
    print("LOGIN_START_FAIL: 応答がdictでない"); sys.exit(1)
if data.get("response_type") == "ErrorResponse":
    print("LOGIN_ERROR: 電話番号またはパスワードが違う可能性"); sys.exit(1)

state = {"client_uuid": cu, "phone": phone, "password": password,
         "otp_reference_id": data.get("otp_reference_id"),
         "otp_prefix": data.get("otp_prefix") or ""}

if data.get("access_token"):
    t["access_token"] = data["access_token"]
    if data.get("refresh_token"): t["refresh_token"] = data["refresh_token"]
    t["uuid"] = cu
    json.dump(t, open(TOKEN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("DIRECT_OK: OTP不要でトークン取得・保存完了(refresh_token有:", bool(data.get("refresh_token")), ")")
    sys.exit(0)

json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("OTP_SENT: SMSにOTPを送信しました")
print("  otp_prefix:", state["otp_prefix"], "  (この後ろにSMSの数字が続く形)")
print("  otp_reference_id:", "有" if state["otp_reference_id"] else "無")

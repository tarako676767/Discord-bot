"""PayPay ログイン専用スクリプト（対話式・1ファイル完結）

使い方:
    python paypay_login.py
        → paypay_token.json の phone / password を使ってログイン開始。
          SMS で届いた認証コードをその場で入力するとトークンを保存する。

    python paypay_login.py 09012345678 パスワード
        → 電話番号・パスワードを指定してログイン（token ファイルにも保存）。

    python paypay_login.py --file paypay_token2.json 09012345678 パスワード
        → 保存先トークンファイルを指定。

    python paypay_login.py --uuid B269B5F0-82D8-4DCA-898A-7AA9F8C3B73C
        → 過去に使っていた端末 UUID を指定してログイン。
          その UUID が OTP 認証済みの端末なら、OTP を求められず
          access_token が直接返る（＝SMS 送信上限を回避できる）ことがある。
          一度指定すれば token ファイルに保存され、次回以降は自動で再利用される。

ポイント:
    - 「電話番号もパスワードも合っているのにログインできない」場合、
      多くは *パスワード間違いではなく* OTP(SMS)送信回数の上限
      (INVALID_AUTH_SEND_OTP_OVER_LIMIT_COUNT) が原因。
      その場合は数時間〜1日ほど待ってから 1 回だけ実行し直すこと。
      ここではそのエラーを分かりやすく表示する。
"""

import sys
import os
import json
import uuid as _uuid
import asyncio

# --- 文字化け対策 ---------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import paypayu  # noqa: E402


def _parse_args(argv):
    """引数を解析して (token_file, phone, password, uuid) を返す。"""
    token_file = os.path.join(_HERE, "paypay_token.json")
    forced_uuid = None
    positional = []
    it = iter(argv)
    for a in it:
        if a in ("--file", "-f"):
            token_file = next(it, token_file)
        elif a.startswith("--file="):
            token_file = a.split("=", 1)[1]
        elif a in ("--uuid", "-u"):
            forced_uuid = next(it, None)
        elif a.startswith("--uuid="):
            forced_uuid = a.split("=", 1)[1]
        else:
            positional.append(a)
    if not os.path.isabs(token_file):
        token_file = os.path.join(_HERE, token_file)
    phone = positional[0] if len(positional) > 0 else None
    password = positional[1] if len(positional) > 1 else None
    return token_file, phone, password, forced_uuid


def _load_token(token_file):
    if os.path.exists(token_file):
        try:
            return json.load(open(token_file, encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


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


def _save_token(token_file, data):
    _jdump_atomic(token_file, data, indent=2)


def _explain_error(data):
    """ログインAPIのエラー応答を読みやすい日本語にして返す。"""
    info = (data or {}).get("result_info", {}) or {}
    code = info.get("result_code") or ""
    msg = info.get("result_msg") or ""
    if "OVER_LIMIT" in code or "over limit" in msg.lower():
        return (
            "OTP(SMS認証コード)の送信回数が上限に達しています。\n"
            "  → 電話番号・パスワードは合っています。数時間〜1日ほど待ってから、\n"
            "     連打せず 1 回だけ実行し直してください。"
        )
    if "PASSWORD" in code or "AUTH" in code:
        return "電話番号またはパスワードが正しくない可能性があります。"
    return f"ログインに失敗しました (code={code})\n  {msg}"


async def _run():
    token_file, arg_phone, arg_pw, forced_uuid = _parse_args(sys.argv[1:])
    tok = _load_token(token_file)

    # 引数で渡されたら上書き保存
    if arg_phone and arg_pw:
        tok["phone"], tok["password"] = arg_phone, arg_pw
        _save_token(token_file, tok)

    phone = tok.get("phone")
    password = tok.get("password")
    if not (phone and password):
        print("[NG] 電話番号/パスワードがありません。")
        print("     python paypay_login.py 09012345678 パスワード  の形で渡してください。")
        return 1

    # 使う client_uuid の決定:
    #   1) --uuid で明示指定
    #   2) token ファイルに保存済みの uuid / client_uuid
    #   3) いずれも無ければ新規生成
    # 過去に OTP 認証を通した端末 UUID を再利用すると、OTP 送信を
    # スキップして access_token が直接返る（＝SMS上限を回避できる）ことがある。
    saved_uuid = tok.get("uuid") or tok.get("client_uuid")
    if forced_uuid:
        client_uuid = forced_uuid.strip()
        uuid_src = "指定(--uuid)"
    elif saved_uuid:
        client_uuid = str(saved_uuid).strip()
        uuid_src = "保存済み(token)"
    else:
        client_uuid = str(_uuid.uuid4())
        uuid_src = "新規生成"

    print(f"[*] トークンファイル: {token_file}")
    print(f"[*] 電話番号: {phone}")
    print(f"[*] client_uuid: {client_uuid}  ({uuid_src})")
    # UUID を token ファイルに残しておき、次回以降も同じ端末として扱わせる
    if tok.get("uuid") != client_uuid:
        tok["uuid"] = client_uuid
        _save_token(token_file, tok)
    print("[*] ログイン開始（PayPay に接続中）...")
    try:
        data = await paypayu.login(phone, password, client_uuid)
    except Exception as e:
        print(f"[NG] 通信エラー: {type(e).__name__}: {e}")
        return 1

    if not isinstance(data, dict):
        print(f"[NG] 予期しない応答: {str(data)[:200]}")
        return 1

    if data.get("response_type") == "ErrorResponse":
        print("[NG] " + _explain_error(data))
        return 1

    # OTP 不要でトークンが返るケース
    if data.get("access_token"):
        tok["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            tok["refresh_token"] = data["refresh_token"]
        tok["uuid"] = client_uuid
        import time
        tok["captured_ts"] = int(time.time())
        _save_token(token_file, tok)
        print("[OK] OTP不要でログイン完了、トークンを保存しました。")
        print("     refresh_token:", "あり" if data.get("refresh_token") else "なし")
        return 0

    # OTP が必要なケース
    otp_reference_id = data.get("otp_reference_id")
    otp_prefix = data.get("otp_prefix") or ""
    print("[*] SMS に認証コードを送信しました。")
    if otp_prefix:
        print(f"    （プレフィックス: {otp_prefix} … この後ろに SMS の数字が続く形）")

    try:
        otp = input("SMS で届いた認証コードを入力してください: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[NG] 入力がキャンセルされました。")
        return 1

    if not otp:
        print("[NG] コードが空です。")
        return 1

    print("[*] 認証コードを送信中...")
    try:
        result = await paypayu.login_otp(client_uuid, otp, otp_reference_id, otp_prefix)
    except Exception as e:
        print(f"[NG] 通信エラー: {type(e).__name__}: {e}")
        return 1

    if result == "ERR" or not isinstance(result, dict) or not result.get("access_token"):
        print(f"[NG] 認証コードが違うか期限切れです。(resp={str(result)[:160]})")
        return 1

    tok["phone"] = phone
    tok["password"] = password
    tok["uuid"] = client_uuid
    tok["access_token"] = result["access_token"]
    if result.get("refresh_token"):
        tok["refresh_token"] = result["refresh_token"]
    import time
    tok["captured_ts"] = int(time.time())
    _save_token(token_file, tok)

    print("[OK] ログイン完了、トークンを保存しました。")
    print("     refresh_token:", "あり" if result.get("refresh_token") else "なし（応答に無し）")
    return 0


def main():
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n中断しました。")
        return 1


if __name__ == "__main__":
    sys.exit(main())

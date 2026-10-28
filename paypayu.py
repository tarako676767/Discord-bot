import os
import aiohttp
import datetime
import yarl

try:
    from useragent_changer import UserAgent
except ModuleNotFoundError:
    class UserAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def set(self):
            return (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            )

ua = UserAgent('iphone')

# Optional HTTP/HTTPS proxy for PayPay requests.  Keep credentials out of
# source code; configure PAYPAY_PROXY_URL in the environment instead.
PROXY_URL = os.environ.get("PAYPAY_PROXY_URL") or None

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=45, sock_connect=10, sock_read=25)


def _diag(msg):
    try:
        import datetime as _dt
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paypay_recv_diag.log")
        with open(log, "a", encoding="utf-8") as fp:
            fp.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


async def login(phoneNumber: str, password: str, uuid: str):
    headers = {
        'User-Agent': ua.set(),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        "scope": "SIGN_IN",
        "client_uuid": f"{uuid}",
        "grant_type": "password",
        "username": phoneNumber,
        "password": password,
        "add_otp_prefix": True,
        "language": "ja",
    }
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        async with session.post(
            "https://www.paypay.ne.jp/app/v1/oauth/token",
            headers=headers, json=payload, proxy=PROXY_URL,
        ) as r:
            return await r.json()


async def login_otp(set_uuid, otp, otpid, otp_pre):
    headers = {
        'User-Agent': ua.set(),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        "scope": "SIGN_IN",
        "client_uuid": f"{set_uuid}",
        "grant_type": "otp",
        "otp_prefix": str(otp_pre),
        "otp": otp,
        "otp_reference_id": otpid,
        "username_type": "MOBILE",
        "language": "ja",
    }
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        async with session.post(
            "https://www.paypay.ne.jp/app/v1/oauth/token",
            headers=headers, json=payload, proxy=PROXY_URL,
        ) as r:
            data = await r.json()
            try:
                if data.get("response_type") == "ErrorResponse":
                    return "ERR"
            except Exception:
                pass
            return data


async def check_link(cd):
    if "https://" in cd:
        cd = cd.replace("https://pay.paypay.ne.jp/", "")
    headers = {
        "Accept": "application/json, text/plain, */*",
        'User-Agent': ua.set(),
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        try:
            async with session.get(
                f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}",
                headers=headers, proxy=PROXY_URL,
            ) as r:
                r.raise_for_status()
                link_info = await r.json()
        except aiohttp.ClientError as e:
            print(f"API_REQ_EXC: {e}")
            return False

    if link_info.get("header", {}).get("resultCode") != "S0000":
        return False
    if link_info.get("payload", {}).get("orderStatus") == "PENDING":
        return link_info
    return False


async def refresh_access_token(refresh_token: str, uuid: str):
    headers = {
        'User-Agent': ua.set(),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        "scope": "SIGN_IN",
        "client_uuid": uuid,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "language": "ja",
    }
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        try:
            async with session.post(
                "https://www.paypay.ne.jp/app/v1/oauth/token",
                headers=headers, json=payload, proxy=PROXY_URL,
            ) as r:
                data = await r.json()
                at = data.get("access_token")
                rt = data.get("refresh_token") or refresh_token
                return (at, rt) if at else (None, None)
        except Exception as e:
            print(f"REFRESH_EXC: {e}")
            return None, None


async def already_received(cd, access_token, external_id=None):
    if not access_token:
        return False
    if "https://" in cd:
        cd = cd.replace("https://pay.paypay.ne.jp/", "").replace("https://paypay.ne.jp/", "")
    cd = (cd or "").strip()
    if not cd:
        return False
    headers = {
        "Accept": "application/json, text/plain, */*",
        'User-Agent': ua.set(),
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            session.cookie_jar.update_cookies(
                {"token": access_token},
                response_url=yarl.URL("https://www.paypay.ne.jp"),
            )
            async with session.get(
                f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}",
                headers=headers, proxy=PROXY_URL,
            ) as r:
                if r.status != 200:
                    return False
                link_info = await r.json()
    except Exception as e:
        _diag(f"ALREADY_RECV_EXC cd={cd} exc={e!r}")
        return False
    payload = link_info.get("payload", {}) or {}
    if payload.get("orderStatus") != "SUCCESS":
        return False
    recv = (payload.get("receiver") or {}).get("externalId")
    if external_id and recv and recv != external_id:
        _diag(f"ALREADY_RECV_MISMATCH cd={cd} receiver={recv} mine={external_id}")
        return False
    _diag(f"ALREADY_RECV cd={cd} (保留承認で成立済み→配布可)")
    return True


async def link_rev(cd, phoneNumber, password, uuid, link_password=None, access_token=None):
    if "https://" in cd:
        cd = cd.replace("https://pay.paypay.ne.jp/", "")

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        if access_token:
            session.cookie_jar.update_cookies(
                {"token": access_token},
                response_url=yarl.URL("https://www.paypay.ne.jp"),
            )

        base_headers = {
            "Accept": "application/json, text/plain, */*",
            'User-Agent': ua.set(),
            "Content-Type": "application/json",
        }

        try:
            async with session.get(
                f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}",
                headers=base_headers, proxy=PROXY_URL,
            ) as r:
                r.raise_for_status()
                link_info = await r.json()
            _os = link_info.get("payload", {}).get("orderStatus")
            if _os != "PENDING":
                _diag(f"NOT_PENDING cd={cd} orderStatus={_os}")
                return False, None
            if link_info.get("payload", {}).get("pendingP2PInfo", {}).get("isSetPasscode") and link_password is None:
                _diag(f"PASSCODE_REQUIRED cd={cd}")
                return False, None
        except aiohttp.ClientError as e:
            print(f"LINK_REQ_EXC: {e}")
            _diag(f"LINK_REQ_EXC cd={cd} exc={e!r}")
            return False, None

        new_token = None

        if not access_token:
            login_payload = {
                "scope": "SIGN_IN",
                "client_uuid": f"{uuid}",
                "grant_type": "password",
                "username": phoneNumber,
                "password": password,
                "add_otp_prefix": True,
                "language": "ja",
            }
            login_headers = {
                'User-Agent': ua.set(),
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'Origin': 'https://www.paypay.ne.jp',
                'Referer': 'https://pay.paypay.ne.jp/' + cd,
            }
            async with session.post(
                "https://www.paypay.ne.jp/app/v1/oauth/token",
                headers=login_headers, json=login_payload, proxy=PROXY_URL,
            ) as r:
                login_response = await r.json()
                try:
                    access_token = login_response["access_token"]
                    new_token = {
                        "access_token": access_token,
                        "refresh_token": login_response.get("refresh_token"),
                    }
                except KeyError:
                    return "LOGINERR", None

        receive_payload = {
            "verificationCode": cd,
            "client_uuid": uuid,
            "requestAt": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900')),
            "requestId": link_info["payload"]["message"]["data"]["requestId"],
            "orderId": link_info["payload"]["message"]["data"]["orderId"],
            "senderMessageId": link_info["payload"]["message"]["messageId"],
            "senderChannelUrl": link_info["payload"]["message"]["chatRoomId"],
            "iosMinimumVersion": "3.45.0",
            "androidMinimumVersion": "3.45.0",
        }
        if link_password:
            receive_payload["passcode"] = link_password

        auth_headers = base_headers.copy()
        if access_token:
            auth_headers["Authorization"] = f"Bearer {access_token}"

        try:
            async with session.post(
                "https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink",
                json=receive_payload,
                headers=auth_headers,
                proxy=PROXY_URL,
            ) as r:
                if r.status == 401:
                    _diag(f"ACCEPT_401 cd={cd} (token expired)")
                    return "TOKEN_EXPIRED", None
                r.raise_for_status()
                receive_data = await r.json()
                if receive_data.get("header", {}).get("resultCode") == "S0000":
                    return True, new_token
                _err = receive_data.get("error") or {}
                if (receive_data.get("header", {}).get("resultCode") == "S9999"
                        and str(_err.get("backendResultCode")) == "42007013"):
                    _diag(f"ACCEPT_HELD cd={cd} (受け取り一時保留)")
                    return "HELD", new_token
                _diag(f"ACCEPT_NG cd={cd} resp={receive_data}")
                return False, new_token
        except aiohttp.ClientError as e:
            print(f"REVERR: {e}")
            _diag(f"REVERR cd={cd} exc={e!r}")
            return False, new_token

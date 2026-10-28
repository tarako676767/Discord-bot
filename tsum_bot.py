import sys, os, io, json, asyncio, functools, traceback, re, time, random, collections, datetime
import concurrent.futures
import threading
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import discord
from discord.ext import commands
import unicodedata
import requests

import tsum_login
import line_password_login
import tsum
import tsum_guest
from tsum_forge import Forge

_cfg_name = os.environ.get("TSUM_BOT_CONFIG", "bot_config.json")
CONFIG_FILE = _cfg_name if os.path.isabs(_cfg_name) else os.path.join(HERE, _cfg_name)
print(f"[bot] 設定ファイル: {os.path.basename(CONFIG_FILE)}")
CREDS_FILE = os.path.join(HERE, "line_credentials.json")
TOKENS_FILE = os.path.join(HERE, "line_tokens_latest.json")
SESSION_FILE = os.path.join(HERE, "tsum_session_headless.json")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[bot] {CONFIG_FILE} がありません。下記の内容で作成してください:")
        print('{\n  "token": "あなたのBOTトークン",\n  "prefix": "!"\n}')
        sys.exit(1)
    return json.load(open(CONFIG_FILE, encoding="utf-8-sig"))

CONFIG = load_config()
try:
    tsum_login.resolve_appver()
except Exception:
    pass
print(f"[bot] appver: {tsum_login.APPVER}")
GUILD_ID = CONFIG.get("guild_id")
PAYPAY_SHARED_FILE = CONFIG.get("paypay_token_file") or "paypay_token.json"
# サーバーごとのPayPay認証ファイルの置き場所（<サーバーID>.json）
PAYPAY_DIR = CONFIG.get("paypay_token_dir") or "paypay_tokens"
if not os.path.isabs(PAYPAY_DIR):
    PAYPAY_DIR = os.path.join(HERE, PAYPAY_DIR)
if not os.path.isabs(PAYPAY_SHARED_FILE):
    PAYPAY_SHARED_FILE = os.path.join(HERE, PAYPAY_SHARED_FILE)
PYTHON_REALIP = CONFIG.get("python_realip") or sys.executable


def _default_game_proxy():
    """LINEログイン・ツムツムAPI・引き継ぎで、この1件の代行の間だけ使うプロキシURL。
    設定の実体は tsum_login._creds_proxy() 1箇所にまとめてある(bot_config.json の
    tor_enabled / tor_socks_url を読む)。PayPay はここを一切通らない
    (paypay_helper.py は python_realip の別プロセスで実行され、この値を渡さない)。

    呼ぶたびに新しいランダムなSOCKS認証情報(user:pass)を発行して埋め込む。
    Tor は SOCKS認証情報が違うと別の出口回線(≒別IP)を割り当てる
    (IsolateSOCKSAuth、Torの既定で有効)ため、これだけで依頼ごとに出口IPが変わる。
    _do_forge/_do_forge_guest はジョブの最初に1回だけこの関数を呼び、返った値を
    ログイン〜盛り処理〜引き継ぎ発行まで使い回すので、同じ依頼の中でIPが
    途中で変わって不自然に見えることはない。"""
    try:
        px = tsum_login._creds_proxy()
    except Exception:
        px = None
    if not isinstance(px, dict):
        return None
    url = px.get("https") or px.get("http")
    if not url:
        return url
    return tsum_login.fresh_socks_identity(url)


# --- 出口IPの使い回し防止 -------------------------------------------------
# 「1代行=1IP、使い回し厳禁」のための仕組み。
# fresh_socks_identity() は毎回新しい回線をTorに要求するが、Torは帯域の太い
# 出口ノードを優先して選ぶため、放っておくと同じIPを再び引くことがある
# (実際 192.42.116.x のオランダ勢が何度も出ていた)。
# そこで実際に出口IPを確認し、過去に使ったIPだったら引き直す。
# --- 実行中の代行の記録(異常終了からの復旧用) --------------------------
# botが落ちたり Discord から切断されたまま終了すると、実行中だった依頼は
# 「お客様に何も伝わらないまま消える」ことになる。そこで開始時にディスクへ
# 書き、終了時に消す。起動時に残っていれば = 前回中断された依頼なので、
# そのチケットに状況を知らせる。
ACTIVE_JOBS_FILE = os.path.join(HERE, "tsum_active_jobs.json")


def _job_key(channel, user):
    return f"{getattr(channel, 'id', 0)}:{getattr(user, 'id', 0)}:{int(time.time())}"


def _job_start(key, channel, user, kw):
    """代行の開始を記録する。失敗しても本処理は止めない。"""
    try:
        data = _jload(ACTIVE_JOBS_FILE, {})
        if not isinstance(data, dict):
            data = {}
        action, val = (next(iter(kw.items())) if kw else ("?", ""))
        data[key] = {
            "ts": time.time(),
            "channel_id": getattr(channel, "id", None),
            "user": str(user) if user else "",
            "action": action,
            "val": str(val),
        }
        _jdump_atomic(ACTIVE_JOBS_FILE, data)
    except Exception as e:
        print(f"[job] 実行中記録の保存に失敗: {e}")


def _job_end(key):
    try:
        data = _jload(ACTIVE_JOBS_FILE, {})
        if isinstance(data, dict) and key in data:
            data.pop(key, None)
            _jdump_atomic(ACTIVE_JOBS_FILE, data)
    except Exception as e:
        print(f"[job] 実行中記録の削除に失敗: {e}")


async def _report_interrupted_jobs():
    """起動時に、前回中断された代行をチケットへ知らせる。
    ゲーム側は「やった分だけ反映済み」なので、もう一度実行すれば続きから進む。"""
    data = _jload(ACTIVE_JOBS_FILE, {})
    if not isinstance(data, dict) or not data:
        return
    print(f"[job] 前回中断された代行が {len(data)} 件あります。チケットに通知します。", flush=True)
    for key, v in list(data.items()):
        ch_id = v.get("channel_id")
        try:
            ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
        except Exception as e:
            print(f"[job] チャンネル {ch_id} を取得できません({type(e).__name__})。記録のみ削除します。")
            ch = None
        if ch is not None:
            try:
                await ch.send(embed=discord.Embed(
                    description=("前回の実行が途中で中断されました（bot側の接続トラブル）。\n"
                                 "**そこまでの分はゲームに反映済み**なので、"
                                 "もう一度同じ内容で実行すると続きから進みます。\n"
                                 "ご迷惑をおかけして申し訳ありません。"),
                    color=0xf1c40f))
                print(f"[job] 通知しました: {v.get('user')} {v.get('action')} {v.get('val')}", flush=True)
            except Exception as e:
                print(f"[job] 通知の送信に失敗: {type(e).__name__}: {e}")
    try:
        _jdump_atomic(ACTIVE_JOBS_FILE, {})
    except Exception:
        pass


USED_IP_FILE = os.path.join(HERE, "tsum_used_exit_ips.json")
USED_IP_KEEP_DAYS = 1           # これより古い記録は捨てて再利用を許す(枯渇防止)
                                # 短くするほど使用禁止リストが小さくなり、IP選定が速くなる
AVOID_RECENT_COUNTRIES = 3      # 直近この件数の国は避ける(国も毎回変えるため)
PROBE_TIMEOUT = 5               # 出口IP確認1回あたりの制限秒。遅い回線はどのみち使いたくない
PROBE_PARALLEL = 6              # 同時に確認する候補数。1つずつ順番に試すと遅いので並列化する
PROBE_ROUNDS = 3                # 並列でやり直す最大ラウンド数
PROBE_TOTAL_BUDGET = 20         # 出口IP選びに使ってよい合計秒数。超えたら妥協して通す

# Tor同梱のGeoIPファイル(IP範囲→国コード)。外部サービスに問い合わせずに国を判定する。
def _find_geoip_file():
    """TorのGeoIPファイルを探す。環境ごとに置き場所が違うので候補を順に見る。
    見つからなくても動作する(国の重複回避が無効になるだけ)。"""
    cand = [CONFIG.get("tor_geoip_file"),
            os.path.join(HERE, "geoip"),         # botフォルダに同梱したもの(最優先)
            os.path.expanduser(r"~\Desktop\Tor Browser\Browser\TorBrowser\Data\Tor\geoip"),
            "/usr/share/tor/geoip",              # Debian/Ubuntu
            "/usr/local/share/tor/geoip",        # 自前ビルド/macOS
            "/etc/tor/geoip",
            os.path.join(HERE, "geoip")]         # 同梱した場合
    for c in cand:
        if c and os.path.exists(c):
            return c
    return cand[1]                               # 見つからないときは既定値(後で警告が出る)


TOR_GEOIP_FILE = _find_geoip_file()
_GEOIP_TABLE = None             # [(開始IP整数, 終了IP整数, 国コード), ...] を昇順で保持


def _load_geoip_table():
    """TorのGeoIPファイルを読み込む(初回のみ)。読めなければ空リスト。"""
    global _GEOIP_TABLE
    if _GEOIP_TABLE is not None:
        return _GEOIP_TABLE
    table = []
    try:
        with open(TOR_GEOIP_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line or line[0] == "#":
                    continue
                parts = line.strip().split(",")
                if len(parts) != 3:
                    continue
                try:
                    table.append((int(parts[0]), int(parts[1]), parts[2]))
                except ValueError:
                    continue
        table.sort()
        print(f"[ip] GeoIP読み込み: {len(table)}件 ({TOR_GEOIP_FILE})", flush=True)
    except Exception as e:
        print(f"[ip] GeoIPを読めませんでした({e})。国の重複回避は無効になります。", flush=True)
        table = []
    _GEOIP_TABLE = table
    return _GEOIP_TABLE


def _ip_country(ip):
    """IPv4アドレスの国コードをローカルのGeoIPから引く。不明なら None。"""
    table = _load_geoip_table()
    if not table or not ip:
        return None
    try:
        a, b, c, d = (int(x) for x in str(ip).split("."))
        val = (a << 24) | (b << 16) | (c << 8) | d
    except Exception:
        return None                     # IPv6等はここでは扱わない
    import bisect
    i = bisect.bisect_right(table, (val, float("inf"), "")) - 1
    if 0 <= i < len(table):
        lo, hi, cc = table[i]
        if lo <= val <= hi and cc and cc != "??":
            return cc
    return None


def _load_used_ips():
    """{ip: {"ts": 時刻, "cc": 国コード}} を返す。古い記録は落とす。
    以前の形式({ip: 時刻})も読めるようにしてある。"""
    data = _jload(USED_IP_FILE, {})
    if not isinstance(data, dict):
        return {}
    cutoff = time.time() - USED_IP_KEEP_DAYS * 86400
    out = {}
    for ip, v in data.items():
        if isinstance(v, dict):
            ts, cc = v.get("ts"), v.get("cc")
        else:
            ts, cc = v, None            # 旧形式
        if isinstance(ts, (int, float)) and ts >= cutoff:
            out[ip] = {"ts": ts, "cc": cc}
    return out


def _recent_countries(n=AVOID_RECENT_COUNTRIES):
    """直近n件で使った国コードの集合。"""
    used = _load_used_ips()
    rows = sorted(used.items(), key=lambda kv: kv[1]["ts"], reverse=True)
    return {v["cc"] for _, v in rows[:n] if v.get("cc")}


def _mark_ip_used(ip, cc=None):
    if not ip:
        return
    data = _load_used_ips()
    data[ip] = {"ts": time.time(), "cc": cc}
    try:
        _jdump_atomic(USED_IP_FILE, data)
    except Exception as e:
        print(f"[ip] 使用済みIPの保存に失敗: {e}")


def _probe_exit_ip(proxy, timeout=PROBE_TIMEOUT):
    """このプロキシで今どの出口IPに出るかを実際に確認する。失敗したら None。"""
    if not proxy:
        return None
    try:
        r = requests.get("https://check.torproject.org/api/ip",
                         proxies={"http": proxy, "https": proxy}, timeout=timeout)
        return (r.json() or {}).get("IP") or None
    except Exception:
        return None


# --- 回線の事前準備(先読み) ------------------------------------------
# 出口IPの確認には回線構築込みで数秒かかる。代行が始まってから確認すると
# その分まるごと待たされるので、バックグラウンドで前もって用意しておく。
_PROXY_POOL = []                # [{"proxy","ip","cc","ts"}, ...]
_POOL_LOCK = threading.Lock()
POOL_SIZE = 3                   # 常備しておく本数
POOL_MAX_AGE = 120              # 秒。古い回線はTor側で切れている可能性があるので捨てる


def _pool_take(avoid_cc, used):
    """条件に合う回線を在庫から取り出す。無ければ None。"""
    now = time.time()
    with _POOL_LOCK:
        # 期限切れを捨てる
        _PROXY_POOL[:] = [e for e in _PROXY_POOL if now - e["ts"] <= POOL_MAX_AGE]
        for i, e in enumerate(_PROXY_POOL):
            if e["ip"] in used:
                continue
            if e["cc"] and avoid_cc and e["cc"] in avoid_cc:
                continue
            return _PROXY_POOL.pop(i)
        # 国の条件を満たすものが無ければ、未使用IPだけで妥協する
        for i, e in enumerate(_PROXY_POOL):
            if e["ip"] not in used:
                return _PROXY_POOL.pop(i)
    return None


def _pool_fill_once():
    """在庫を1本補充する(バックグラウンド用)。"""
    proxy = _default_game_proxy()
    if not proxy:
        return False
    ip = _probe_exit_ip(proxy)
    if not ip:
        return False
    with _POOL_LOCK:
        if any(e["ip"] == ip for e in _PROXY_POOL):
            return False                      # 在庫内で重複しても意味がない
        _PROXY_POOL.append({"proxy": proxy, "ip": ip,
                            "cc": _ip_country(ip), "ts": time.time()})
    return True


async def _proxy_pool_worker():
    """在庫が減ったら裏で補充し続ける。代行の待ち時間から確認作業を追い出す。"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if not _default_game_proxy():
                await asyncio.sleep(30)       # Tor未使用なら何もしない
                continue
            with _POOL_LOCK:
                need = POOL_SIZE - len(_PROXY_POOL)
            if need > 0:
                loop = asyncio.get_running_loop()
                # 補充もイベントループを止めないよう別スレッドで
                await asyncio.gather(*[loop.run_in_executor(None, _pool_fill_once)
                                       for _ in range(min(need, 3))])
            else:
                await asyncio.sleep(5)
        except Exception as e:
            print(f"[ip] 回線の事前準備でエラー: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(10)


def _game_api_proxy(line_proxy):
    """ツムツム本体(ゲームAPI)に使う経路を決める。

    bot_config.json の tor_game_api:
      true  … ゲームAPIもTor経由(IPは完全に隠れるが、1件あたり数百〜数千回
              叩くため極端に遅い。実測でTorは1回562〜640ms、直接は35ms)
      false … ゲームAPIは直接接続(速い)。LINEログインはTorのままなので、
              お客様のLINEに届く通知のIPは毎回変わる。
              ただしゲーム運営からはサーバーの素のIPが見える。
    """
    if CONFIG.get("tor_game_api", True):
        return line_proxy
    return None


def _unique_game_proxy():
    """未使用の出口IPを引き当ててから、そのプロキシURLを返す。
    条件は2つ:
      1. 過去に使ったIPは使わない(「1代行=1IP、使い回し厳禁」)
      2. 直近 AVOID_RECENT_COUNTRIES 件で使った国も避ける(国も毎回変える)

    候補を1つずつ順番に確認すると、外れるたびに待ち時間が積み上がって
    1件あたり数十秒かかっていた。そこで PROBE_PARALLEL 個の候補を
    同時に確認し、条件を満たしたものを即採用する。1ラウンドの所要時間は
    候補1個ぶん(最大 PROBE_TIMEOUT 秒)で済む。

    国まで一致する候補が無い場合は、IPさえ未使用なら妥協して使う
    (Torは帯域の太い国を優先するため、国を厳密に求めると決まらないことがある)。

    【重要】この関数はTor経由の通信を行うので秒単位でブロックする。
    asyncioのイベントループ上から直接呼ぶとDiscordのハートビートが止まるため、
    必ず run_in_executor 経由(別スレッド)で呼ぶこと。"""
    if not _default_game_proxy():
        return None                           # Tor無効時はそのまま(直接接続)

    avoid_cc = _recent_countries()
    used = _load_used_ips()

    # まず事前準備しておいた回線を使う(確認済みなので待ち時間ゼロ)
    got = _pool_take(avoid_cc, used)
    if got:
        _mark_ip_used(got["ip"], got["cc"])
        _dbg(f"[ip] 今回の出口IP: {got['ip']} ({got['cc'] or '国不明'}) 事前準備分")
        print(f"[ip] 今回の代行で使う出口IP: {got['ip']} ({got['cc'] or '国不明'})", flush=True)
        return got["proxy"]

    deadline = time.time() + PROBE_TOTAL_BUDGET
    fallback = None                           # 未使用だが国が直近と被る候補
    last = None

    for rnd in range(PROBE_ROUNDS):
        if time.time() > deadline:
            break
        cands = [_default_game_proxy() for _ in range(PROBE_PARALLEL)]
        last = cands[-1]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(cands)) as ex:
                # 各確認に個別のタイムアウトがあるので、1ラウンドは最大 PROBE_TIMEOUT 秒程度
                ips = list(ex.map(_probe_exit_ip, cands))
        except Exception as e:
            _dbg(f"[ip] 並列確認で例外: {type(e).__name__}: {e}")
            continue

        for proxy, ip in zip(cands, ips):
            if not ip or ip in used:
                continue
            cc = _ip_country(ip)
            if cc and avoid_cc and cc in avoid_cc:
                if fallback is None:
                    fallback = (proxy, ip, cc)   # 国は被るが未使用。最後の手段に取っておく
                continue
            _mark_ip_used(ip, cc)
            _dbg(f"[ip] 今回の出口IP: {ip} ({cc or '国不明'}) "
                 f"{rnd+1}ラウンド目 直近の国={sorted(avoid_cc)}")
            print(f"[ip] 今回の代行で使う出口IP: {ip} ({cc or '国不明'})", flush=True)
            return proxy
        _dbg(f"[ip] {rnd+1}ラウンド目({len(cands)}件同時)では条件に合う候補なし")

    if fallback:
        proxy, ip, cc = fallback
        _mark_ip_used(ip, cc)
        _dbg(f"[ip] 今回の出口IP: {ip} ({cc}) ※国は直近と重複(未使用IPを優先)")
        print(f"[ip] 今回の代行で使う出口IP: {ip} ({cc}) ※国は直近と重複", flush=True)
        return proxy

    print("[ip] 警告: 条件に合う出口IPを引けませんでした。そのまま実行します。", flush=True)
    return last


PAYPAY_HELPER = os.path.join(HERE, "paypay_helper.py")
PP_RELOGIN_START = os.path.join(HERE, "_pp_relogin_start.py")
PP_RELOGIN_OTP = os.path.join(HERE, "_pp_relogin_otp.py")
PAYPAY_PROXY_URL = str(CONFIG.get("paypay_proxy_url") or "").strip()
if PAYPAY_PROXY_URL:
    # The helper runs in a child process and reads this value when importing
    # paypayu.  Do not print it because it may contain proxy credentials.
    os.environ["PAYPAY_PROXY_URL"] = PAYPAY_PROXY_URL

from discord import app_commands

def save_config():
    _jdump_atomic(CONFIG_FILE, CONFIG, indent=2)


SALES_FILE = os.path.join(HERE, CONFIG.get("sales_file", "tsum_sales.json"))
FREE_USED_FILE = os.path.join(HERE, CONFIG.get("free_used_file", "tsum_free_used.json"))
ACTIVE_ORDERS = {}


def _jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8-sig"))
    except Exception:
        return default


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


class LoginHookAbort(Exception):
    """on_login フックが「この依頼は実行してはいけない」と判断したときに投げる。
    予期しない例外は従来どおり握り潰して続行するが、これだけは処理を中止させる。"""
    def __init__(self, user_msg):
        super().__init__(user_msg)
        self.user_msg = user_msg


def _dbg(msg):
    try:
        with open(os.path.join(HERE, "tsum_debug.log"), "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


def record_sale(user_id, action, amount):
    data = _jload(SALES_FILE, [])
    data.append({"ts": int(time.time()), "user_id": int(user_id), "action": action, "amount": int(amount)})
    try:
        _jdump_atomic(SALES_FILE, data)
    except Exception:
        pass


def sales_summary():
    import datetime
    data = _jload(SALES_FILE, [])
    JST = datetime.timezone(datetime.timedelta(hours=9))
    today0 = datetime.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    now = time.time()
    def agg(since):
        rows = [d for d in data if d.get("ts", 0) >= since]
        return sum(int(d.get("amount", 0)) for d in rows), len(rows)
    return {"today": agg(today0), "week": agg(now - 7 * 86400),
            "month": agg(now - 30 * 86400),
            "total": (sum(int(d.get("amount", 0)) for d in data), len(data))}


def reset_sales():
    try:
        _jdump_atomic(SALES_FILE, [])
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 実績カウンター
#   ※ 貸し出しは「そのサーバーで bot を使える権利」であって、代行メニューの料金が
#     無料になるわけではない（お客さんは通常どおり料金を支払う）。
# ----------------------------------------------------------------------------
RENTAL_FILE = os.path.join(HERE, CONFIG.get("rental_file", "tsum_rentals.json"))
RENTAL_UNLIMITED = 0          # expires == 0 は「永久」
# 月数 → 料金（0 = 永久）
DEFAULT_RENTAL_PRICES = {"1": 2000, "3": 5000, "6": 9000, "12": 12000, "0": 15000}
# 貸し出し先サーバーへ招待するときに必要な権限
RENTAL_INVITE_PERMS = (
    (1 << 4)     # チャンネルの管理（チケット作成・実績カウンター）
    | (1 << 10)  # チャンネルを見る
    | (1 << 11)  # メッセージを送信
    | (1 << 13)  # メッセージの管理
    | (1 << 14)  # 埋め込みリンク
    | (1 << 15)  # ファイルを添付
    | (1 << 16)  # メッセージ履歴を読む
    | (1 << 34)  # スレッドの管理
    | (1 << 35)  # 公開スレッドの作成
    | (1 << 36)  # プライベートスレッドの作成
    | (1 << 38)  # スレッドでメッセージを送信
)


def rental_enabled():
    return bool(CONFIG.get("rental_enabled", True))


def guild_lock_enabled():
    """貸し出したサーバーでだけ動かす（未貸出のサーバーでは断る）か。"""
    return rental_enabled() and bool(CONFIG.get("rental_guild_lock", True))


def rental_prices():
    """{月数(int): 料金(int)} を返す。月数 0（永久）は末尾。"""
    raw = CONFIG.get("rental_prices")
    # 設定が無いときだけ既定プラン。空の {} は「販売停止」としてそのまま扱う。
    if not isinstance(raw, dict):
        raw = DEFAULT_RENTAL_PRICES
    out = {}
    for k, v in raw.items():
        try:
            months, price = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if months >= 0 and price >= 0:
            out[months] = price
    return dict(sorted(out.items(), key=lambda kv: (kv[0] == 0, kv[0])))


def rental_plan_label(months):
    """1→「1ヶ月」/ 12→「1年間」/ 0→「永久」。"""
    months = int(months or 0)
    if months <= 0:
        return "永久"
    if months % 12 == 0:
        return f"{months // 12}年間"
    return f"{months}ヶ月"


def rental_load():
    """{"サーバーID": {...}} を返す。"""
    data = _jload(RENTAL_FILE, {})
    if not isinstance(data, dict):
        return {}
    if "guilds" in data or "users" in data:
        data = data.get("guilds") or {}
    return data if isinstance(data, dict) else {}


def rental_save(data):
    try:
        json.dump({"guilds": data}, open(RENTAL_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        _dbg(f"[rental] 保存に失敗: {e}")
        return False


def _rental_expires_of(rec):
    """レコードから期限を取り出す。0=永久 / None=貸し出しなし。
    expires が無い・壊れている場合は「貸し出しなし」として扱う（永久にしない）。"""
    if not isinstance(rec, dict) or "expires" not in rec:
        return None
    try:
        return float(rec.get("expires") or 0)
    except (TypeError, ValueError):
        return None


def rental_record(guild_id):
    try:
        rec = rental_load().get(str(int(guild_id)))
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def rental_expires(guild_id):
    """期限のUNIX時刻。0=永久 / None=貸し出しなし。"""
    return _rental_expires_of(rental_record(guild_id))


def rental_active(guild_id):
    if guild_id is None or not rental_enabled():
        return False
    exp = rental_expires(guild_id)
    if exp is None:
        return False
    return exp == RENTAL_UNLIMITED or exp > time.time()


def rental_add_months(base_ts, months):
    """base_ts に months ヶ月を足した UNIX 時刻（月末は丸める）。"""
    import calendar, datetime
    months = int(months or 0)
    if months <= 0:
        return base_ts
    JST = datetime.timezone(datetime.timedelta(hours=9))
    dt = datetime.datetime.fromtimestamp(base_ts, JST)
    total = (dt.year * 12 + (dt.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day).timestamp()


def rental_grant(guild_id, months=0, seconds=0, unlimited=False, by=None, note="", extend=True):
    """貸し出しを付与する。extend=True なら残り期間へ加算、False なら今から。
    戻り値は新しい期限（0=永久）。"""
    key = str(int(guild_id))
    now = time.time()
    data = rental_load()
    rec = data.get(key) if isinstance(data.get(key), dict) else {}
    cur = _rental_expires_of(rec)
    if unlimited or (extend and rec and cur == RENTAL_UNLIMITED):
        new_exp = RENTAL_UNLIMITED          # 永久は延長しても永久のまま
    else:
        base = cur if (extend and cur and cur > now) else now
        new_exp = rental_add_months(base, months) + float(seconds or 0)
    rec.update({
        "expires": new_exp,
        "granted_by": int(by) if by else rec.get("granted_by", 0),
        "granted_at": now,
        "note": note or rec.get("note", ""),
    })
    data[key] = rec
    rental_save(data)
    return new_exp


def rental_revoke(guild_id):
    data = rental_load()
    if data.pop(str(int(guild_id)), None) is None:
        return False
    rental_save(data)
    return True


def rental_list():
    """有効な貸し出しを [(サーバーID, 期限)] で返す（期限が近い順・永久は末尾）。"""
    now = time.time()
    rows = []
    for gid, rec in rental_load().items():
        exp = _rental_expires_of(rec)
        if exp is None:
            continue
        if exp == RENTAL_UNLIMITED or exp > now:
            try:
                rows.append((int(gid), exp))
            except (TypeError, ValueError):
                continue
    rows.sort(key=lambda r: (r[1] == RENTAL_UNLIMITED, r[1]))
    return rows


def rental_prune():
    """期限切れの貸し出しを削除し、そのサーバーIDの一覧を返す。"""
    now = time.time()
    data = rental_load()
    gone = []
    for gid, rec in list(data.items()):
        exp = _rental_expires_of(rec)
        if exp is not None and exp != RENTAL_UNLIMITED and exp <= now:
            data.pop(gid, None)
            try:
                gone.append(int(gid))
            except (TypeError, ValueError):
                pass
    if gone:
        rental_save(data)
    return gone


def rental_fmt_duration(sec):
    """秒を「30日12時間 / 5時間30分 / 20分」のような表記にする（0の位は省く）。"""
    sec = int(max(0, sec))
    d, rem = divmod(sec, 86400)
    h, m = rem // 3600, (rem % 3600) // 60
    if d:
        return f"{d}日{h}時間" if h else f"{d}日"
    if h:
        return f"{h}時間{m}分" if m else f"{h}時間"
    return f"{m}分"


def rental_fmt_jst(ts):
    import datetime
    JST = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.fromtimestamp(ts, JST).strftime("%Y/%m/%d %H:%M")


def rental_until_text(exp):
    return "永久" if exp == RENTAL_UNLIMITED else f"{rental_fmt_jst(exp)} まで"


def rental_status_text(guild_id):
    exp = rental_expires(guild_id)
    if exp is None:
        return "このサーバーは貸し出しされていません。"
    if exp == RENTAL_UNLIMITED:
        return "このサーバーは **永久** で貸し出し中です。"
    left = exp - time.time()
    if left <= 0:
        return "このサーバーの貸し出し期間は終了しています。"
    return (f"このサーバーは **{rental_fmt_jst(exp)}** まで利用できます"
            f"（残り {rental_fmt_duration(left)}）。")


# --- オーナー判定・サーバーの利用可否 ---------------------------------------
def bot_owner_ids():
    """botの持ち主（allowed_user_ids ＋ アプリの所有者/チーム）。"""
    ids = set()
    for x in (CONFIG.get("allowed_user_ids") or []):
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            continue
    app = getattr(bot, "application", None)
    owner = getattr(app, "owner", None)
    if owner is not None:
        ids.add(owner.id)
    team = getattr(app, "team", None)
    for m in (getattr(team, "members", None) or []):
        ids.add(m.id)
    return ids


def is_bot_owner(user):
    uid = getattr(user, "id", user)
    try:
        return int(uid) in bot_owner_ids()
    except (TypeError, ValueError):
        return False


def rental_home_guild_ids():
    """貸し出しに関係なく常に使えるサーバー（自分のサーバー）。"""
    ids = set()
    for x in ([GUILD_ID] + list(CONFIG.get("rental_home_guild_ids") or [])):
        try:
            if x:
                ids.add(int(x))
        except (TypeError, ValueError):
            continue
    return ids


def guild_allowed(guild_id, user=None):
    """そのサーバーで bot を動かしてよいか。"""
    if not guild_lock_enabled():
        return True
    if guild_id is None:                       # DM
        return True
    try:
        gid = int(guild_id)
    except (TypeError, ValueError):
        return True
    if gid in rental_home_guild_ids():
        return True
    if rental_active(gid):
        return True
    return user is not None and is_bot_owner(user)


def guild_block_embed(guild_id):
    exp = rental_expires(guild_id)
    if exp is None:
        desc = ("このサーバーではまだ bot を利用できません。\n"
                "貸し出し（レンタル）を申し込むと、このサーバーで使えるようになります。")
    else:
        desc = ("このサーバーの利用期間は終了しました。\n"
                "延長をご希望の場合はオーナーへご連絡ください。")
    return notice_embed(desc + f"\n\nサーバーID: `{guild_id}`")


async def deny_unlicensed(interaction: discord.Interaction) -> bool:
    """貸し出されていないサーバーなら断る。断ったら True。"""
    if guild_allowed(interaction.guild_id, interaction.user):
        return False
    try:
        await interaction.response.send_message(
            embed=guild_block_embed(interaction.guild_id), ephemeral=True)
    except Exception as e:
        _dbg(f"[rental] 未貸出サーバーへの案内送信失敗: {e}")
    return True


async def resolve_guild_id(text):
    """入力からサーバーIDを取り出す。招待リンク・チャンネルリンク・IDに対応。
    戻り値は (サーバーID or None, エラーメッセージ or None)。"""
    s = (text or "").strip()
    if not s:
        return None, None
    # https://discord.com/channels/<サーバーID>/... （チャンネル/メッセージのリンク）
    m = re.search(r"discord(?:app)?\.com/channels/(\d{15,25})", s)
    if m:
        return int(m.group(1)), None
    # https://discord.gg/xxxx / https://discord.com/invite/xxxx （招待リンク）
    m = re.search(r"(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9\-_]+)", s)
    if m:
        try:
            inv = await bot.fetch_invite(m.group(1))
        except discord.NotFound:
            return None, "招待リンクが無効か期限切れです。作り直して貼り付けてください。"
        except Exception as e:
            _dbg(f"[rental] 招待リンクの確認に失敗: {e}")
            return None, "招待リンクを確認できませんでした。サーバーIDで入力してください。"
        gid = getattr(getattr(inv, "guild", None), "id", None)
        if not gid:
            return None, "そのリンクからサーバーを特定できませんでした。"
        return int(gid), None
    m = re.search(r"\d{15,25}", s)
    if m:
        return int(m.group(0)), None
    return None, "サーバーの招待リンク、またはサーバーIDを入力してください。"


def bot_invite_url():
    app_id = getattr(bot, "application_id", None) or getattr(bot.user, "id", None)
    if not app_id:
        return ""
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&permissions={RENTAL_INVITE_PERMS}&scope=bot%20applications.commands")


# --- サーバーごとの設定（貸し出し先ごとにチケットカテゴリ等を持てるように） ---
def guild_setting(guild_id, key, default=None):
    """サーバー個別の設定。無ければ全体設定 → default の順でフォールバック。"""
    try:
        gs = (CONFIG.get("guild_settings") or {}).get(str(int(guild_id)))
    except (TypeError, ValueError):
        gs = None
    if isinstance(gs, dict) and key in gs:
        return gs[key]
    return CONFIG.get(key, default)


def set_guild_setting(guild_id, key, value):
    ent = CONFIG.setdefault("guild_settings", {}).setdefault(str(int(guild_id)), {})
    if value is None:
        ent.pop(key, None)
    else:
        ent[key] = value
    save_config()


# --- サーバーごとのPayPay受取口座 -------------------------------------------
PAYPAY_UNSET_MSG = ("このサーバーの受取PayPayアカウントが設定されていません。\n"
                    "サーバーの管理者が `/paypayログイン` で設定してください。")


def paypay_guild_file(guild_id):
    """そのサーバー専用のPayPay認証ファイルのパス（存在しなくても返す）。"""
    try:
        return os.path.join(PAYPAY_DIR, f"{int(guild_id)}.json")
    except (TypeError, ValueError):
        return PAYPAY_SHARED_FILE


def paypay_file_for(guild_id, fallback=None):
    """受け取りに使うPayPayファイル。設定されていなければ "" を返す。
    fallback=None なら、自分のサーバー（または paypay_fallback_shared が true）の
    ときだけ全体の paypay_token.json を使う。"""
    path = paypay_guild_file(guild_id)
    if path != PAYPAY_SHARED_FILE and os.path.exists(path):
        return path
    if fallback is None:
        try:
            fallback = int(guild_id) in rental_home_guild_ids()
        except (TypeError, ValueError):
            fallback = True
        fallback = fallback or bool(CONFIG.get("paypay_fallback_shared", False))
    if fallback and os.path.exists(PAYPAY_SHARED_FILE):
        return PAYPAY_SHARED_FILE
    return ""


# 貸し出し先の管理者向けの説明（コマンド本体の description より短く分かりやすく）
GUILD_COMMAND_NOTES = {
    "paypayログイン": "受け取り用のPayPayにログイン（最初にこれ）",
    "paypayotp": "SMSで届いたコードを入れて登録を完了",
    "paypay状態": "登録されている口座と状態を確認",
    "paypayログアウト": "登録した口座情報を削除",
    "チケットカテゴリー": "注文チケットを作るカテゴリーを設定",
    "パネル設置": "注文パネル（料金表）を設置",
    "パネル再設置": "注文パネルを設置し直す",
    "無料代行": "無料代行パネル（100万コイン・月1回）を設置",
    "実績チャンネル設定": "代行完了の実績を投稿するチャンネルを設定",
    "チケットチャンネル設定": "（予備）チケット用チャンネルを設定",
    "注文状況": "いま処理中の注文を表示",
    "サーバー貸し出し": "このサーバーの利用期限を確認（操作:確認）",
    "実績カウント": "実績の件数を確認",
    "実績カウント設定": "実績の件数を手動で直す",
    "実績カウンター設定": "件数を付けるチャンネルを別にする（既定は実績チャンネル）",
    "コマンド一覧": "使えるコマンドをもう一度表示",
}
GUILD_COMMAND_ORDER = [
    "paypayログイン", "paypayotp", "paypay状態", "paypayログアウト",
    "チケットカテゴリー", "パネル設置", "パネル再設置", "無料代行",
    "実績チャンネル設定", "実績カウント", "実績カウント設定", "実績カウンター設定",
    "チケットチャンネル設定", "注文状況", "サーバー貸し出し", "コマンド一覧",
]


def guild_admin_command_lines():
    """貸し出し先の管理者が使えるコマンドの一覧。"""
    have = {c.name: c for c in bot.tree.get_commands() if c.name in GUILD_ADMIN_COMMANDS}
    names = [n for n in GUILD_COMMAND_ORDER if n in have]
    names += sorted(n for n in have if n not in GUILD_COMMAND_ORDER)
    return [f"`/{n}` … {GUILD_COMMAND_NOTES.get(n) or have[n].description}" for n in names]


def owner_command_lines():
    """オーナーだけが使えるコマンドの一覧。"""
    names = sorted(c.name for c in bot.tree.get_commands()
                   if c.name not in GUILD_ADMIN_COMMANDS)
    return [f"`/{n}`" for n in names]


PAYPAY_SETUP_STEPS = (
    "**登録のしかた（サーバーの管理者が行ってください）**\n"
    "1. `/paypayログイン` に PayPayの電話番号とパスワードを入力\n"
    "2. SMSで届いたコードを `/paypayotp` に入力\n"
    "3. `/paypay状態` で「このサーバー専用」と表示されたら完了"
)


def paypay_setup_embed(guild):
    """サーバーに入ったとき最初に出す案内。受取PayPayの登録が最優先。"""
    if not paypay_file_for(guild.id):
        emb = discord.Embed(
            title="最初に受取PayPayを登録してください",
            description=(
                "このサーバーで代行を運営するには、受け取り用のPayPayアカウントの登録が必要です。\n"
                "**登録すると、このサーバーの代行料金はあなたのPayPayアカウントに直接送金されます。**\n\n"
                + PAYPAY_SETUP_STEPS +
                "\n\n**登録が済んだら**\n"
                "・`/チケットカテゴリー` … 注文チケットを作るカテゴリーを指定\n"
                "・`/パネル設置` … 注文パネルを設置\n\n"
                "※ 登録が済むまで、有料メニューの注文は受け付けられません。"
            ),
            color=0xf1c40f,
        )
        return _with_command_list(emb)
    emb = discord.Embed(
        title="ツムツム代行bot",
        description=(
            "受取PayPayは登録済みです（`/paypay状態` で確認できます）。\n\n"
            "・`/チケットカテゴリー` … 注文チケットを作るカテゴリーを指定\n"
            "・`/パネル設置` … 注文パネルを設置"
        ),
        color=0x2ecc71,
    )
    return _with_command_list(emb)


def _with_command_list(emb):
    """埋め込みに「使えるコマンド」欄を足す（Discordの1024文字制限に収める）。"""
    lines, text = guild_admin_command_lines(), ""
    for ln in lines:
        if len(text) + len(ln) + 1 > 1000:
            text += "…"
            break
        text += ln + "\n"
    emb.add_field(name="使えるコマンド（サーバー管理者のみ）", value=text.strip() or "なし",
                  inline=False)
    return emb


def paypay_file_label(path):
    if not path:
        return "未設定"
    return "全体共通" if path == PAYPAY_SHARED_FILE else "このサーバー専用"


async def _rental_notify(text, color=0x9b59b6):
    """貸し出しの記録用チャンネル（任意設定）へ通知する。"""
    cid = CONFIG.get("rental_log_channel_id")
    if not cid:
        return
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return
    ch = bot.get_channel(cid)
    if ch is None:
        try:
            ch = await bot.fetch_channel(cid)
        except Exception as e:
            _dbg(f"[rental] 通知チャンネル取得失敗 id={cid}: {e}")
            return
    try:
        await ch.send(embed=discord.Embed(description=text, color=color),
                      allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        _dbg(f"[rental] 通知送信失敗: {e}")


async def _rental_guild_expired(guild_id):
    """貸し出し期間が切れたサーバーの後始末（案内 → 設定によっては自動退出）。"""
    guild = bot.get_guild(guild_id)
    name = guild.name if guild else guild_id
    print(f"[rental] サーバー期限切れ: {name} ({guild_id})", flush=True)
    await _rental_notify(f"サーバー **{name}**（{guild_id}）の貸し出し期間が終了しました。", 0x95a5a6)
    if guild is None:
        return
    try:
        me = guild.me
        ch = guild.system_channel
        if ch is None or not ch.permissions_for(me).send_messages:
            ch = next((c for c in guild.text_channels
                       if c.permissions_for(me).send_messages), None)
        if ch is not None:
            await ch.send(embed=notice_embed(
                "botの利用期間が終了しました。延長をご希望の場合はオーナーへご連絡ください。"))
    except Exception as e:
        _dbg(f"[rental] 期限切れ案内の送信失敗 guild={guild_id}: {e}")
    if bool(CONFIG.get("rental_leave_unlicensed", False)):
        try:
            await guild.leave()
            print(f"[rental] 期限切れのため退出: {name} ({guild_id})", flush=True)
        except Exception as e:
            _dbg(f"[rental] 退出失敗 guild={guild_id}: {e}")


async def _rental_expiry_watcher():
    """期限切れの貸し出しを定期的に片付ける worker。"""
    await bot.wait_until_ready()
    while True:
        try:
            for gid in rental_prune():
                await _rental_guild_expired(gid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _dbg(f"[rental] watcher 例外: {e}")
        await asyncio.sleep(60)

#   完了(post_public_result)のたびに +1 して永続化し、その合計値を
#   指定チャンネル名の末尾に "-1234" の形で表示する。
#   Discord のチャンネル名変更は 10 分に 2 回までの制限があるため、
#   完了は即カウントしつつ、リネームは自前でレート制御して「変更できる
#   ようになった瞬間に最新の合計値へまとめて反映」する（溜まった分は
#   1 回のリネームに合流する）。
# ----------------------------------------------------------------------------
# 実績数はチャンネルのメッセージ数から数えるようになったため、
# 保存ファイル(tsum_achievements.json)は使わなくなった。
_ACHV_RENAME_LIMIT = 2        # 10 分あたりの最大リネーム回数
_ACHV_RENAME_WINDOW = 600     # 秒
_achv_rename_times = collections.deque()  # 直近の成功リネーム時刻
_achv_dirty = None            # asyncio.Event（setup_hook で生成）


# 実績数はチャンネルの実メッセージ数を正とする。
# ただし毎回数えると件数が増えたとき重い(Discordは100件ごとに1リクエスト)ので、
#   起動直後に1回数える → 以降はメモリ上で加算 → 一定間隔で数え直してズレを補正
# という持ち方にする。保存ファイルには依存しないので、消えても自己修復する。
ACHV_RECOUNT_SEC = 1800            # この間隔で数え直す(30分)
_ACHV_CACHE = {"count": None, "ts": 0.0}


def achievement_count():
    """今わかっている実績数。まだ数えていなければ 0。
    (実数の取得は非同期なので _achv_get_count を使う)"""
    n = _ACHV_CACHE["count"]
    return int(n) if isinstance(n, int) else 0


def set_achievement_count(n):
    """手動設定。次の数え直しで実際のメッセージ数に上書きされる。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    _ACHV_CACHE["count"] = n
    _ACHV_CACHE["ts"] = time.time()
    return n


def increment_achievement(step=1):
    """完了1件ぶん進める。チャンネルにも1件投稿されるので実数と一致する。
    まだ一度も数えていない場合は何もしない(次の数え直しで正しい値が入る)。"""
    if isinstance(_ACHV_CACHE["count"], int):
        _ACHV_CACHE["count"] += step
    return achievement_count()


async def _achv_get_count(channel, force=False):
    """実績数を返す。基本はメモリの値、起動直後と一定間隔だけ実際に数え直す。"""
    now = time.time()
    need = (_ACHV_CACHE["count"] is None
            or force
            or now - _ACHV_CACHE["ts"] >= ACHV_RECOUNT_SEC)
    if need:
        n = await _achv_count_messages(channel)
        if n is not None:
            before = _ACHV_CACHE["count"]
            _ACHV_CACHE["count"] = n
            _ACHV_CACHE["ts"] = now
            if before is not None and before != n:
                _dbg(f"[achv] 数え直し: {before} → {n} (ズレを補正)")
            else:
                _dbg(f"[achv] メッセージ数を数えました: {n}")
    return _ACHV_CACHE["count"]


def request_achievement_update():
    """実績カウンターのチャンネル名反映を要求する（レート制御は worker 側）。"""
    ev = _achv_dirty
    if ev is not None:
        try:
            ev.set()
        except Exception:
            pass


def _achv_base_name(name):
    """チャンネル名から末尾の "-数字" を取り除いたベース名を返す。
    ただし剥がすのは自分が付けた数字だけ。無条件に剥がすと
    「実績-2026」のような名前から年号を食べてしまうため。"""
    # 設定でベース名が明示されていれば、それをそのまま使う(推測しない)。
    # 推測に頼ると、剥がし損ねた数字が次のベース名に取り込まれて
    # 「実績-222-1」のように名前が連鎖的に壊れる。
    fixed = str(CONFIG.get("achievement_channel_base") or "").strip()
    if fixed:
        return fixed
    # 設定が無い場合は末尾の "-数字" を剥がす。
    # 実績数は毎回チャンネルのメッセージ数から数え直すので、末尾の数字は
    # 常に自分が付けたものとみなせる(保存値との突き合わせは不要になった)。
    # ただし「実績-2026」のような名前を使いたい場合は、上の
    # achievement_channel_base に正しいベース名を設定しておくこと。
    name = name or ""
    m = re.search(r"-(\d+)$", name)
    if not m:
        return name
    return name[:m.start()].rstrip() or name


async def _achv_count_messages(channel):
    """実績チャンネルの実メッセージ数を数える。
    保存値を足し引きする方式だとファイルが消えたりズレたりすると狂うが、
    毎回チャンネルを数え直せば常に実態と一致する(自己修復する)。
    数えられなければ None を返す(その場合リネームしない)。"""
    try:
        n = 0
        async for _ in channel.history(limit=None):
            n += 1
        return n
    except discord.Forbidden:
        _dbg("[achv] 履歴の読み取り権限がありません(メッセージ履歴を読む権限が必要)")
    except Exception as e:
        _dbg(f"[achv] メッセージ数の取得に失敗: {type(e).__name__}: {e}")
    return None


async def _achv_apply_name():
    """現在の合計値をチャンネル名へ反映する。
    戻り値: "renamed"(実際に変更した / レート枠を消費) /
            "noop"(対象なし・既に最新 / 枠を消費しない) /
            "fail"(通信・権限エラー / 後で再試行)。"""
    cid = CONFIG.get("achievement_channel_id")
    if not cid:
        return "noop"  # 未設定なら何もしない
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return "noop"
    guild = None
    for g in bot.guilds:
        if g.get_channel(cid):
            guild = g
            break
    channel = None
    if guild is not None:
        channel = guild.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception as e:
            _dbg(f"[achv] チャンネル取得失敗 id={cid}: {e}")
            return "fail"
    count = await _achv_get_count(channel)
    if count is None:
        return "fail"                      # 数えられなければ名前を変えない
    new_name = f"{_achv_base_name(channel.name)}-{count}"
    if channel.name == new_name:
        return "noop"  # 既に最新 → 枠を消費しない
    try:
        await channel.edit(name=new_name, reason="実績カウンター更新")
        _dbg(f"[achv] リネーム成功 -> {new_name} (メッセージ数 {count})")
        return "renamed"
    except discord.Forbidden:
        _dbg("[achv] リネーム権限がありません(チャンネルの管理権限を付与してください)")
        return "fail"
    except discord.HTTPException as e:
        _dbg(f"[achv] リネーム失敗(HTTP {getattr(e,'status','?')}): {e}")
        return "fail"
    except Exception as e:
        _dbg(f"[achv] リネーム失敗: {e}")
        return "fail"


async def _achievement_channel_updater():
    """実績カウンターのチャンネル名を、レート制限を守りつつ最新値へ反映する worker。"""
    await bot.wait_until_ready()
    while True:
        try:
            await _achv_dirty.wait()
            # 10分に2回の制限内に収まるまで待つ
            while True:
                now = time.time()
                while _achv_rename_times and now - _achv_rename_times[0] >= _ACHV_RENAME_WINDOW:
                    _achv_rename_times.popleft()
                if len(_achv_rename_times) < _ACHV_RENAME_LIMIT:
                    break
                wait_s = _ACHV_RENAME_WINDOW - (now - _achv_rename_times[0]) + 1.0
                _dbg(f"[achv] レート制限中。{int(wait_s)}秒後にまとめて反映します。")
                await asyncio.sleep(max(1.0, wait_s))
            # 反映直前に dirty をクリア。反映中に新しい実績が来たら再度立って次周回で反映される。
            _achv_dirty.clear()
            status = await _achv_apply_name()
            if status == "renamed":
                _achv_rename_times.append(time.time())  # 実変更のみ枠を消費
            elif status == "fail":
                # 権限・通信エラー等。少し待って再試行。
                await asyncio.sleep(30)
                _achv_dirty.set()
            # "noop" は何もしない（枠を消費せず、再試行もしない）
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _dbg(f"[achv] updater 例外: {e}")
            await asyncio.sleep(5)


def _free_key_account(login_id):
    s = unicodedata.normalize("NFKC", str(login_id or "")).strip().lower()
    s = re.sub(r"[\s\-()]", "", s)
    return "a:" + s if s else ""


def free_unlimited_enabled():
    return bool(CONFIG.get("free_unlimited_enabled", True))


def free_is_unlimited(user_id):
    if user_id is None or not free_unlimited_enabled():
        return False
    try:
        return int(user_id) in [int(x) for x in (CONFIG.get("free_unlimited_ids") or [])]
    except Exception:
        return False


def free_can_use(user_id=None, login_id=None, game_userid=None, days=30):
    data = _jload(FREE_USED_FILE, {})
    now = time.time()
    keys = []
    if user_id is not None and not free_is_unlimited(user_id):
        keys += [f"u:{user_id}", str(user_id)]
    if login_id:
        keys.append(_free_key_account(login_id))
    if game_userid:
        keys.append(f"g:{game_userid}")
    for k in keys:
        if not k:
            continue
        last = data.get(k)
        if last and (now - last) <= days * 86400:
            return False
    return True


def _free_write(mutate):
    data = _jload(FREE_USED_FILE, {})
    mutate(data)
    try:
        _jdump_atomic(FREE_USED_FILE, data)
    except Exception:
        pass


def free_mark_used(user_id=None, login_id=None, game_userid=None):
    now = time.time()
    def _m(data):
        if user_id is not None and not free_is_unlimited(user_id):
            data[f"u:{user_id}"] = now
        if login_id:
            k = _free_key_account(login_id)
            if k:
                data[k] = now
        if game_userid:
            data[f"g:{game_userid}"] = now
    _free_write(_m)


def free_unmark_used(user_id=None, login_id=None, game_userid=None):
    def _m(data):
        if user_id is not None:
            data.pop(f"u:{user_id}", None)
            data.pop(str(user_id), None)
        if login_id:
            k = _free_key_account(login_id)
            if k:
                data.pop(k, None)
        if game_userid:
            data.pop(f"g:{game_userid}", None)
    _free_write(_m)


intents = discord.Intents.default()
intents.message_content = True

class TsumBot(commands.Bot):
    async def setup_hook(self):
        try:
            self.add_view(MenuView())
        except Exception:
            pass
        try:
            self.add_view(TicketView())
        except Exception:
            pass
        try:
            self.add_view(FreeDaikouView())
        except Exception:
            pass
        try:
            self.add_view(RentalPanelView())
        except Exception:
            pass
        try:
            self.loop.create_task(_rental_expiry_watcher())
        except Exception as _e:
            print(f"[setup] 貸し出し期限 worker 起動失敗: {_e}")
        try:
            self.add_dynamic_items(RetryLoginButton)
        except Exception as _e:
            print(f"[setup] add_dynamic_items(RetryLoginButton) failed: {_e}")
        try:
            self.add_dynamic_items(GuestRetryButton)
        except Exception as _e:
            print(f"[setup] add_dynamic_items(GuestRetryButton) failed: {_e}")
        try:
            global _achv_dirty
            if _achv_dirty is None:
                _achv_dirty = asyncio.Event()
            self.loop.create_task(_achievement_channel_updater())
            self.loop.create_task(_discord_proxy_watchdog())
            self.loop.create_task(_proxy_pool_worker())
        except Exception as _e:
            print(f"[setup] 実績カウンター worker 起動失敗: {_e}")
        try:
            _admin_default = discord.Permissions(manage_guild=True)
            for _c in self.tree.walk_commands():
                try:
                    _c.guild_only = True
                    _c.default_permissions = _admin_default
                except Exception:
                    pass
            if GUILD_ID:
                g = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
            else:
                await self.tree.sync()
        except Exception as e:
            print(f"[bot] tree sync 失敗: {e}")

def _discord_proxy_url():
    """Discord(REST/ゲートウェイ)通信で使うプロキシURL。
    ツムツムbot独自の設定(bot_config.json の discord_proxy_url / 環境変数
    TSUM_DISCORD_PROXY)のみを見る。他プロジェクトの設定とは無関係に完全に独立させてある。
    PayPay はここに一切関与しない。"""
    env = os.environ.get("TSUM_DISCORD_PROXY", "").strip()
    url = env
    if not url:
        if not CONFIG.get("tor_enabled"):
            return None
        url = str(CONFIG.get("discord_proxy_url") or "").strip()
    if not url:
        return None
    # Privoxy等を入れていないホスティング環境でも起動できるように、
    # 繋がらないプロキシは指定しない(指定するとbotがDiscordに接続できず起動できない)。
    if not tsum_login._proxy_reachable(url):
        print(f"[bot] Discordプロキシ {url} に接続できません。直接接続で起動します。")
        return None
    return url


_DISCORD_PROXY = _discord_proxy_url()
print(f"[bot] Discord接続プロキシ: {_DISCORD_PROXY or '(未設定・直接接続)'}")

# 起動時に決めたプロキシ設定(監視タスクが参照する)。
# 環境変数/設定ファイルの値であって、「今使っているか」とは別。
_DISCORD_PROXY_CONFIGURED = (os.environ.get("TSUM_DISCORD_PROXY", "").strip()
                             or (str(CONFIG.get("discord_proxy_url") or "").strip()
                                 if CONFIG.get("tor_enabled") else ""))
DISCORD_PROXY_CHECK_SEC = 20


async def _discord_proxy_watchdog():
    """Discord用プロキシ(Privoxy)の生死を見張り、落ちたら直接接続に切り替える。

    discord.py は接続のたびに http.proxy を読むので、実行中に差し替えれば
    次の再接続から反映される。これが無いと、Privoxyが落ちた瞬間に
    botがDiscordへ一切繋がらなくなり、自力で復帰できない
    (再接続を延々と繰り返して事実上の停止になる)。"""
    if not _DISCORD_PROXY_CONFIGURED:
        return                                  # プロキシを使わない設定なら何もしない
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            alive = tsum_login._proxy_reachable(_DISCORD_PROXY_CONFIGURED, timeout=3.0)
            using = bot.http.proxy
            if not alive and using:
                bot.http.proxy = None
                print(f"[bot] Discordプロキシ {_DISCORD_PROXY_CONFIGURED} が落ちました。"
                      f"直接接続に切り替えます。", flush=True)
            elif alive and not using:
                bot.http.proxy = _DISCORD_PROXY_CONFIGURED
                print(f"[bot] Discordプロキシ {_DISCORD_PROXY_CONFIGURED} が復活しました。"
                      f"プロキシ経由に戻します。", flush=True)
        except Exception as e:
            print(f"[bot] Discordプロキシ監視でエラー: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(DISCORD_PROXY_CHECK_SEC)

bot = TsumBot(command_prefix="!", intents=intents, help_command=None, proxy=_DISCORD_PROXY)

paypay_lock = asyncio.Lock()
account_locks = {}
ticket_locks = {}


def account_lock_key(login_id):
    value = unicodedata.normalize("NFKC", login_id or "")
    value = "".join(value.split()).lower()
    if "@" in value:
        return f"mail:{value}"

    digits = re.sub(r"\D+", "", value)
    if digits.startswith("0081"):
        digits = "0" + digits[4:]
    elif digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    if len(digits) >= 8:
        return f"phone:{digits}"
    return value


def is_guest_account(login_id):
    s = unicodedata.normalize("NFKC", (login_id or "")).strip()
    if "@" in s:
        return False
    digits = re.sub(r"\D+", "", s)
    if digits.startswith("0081"):
        digits = "0" + digits[4:]
    elif digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    has_alpha = any(c.isalpha() for c in s)
    if not has_alpha and digits.startswith("0") and 10 <= len(digits) <= 11:
        return False
    return True


def get_account_lock(login_id):
    key = account_lock_key(login_id)
    if not key:
        key = "__empty__"
    lock = account_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        account_locks[key] = lock
    return key, lock


def get_ticket_lock(channel_id):
    key = str(channel_id or "__unknown__")
    lock = ticket_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        ticket_locks[key] = lock
    return key, lock


def clear_tsum_login_files(preserve_proxy=True):
    removed = []
    for path in (CREDS_FILE, TOKENS_FILE, SESSION_FILE):
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(os.path.basename(path))
            except Exception as e:
                print(f"[login] cleanup failed {path}: {e}")
    return removed


# save_input_account(): パスワードを平文でファイルに保存する関数だったが、
# どこからも呼ばれていなかったため削除した。
# CREDS_FILE 自体は clear_tsum_login_files() の掃除対象として残してある。

class CaptchaInputModal(discord.ui.Modal):
    def __init__(self, future):
        super().__init__(title="画像の文字を入力")
        self.future = future
        self.text = discord.ui.TextInput(
            label="画像の文字",
            placeholder="画像に表示されている文字",
            max_length=32,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        import unicodedata
        value = unicodedata.normalize("NFKC", self.text.value)
        value = "".join(value.split())
        if not value:
            await interaction.response.send_message(embed=notice_embed("文字を入力してください。"), ephemeral=True)
            return
        if not self.future.done():
            self.future.set_result(value)
        await interaction.response.send_message("入力を受け付けました。", ephemeral=True)

def can_operate_ticket(interaction: discord.Interaction, owner_id=None) -> bool:
    if owner_id and interaction.user.id == owner_id:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_channels", False)
        or getattr(perms, "manage_threads", False)
    )

# 貸し出し先サーバーの管理者が使えるコマンド（オーナーはすべて使える）。
# 料金・収益・PayPay・実績カウンターなどオーナーの商売に関わるものは含めない。
GUILD_ADMIN_COMMANDS = {
    "パネル設置", "パネル再設置", "無料代行", "チケットカテゴリー",
    "チケットチャンネル設定", "実績チャンネル設定", "注文状況", "サーバー貸し出し",
    # 受取PayPayはサーバーごとなので、貸出先の管理者が自分で設定する
    "paypayログイン", "paypayotp", "paypay状態", "paypayログアウト",
    # 実績カウンターもサーバーごとなので、その3つも貸出先で使えるようにする
    "実績カウント", "実績カウント設定", "実績カウンター設定",
    "コマンド一覧",
}

def is_bot_admin(interaction: discord.Interaction) -> bool:
    try:
        ids = [int(x) for x in (CONFIG.get("allowed_user_ids") or [])]
        if ids and interaction.user.id in ids:
            return True
    except Exception:
        pass
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms:
        return False
    return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

async def _tree_admin_only(interaction: discord.Interaction) -> bool:
    owner = is_bot_owner(interaction.user)
    # 1) 貸し出されていない(期限切れの)サーバーでは動かさない
    if not owner and await deny_unlicensed(interaction):
        return False
    if owner:
        return True
    if not is_bot_admin(interaction):
        try:
            await interaction.response.send_message(
                embed=notice_embed("このコマンドは管理者のみ使用できます。"), ephemeral=True)
        except Exception:
            pass
        return False
    # 2) 貸し出し先サーバーの管理者は、そのサーバー用のコマンドだけ
    gid = interaction.guild_id
    name = getattr(interaction.command, "name", "")
    if (guild_lock_enabled() and gid is not None
            and int(gid) not in rental_home_guild_ids()
            and name not in GUILD_ADMIN_COMMANDS):
        try:
            await interaction.response.send_message(
                embed=notice_embed("このコマンドは bot のオーナーのみ使用できます。"), ephemeral=True)
        except Exception:
            pass
        return False
    return True

bot.tree.interaction_check = _tree_admin_only


@bot.tree.error
async def _on_app_command_error(interaction: discord.Interaction, error: Exception):
    # インタラクションのタイミング系エラー(3秒ルール超過/二重応答)は
    # トレースバックを吐かず、ログに1行だけ残して握りつぶす。
    #   40060 = Interaction has already been acknowledged
    #   10062 = Unknown interaction (トークン期限切れ)
    err = getattr(error, "original", error)
    code = getattr(err, "code", None)
    if isinstance(err, (discord.NotFound, discord.HTTPException)) and code in (40060, 10062):
        _dbg(f"[interaction] タイミング系エラーを無視 (code={code}) cmd={getattr(interaction.command,'name',None)}")
        return
    _dbg(f"[app_command_error] {type(err).__name__}: {err}")
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=notice_embed("処理中にエラーが発生しました。もう一度お試しください。"), ephemeral=True)
    except Exception:
        pass


class CaptchaView(discord.ui.View):
    def __init__(self, user_id, future):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.future = future

    async def interaction_check(self, interaction: discord.Interaction):
        if not can_operate_ticket(interaction, self.user_id):
            await interaction.response.send_message(embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="画像の文字を入力", style=discord.ButtonStyle.success)
    async def input_captcha(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(CaptchaInputModal(self.future))
        except discord.NotFound:
            # 受け付けが期限切れ(10062)。ボタンを押し直せばやり直せる。
            print("[interaction] CAPTCHA入力欄の表示が期限切れ。押し直しで再表示できます。")
        except discord.HTTPException as e:
            print(f"[interaction] CAPTCHA入力欄の表示に失敗: {e}")

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.future.done():
            self.future.set_result("")
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        if not self.future.done():
            self.future.set_result("")

def make_solver(channel, user, loop):
    def solver(image_bytes):
        fut = asyncio.run_coroutine_threadsafe(_ask_captcha(channel, user, image_bytes), loop)
        try:
            return fut.result(timeout=200)
        except Exception:
            return ""
    return solver


def make_pin_notice(channel, loop):
    def notice(pincode):
        emb = discord.Embed(
            description=f"LINEアプリで本人確認の番号を入力してください。\nPIN: **{pincode}**",
            color=0xf1c40f,
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(channel.send(embed=emb), loop)
            fut.result(timeout=30)
        except Exception:
            print("[pin] notice send failed")
    return notice

# --- 進行状況の表示 ---------------------------------------------------
# 代行は Tor 経由だと数十秒〜数分かかる。何も出ないと固まったように見えるので、
# 今どの段階かを1枚のメッセージに出し、進むたびに編集して更新する。
PROGRESS_STAGES = [
    "アカウント認証",
    "ゲームデータ取得",
    "代行処理",
    "データ反映",
    "完了",
]
PROGRESS_MIN_INTERVAL = 1.5      # 秒。Discordの編集レート制限に配慮して詰めすぎない


class ProgressReporter:
    """代行の進行状況を1枚のメッセージで見せる。

    forge_sync はワーカースレッドで動くので、そこから直接 await できない。
    make_pin_notice と同じく run_coroutine_threadsafe でループへ渡す。
    表示が出せなくても代行自体は止めない(あくまで表示なので握りつぶす)。"""

    def __init__(self, channel, loop):
        self.channel = channel
        self.loop = loop
        self.message = None
        self.stage = 0
        self.note = ""
        self._last_edit = 0.0
        self._done = False

    def _embed(self):
        total = len(PROGRESS_STAGES)
        done = total if self._done else self.stage
        failed = getattr(self, "_failed", False)
        pct = int(done / total * 100)
        filled = int(pct / 10)
        bar = "▓" * filled + "░" * (10 - filled)
        lines = []
        for i, name in enumerate(PROGRESS_STAGES):
            if self._done or i < self.stage:
                lines.append(f"✅ {name}")
            elif i == self.stage:
                lines.append(f"🔄 **{name}**")
            else:
                lines.append(f"⏳ {name}")
        desc = f"`{bar}` {pct}%\n\n" + ("\n".join(lines))
        if self.note:
            desc += "\n\n現在: " + self.note
        if failed:
            title, color = "ツムツム代行 中断", 0xe74c3c
        elif self._done:
            title, color = "ツムツム代行 完了", 0x2ecc71
        else:
            title, color = "ツムツム代行を実行中...", 0x3498db
        return discord.Embed(title=title, description=desc, color=color)

    async def _start(self):
        try:
            self.message = await self.channel.send(embed=self._embed())
        except Exception as e:
            print(f"[progress] 表示の作成に失敗: {type(e).__name__}")

    async def _edit(self):
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self._embed())
        except Exception:
            pass                      # 消された/権限が無い等。表示だけなので黙って諦める

    def start(self):
        """最初のメッセージを出す(ワーカースレッドから呼べる)。"""
        try:
            asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=20)
        except Exception:
            pass

    def update(self, stage=None, note=None, force=False):
        """段階や補足を更新する。短時間に何度呼んでも編集は間引く。"""
        if stage is not None:
            self.stage = stage
        if note is not None:
            self.note = note
        now = time.time()
        if not force and now - self._last_edit < PROGRESS_MIN_INTERVAL:
            return
        self._last_edit = now
        try:
            asyncio.run_coroutine_threadsafe(self._edit(), self.loop)
        except Exception:
            pass

    def finish(self, ok=True):
        self._failed = not ok
        if ok:
            self._done = True
            self.stage = len(PROGRESS_STAGES)
            self.note = ""
        else:
            self.note = "途中で失敗しました。下のメッセージをご確認ください。"
        try:
            fut = asyncio.run_coroutine_threadsafe(self._edit(), self.loop)
            fut.result(timeout=15)
        except Exception:
            pass


def notice_embed(text, color=0xe74c3c):
    return discord.Embed(description=text, color=color)


async def _safe_defer(interaction, ephemeral=True):
    """インタラクションの受け付け(defer)。3秒以内に応答できないとDiscord側で
    トークンが失効し 10062 Unknown interaction になる。Discord通信をTor経由に
    しているぶん遅延が乗るので起こりやすい。失敗しても処理自体は続けたいので
    ここで握って True/False を返す。"""
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        print("[interaction] 受け付けが期限切れ(10062)。処理は続行します。")
    except discord.HTTPException as e:
        print(f"[interaction] 受け付けに失敗: {e}")
    return False


def improve_captcha_image(image_bytes):
    try:
        from PIL import Image, ImageEnhance, ImageOps
    except Exception:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes or b"")).convert("RGBA")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        white.alpha_composite(img)
        img = white.convert("L")
        img = ImageOps.autocontrast(img)
        img = ImageEnhance.Contrast(img).enhance(1.8)
        img = ImageEnhance.Sharpness(img).enhance(1.4)
        img = img.resize((img.width * 3, img.height * 3), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        print(f"[captcha] improve failed: {e}")
        return image_bytes

async def _ask_captcha(channel, user, image_bytes):
    try:
        image_bytes = improve_captcha_image(image_bytes)
        f = discord.File(io.BytesIO(image_bytes or b""), filename="captcha.png")
        await channel.send(file=f)
    except Exception as e:
        print(f"[captcha] send failed: {e}")
        await channel.send(embed=notice_embed("CAPTCHA画像を送信できませんでした。もう一度試してください。"))
        return ""

    fut = asyncio.get_running_loop().create_future()
    view = CaptchaView(user.id, fut)
    emb = discord.Embed(
        description="下のボタンから画像の文字を入力してください。",
        color=0xf1c40f,
    )
    prompt = await channel.send(embed=emb, view=view)
    text = await fut
    for child in view.children:
        child.disabled = True
    try:
        await prompt.edit(view=view)
    except Exception:
        pass
    if not text:
        await channel.send(embed=notice_embed("CAPTCHA入力がキャンセルまたはタイムアウトしました。"))
        return ""
    return text.strip()

def _failure_hint_from_response(resp):
    if not isinstance(resp, dict):
        return "原因を特定できませんでした"
    retcode = resp.get("retcode")
    retsubcode = resp.get("retsubcode")
    msg = " ".join(str(resp.get(k, "")) for k in ("retmsg", "message", "errmsg", "error"))
    low = msg.lower()
    if retcode == 8 and retsubcode == 2:
        return "ゲーム開始時の入力パラメータが不正です"
    if "heart" in low or "ハート" in msg:
        return "ハート不足、またはプレイ開始条件が足りていません"
    if any(k in low for k in ("session", "token", "checkval", "hash", "auth", "login")):
        return "ログイン状態が途中で無効になりました"
    if any(k in low for k in ("version", "appver", "resver", "update")) or "更新" in msg:
        return "アプリのバージョン情報が合っていません"
    if any(k in low for k in ("maintenance", "maint")) or "メンテ" in msg:
        return "ゲーム側がメンテナンス中の可能性があります"
    if any(k in low for k in ("ban", "block", "restrict", "limit", "forbidden")) or any(k in msg for k in ("制限", "拒否", "禁止")):
        return "通信またはアカウントが制限されています"
    if "tsum" in low or "ツム" in msg:
        return "ツム設定が合っていない可能性があります"
    if retcode not in (None, 0, "0"):
        return "ゲーム側が処理を拒否しました"
    return "必要なデータが返ってきませんでした"

def describe_tsum_api_failure(stage, resp=None, forge=None, missing_data=False):
    status = getattr(forge, "last_http_status", None)
    error = getattr(forge, "last_error", None)

    if error == "proxy_error":
        return f"{stage}に失敗しました（VPN/プロキシ接続エラー）。VPN設定と接続先を確認して、もう一度お試しください。"
    if error == "timeout":
        return f"{stage}に失敗しました（通信タイムアウト）。VPNまたは回線が不安定な可能性があります。"
    if error == "network_error":
        return f"{stage}に失敗しました（通信エラー）。VPN、回線、接続先IPを確認してください。"
    if status in (401, 403):
        return f"{stage}に失敗しました（通信が拒否されました）。VPNのIP、アカウント制限、ログイン状態のどれかが原因の可能性があります。"
    if status == 404:
        return f"{stage}に失敗しました（接続先が見つかりません）。アプリのバージョン情報が合っていない可能性があります。"
    if status and status >= 500:
        return f"{stage}に失敗しました（ゲーム側の応答エラー）。少し待ってからもう一度お試しください。"
    if error == "decrypt_failed":
        return f"{stage}に失敗しました（ゲーム側の応答を読み取れませんでした）。VPN/IP制限、ログイン状態、アプリのバージョン情報を確認してください。"
    if isinstance(resp, str):
        return f"{stage}に失敗しました（想定外の応答）。VPN/IP制限、ログイン状態、アプリのバージョン情報を確認してください。"

    hint = _failure_hint_from_response(resp)
    if missing_data and hint == "必要なデータが返ってきませんでした":
        return f"{stage}に失敗しました（開始用データが返ってきませんでした）。ログイン状態、VPN/IP、アカウント状態を確認してください。"
    return f"{stage}に失敗しました（{hint}）。もう一度お試しください。"

def describe_login_failure(reason):
    reason = str(reason or "").strip()
    low = reason.lower()
    if "PIN" in reason.upper() or "pincode" in low or "端末認証" in reason:
        return "LINEの本人確認（PIN）が完了しませんでした。LINEアプリで番号を入力してから、もう一度お試しください"
    if "captcha" in low or "画像認証" in reason:
        return ("画像認証が繰り返し出てログインできませんでした。LINE側で一時的に制限がかかっている可能性が高いので、"
                "20〜30分ほど時間を置いてからお試しください（続けて試すと制限が延びることがあります）。")
    if "通信エラー" in reason or "接続が不安定" in reason or "プロキシ" in reason:
        return "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください"
    if "アカウント情報が違う" in reason or "errorcode=445" in low:
        return "メールアドレスかパスワードが違います"
    if ("中断されました" in reason or "追加認証" in reason or "一時制限" in reason
            or "連続試行" in reason or "errorcode=446" in low or "errorcode=401" in low):
        return "LINE側で一時的にログイン制限中です。時間をおいて再度お試しください"
    return "メールアドレスかパスワードが違います"

def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _is_owned_tsum(row):
    if not isinstance(row, dict):
        return False
    if row.get("setflg") in (1, "1", True):
        return True
    for key in ("getflg", "ownflg", "haveflg", "possessionflg"):
        if row.get(key) in (1, "1", True):
            return True
    return _to_int(row.get("lv") or row.get("level")) > 0

def select_play_tsumids(forge, requested_tsumid=None):
    requested = _to_int(requested_tsumid)
    info = forge.get_info()
    if not isinstance(info, dict):
        return None, None, describe_tsum_api_failure("ツム情報取得", info, forge)

    tsum_rows = [t for t in (info.get("tsuminfo") or []) if isinstance(t, dict)]
    owned = [t for t in tsum_rows if _is_owned_tsum(t) and _to_int(t.get("tsumid"))]
    candidates = []

    def add_candidate(tsumid):
        tsumid = _to_int(tsumid)
        if tsumid and tsumid not in candidates:
            candidates.append(tsumid)

    if requested:
        for t in owned:
            if _to_int(t.get("tsumid")) == requested:
                add_candidate(requested)

    for t in owned:
        if t.get("setflg") in (1, "1", True):
            add_candidate(t.get("tsumid"))
    if owned:
        for t in owned:
            add_candidate(t.get("tsumid"))
    if candidates:
        return candidates[:20], info, None
    return None, info, "使用できるツムが見つかりませんでした。ゲームを開いてマイツムをセットしてから、もう一度お試しください。"

def heart_count_from_info(info):
    ui = (info.get("userinfo") or {}) if isinstance(info, dict) else {}
    return _to_int(ui.get("bheart")) + _to_int(ui.get("pheart"))

def heart_state_from_info(info):
    ui = (info.get("userinfo") or {}) if isinstance(info, dict) else {}
    bheart = _to_int(ui.get("bheart"))
    pheart = _to_int(ui.get("pheart"))
    return bheart, pheart, bheart + pheart

def heart_info_is_reliable(info):
    """getInfo が本当に成功して userinfo を返したときだけ True。
    retcode 201 などのエラー応答も dict なので、そのまま bheart/pheart を読むと
    「ハート0＝ハート不足」と誤判定してしまう。それを防ぐためのガード。"""
    if not isinstance(info, dict):
        return False
    rc = info.get("retcode")
    if rc is None or _to_int(rc) != 0:
        return False
    ui = info.get("userinfo")
    return isinstance(ui, dict) and ("bheart" in ui or "pheart" in ui)

def hearttype_candidates_from_info(info):
    bheart, pheart, total = heart_state_from_info(info)
    if total < 1:
        return []
    candidates = []
    if bheart > 0:
        candidates.append(0)
    if pheart > 0:
        candidates.append(1)
    for value in (0, 1):
        if value not in candidates:
            candidates.append(value)
    return candidates

_EVENT_CACHE = {"ts": 0.0, "cands": None}

def _cached_event_candidates(f, ttl=900):
    now = time.time()
    if _EVENT_CACHE["cands"] and (now - _EVENT_CACHE["ts"] < ttl):
        return _EVENT_CACHE["cands"]
    cands = f.event_candidates()
    if cands:
        _EVENT_CACHE["cands"] = cands
        _EVENT_CACHE["ts"] = now
    return cands


_GACHA_CACHE = {"ts": 0.0, "ids": None}

def _cached_gacha_ids(f, ttl=600):
    now = time.time()
    if _GACHA_CACHE["ids"] and (now - _GACHA_CACHE["ts"] < ttl):
        return _GACHA_CACHE["ids"]
    m = f.get_mast()
    if not isinstance(m, dict) or m.get("retcode") not in (0, None):
        return None
    rows = m.get("gachamst")
    if not isinstance(rows, list) or not rows:
        return None
    by_type = {}
    for g in rows:
        if not isinstance(g, dict):
            continue
        try:
            t = int(g.get("type"))
            gid = int(g.get("gachaid"))
        except Exception:
            continue
        by_type.setdefault(t, gid)
    box_id = by_type.get(41)
    if not box_id and CONFIG.get("box_fallback_premium_box"):
        box_id = by_type.get(64)
    ids = {"premium": by_type.get(11), "box": box_id, "has_select_box": bool(by_type.get(41))}
    if ids["premium"] or ids["box"]:
        _GACHA_CACHE["ids"] = ids
        _GACHA_CACHE["ts"] = now
    return ids


def resolve_gacha_id(f, kind, fallback):
    try:
        ids = _cached_gacha_ids(f)
        if ids is not None:
            gid = ids.get(kind)
            if gid:
                if int(gid) != int(fallback):
                    _dbg(f"[gacha] {kind}: config={fallback} → 現行={gid} に自動切替")
                return int(gid), "auto"
            _dbg(f"[gacha] {kind}: 現在開催されていません(config={fallback}は古い可能性)")
            return None, "not_running"
    except Exception as e:
        print(f"[gacha] 自動取得に失敗→configの値を使用: {type(e).__name__}", flush=True)
    return int(fallback), "fallback"


def forge_sync(score=None, coin=None, exp=None, medal=5, tsumid=860, tsum_lv=None, box=None,
               login_id=None, password=None, captcha_solver=None, proxy=None, sess=None, f=None,
               pin_notice=None, gacha_full=None, on_login=None, stage_box=None, progress=None):
    # stage_box: 失敗がログイン段階か実行段階かを呼び出し元に伝えるための箱
    if stage_box is None:
        stage_box = {}
    stage_box["stage"] = "login"
    if progress:
        progress.update(0, "アカウント認証を開始しています", force=True)
    if sess is None or f is None:
        if not login_id or not password:
            return None, "アカウント情報を入力してください。"
        sess = None
        login_error = {}
        pin_state = {"shown": False}
        def _pin_relay(pincode):
            pin_state["shown"] = True
            if pin_notice:
                try:
                    pin_notice(pincode)
                except Exception:
                    pass
        captcha_state = {"shown": False}
        def _captcha_relay(img_bytes):
            captcha_state["shown"] = True
            return captcha_solver(img_bytes) if captcha_solver else ""
        for _try in range(3):
            login_error = {}
            if _try > 0:
                # リトライは新しいTor回線(=別IP)で試す。同じ回線のまま3回叩くと、
                # たまたま繋がりの悪い出口ノードに当たっただけで3回とも同じ理由で
                # 失敗してしまうため。
                fresh = _unique_game_proxy()
                if fresh:
                    proxy = fresh
                    _dbg(f"[login] リトライ{_try+1}回目: 別のTor回線(未使用IP)に切替")
            try:
                sess = tsum_login.headless_session_direct(
                    login_id, password, proxy=proxy, game_proxy=_game_api_proxy(proxy),
                    captcha_solver=_captcha_relay, save=False, verbose=False, error_box=login_error,
                    pin_notice=_pin_relay,
                )
            except (requests.exceptions.RequestException, ValueError) as e:
                sess = None
                _dbg(f"[login例外] id={str(login_id)[:3]}*** try={_try+1} {type(e).__name__}: {str(e)[:150]}")
                if _try < 2 and not pin_state["shown"] and not captcha_state["shown"]:
                    time.sleep(0.6)
                    continue
                return None, "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください。"
            if sess:
                if on_login:
                    try:
                        on_login(str(sess.get("userid") or ""))
                    except LoginHookAbort as _abort:
                        # 例: このゲームアカウントは無料枠を使用済み → ここで確実に止める
                        _dbg(f"[on_login] 中止: {_abort.user_msg}")
                        return None, _abort.user_msg
                    except Exception as _e:
                        _dbg(f"[on_login] 想定外の例外(続行): {type(_e).__name__}: {str(_e)[:150]}")
                break
            _raw = login_error.get("error") or ""
            _dbg(f"[login失敗] id={str(login_id)[:3]}*** try={_try+1} raw={_raw!r}")
            low = _raw.lower()
            ip_blocked = ("errorcode=446" in low or "errorcode=401" in low or "中断されました" in _raw
                          or "プロキシ接続エラー" in _raw or "通信エラー" in _raw)
            if ip_blocked and _try < 2 and not pin_state["shown"] and not captcha_state["shown"]:
                continue
            return None, describe_login_failure(_raw)
        if not sess:
            return None, describe_login_failure(login_error.get("error"))
        f = Forge(userid=sess["userid"], hashv=sess["hash"], checkval=sess["checkval"],
                  proxy=_game_api_proxy(proxy))
        try:
            f.get_public_key()
        except Exception as e:
            _dbg(f"[getPublicKey例外] {type(e).__name__}: {str(e)[:150]}")
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
    # ここまで来たらログインは成功している
    stage_box["stage"] = "action"
    if progress:
        progress.update(1, "ゲームデータを取得しています", force=True)

    if tsum_lv is not None:
        tid = int(tsum_lv)
        if tid == 0:
            info = f.get_info()
            if not isinstance(info, dict):
                return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
            for t in (info.get("tsuminfo") or []):
                if t.get("setflg") in (1, "1"):
                    tid = t.get("tsumid"); break
            if not tid:
                return None, "セット中のマイツムが見つかりませんでした。ゲームでマイツムをセットしてから、もう一度お試しください。"
        if progress:
            progress.update(2, "ツムのレベルを上げています", force=True)
        res = f.max_tsum_level(tid, target=50, min_heart=10, sleep_s=0.3, log=_dbg)
        st = res.get("status")
        # 失敗系はデバッグログに詳細(サーバー応答含む)を残す
        if st != "done":
            _dbg(f"[tsum_lv失敗] tsumid={tid} status={st} detail={res}")
        if st == "done":
            return res, "セット中のツムをレベルMAXにしました\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
        if st == "api_error":
            return None, describe_tsum_api_failure("ツムのレベルMAX", res.get("resp"), f, missing_data=True)
        if st == "insufficient_heart":
            return None, f"ハートが足りないため中断しました（所持 {res['have']} / 必要 {res['need']}）。ハートを貯めてからもう一度お試しください。"
        if st == "heart_ran_out":
            return None, "ハートが切れたため途中で終了しました。ハートを補充すると続きから実行できます。"
        if st == "stuck":
            return None, "途中で中断しました。もう一度お試しください。"
        if st == "level_error":
            return None, ("プレイ開始をサーバーに拒否されました（アカウントがチュートリアル未完了/"
                          "イベントIDやハート状態の不一致の可能性）。ゲームをホームまで進めてから再度お試しください。"
                          f"\n[詳細] {str(res.get('resp'))[:150]}")
        if st == "release_error":
            return None, ("レベル上限の開放をサーバーに拒否されました（コイン不足などの可能性）。"
                          f"\n[詳細] {str(res.get('resp'))[:150]}")
        if st == "max_iter":
            return None, f"規定回数を試しましたがレベルMAXに届きませんでした（現在lv={res.get('lv')}）。もう一度お試しください。"
        return None, f"ツムのレベルMAXに失敗しました（status={st}）。もう一度お試しください。"

    if box is not None:
        need = 30_000_000
        box, _st = resolve_gacha_id(f, "box", box)
        if _st == "not_running":
            return None, "現在セレクトBOXが開催されていないため実行できません。"
        if progress:
            progress.update(2, "セレクトBOXを引いています", force=True)
        res = f.complete_box(int(box), min_coin=need, sleep_s=0, log=_dbg)
        st = res.get("status")
        if st in ("complete", "already_complete"):
            return res, f"セレクトBOXを完売しました ({res.get('draws','-')}連)\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。"
        if st == "api_error":
            return None, describe_tsum_api_failure("セレクトBOXの完売", res.get("resp"), f, missing_data=True)
        if st == "insufficient_coin":
            return None, f"コインが足りないため中断しました（所持 {res['have']:,} / 必要 {res['need']:,}）。先にコインを増やしてからお試しください。"
        if st == "coin_ran_out":
            return None, "コインが切れたため途中で終了しました。もう一度お試しください。"
        _dbg(f"[box失敗] gachaid={box} status={st} detail={res}")
        if st == "draw_error":
            return None, ("ガチャの抽選をサーバーに拒否されました"
                          f"（{res.get('draws', 0)}連時点）。\n"
                          "開催中の別のBOXに切り替わった可能性があります。\n"
                          f"[詳細] {str(res.get('resp'))[:150]}")
        if st == "max_reached":
            return None, f"上限まで引きましたが完売しませんでした（{res.get('draws','-')}連）。もう一度お試しください。"
        return None, f"セレクトBOXの完売に失敗しました（status={st}）。もう一度お試しください。"

    if gacha_full is not None:
        gacha_full, _st = resolve_gacha_id(f, "premium", gacha_full)
        if _st == "not_running":
            return None, "現在プレミアムガチャが開催されていないため実行できません。"
        if progress:
            progress.update(2, "プレミアムガチャを引いています", force=True)
        res = f.complete_box(int(gacha_full), min_coin=300_000, sleep_s=0, log=_dbg)
        st = res.get("status")
        if st in ("complete", "already_complete"):
            return res, f"プレミアムガチャを完売しました ({res.get('draws','-')}連)\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。"
        if st == "api_error":
            return None, describe_tsum_api_failure("プレミアムガチャの完売", res.get("resp"), f, missing_data=True)
        if st == "insufficient_coin":
            return None, f"コインが足りません（所持 {res['have']:,}）。先にコインを増やしてからお試しください。"
        if st == "coin_ran_out":
            return None, f"コインが足りず途中で終了しました（{res.get('draws','-')}連）。コインを増やしてからお試しください。"
        _dbg(f"[premium失敗] gachaid={gacha_full} status={st} detail={res}")
        if st == "draw_error":
            return None, ("ガチャの抽選をサーバーに拒否されました"
                          f"（{res.get('draws', 0)}連時点）。\n"
                          "開催中の別のガチャに切り替わった可能性があります。\n"
                          f"[詳細] {str(res.get('resp'))[:150]}")
        if st == "max_reached":
            return None, f"上限まで引きましたが完売しませんでした（{res.get('draws','-')}連）。もう一度お試しください。"
        return None, f"プレミアムガチャの完売に失敗しました（status={st}）。もう一度お試しください。"

    if progress:
        progress.update(2, "代行処理を実行しています", force=True)
    candidates, _info, _err = select_play_tsumids(f, tsumid)
    tsum_candidates = list(candidates or [])[:3]
    if isinstance(_info, dict):
        for t in (_info.get("tsuminfo") or []):
            tid = _to_int(t.get("tsumid")) if isinstance(t, dict) else 0
            if tid and tid not in tsum_candidates and len(tsum_candidates) < 5:
                tsum_candidates.append(tid)
    if tsumid and tsumid not in tsum_candidates:
        tsum_candidates.append(tsumid)
    if not tsum_candidates:
        tsum_candidates = [tsumid]

    ht_candidates = hearttype_candidates_from_info(_info) or [0, 1]
    bheart, pheart, total_heart = heart_state_from_info(_info)
    _set_tsum = next((t.get("tsumid") for t in (_info.get("tsuminfo") or [])
                      if isinstance(t, dict) and t.get("setflg") in (1, "1", True)), None) if isinstance(_info, dict) else None
    uid = str(sess.get("userid") or "")
    try:
        ev_order = _cached_event_candidates(f)
    except Exception as _e:
        ev_order = ["9999", "0"]
        print(f"[gs] event_candidates失敗→fallback: {_e}", flush=True)
    if not ev_order:
        ev_order = ["9999", "0"]
    _dbg(f"[gs v7] userid={uid[:3]}… set_tsum={_set_tsum} bheart={bheart} pheart={pheart} "
         f"ht={ht_candidates} tsum候補={tsum_candidates[:4]} eventid候補={ev_order}")

    r1 = None
    playcode = None
    won_tid = None
    won_ht = None
    won_ev = None
    attempts = []
    def _attempt(tid, ht, ev):
        nonlocal r1, playcode, won_tid, won_ht, won_ev
        r1 = f.game_start(tsumid=tid, hearttype=ht, probmstver="2", eventid=ev)
        rc = (r1.get("retcode"), r1.get("retsubcode")) if isinstance(r1, dict) else None
        attempts.append((tid, ht, f"ev{ev}", rc))
        playcode = (r1.get("userinfo") or {}).get("playcode") or r1.get("playcode") if isinstance(r1, dict) else None
        if playcode:
            won_tid = tid; won_ht = ht; won_ev = ev
        return playcode

    play_tids = []
    for t in ([_set_tsum] + list(tsum_candidates) + [tsumid]):
        if t and t not in play_tids:
            play_tids.append(t)
    for tid in play_tids[:4]:
        for ht in ht_candidates:
            for ev in ev_order:
                if _attempt(tid, ht, ev):
                    break
                time.sleep(0.15)
            if playcode:
                break
        if playcode:
            break
    before_coin = (r1.get("userinfo") or {}).get("bcoin") if isinstance(r1, dict) else None
    if not playcode:
        _EVENT_CACHE["cands"] = None
        try:
            ui = (_info.get("userinfo") or {}) if isinstance(_info, dict) else {}
            set_tsum = next((t.get("tsumid") for t in (_info.get("tsuminfo") or [])
                             if isinstance(t, dict) and t.get("setflg") in (1, "1", True)), None) if isinstance(_info, dict) else None
            dump = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "userid": sess.get("userid"),
                "hashlen": len(str(sess.get("hash") or "")),
                "checkval_present": bool(sess.get("checkval")),
                "getInfo_retcode": _info.get("retcode") if isinstance(_info, dict) else f"non-dict({type(_info).__name__})",
                "getInfo_keys": sorted(_info.keys()) if isinstance(_info, dict) else None,
                "userinfo": ui,
                "n_tsum": len([t for t in (_info.get("tsuminfo") or []) if isinstance(t, dict)]) if isinstance(_info, dict) else 0,
                "set_tsum": set_tsum,
                "sample_tsums": [t for t in ((_info.get("tsuminfo") or [])[:5]) if isinstance(t, dict)] if isinstance(_info, dict) else [],
                "attempts": [list(a) for a in attempts],
                "gameStart_response": r1 if isinstance(r1, dict) else repr(r1),
            }
            with open(os.path.join(HERE, "tsum_gs_fulldump.json"), "w", encoding="utf-8") as df:
                json.dump(dump, df, ensure_ascii=False, indent=2)
            with open(os.path.join(HERE, "tsum_gs_diag.log"), "a", encoding="utf-8") as lf:
                lf.write(f"{dump['ts']} GS_FAIL userid={dump['userid']} getInfo_rc={dump['getInfo_retcode']} "
                         f"n_tsum={dump['n_tsum']} set_tsum={set_tsum} bheart={bheart} pheart={pheart} "
                         f"attempts={attempts}  (full -> tsum_gs_fulldump.json)\n")
        except Exception as _e:
            print(f"[gs_diag] dump failed: {_e}")
        if heart_info_is_reliable(_info) and total_heart < 1:
            return None, "ハートが足りないため開始できませんでした。ハートが回復してから、もう一度お試しください。"
        if not heart_info_is_reliable(_info):
            # getInfo 自体が拒否されている場合は、ハートではなく本当の原因を返す
            return None, describe_tsum_api_failure("ゲーム開始", _info if isinstance(_info, dict) else r1,
                                                   f, missing_data=True)
        return None, describe_tsum_api_failure("ゲーム開始", r1, f, missing_data=True)
    vanishcnt = f"{won_tid},0|9,0|12,0|309,0|4,0|" if won_tid else None

    credited = 0
    games = 0
    chunk_field = "exp" if exp is not None else None
    target = int(exp) if exp is not None else 0
    if chunk_field:
        chunk = int(CONFIG.get(f"{chunk_field}_per_game", 40_000_000))
        r2 = None
        pc = playcode
        while credited < target and games < 30:
            send = min(chunk, target - credited)
            if pc is None:
                rs = f.game_start(tsumid=won_tid, hearttype=won_ht, probmstver="2", eventid=won_ev)
                pc = (rs.get("userinfo") or {}).get("playcode") or rs.get("playcode") if isinstance(rs, dict) else None
                if not pc:
                    break
            ge = dict(score=score if score is not None else 95000, coin=61,
                      medal=medal, exp=120, vanishcnt=vanishcnt)
            ge[chunk_field] = send
            rr = f.game_end(pc, **ge)
            pc = None
            if isinstance(rr, dict) and rr.get("retcode") == 0:
                r2 = rr; credited += send; games += 1
            elif chunk > 1_000_000:
                # 送信量が実際に減るまで一気に縮める。
                # 変わらないまま再試行すると、そのたび game_start でハートを1個捨てる。
                new_chunk = chunk // 2
                while new_chunk > 1_000_000 and min(new_chunk, target - credited) == send:
                    new_chunk //= 2
                chunk = new_chunk
                if min(chunk, target - credited) == send:
                    break
            else:
                break
            time.sleep(0.2)
        _dbg(f"[{chunk_field}] target={target} credited={credited} games={games} last_chunk={chunk}")
        if credited <= 0 or r2 is None:
            return None, "送信に失敗しました。もう一度試してください。"
    else:
        r2 = f.game_end(playcode,
                        score=score if score is not None else 95000,
                        coin=coin if coin is not None else 61,
                        medal=medal, exp=120,
                        vanishcnt=vanishcnt)
        if not isinstance(r2, dict) or r2.get("retcode") != 0:
            return None, "送信に失敗しました。もう一度試してください。"
    if progress:
        progress.update(3, "結果を反映しています", force=True)
    ui = r2.get("userinfo") or {}
    after_coin = ui.get("bcoin")
    lines = []
    if coin is not None:
        if before_coin is not None and after_coin is not None:
            lines.append(f"コイン: {before_coin:,} → {after_coin:,} ( +{after_coin - before_coin:,} )")
        else:
            lines.append(f"コイン: +{coin:,}")
    if score is not None:
        lines.append(f"スコア: {score:,}")
    if exp is not None:
        lines.append("プレイヤーレベルMAX" if credited >= target else f"経験値を付与しました（{credited:,}）")
    lines.append("完了しました。")
    lines.append("またのご利用をお待ちしております。")
    return r2, "\n".join(lines)

async def _do_forge(channel, user, login_id, password, stage_box=None, **kw):
    if stage_box is None:
        stage_box = {}
    stage_box["stage"] = "login"
    lock_key, account_lock = get_account_lock(login_id)
    if account_lock.locked():
        await channel.send(embed=discord.Embed(
            description="同じアカウントの依頼を処理中です。順番に実行します。",
            color=0xf1c40f,
        ))

    try:
        await asyncio.wait_for(account_lock.acquire(), timeout=900)
    except asyncio.TimeoutError:
        await channel.send(embed=notice_embed("同じアカウントの処理待ちが長すぎるため中止しました。もう一度試してください。"))
        return False, "同じアカウントの処理待ちが長すぎるため中止しました。"

    result_obj = None
    text = ""
    progress = None
    # 実行中であることをディスクに残す。落ちても「中断された依頼」が分かるように。
    _job_id = _job_key(channel, user)
    _job_start(_job_id, channel, user, kw)
    # ロック取得後の処理は必ず try/finally の中に置く。
    # ここで channel.send などが例外を投げるとロックが解放されず、
    # そのアカウントへの依頼が以後ずっと 900 秒待ちで失敗するため。
    try:
        loop = asyncio.get_running_loop()
        # 進行状況の表示(数分かかるので、今どこかを見せる)
        progress = ProgressReporter(channel, loop)
        await progress._start()
        captcha_solver = make_solver(channel, user, loop)
        pin_notice = make_pin_notice(channel, loop)
        # 出口IPの選定はTor経由の通信を伴い数十秒かかりうる。イベントループ上で
        # 直接呼ぶとDiscordのハートビートが止まるので、必ず別スレッドで実行する。
        proxy = await loop.run_in_executor(None, _unique_game_proxy)
        result_obj, text = await loop.run_in_executor(
            None,
            functools.partial(
                forge_sync,
                login_id=login_id,
                password=password,
                captcha_solver=captcha_solver,
                proxy=proxy,
                pin_notice=pin_notice,
                stage_box=stage_box,
                progress=progress,
                **kw,
            ),
        )
    except Exception:
        print("[forge] error")
        traceback.print_exc()
        try:
            with open(os.path.join(HERE, "forge_error.log"), "a", encoding="utf-8") as _lf:
                _lf.write("\n==== forgeエラー id=%s*** kw=%s ====\n" % (str(login_id)[:3], kw))
                _lf.write(traceback.format_exc())
        except Exception:
            pass
        text = "エラーが発生しました。しばらく待ってからもう一度お試しください。"
    finally:
        try:
            if progress:
                progress.finish(ok=result_obj is not None)
        except Exception:
            pass
        _job_end(_job_id)                    # 正常・異常どちらでも記録を消す
        clear_tsum_login_files()
        if account_lock.locked():
            account_lock.release()
        # account_locks からは削除しない。
        # release 直後は待機中タスクがまだ起きておらず locked() が False に見えるため、
        # ここで消すと待機側と新規側が別々のロックを掴んで同一アカウントの二重実行になる。
    success = result_obj is not None
    color = 0x2ecc71 if success else 0xe74c3c
    try:
        await channel.send(embed=discord.Embed(description=text, color=color))
    except Exception as _e:
        print(f"[ticket] result send failed: {type(_e).__name__}: {str(_e)[:150]}")
    return success, text

async def run_forge(ctx, **kw):
    await _do_forge(ctx.channel, ctx.author, None, None, **kw)


def _proxies_dict(proxy):
    return {"http": proxy, "https": proxy} if proxy else None


def _save_orphan_guest(sess, migration_id):
    try:
        path = os.path.join(HERE, "tsum_guest_orphans.json")
        data = []
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8-sig")) or []
            except Exception:
                data = []
        data.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "orig_migration_id": migration_id,
            "uuid": sess.get("uuid"),
            "userToken": sess.get("userToken"),
            "userid": sess.get("userid"),
            "hash": sess.get("hash"),
            "checkval": sess.get("checkval"),
            "migrated_userKey": sess.get("migrated_userKey"),
        })
        _jdump_atomic(path, data, indent=2)
        print(f"[guest] orphan session saved -> {path} (uuid={sess.get('uuid')} userid={sess.get('userid')})")
    except Exception:
        traceback.print_exc()


def _save_guest_transfer(sess, old_migration_id, new_id, new_pw):
    """発行できた新しい引き継ぎ情報を必ずディスクに残す。
    Discord への送信が失敗するとアカウントに二度と入れなくなるため、送信より先に保存する。"""
    path = os.path.join(HERE, "tsum_guest_transfers.json")
    try:
        data = _jload(path, [])
        if not isinstance(data, list):
            data = []
        data.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "userid": (sess or {}).get("userid"),
            "old_migration_id": old_migration_id,
            "new_id": new_id,
            "new_pw": new_pw,
        })
        _jdump_atomic(path, data, ensure_ascii=False, indent=2)
        print(f"[guest] 新しい引き継ぎ情報を保存 -> {path} (userid={(sess or {}).get('userid')})")
    except Exception:
        traceback.print_exc()


def forge_sync_guest(migration_id, password, score=None, coin=None, exp=None, medal=5,
                     tsumid=860, tsum_lv=None, box=None, proxy=None, gacha_full=None, **_ignore):
    if not migration_id or not password:
        return None, "引き継ぎ番号とパスワードを入力してください。", {"moved": False}
    proxies = _proxies_dict(proxy)
    try:
        sess, err = tsum_guest.guest_takeover(migration_id, password, proxies)
    except Exception:
        traceback.print_exc()
        return None, "引き継ぎ処理でエラーが発生しました。番号とパスワードを確認して、もう一度お試しください。", {"moved": False}
    if not sess:
        return None, (err or "引き継ぎに失敗しました（番号かパスワードが違う/期限切れ）。"), {"moved": False}

    info = {"moved": True, "new_id": None, "new_pw": None}
    result_obj, text = None, ""
    try:
        if sess.get("login_failed"):
            # 引き継ぎ自体は完了しているが checkval が取れていない。
            # この状態では全 API が必ず失敗するので、盛り処理は飛ばして引き継ぎ発行だけ試す。
            text = "引き継ぎは完了しましたが、ゲームサーバーへの接続に失敗したため代行を実行できませんでした。"
            _dbg(f"[guest] login_failed セッション userid={sess.get('userid')} → 盛り処理をスキップ")
        else:
            f = Forge(userid=sess["userid"], hashv=sess.get("hash", ""), checkval=sess.get("checkval", ""), proxy=proxy)
            f.get_public_key()
            result_obj, text = forge_sync(score=score, coin=coin, exp=exp, medal=medal, tsumid=tsumid,
                                          tsum_lv=tsum_lv, box=box, proxy=proxy, sess=sess, f=f,
                                          gacha_full=gacha_full)
    except Exception:
        traceback.print_exc()
        try:
            with open(os.path.join(HERE, "guest_forge_error.log"), "a", encoding="utf-8") as _lf:
                _lf.write("\n==== guest盛りエラー coin=%s score=%s exp=%s box=%s gacha_full=%s tsum_lv=%s ====\n"
                          % (coin, score, exp, box, gacha_full, tsum_lv))
                _lf.write(traceback.format_exc())
        except Exception:
            pass
        result_obj, text = None, "盛り処理でエラーが発生しました。"

    new_id = new_pw = None
    for _ in range(3):
        try:
            nid, npw = tsum_guest.guest_issue_transfer(sess, proxies)
        except Exception:
            traceback.print_exc()
            nid, npw = None, None
        if nid:
            new_id, new_pw = nid, npw
            break
        time.sleep(0.4)
    info["new_id"], info["new_pw"] = new_id, new_pw

    if new_id:
        # Discord に出す前に保存する（送信が失敗しても失われないように）
        _save_guest_transfer(sess, migration_id, new_id, new_pw)
        try:
            tsum_guest.mark_pending_done(sess.get("uuid"), new_id)
        except Exception:
            pass
        text += ("\n\n──────────\n**新しい引き継ぎ情報**\n"
                 f"引き継ぎ番号: `{new_id}`\nパスワード: `{new_pw}`\n"
                 "ゲームの『設定 → 引き継ぎ』からこの情報で引き継いでください。必ず控えてください。")
    else:
        _save_orphan_guest(sess, migration_id)
        text += ("\n\n──────────\n⚠️新しい引き継ぎ情報の発行に失敗しました。"
                 "この画面をスクリーンショットして管理者にご連絡ください（管理者側で復旧できます）。")
    return result_obj, (text or "処理に失敗しました。"), info


async def _do_forge_guest(channel, user, migration_id, password, **kw):
    lock_key, account_lock = get_account_lock("guest:" + str(migration_id))
    if account_lock.locked():
        await channel.send(embed=discord.Embed(
            description="同じ引き継ぎ番号の依頼を処理中です。順番に実行します。", color=0xf1c40f))
    try:
        await asyncio.wait_for(account_lock.acquire(), timeout=900)
    except asyncio.TimeoutError:
        await channel.send(embed=notice_embed("処理待ちが長すぎるため中止しました。もう一度試してください。"))
        return False, "処理待ちが長すぎるため中止しました。", {"moved": False}

    result_obj = None
    text = ""
    info = {"moved": False}
    _job_id = _job_key(channel, user)
    _job_start(_job_id, channel, user, kw)
    # ここも同じ理由で try の中に入れる（ロックを取りっぱなしにしない）
    try:
        await channel.send(embed=discord.Embed(description="引き継ぎ中です...", color=0x3498db))
        loop = asyncio.get_running_loop()
        # ここも同じ理由で別スレッドに逃がす(イベントループを止めない)
        proxy = await loop.run_in_executor(None, _unique_game_proxy)
        result_obj, text, info = await loop.run_in_executor(
            None,
            functools.partial(forge_sync_guest, migration_id, password, proxy=proxy, **kw),
        )
    except Exception:
        print("[forge_guest] error")
        traceback.print_exc()
        info = {"moved": True}
        text = "エラーが発生しました。お手数ですが管理者にご連絡ください。"
    finally:
        _job_end(_job_id)
        if account_lock.locked():
            account_lock.release()
        # 上と同じ理由で account_locks からは削除しない
    success = result_obj is not None
    color = 0x2ecc71 if success else 0xe74c3c
    try:
        await channel.send(embed=discord.Embed(description=text, color=color))
    except Exception as _e:
        # NotFound/Forbidden だけでなくレート制限等も拾う。ここで例外を上に投げると
        # 呼び出し元の DM 送信にも到達せず、新しい引き継ぎ情報が誰にも届かなくなる。
        print(f"[ticket] guest result send failed: {type(_e).__name__}: {str(_e)[:150]}")
        if info.get("new_id"):
            print(f"[guest] ★送信失敗。引き継ぎ情報は tsum_guest_transfers.json に保存済み "
                  f"(userid={(info or {}).get('userid') or '?'})")
    return success, text, info

def status_text():
    label, cid = tsum.current_label()
    ip = "?"
    region = ""
    try:
        import requests
        ip = requests.get("https://api.ipify.org", timeout=25).text.strip()
        try:
            j = requests.get(f"http://ip-api.com/json/{ip}?fields=country,regionName&lang=ja", timeout=10).json()
            region = " ".join(x for x in (j.get("country"), j.get("regionName")) if x)
        except Exception:
            pass
    except Exception:
        ip = "?"
    st, info = tsum.tsum_status_check_token()
    try:
        cap, recap = tsum.check_captcha()
    except Exception:
        cap, recap = "?", "?"
    tok = "有効" if st == "ok" else info
    capj = "reCAPTCHA)" if recap == "true" else ("画像CAPTCHA" if cap == "true" else "無し")
    ip_line = f"接続元IP: `{ip}`" + (f"（{region}）" if region else "")
    return (f"**状態**\n{ip_line}\nアカウント: {label} (`{cid}`)\n"
            f"トークン: {tok}\nログイン障壁: {capj}")


def tsum_login_debug_text():
    """Run a read-only game login and return sanitized diagnostics.

    Authentication material such as userToken, hash and checkval is
    intentionally never included in the returned text.
    """
    try:
        sess = tsum_login.headless_session(save=False, verbose=False)
    except Exception as exc:
        return f"ログイン処理で例外が発生しました: `{type(exc).__name__}`"

    if not isinstance(sess, dict):
        reason = getattr(tsum_login, "LAST_ERROR", "") or "セッションを取得できませんでした"
        return f"ツムツムログイン失敗: {reason[:180]}"

    userid = str(sess.get("userid") or "")
    safe_userid = ("*" * max(0, len(userid) - 4) + userid[-4:]) if userid else "(不明)"

    try:
        forge = Forge(
            userid=sess.get("userid"),
            hashv=sess.get("hash"),
            checkval=sess.get("checkval"),
        )
        info = forge.get_info()
    except Exception as exc:
        return (
            "**ツムツムログイン確認**\n"
            "ログイン: 成功\n"
            f"ユーザーID: `{safe_userid}`\n"
            f"ゲーム情報取得: 失敗 (`{type(exc).__name__}`)"
        )

    if not isinstance(info, dict):
        return (
            "**ツムツムログイン確認**\n"
            "ログイン: 成功\n"
            f"ユーザーID: `{safe_userid}`\n"
            "ゲーム情報取得: 応答なし"
        )

    resources = forge.resources(info)
    userinfo = info.get("userinfo") or {}
    resource_line = " / ".join(
        f"{key}={resources.get(key, 0)}"
        for key in ("bcoin", "pcoin", "bheart", "pheart", "bruby", "pruby")
    )
    tsum_count = len(info.get("tsuminfo") or [])
    return (
        "**ツムツムログイン確認**\n"
        "ログイン: 成功\n"
        f"ユーザーID: `{safe_userid}`\n"
        f"ゲーム情報: 取得成功（キー数 {len(info)} / ツム情報 {tsum_count}件）\n"
        f"リソース: `{resource_line}`\n"
        f"userinfo: `{'取得済み' if userinfo else 'なし'}`\n"
        "認証トークン: 非表示"
    )


MAX_PRESETS = {
    "score_max": ("score", 2_147_483_647),
    "level_max": ("exp", 100_000_000),
    "coin_max":  ("coin", 200_000_000),
}
DEFAULT_SELECT_BOX_ID = int(CONFIG.get("select_box_id", 12006012))
DEFAULT_PREMIUM_GACHA_ID = int(CONFIG.get("premium_gacha_id", 12006007))
PREMIUM_FILL_COINS = int(CONFIG.get("premium_fill_coins", 160_000_000))
FREE_PAYMENT_USER_ID = int(CONFIG.get("free_payment_user_id", 0) or 0)
DEFAULT_MENU_PRICES = {
    "score_max": 100,
    "level_max": 100,
    "coin_max": 100,
    "tsum_lv": 100,
    "box": 200,
    "premium": 200,
    "coin": 100,
    "score": 100,
    "guest_create": 0,
}
CONFIG_PRICES = CONFIG.get("menu_prices", {})
MENU_PRICES = {
    key: int(CONFIG_PRICES.get(key, price))
    for key, price in DEFAULT_MENU_PRICES.items()
}
MENU_ACTIONS = [
    ("coin",    "コイン指定"),
    ("coin_max",  "コインMAX"),
    ("level_max", "プレイヤーレベルMAX"),
    ("score",   "スコア指定"),
    ("score_max", "スコアMAX"),
    ("tsum_lv",   "ツムレベルMAX"),
    ("box",       "セレクトBOX完売"),
    ("premium",   "プレミアム完売"),
    ("guest_create", "ゲストアカウント生成"),
]

PRICE_MENU_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, label in MENU_ACTIONS
]

def menu_description():
    return "\n".join([
        "ご希望のメニューを選択してください。",
        "",
        f"**コイン指定 - ¥{menu_price('coin'):,}**",
        "0〜2億枚まで指定可能",
        f"**コインMAX - ¥{menu_price('coin_max'):,}**",
        "コインを2億枚にします",
        f"**プレイヤーレベルMAX - ¥{menu_price('level_max'):,}**",
        "レベルMAXにします",
        f"**スコア指定 - ¥{menu_price('score'):,}**",
        "好きなスコアを指定可能",
        f"**スコアMAX - ¥{menu_price('score_max'):,}**",
        "スコアMAXにします",
        f"**ツムレベルMAX - ¥{menu_price('tsum_lv'):,}**",
        "セット中のツムをレベル50",
        f"**セレクトBOX完売 - ¥{menu_price('box'):,}**",
        "完売まで",
        f"**プレミアム完売 - ¥{menu_price('premium'):,}**",
        "プレミアムガチャを完売まで",
        f"**ゲストアカウント生成 - ¥{menu_price('guest_create'):,}**", # ← 追加
        "新規ゲストアカウントを作成・引き継ぎコードを発行します",
    ])

def is_free_payment_user(user):
    return user.id == FREE_PAYMENT_USER_ID

def menu_price(menu_key):
    return MENU_PRICES.get(menu_key, DEFAULT_MENU_PRICES.get(menu_key, 0))

def load_paypay(path=None):
    path = path or PAYPAY_SHARED_FILE
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8-sig"))
    except Exception:
        return {}

def save_paypay(data, path=None):
    _jdump_atomic(path or PAYPAY_SHARED_FILE, data, indent=2)

def normalize_paypay_link(raw):
    m = re.search(r"https?://(?:pay\.paypay\.ne\.jp|paypay\.ne\.jp)/[^\s]+", raw or "")
    if not m:
        return ""
    return m.group(0).strip("<>、。,.")

def payment_from_inputs(user, menu_key, paypay_raw):
    if is_free_payment_user(user):
        return 0, "", None

    amount = menu_price(menu_key)
    if amount <= 0:
        return 0, "", None

    link = normalize_paypay_link(paypay_raw)
    if not link:
        return amount, "", "PayPay送金リンクを入力してください（¥%s ちょうど）。" % format(amount, ",")
    return amount, link, None

def _paypay_subprocess_env():
    """PayPayの子プロセスに渡す環境変数。
    どんな理由であれ(この端末のユーザー環境変数、将来のコード変更など)プロキシ関連の
    環境変数が親プロセスに入っていても、PayPayの通信にだけは絶対に伝播させない。
    (game/LINE 側や Discord 接続へのプロキシは env 変数ではなく明示的な引数で渡している
    ので、この関数を通さなくても混ざらない。これは念のための多重防御。)"""
    env = dict(os.environ)
    for k in list(env.keys()):
        if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
                         "DISCORD_PROXY", "TSUM_PROXY_URL", "TSUM_DISCORD_PROXY"):
            env.pop(k, None)
    return env


async def _run_paypay_helper_dict(*args, timeout=180):
    try:
        proc = await asyncio.create_subprocess_exec(
            PYTHON_REALIP, PAYPAY_HELPER, *[str(a) for a in args],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_paypay_subprocess_env(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except Exception as e:
        print(f"[paypay] helper spawn failed: {type(e).__name__}")
        return {"ok": False, "msg": "PayPay処理に失敗しました。"}
    text = (out or b"").decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    print(f"[paypay] helper bad output err={(err or b'')[:200]!r}")
    return {"ok": False, "msg": "PayPay処理に失敗しました。"}


async def _run_paypay_helper(*args, timeout=180):
    d = await _run_paypay_helper_dict(*args, timeout=timeout)
    return bool(d.get("ok")), d.get("msg", "")


async def _run_pp_script(script, *args, timeout=180):
    try:
        proc = await asyncio.create_subprocess_exec(
            PYTHON_REALIP, script, *[str(a) for a in args],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=_paypay_subprocess_env(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (out or b"").decode("utf-8", "replace").strip()
    except Exception as e:
        print(f"[paypay] login script spawn failed: {type(e).__name__}")
        return f"SPAWN_FAIL: {type(e).__name__}"


async def verify_paypay_link(paypay_link, amount):
    return await _run_paypay_helper("check", paypay_link, str(int(amount)))

def _paypay_receive_sync(paypay_link, phone, password, client_uuid, access_token=None):
    from PayPaython import PayPay
    from PayPaython.main import PayPayError, PayPayLoginError

    last_err = None
    if access_token:
        try:
            pp = PayPay(phone=phone, password=password, client_uuid=client_uuid, access_token=access_token)
            pp.link_receive(paypay_link)
            return True, access_token, None
        except PayPayLoginError:
            pass
        except PayPayError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("already", "expired", "invalid", "not_found", "claimed", "受取済", "受け取り済")):
                return False, access_token, "link_invalid"
            last_err = e
        except Exception as e:
            last_err = e

    try:
        pp = PayPay(phone=phone, password=password, client_uuid=client_uuid)
        new_token = getattr(pp, "access_token", None)
    except Exception:
        return False, access_token, "login_failed"

    try:
        pp.link_receive(paypay_link)
        return True, new_token, None
    except PayPayLoginError:
        return False, new_token, "login_failed"
    except PayPayError as e:
        msg = str(e).lower()
        if any(k in msg for k in ("already", "expired", "invalid", "not_found", "claimed", "受取済", "受け取り済")):
            return False, new_token, "link_invalid"
        last_err = e
    except Exception as e:
        last_err = e

    return False, new_token, f"receive_failed:{type(last_err).__name__ if last_err else 'unknown'}"

async def receive_paypay_link(paypay_link, guild_id=None):
    path = paypay_file_for(guild_id) if guild_id is not None else PAYPAY_SHARED_FILE
    creds = load_paypay(path) if path else {}
    client_uuid = (creds.get("uuid") or creds.get("client_uuid")) if creds else None
    if not creds or not creds.get("phone") or not creds.get("password") or not client_uuid:
        return False, "PayPay受取アカウントが未設定です。"

    async with paypay_lock:
        return await _run_paypay_helper("receive", paypay_link, path)

_CONSUMED_LINKS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(PAYPAY_SHARED_FILE)), "consumed_paypay_links.json")


def _link_key(pay_link):
    s = (pay_link or "").strip()
    m = re.search(r"https?://(?:pay\.paypay\.ne\.jp|paypay\.ne\.jp)/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    return s


def _is_link_consumed(pay_link):
    k = _link_key(pay_link)
    if not k:
        return False
    try:
        with open(_CONSUMED_LINKS_FILE, encoding="utf-8") as f:
            return k in set(json.load(f))
    except Exception:
        return False


def _mark_link_consumed(pay_link):
    k = _link_key(pay_link)
    if not k:
        return
    try:
        cur = []
        if os.path.exists(_CONSUMED_LINKS_FILE):
            with open(_CONSUMED_LINKS_FILE, encoding="utf-8") as f:
                cur = json.load(f)
        if k not in cur:
            cur.append(k)
            _jdump_atomic(_CONSUMED_LINKS_FILE, cur)
    except Exception as e:
        print(f"[tsum] consumed link save error: {e}")


def _hold_embed():
    return discord.Embed(
        title="受け取り一時保留になりました",
        description=("PayPayのアプリ側から取引を承認してください。\n"
                     "承認した場合は下のボタンを押してください。\n"
                     "承認が確認できたら代行を開始します。\n\n"
                     "ボタンの有効期限: 1時間"),
        color=0xf1c40f,
    )


class TsumHoldApproveView(discord.ui.View):

    def __init__(self, func, kwargs):
        super().__init__(timeout=3600)
        self.func = func
        self.kwargs = kwargs
        self._last = 0.0

    @discord.ui.button(label="承認しました", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = time.monotonic()
        if now - self._last < 10:
            await interaction.response.send_message(
                "少し待ってからもう一度押してください。", ephemeral=True)
            return
        self._last = now
        try:
            await self.func(interaction, **self.kwargs)
        except Exception as e:
            print(f"[tsum] hold retry failed: {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    embed=notice_embed("処理中に問題が発生しました。オーナーにご連絡ください。"), ephemeral=True)
            except Exception:
                pass


def ticket_channel_name(name, user_id):
    base = "".join(c.lower() if c.isascii() and c.isalnum() else "-" for c in name)
    base = "-".join(part for part in base.split("-") if part)
    if not base:
        base = "ticket"
    return f"ticket-{user_id}-{base}"[:90]

async def get_ticket_category(guild):
    category_id = guild_setting(guild.id, "ticket_category_id") if guild else None
    if not guild or not category_id:
        return None
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        return None

    category = guild.get_channel(category_id)
    if category is None:
        try:
            category = await guild.fetch_channel(category_id)
        except Exception:
            return None
    if isinstance(category, discord.CategoryChannel):
        return category
    return None

async def open_ticket(interaction, name):
    category = await get_ticket_category(interaction.guild)
    if category is not None:
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            ),
        }
        if interaction.guild.me:
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                manage_channels=True,
            )
        async def _create_ticket_channel():
            return await interaction.guild.create_text_channel(
                ticket_channel_name(name, interaction.user.id),
                category=category,
                overwrites=overwrites,
                topic=str(interaction.user.id),
                reason=f"ticket for {interaction.user}",
            )
        try:
            return await _create_ticket_channel()
        except discord.HTTPException as e:
            full = (getattr(e, "code", None) == 50035) or ("Maximum number of channels" in str(e))
            if full:
                try:
                    # 実行中の依頼のチケットは絶対に消さない（結果や引き継ぎ情報が届かなくなる）
                    busy = {v.get("channel_id") for v in ACTIVE_ORDERS.values() if v.get("channel_id")}
                    cutoff = discord.utils.utcnow() - datetime.timedelta(hours=1)
                    tickets = [c for c in getattr(category, "channels", [])
                               if isinstance(c, discord.TextChannel)
                               and str(getattr(c, "topic", "") or "").isdigit()
                               and c.id not in busy
                               and c.created_at < cutoff]     # 作成1時間以内も対象外
                    if not tickets:
                        print("[ticket] カテゴリが満杯だが、削除してよい古いチケットがありません")
                        raise RuntimeError("no prunable ticket")
                    for old_ch in sorted(tickets, key=lambda c: c.created_at)[:5]:
                        try:
                            await old_ch.delete(reason="ticket category full: prune oldest")
                        except Exception:
                            pass
                    return await _create_ticket_channel()
                except Exception as e2:
                    print(f"[ticket] category full cleanup+retry failed: {e2}")
            else:
                print(f"[ticket] category channel create failed: {e}")
        except Exception as e:
            print(f"[ticket] category channel create failed: {e}")

    ch = interaction.channel
    try:
        th = await ch.create_thread(name=name, type=discord.ChannelType.private_thread, invitable=False)
        try: await th.add_user(interaction.user)
        except Exception: pass
        return th
    except Exception:
        try:
            th = await ch.create_thread(name=name, type=discord.ChannelType.public_thread)
            return th
        except Exception:
            return ch

def ticket_link(ch):
    return getattr(ch, "mention", None) or "このチャット"

def ticket_request_text(action, val):
    if action == "tsum_lv":
        return "ツムlvMAX\nセット中のツム"
    if action == "box":
        return "セレクトBOX完売"
    if action == "gacha_full":
        return "プレミアムガチャ完売"
    labels = {
        "coin": "コイン指定",
        "score": "スコア指定",
        "exp": "プレイヤーレベルMAX",
        "tsum_lv": "ツムlvMAX",
        "box": "セレクトBOX",
    }
    label = labels.get(action, action)
    if not val:
        return f"{label}"
    return f"{label}\n{val:,}"

def ticket_embed(user, action, val, amount, paypay_link):
    emb = discord.Embed(title="ツムツム代行", color=0x2ecc71)
    pay_text = f"支払い済み（¥{amount:,}）" if amount > 0 else "無料"
    emb.description = (
        f"**依頼内容**\n{ticket_request_text(action, val)}\n"
        f"**金額**\n¥{amount:,}\n"
        f"**支払い状況**\n{pay_text}\n"
        f"**実行ユーザー**\n{user.mention} ({user.id})"
    )
    return emb

class TicketView(discord.ui.View):
    def __init__(self, owner_id=None):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.custom_id = "tsum_ticket_delete"

    @discord.ui.button(label="チケット削除", style=discord.ButtonStyle.danger)
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, (discord.Thread, discord.TextChannel)):
            await interaction.response.send_message(embed=notice_embed("この場所ではチケット削除を実行できません。"), ephemeral=True)
            return
        try:
            await interaction.response.send_message("チケットを削除します。", ephemeral=True)
        except discord.NotFound:
            # 受け付けが期限切れ(10062)。削除自体は実行する。
            print("[interaction] チケット削除の受け付けが期限切れ。削除は続行します。")
        except discord.HTTPException as e:
            print(f"[interaction] チケット削除の応答に失敗: {e}")
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

    def resolve_owner_id(self, interaction: discord.Interaction):
        if self.owner_id:
            return self.owner_id
        channel = interaction.channel
        topic = getattr(channel, "topic", None)
        if topic and str(topic).isdigit():
            return int(topic)
        name = getattr(channel, "name", "") or ""
        match = re.search(r"ticket-(\d{15,25})-", name)
        if match:
            return int(match.group(1))
        message = getattr(interaction, "message", None)
        for embed in getattr(message, "embeds", []) or []:
            text = " ".join(filter(None, [
                getattr(embed, "description", None),
                getattr(embed, "title", None),
            ]))
            match = re.search(r"\((\d{15,25})\)", text) or re.search(r"<@!?(\d{15,25})>", text)
            if match:
                return int(match.group(1))
        return None

    async def interaction_check(self, interaction: discord.Interaction):
        owner_id = self.resolve_owner_id(interaction)
        if owner_id and not can_operate_ticket(interaction, owner_id):
            await interaction.response.send_message(embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return False
        return True

async def send_ticket_header(channel, user, action, val, amount, paypay_link):
    await channel.send(embed=ticket_embed(user, action, val, amount, paypay_link), view=TicketView(user.id))

def ticket_thread_name(action, val):
    if action == "box":
        return "セレクトBOX完売"
    if action == "gacha_full":
        return "プレミアム完売"
    if action == "tsum_lv":
        return "ツムlvMAX"
    labels = {
        "score": "スコア",
        "coin": "コイン",
        "exp": "レベルMAX",
    }
    label = labels.get(action, action)
    return f"{label}-{val}"

async def reset_menu_message(message):
    if message is None:
        return
    try:
        await message.edit(view=MenuView())
    except Exception as e:
        print(f"[menu] reset failed: {e}")

async def post_public_result(guild, user, action, val, amount):
    # 実績カウンター: 完了ごとに +1 して、指定チャンネル名へ反映を要求する。
    # （公開実績チャンネルの設定有無に関係なく常にカウントする）
    increment_achievement()
    request_achievement_update()

    channel_id = guild_setting(guild.id, "public_result_channel_id") if guild else None
    if not guild or not channel_id:
        return
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return

    emb = discord.Embed(color=0x2ecc71)
    emb.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    # 右上にユーザーのアイコンを表示
    emb.set_thumbnail(url=user.display_avatar.url)
    emb.description = (
        f"**ご利用者**\n**{user.mention}**\n"
        f"**注文内容**\n**{ticket_request_text(action, val)}**\n"
        f"**金額**\n**¥{amount:,}**"
    )
    try:
        # 本文のメンション(通知)はしない。埋め込み内の表示のみ（通知は飛ばない）。
        await channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[public_result] send failed: {e}")

def ticket_owner_user(guild, owner_id, fallback):
    try:
        owner_id = int(owner_id)
    except (TypeError, ValueError):
        return fallback
    if guild:
        member = guild.get_member(owner_id)
        if member:
            return member
    user = bot.get_user(owner_id)
    return user or fallback

class RetryLoginModal(discord.ui.Modal):
    def __init__(self, owner_id, action, val, amount, prompt_message=None):
        super().__init__(title="アカウント情報を再入力")
        self.owner_id = owner_id
        self.action = action
        self.val = val
        self.amount = amount
        self.prompt_message = prompt_message
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
            style=discord.TextStyle.short,
        )
        self.add_item(self.login_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if self.prompt_message:
                await self.prompt_message.edit(view=None)
        except Exception:
            pass

        channel = interaction.channel
        if not isinstance(channel, (discord.Thread, discord.TextChannel)):
            await interaction.followup.send("この場所では再実行できません。", ephemeral=True)
            return

        lock_key, ticket_lock = get_ticket_lock(channel.id)
        if ticket_lock.locked():
            await interaction.followup.send("このチケットは処理中です。完了までお待ちください。", ephemeral=True)
            return

        is_free = (self.amount == 0 and self.action == "coin" and int(self.val) == FREE_DAIKOU_COIN)
        if is_free:
            if not free_can_use(self.owner_id, login_id=login_id):
                _m = (FREE_DAIKOU_ACCOUNT_LIMIT_MSG if not free_can_use(login_id=login_id)
                      else FREE_DAIKOU_LIMIT_MSG)
                await interaction.followup.send(embed=notice_embed(_m), ephemeral=True)
                return
            free_mark_used(self.owner_id, login_id=login_id)

        await interaction.followup.send("再実行します。", ephemeral=True)
        ok = False
        _game_state = {"userid": "", "blocked": False}
        def _on_login_free(game_userid):
            _game_state["userid"] = game_userid or ""
            if game_userid and not free_can_use(game_userid=game_userid):
                _game_state["blocked"] = True
                raise LoginHookAbort(FREE_DAIKOU_ACCOUNT_LIMIT_MSG)
            if game_userid:
                free_mark_used(game_userid=game_userid)
        _stage = {}
        try:
            async with ticket_lock:
                _kw = {"on_login": _on_login_free} if is_free else {}
                ok, _ = await _do_forge(channel, interaction.user, login_id, password,
                                        stage_box=_stage, **{self.action: self.val}, **_kw)
        finally:
            if is_free and not ok:
                if _game_state["blocked"]:
                    free_mark_used(self.owner_id, login_id=login_id, game_userid=_game_state["userid"])
                    try:
                        await interaction.followup.send(
                            embed=notice_embed(FREE_DAIKOU_ACCOUNT_LIMIT_MSG), ephemeral=True)
                    except Exception:
                        pass
                else:
                    free_unmark_used(self.owner_id, login_id=login_id, game_userid=_game_state["userid"])
        if not ticket_lock.locked():
            ticket_locks.pop(lock_key, None)

        owner = ticket_owner_user(interaction.guild, self.owner_id, interaction.user)
        if ok:
            await post_public_result(interaction.guild, owner, self.action, self.val, self.amount)
        else:
            await send_retry_prompt(channel, self.owner_id, self.action, self.val, self.amount,
                                    login_ok=(_stage.get("stage") == "action"))

class RetryLoginButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r'tsum_retry_login:(?P<owner>\d+):(?P<action>[A-Za-z_]+):(?P<val>-?\d+)(?::(?P<amount>-?\d+))?'):
    def __init__(self, owner_id, action, val, amount):
        self.owner_id = int(owner_id)
        self.action = str(action)
        self.val = int(val)
        self.amount = int(amount)
        super().__init__(
            discord.ui.Button(
                label="アカウント情報を再入力",
                style=discord.ButtonStyle.primary,
                custom_id=f"tsum_retry_login:{self.owner_id}:{self.action}:{self.val}:{self.amount}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        amt = match.group("amount")
        return cls(int(match["owner"]), match["action"], int(match["val"]), int(amt) if amt else 0)

    async def callback(self, interaction: discord.Interaction):
        if not can_operate_ticket(interaction, self.owner_id):
            await interaction.response.send_message(
                embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return
        await interaction.response.send_modal(
            RetryLoginModal(self.owner_id, self.action, self.val, self.amount, interaction.message)
        )

async def send_retry_prompt(channel, owner_id, action, val, amount, login_ok=False):
    if login_ok:
        # ログインは通っている＝メアド/パスワードの誤りではない。誤解させない文言にする。
        desc = ("成功しませんでした。**ログインは成功している**ので、メールアドレス・パスワードの間違いではありません。\n"
                "上の失敗理由をご確認のうえ、下のボタンから同じチケットで再実行できます。")
    else:
        desc = "成功しませんでした。アカウント情報を入力し直して、同じチケットで再実行できます。"
    emb = discord.Embed(description=desc, color=0xf1c40f)
    view = discord.ui.View(timeout=None)
    try:
        view.add_item(RetryLoginButton(owner_id, action, val, amount))
    except Exception as _e:
        print(f"[ticket] retry button build failed: {_e}")
        return
    try:
        await channel.send(embed=emb, view=view)
    except (discord.NotFound, discord.Forbidden):
        print("[ticket] retry prompt skipped: channel unavailable")

async def create_ticket_and_forge(interaction: discord.Interaction, action, val, amount, paypay_link, login_id, password, menu_message=None, pp_id="", on_login=None):
    if interaction.guild is None:
        try:
            await interaction.response.send_message(
                embed=notice_embed("ご注文はサーバー内のチャンネルからお願いします。DMでは受け付けていません。"))
        except Exception:
            pass
        return False
    if is_guest_account(login_id):
        return await create_ticket_and_forge_guest(
            interaction, action, val, amount, paypay_link, login_id, password, menu_message)
    await _safe_defer(interaction)
    if amount > 0:
        pp_file = paypay_file_for(interaction.guild_id)
        if not pp_file:
            await interaction.followup.send(embed=notice_embed(PAYPAY_UNSET_MSG), ephemeral=True)
            await reset_menu_message(menu_message)
            return False
        async with paypay_lock:
            if _is_link_consumed(paypay_link):
                await interaction.followup.send(
                    embed=notice_embed("この支払いは既に処理済みです。届いていない場合はオーナーにご連絡ください。"), ephemeral=True)
                return False
            pres = await _run_paypay_helper_dict("verify_receive", paypay_link, str(int(amount)), pp_file)
            if pres.get("ok"):
                _mark_link_consumed(paypay_link)
        if pres.get("held"):
            await interaction.followup.send(
                embed=_hold_embed(),
                view=TsumHoldApproveView(create_ticket_and_forge, dict(
                    action=action, val=val, amount=amount, paypay_link=paypay_link,
                    login_id=login_id, password=password, menu_message=menu_message)),
                ephemeral=True)
            return False
        if not pres.get("ok"):
            await interaction.followup.send(
                embed=notice_embed(pres.get("msg") or "PayPayの受け取りに失敗しました。"), ephemeral=True)
            clear_tsum_login_files()
            await reset_menu_message(menu_message)
            return False

    th = await open_ticket(interaction, ticket_thread_name(action, val))
    await interaction.followup.send(f"チケット作成: {ticket_link(th)}", ephemeral=True)
    await send_ticket_header(th, interaction.user, action, val, amount, paypay_link)
    await reset_menu_message(menu_message)
    okey = f"{interaction.id}"
    ACTIVE_ORDERS[okey] = {"user": str(interaction.user), "action": action, "val": val,
                           "ts": int(time.time()), "channel_id": getattr(th, "id", None)}
    _stage = {}
    try:
        _extra = {"on_login": on_login} if on_login else {}
        ok, _ = await _do_forge(th, interaction.user, login_id, password,
                                stage_box=_stage, **{action: val}, **_extra)
    finally:
        ACTIVE_ORDERS.pop(okey, None)
    print(f"[注文] {interaction.user} {action} {val} ¥{amount} → {'成功' if ok else '失敗'}", flush=True)
    if ok:
        if amount > 0:
            record_sale(interaction.user.id, action, amount)
        await post_public_result(interaction.guild, interaction.user, action, val, amount)
    else:
        await send_retry_prompt(th, interaction.user.id, action, val, amount,
                                login_ok=(_stage.get("stage") == "action"))
    return ok

VALUE_LABELS = {
    "score": ("スコア", "数値（半角）", "例: 3000000"),
    "coin":  ("コイン", "数値（半角）", "例: 100000000"),
}

class ValueModal(discord.ui.Modal):
    def __init__(self, action, menu_message=None, menu_key=None):
        title_name, in_label, in_ph = VALUE_LABELS.get(action, ("値", "数値（半角）", ""))
        super().__init__(title=title_name + " とアカウント情報")
        self.action = action
        self.menu_message = menu_message
        self.menu_key = menu_key or action
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
        )
        self.tin = discord.ui.TextInput(label=in_label, placeholder=in_ph)
        self.add_item(self.login_id)
        self.add_item(self.password)
        self.add_item(self.tin)
        self.paypay = None
        if menu_price(self.menu_key) > 0:
            self.paypay = discord.ui.TextInput(
                label="PayPay送金リンク",
                placeholder="https://pay.paypay.ne.jp/xxxx（料金ちょうどを送金）",
                required=True,
                max_length=200,
            )
            self.add_item(self.paypay)
    async def on_submit(self, interaction: discord.Interaction):
        raw = self.tin.value.strip().replace(",", "").replace("，", "")
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        paypay_raw = self.paypay.value.strip() if self.paypay is not None else ""
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        if not raw.isdigit():
            await interaction.response.send_message(embed=notice_embed("数値で入力してください。"), ephemeral=True); return
        val = int(raw)
        amount, paypay_link, error = payment_from_inputs(interaction.user, self.menu_key, paypay_raw)
        if error:
            await interaction.response.send_message(embed=notice_embed(error), ephemeral=True)
            return
        await create_ticket_and_forge(interaction, self.action, val, amount, paypay_link, login_id, password, self.menu_message)

class AccountBeforeTicketModal(discord.ui.Modal):
    def __init__(self, action, val, menu_message=None, menu_key=None):
        super().__init__(title="アカウント情報を入力")
        self.action = action
        self.val = val
        self.menu_message = menu_message
        self.menu_key = menu_key or action
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
        )
        self.add_item(self.login_id)
        self.add_item(self.password)
        self.paypay = None
        if menu_price(self.menu_key) > 0:
            self.paypay = discord.ui.TextInput(
                label="PayPay送金リンク",
                placeholder="https://pay.paypay.ne.jp/xxxx（料金ちょうどを送金）",
                required=True,
                max_length=200,
            )
            self.add_item(self.paypay)

    async def on_submit(self, interaction: discord.Interaction):
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        paypay_raw = self.paypay.value.strip() if self.paypay is not None else ""
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        amount, paypay_link, error = payment_from_inputs(interaction.user, self.menu_key, paypay_raw)
        if error:
            await interaction.response.send_message(embed=notice_embed(error), ephemeral=True)
            return
        await create_ticket_and_forge(interaction, self.action, self.val, amount, paypay_link, login_id, password, self.menu_message)

class GuestCreateModal(discord.ui.Modal):
    def __init__(self, menu_message=None):
        super().__init__(title="ゲストアカウント生成")
        self.menu_message = menu_message
        self.paypay = None
        if menu_price("guest_create") > 0:
            self.paypay = discord.ui.TextInput(
                label="PayPay送金リンク",
                placeholder="https://pay.paypay.ne.jp/xxxx（料金ちょうどを送金）",
                required=True,
                max_length=200,
            )
            self.add_item(self.paypay)

    async def on_submit(self, interaction: discord.Interaction):
        paypay_raw = self.paypay.value.strip() if self.paypay is not None else ""
        amount, paypay_link, error = payment_from_inputs(interaction.user, "guest_create", paypay_raw)
        if error:
            await interaction.response.send_message(embed=notice_embed(error), ephemeral=True)
            return
        await run_guest_create(interaction, amount, paypay_link, self.menu_message)


async def handle_guest_create(interaction: discord.Interaction, menu_message=None):
    # 有料設定なら PayPay送金リンクを入力してもらう。0円ならそのまま生成へ。
    if menu_price("guest_create") > 0:
        await interaction.response.send_modal(GuestCreateModal(menu_message))
        return False
    return await run_guest_create(interaction, 0, "", menu_message)


async def run_guest_create(interaction: discord.Interaction, amount=0, paypay_link="", menu_message=None):
    """（必要なら支払いを確認してから）ゲストアカウントを作って引き継ぎ情報を渡す。"""
    if interaction.guild is None:
        try:
            await interaction.response.send_message(
                embed=notice_embed("ご注文はサーバー内のチャンネルからお願いします。DMでは受け付けていません。"))
        except Exception:
            pass
        return False
    await _safe_defer(interaction)

    if amount > 0:
        pp_file = paypay_file_for(interaction.guild_id)
        if not pp_file:
            await interaction.followup.send(embed=notice_embed(PAYPAY_UNSET_MSG), ephemeral=True)
            await reset_menu_message(menu_message)
            return False
        async with paypay_lock:
            if _is_link_consumed(paypay_link):
                await interaction.followup.send(
                    embed=notice_embed("この支払いは既に処理済みです。届いていない場合はオーナーにご連絡ください。"),
                    ephemeral=True)
                return False
            pres = await _run_paypay_helper_dict("verify_receive", paypay_link, str(int(amount)), pp_file)
            if pres.get("ok"):
                _mark_link_consumed(paypay_link)
        if pres.get("held"):
            await interaction.followup.send(
                embed=_hold_embed(),
                view=TsumHoldApproveView(run_guest_create, dict(
                    amount=amount, paypay_link=paypay_link, menu_message=menu_message)),
                ephemeral=True)
            return False
        if not pres.get("ok"):
            await interaction.followup.send(
                embed=notice_embed(pres.get("msg") or "PayPayの受け取りに失敗しました。"), ephemeral=True)
            await reset_menu_message(menu_message)
            return False

    th = await open_ticket(interaction, "ゲスト生成")
    await interaction.followup.send(f"チケット作成: {ticket_link(th)}", ephemeral=True)
    await send_ticket_header(th, interaction.user, "ゲストアカウント生成", 0, amount, paypay_link)
    await reset_menu_message(menu_message)

    await th.send(embed=discord.Embed(description="ゲストアカウントを生成中です...", color=0x3498db))

    loop = asyncio.get_running_loop()
    def _exec_create():
        # ゲスト認証自体も通信が不安定だと落ちるのでリトライする
        sess = err = None
        for _try in range(3):
            sess, err = tsum_guest.guest_create()
            if sess:
                break
            print(f"[guest] ゲスト生成に失敗({_try+1}/3): {err}", flush=True)
            time.sleep(0.5)
        if not sess:
            return None, None, err or "ゲスト生成に失敗しました"

        # 引き継ぎコードの発行もリトライする。
        # Tor経由だと1回の通信エラーで落ちるため、1回きりだと失敗しやすい
        # (引き継ぎ代行側は元から3回試す作りになっていた)。
        nid = npw = None
        for _try in range(3):
            try:
                nid, npw = tsum_guest.guest_issue_transfer(sess)
            except Exception:
                traceback.print_exc()
                nid, npw = None, None
            if nid:
                break
            print(f"[guest] 引き継ぎコードの発行に失敗({_try+1}/3): {npw}", flush=True)
            time.sleep(0.5)
        if not nid:
            _save_orphan_guest(sess, "(新規作成)")
            return None, None, "引き継ぎコードの発行に失敗しました: " + str(npw)
        # 送信が失敗してもアカウントを失わないよう、渡す前に保存しておく
        _save_guest_transfer(sess, "(新規作成)", nid, npw)
        try:
            tsum_guest.mark_pending_done(sess.get("uuid"), nid)
        except Exception:
            pass
        return nid, npw, None

    nid, npw, err_msg = await loop.run_in_executor(None, _exec_create)

    if nid and npw:
        msg = (
            "**ゲストアカウントの生成が完了しました！**\n\n"
            "──────────\n"
            "**引き継ぎ情報**\n"
            f"引き継ぎ番号: `{nid}`\n"
            f"パスワード: `{npw}`\n"
            "──────────\n"
            "※ツムツムアプリの『設定 → 引き継ぎ』からログインしてください。"
        )
        await th.send(embed=discord.Embed(description=msg, color=0x2ecc71))
        if amount > 0:
            record_sale(interaction.user.id, "guest_create", amount)
        await post_public_result(interaction.guild, interaction.user, "ゲストアカウント生成", 0, amount)
        return True
    await th.send(embed=notice_embed(err_msg or "エラーが発生しました。"))
    return False

class AccountInputModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="アカウント情報を入力")
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
        )
        self.add_item(self.login_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        clear_tsum_login_files()
        await interaction.response.send_message(
            "アカウント情報は保存しません。メニューを選んだ後の入力画面で、その都度入力してください。",
            ephemeral=True,
        )

class MenuSelect(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=label, value=key)
                for key, label in MENU_ACTIONS]
        super().__init__(placeholder="ご希望のメニューを選択", options=opts, custom_id="tsum_menu_select")
    async def callback(self, interaction: discord.Interaction):
        if await deny_unlicensed(interaction):
            return
        key = self.values[0]
        menu_msg = interaction.message
        if key == "guest_create":
            await handle_guest_create(interaction, menu_msg)
            return
        elif key in MAX_PRESETS:
            action, val = MAX_PRESETS[key]
            factory = (lambda a=action, v=val, k=key, m=menu_msg:
                       AccountBeforeTicketModal(a, v, m, k))
        elif key == "tsum_lv":
            factory = lambda m=menu_msg: AccountBeforeTicketModal("tsum_lv", 0, m, "tsum_lv")
        elif key == "box":
            factory = lambda m=menu_msg: AccountBeforeTicketModal("box", DEFAULT_SELECT_BOX_ID, m, "box")
        elif key == "premium":
            factory = lambda m=menu_msg: AccountBeforeTicketModal("gacha_full", DEFAULT_PREMIUM_GACHA_ID, m, "premium")
        elif key in ("score", "coin", "exp"):
            factory = lambda k=key, m=menu_msg: ValueModal(k, m, k)
        elif key == "account":
            await interaction.response.send_modal(AccountInputModal())
            return
        elif key == "status":
            await interaction.response.defer(ephemeral=True)
            loop = asyncio.get_running_loop()
            txt = await loop.run_in_executor(None, status_text)
            await interaction.followup.send(txt, ephemeral=True)
            return
        else:
            return
        try:
            await interaction.response.send_modal(factory())
        except (discord.NotFound, discord.HTTPException) as e:
            # 10062(Unknown interaction)=3秒以内に開けず期限切れ / 40060=二重。
            # ユーザーはもう一度メニューを選べばよいので、ログだけ残して無視。
            _dbg(f"[menu] モーダル表示失敗 (code={getattr(e,'code',None)}): {e}")

class MenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(MenuSelect())




# ----------------------------------------------------------------------------
# 貸し出しパネル（お客さんが PayPay で購入 → そのサーバーIDへ自動で貸し出し）
# ----------------------------------------------------------------------------
def rental_panel_embed():
    prices = rental_prices()
    lines = [
        "このbotをあなたのサーバーへ貸し出します。",
        "期間中は、あなたのサーバーで代行パネル（注文受付）を設置して運営できます。",
        "",
        "**💰 料金**",
    ]
    if prices:
        for months, price in prices.items():
            lines.append(f"💎 {rental_plan_label(months)}　{price:,}円")
    else:
        lines.append("現在は販売していません。")
    lines += [
        "",
        "下のメニューから期間を選び、表示される画面に",
        "**あなたのサーバーの招待リンク** と **PayPay送金リンク（料金ちょうど）** を入力してください。",
        "受け取りが完了すると、その場でそのサーバーが使えるようになります。",
        "（招待リンクは サーバー名を右クリック →「友達を招待」から作れます。",
        "　サーバーIDやチャンネルのリンクを貼っても大丈夫です）",
    ]
    return discord.Embed(title="bot貸し出し（サーバーレンタル）",
                         description="\n".join(lines), color=0x9b59b6)


async def _rental_finish_grant(interaction: discord.Interaction, months, amount, guild_id):
    """支払い確認後に、指定サーバーへ貸し出しを付与して案内する。"""
    exp = rental_grant(guild_id, months=months, unlimited=(int(months) <= 0),
                       by=interaction.user.id, note=f"購入({rental_plan_label(months)})",
                       extend=True)
    if amount > 0:
        record_sale(interaction.user.id, f"rental_{months}m", amount)
    guild = bot.get_guild(int(guild_id))
    invite = bot_invite_url()
    msg = [f"**貸し出しを開始しました（{rental_plan_label(months)}）**",
           f"サーバー: {guild.name if guild else '未参加'}（`{guild_id}`）",
           f"有効期限: {rental_until_text(exp)}",
           ""]
    if guild is None and invite:
        msg += ["まだ bot がそのサーバーにいません。下のリンクから招待してください。",
                invite, ""]
    msg += ["**サーバーに招待したら、最初に受取PayPayを登録してください。**",
            "登録すると、そのサーバーの代行料金はあなたのPayPayアカウントに直接送金されます。",
            "`/paypayログイン` → `/paypayotp` で登録し、そのあと "
            "`/チケットカテゴリー`・`/パネル設置` を実行してください。"]
    try:
        await interaction.followup.send(
            embed=discord.Embed(description="\n".join(msg), color=0x2ecc71), ephemeral=True)
    except Exception as e:
        _dbg(f"[rental] 購入案内の送信失敗: {e}")
    print(f"[貸し出し] 購入 {interaction.user} guild={guild_id} "
          f"{rental_plan_label(months)} ¥{amount}", flush=True)
    await _rental_notify(
        f"{interaction.user}（{interaction.user.id}）が **{rental_plan_label(months)}** の"
        f"貸し出しを購入しました。\nサーバー: {guild.name if guild else '未参加'}"
        f"（{guild_id}）/ ¥{amount:,} / {rental_until_text(exp)}", 0x2ecc71)
    return True


async def process_rental_purchase(interaction: discord.Interaction, months, amount, paypay_link,
                                  guild_id, deferred=False):
    """PayPayリンクを受け取り、成功したらそのサーバーへ貸し出す。"""
    if not deferred:
        await interaction.response.defer(ephemeral=True)
    if amount > 0:
        pp_file = paypay_file_for(interaction.guild_id, fallback=True)
        if not pp_file:
            await interaction.followup.send(
                embed=notice_embed("受取PayPayアカウントが未設定です。オーナーにご連絡ください。"),
                ephemeral=True)
            return False
        async with paypay_lock:
            if _is_link_consumed(paypay_link):
                await interaction.followup.send(
                    embed=notice_embed("この支払いは既に処理済みです。反映されていない場合はオーナーにご連絡ください。"),
                    ephemeral=True)
                return False
            pres = await _run_paypay_helper_dict(
                "verify_receive", paypay_link, str(int(amount)), pp_file)
            if pres.get("ok"):
                _mark_link_consumed(paypay_link)
        if pres.get("held"):
            await interaction.followup.send(
                embed=_hold_embed(),
                view=TsumHoldApproveView(process_rental_purchase, dict(
                    months=months, amount=amount, paypay_link=paypay_link, guild_id=guild_id)),
                ephemeral=True)
            return False
        if not pres.get("ok"):
            await interaction.followup.send(
                embed=notice_embed(pres.get("msg") or "PayPayの受け取りに失敗しました。"), ephemeral=True)
            return False
    return await _rental_finish_grant(interaction, months, amount, guild_id)


class RentalPayModal(discord.ui.Modal):
    def __init__(self, months, price):
        super().__init__(title=f"貸し出しの購入（{rental_plan_label(months)}）")
        self.months = months
        self.price = price
        self.guild_id = discord.ui.TextInput(
            label="貸し出し先サーバーの招待リンク",
            placeholder="https://discord.gg/xxxx（サーバーIDでも可）",
            max_length=200,
        )
        self.paypay = discord.ui.TextInput(
            label="PayPay送金リンク",
            placeholder=f"https://pay.paypay.ne.jp/xxxx（{price:,}円 ちょうどを送金）",
            max_length=200,
        )
        self.add_item(self.guild_id)
        self.add_item(self.paypay)

    async def on_submit(self, interaction: discord.Interaction):
        # 招待リンクの確認に時間がかかることがあるので先に defer しておく
        await interaction.response.defer(ephemeral=True)
        gid, err = await resolve_guild_id(self.guild_id.value)
        if gid is None:
            await interaction.followup.send(
                embed=notice_embed(err or "サーバーを特定できませんでした。"), ephemeral=True)
            return
        link = normalize_paypay_link(self.paypay.value)
        if not link:
            await interaction.followup.send(
                embed=notice_embed("PayPay送金リンクを入力してください（%s円 ちょうど）。"
                                   % format(self.price, ",")), ephemeral=True)
            return
        await process_rental_purchase(interaction, self.months, self.price, link, gid,
                                      deferred=True)


class RentalPlanSelect(discord.ui.Select):
    def __init__(self):
        # Discord のセレクトは選択肢25個まで
        opts = [discord.SelectOption(label=f"{rental_plan_label(months)} - {price:,}円",
                                     value=str(months))
                for months, price in list(rental_prices().items())[:25]]
        if not opts:
            opts = [discord.SelectOption(label="現在は販売していません", value="none")]
        super().__init__(placeholder="貸し出し期間を選択", options=opts,
                         custom_id="tsum_rental_select", row=0)

    async def callback(self, interaction: discord.Interaction):
        if not rental_enabled():
            await interaction.response.send_message(
                embed=notice_embed("現在、貸し出しは受け付けていません。"), ephemeral=True)
            return
        try:
            months = int(self.values[0])
        except (TypeError, ValueError):
            months = -1
        price = rental_prices().get(months)
        if price is None:
            await interaction.response.send_message(
                embed=notice_embed("このプランは現在販売していません。オーナーにパネルの貼り直しをご依頼ください。"),
                ephemeral=True)
            return
        try:
            await interaction.response.send_modal(RentalPayModal(months, price))
        except (discord.NotFound, discord.HTTPException) as e:
            # 10062(3秒以内に開けず期限切れ) / 40060(二重応答)。選び直せばよいのでログのみ。
            _dbg(f"[rental] モーダル表示失敗 (code={getattr(e,'code',None)}): {e}")


class RentalCheckModal(discord.ui.Modal):
    def __init__(self, default_guild_id=None):
        super().__init__(title="残り期間を確認")
        self.guild_id = discord.ui.TextInput(
            label="確認したいサーバー（招待リンク/ID）",
            placeholder="https://discord.gg/xxxx（サーバーIDでも可）",
            default=str(default_guild_id or ""),
            max_length=200,
        )
        self.add_item(self.guild_id)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid, err = await resolve_guild_id(self.guild_id.value)
        if gid is None:
            await interaction.followup.send(
                embed=notice_embed(err or "サーバーを特定できませんでした。"), ephemeral=True)
            return
        guild = bot.get_guild(gid)
        name = guild.name if guild else "(bot未参加)"
        await interaction.followup.send(
            embed=discord.Embed(description=f"**{name}**（`{gid}`）\n{rental_status_text(gid)}",
                                color=0x9b59b6),
            ephemeral=True)


class RentalPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RentalPlanSelect())

    @discord.ui.button(label="残り期間を確認", style=discord.ButtonStyle.secondary,
                       custom_id="tsum_rental_check", row=1)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(RentalCheckModal(interaction.guild_id))
        except (discord.NotFound, discord.HTTPException) as e:
            _dbg(f"[rental] 確認モーダル表示失敗 (code={getattr(e,'code',None)}): {e}")

FREE_DAIKOU_COIN = 1000000
FREE_DAIKOU_LIMIT_MSG = "無料代行は1ヶ月に1回までです。また来月ご利用ください。"
FREE_DAIKOU_ACCOUNT_LIMIT_MSG = "このツムツムのアカウントは今月すでに無料代行を利用しています。"


class FreeDaikouModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="無料代行（100万コイン）")
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号", placeholder="メール・電話番号・引き継ぎ番号のいずれか", max_length=200)
        self.password = discord.ui.TextInput(label="パスワード", placeholder="パスワード", max_length=200)
        self.add_item(self.login_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        if not free_can_use(uid, login_id=login_id):
            _msg = (FREE_DAIKOU_ACCOUNT_LIMIT_MSG if not free_can_use(login_id=login_id)
                    else FREE_DAIKOU_LIMIT_MSG)
            await interaction.response.send_message(embed=notice_embed(_msg), ephemeral=True)
            return
        free_mark_used(uid, login_id=login_id)
        if not login_id or not password:
            free_unmark_used(uid, login_id=login_id)
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        game_state = {"userid": "", "blocked": False}
        def _on_login(game_userid):
            game_state["userid"] = game_userid or ""
            if game_userid and not free_can_use(game_userid=game_userid):
                game_state["blocked"] = True
                raise LoginHookAbort(FREE_DAIKOU_ACCOUNT_LIMIT_MSG)
            if game_userid:
                free_mark_used(game_userid=game_userid)
        ok = False
        try:
            ok = await create_ticket_and_forge(interaction, "coin", FREE_DAIKOU_COIN, 0, "", login_id, password,
                                               None, on_login=_on_login)
        finally:
            if not ok:
                if game_state["blocked"]:
                    free_mark_used(uid, login_id=login_id, game_userid=game_state["userid"])
                    try:
                        await interaction.followup.send(
                            embed=notice_embed(FREE_DAIKOU_ACCOUNT_LIMIT_MSG), ephemeral=True)
                    except Exception:
                        pass
                else:
                    free_unmark_used(uid, login_id=login_id, game_userid=game_state["userid"])


class FreeDaikouView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="無料代行をうける", style=discord.ButtonStyle.success, custom_id="tsum_free_daikou")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_unlicensed(interaction):
            return
        if not free_can_use(user_id=interaction.user.id):
            await interaction.response.send_message(embed=notice_embed(FREE_DAIKOU_LIMIT_MSG), ephemeral=True)
            return
        await interaction.response.send_modal(FreeDaikouModal())


def free_daikou_embed():
    return discord.Embed(
        title="ツムツム無料代行",
        description=("1人1ヶ月に1回まで、**100万コイン**を無料で代行します。\n"
                     "下のボタンから、アカウント情報を入力してください。"),
        color=0x2ecc71)


async def create_ticket_and_forge_guest(interaction: discord.Interaction, action, val, amount,
                                         paypay_link, migration_id, password, menu_message=None):
    if interaction.guild is None:
        try:
            await interaction.response.send_message(
                embed=notice_embed("ご注文はサーバー内のチャンネルからお願いします。DMでは受け付けていません。"))
        except Exception:
            pass
        return False
    await _safe_defer(interaction)
    if amount > 0:
        pp_file = paypay_file_for(interaction.guild_id)
        if not pp_file:
            await interaction.followup.send(embed=notice_embed(PAYPAY_UNSET_MSG), ephemeral=True)
            await reset_menu_message(menu_message)
            return False
        async with paypay_lock:
            if _is_link_consumed(paypay_link):
                await interaction.followup.send(
                    embed=notice_embed("この支払いは既に処理済みです。届いていない場合はオーナーにご連絡ください。"), ephemeral=True)
                return False
            pres = await _run_paypay_helper_dict("verify_receive", paypay_link, str(int(amount)), pp_file)
            if pres.get("ok"):
                _mark_link_consumed(paypay_link)
        if pres.get("held"):
            await interaction.followup.send(
                embed=_hold_embed(),
                view=TsumHoldApproveView(create_ticket_and_forge_guest, dict(
                    action=action, val=val, amount=amount, paypay_link=paypay_link,
                    migration_id=migration_id, password=password, menu_message=menu_message)),
                ephemeral=True)
            return False
        if not pres.get("ok"):
            await interaction.followup.send(
                embed=notice_embed(pres.get("msg") or "PayPayの受け取りに失敗しました。"), ephemeral=True)
            await reset_menu_message(menu_message)
            return False

    th = await open_ticket(interaction, ticket_thread_name(action, val))
    await interaction.followup.send(f"チケット作成: {ticket_link(th)}", ephemeral=True)
    await send_ticket_header(th, interaction.user, action, val, amount, paypay_link)
    await reset_menu_message(menu_message)
    okey = f"{interaction.id}"
    ACTIVE_ORDERS[okey] = {"user": str(interaction.user), "action": action, "val": val,
                           "ts": int(time.time()), "channel_id": getattr(th, "id", None)}
    info = {"moved": False}
    try:
        ok, _text, info = await _do_forge_guest(th, interaction.user, migration_id, password, **{action: val})
    finally:
        ACTIVE_ORDERS.pop(okey, None)
    print(f"[注文/guest] {interaction.user} {action} {val} ¥{amount} → "
          f"{'成功' if ok else '失敗'} moved={info.get('moved')}", flush=True)
    if info.get("new_id"):
        try:
            await interaction.user.send(embed=discord.Embed(
                title="ツムツム代行 — 新しい引き継ぎ情報",
                description=(f"引き継ぎ番号: `{info['new_id']}`\nパスワード: `{info['new_pw']}`\n"
                             "ゲームの『設定 → 引き継ぎ』からこの情報で引き継いでください。"),
                color=0x2ecc71))
        except Exception:
            pass
    if ok:
        if amount > 0:
            record_sale(interaction.user.id, action, amount)
        await post_public_result(interaction.guild, interaction.user, action, val, amount)
    else:
        if not info.get("moved"):
            await send_guest_retry_prompt(th, interaction.user.id, action, val, amount)
    return ok


class GuestRetryModal(discord.ui.Modal):
    def __init__(self, owner_id, action, val, amount, prompt_message=None):
        super().__init__(title="引き継ぎ情報を再入力")
        self.owner_id = owner_id
        self.action = action
        self.val = val
        self.amount = amount
        self.prompt_message = prompt_message
        self.migration_id = discord.ui.TextInput(
            label="引き継ぎ番号", placeholder="ゲームで発行した引き継ぎ番号", max_length=100)
        self.password = discord.ui.TextInput(
            label="引き継ぎパスワード", placeholder="引き継ぎパスワード", max_length=100)
        self.add_item(self.migration_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        migration_id = self.migration_id.value.strip()
        password = self.password.value.strip()
        if not migration_id or not password:
            await interaction.response.send_message(
                embed=notice_embed("引き継ぎ番号とパスワードを入力してください。"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if self.prompt_message:
                await self.prompt_message.edit(view=None)
        except Exception:
            pass

        channel = interaction.channel
        if not isinstance(channel, (discord.Thread, discord.TextChannel)):
            await interaction.followup.send("この場所では再実行できません。", ephemeral=True)
            return

        lock_key, ticket_lock = get_ticket_lock(channel.id)
        if ticket_lock.locked():
            await interaction.followup.send("このチケットは処理中です。完了までお待ちください。", ephemeral=True)
            return

        is_free = (self.amount == 0 and self.action == "coin" and int(self.val) == FREE_DAIKOU_COIN)
        if is_free:
            if not free_can_use(self.owner_id, login_id=migration_id):
                _m = (FREE_DAIKOU_ACCOUNT_LIMIT_MSG if not free_can_use(login_id=migration_id)
                      else FREE_DAIKOU_LIMIT_MSG)
                await interaction.followup.send(embed=notice_embed(_m), ephemeral=True)
                return
            free_mark_used(self.owner_id, login_id=migration_id)

        await interaction.followup.send("再実行します。", ephemeral=True)
        ok = False
        info = {"moved": False}
        try:
            async with ticket_lock:
                ok, _t, info = await _do_forge_guest(
                    channel, interaction.user, migration_id, password, **{self.action: self.val})
        finally:
            if is_free and not ok:
                free_unmark_used(self.owner_id, login_id=migration_id)
        if not ticket_lock.locked():
            ticket_locks.pop(lock_key, None)

        owner = ticket_owner_user(interaction.guild, self.owner_id, interaction.user)
        if ok:
            await post_public_result(interaction.guild, owner, self.action, self.val, self.amount)
        elif not info.get("moved"):
            await send_guest_retry_prompt(channel, self.owner_id, self.action, self.val, self.amount)


class GuestRetryButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r'tsum_guest_retry:(?P<owner>\d+):(?P<action>[A-Za-z_]+):(?P<val>-?\d+)(?::(?P<amount>-?\d+))?'):
    def __init__(self, owner_id, action, val, amount):
        self.owner_id = int(owner_id)
        self.action = str(action)
        self.val = int(val)
        self.amount = int(amount)
        super().__init__(
            discord.ui.Button(
                label="引き継ぎ情報を再入力",
                style=discord.ButtonStyle.primary,
                custom_id=f"tsum_guest_retry:{self.owner_id}:{self.action}:{self.val}:{self.amount}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        amt = match.group("amount")
        return cls(int(match["owner"]), match["action"], int(match["val"]), int(amt) if amt else 0)

    async def callback(self, interaction: discord.Interaction):
        if not can_operate_ticket(interaction, self.owner_id):
            await interaction.response.send_message(
                embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return
        await interaction.response.send_modal(
            GuestRetryModal(self.owner_id, self.action, self.val, self.amount, interaction.message))


async def send_guest_retry_prompt(channel, owner_id, action, val, amount):
    emb = discord.Embed(
        description="引き継ぎに失敗しました。引き継ぎ番号とパスワードを入力し直して、同じチケットで再実行できます。",
        color=0xf1c40f)
    view = discord.ui.View(timeout=None)
    try:
        view.add_item(GuestRetryButton(owner_id, action, val, amount))
    except Exception as _e:
        print(f"[ticket] guest retry button build failed: {_e}")
        return
    try:
        await channel.send(embed=emb, view=view)
    except (discord.NotFound, discord.Forbidden):
        print("[ticket] guest retry prompt skipped: channel unavailable")


@bot.event
async def on_ready():
    print(f"[bot] ログイン: {bot.user} (id={bot.user.id})")
    _ids = CONFIG.get("allowed_user_ids") or []
    print(f"[bot] /コマンド: manage_guild必須(Discord側で一般ユーザーには非表示) "
          f"＋実行時ゲート(allowed_user_ids={_ids or '未設定'} / 管理者権限)  "
          f"同期: {'guild即時' if GUILD_ID else '全体(最大1h)'}")
    # 起動時に実績カウンターを同期する。
    # 起動直後はまだ数えていない(count=None)ので、ここで必ず1回走らせる。
    # 実際に数えた結果が0件なら、リネーム側が "既に最新" と判断して
    # 10分2回の枠は消費しない。
    request_achievement_update()
    # 前回中断された代行があればチケットに知らせる
    try:
        await _report_interrupted_jobs()
    except Exception as e:
        print(f"[job] 中断分の通知に失敗: {type(e).__name__}: {e}")

async def _ticket_forge(interaction: discord.Interaction, **kw):
    action, val = next(iter(kw.items()))
    await interaction.response.send_modal(AccountBeforeTicketModal(action, val))

def _menu_panel_embed():
    return discord.Embed(title="ツムツム代行料金表", description=menu_description(), color=0x2fd4ff)


# ----------------------------------------------------------------------------
# 設置済みパネルの記録
#   料金を変えたときに、貼り直さなくても表示が最新になるように、設置した
#   メッセージの場所を覚えておいて後から編集する。
#   kind: "menu"(代行の料金表) / "rental"(貸し出しの購入パネル)
# ----------------------------------------------------------------------------
PANEL_MEMORY_MAX = 10


def _panel_refs(kind):
    store = CONFIG.setdefault("panel_messages", {})
    refs = store.setdefault(kind, [])
    if not isinstance(refs, list):
        refs = store[kind] = []
    return refs


def remember_panel(kind, message):
    if message is None:
        return
    refs = _panel_refs(kind)
    ref = {"channel_id": str(message.channel.id), "message_id": str(message.id)}
    keep = [r for r in refs if r.get("message_id") != ref["message_id"]]
    refs[:] = keep[-(PANEL_MEMORY_MAX - 1):] + [ref]
    save_config()


async def refresh_panels(kind):
    """設置済みパネルを今の料金で描き直す。戻り値は更新できた枚数。"""
    refs = _panel_refs(kind)
    alive, done = [], 0
    for r in list(refs):
        try:
            cid, mid = int(r["channel_id"]), int(r["message_id"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            msg = await ch.fetch_message(mid)
        except (discord.NotFound, discord.Forbidden):
            continue                      # 消された・見えない → 記録から外す
        except Exception as e:
            _dbg(f"[panel] 取得失敗 {kind} {cid}/{mid}: {e}")
            alive.append(r)               # 一時的な失敗は記録を残す
            continue
        try:
            if kind == "rental":
                await msg.edit(embed=rental_panel_embed(), view=RentalPanelView())
            else:
                await msg.edit(embed=_menu_panel_embed(), view=MenuView())
            done += 1
            alive.append(r)
        except Exception as e:
            _dbg(f"[panel] 更新失敗 {kind} {cid}/{mid}: {e}")
            alive.append(r)
    if alive != refs:
        refs[:] = alive
        save_config()
    return done


def _panel_updated_note(done):
    if done:
        return f"\n設置済みのパネル {done} 件も更新しました。"
    return "\n※ 設置済みのパネルは見つかりませんでした。必要なら貼り直してください。"

@bot.tree.command(name="パネル設置", description="メニュー(注文パネル)を設置します")
async def slash_menu(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_menu_panel_embed(), view=MenuView())
    try:
        remember_panel("menu", await interaction.original_response())
    except Exception as e:
        _dbg(f"[panel] メニューパネルの記録に失敗: {e}")


@bot.tree.command(name="パネル再設置", description="メニューパネルを再設置します(特に意味なし)")
async def slash_menu_redisplay(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_menu_panel_embed(), view=MenuView())
    try:
        remember_panel("menu", await interaction.original_response())
    except Exception as e:
        _dbg(f"[panel] メニューパネルの記録に失敗: {e}")


@bot.tree.command(name="料金読込", description="bot_config.jsonの料金変更を読み込みます")
async def slash_menu_reload(interaction: discord.Interaction):
    global CONFIG
    await interaction.response.defer(ephemeral=True)
    try:
        CONFIG = load_config()
        MENU_PRICES.update(CONFIG.get("menu_prices", {}))
    except Exception as e:
        await interaction.followup.send(embed=notice_embed(f"再読込に失敗しました: {e}"), ephemeral=True)
        return
    done = await refresh_panels("menu")
    await interaction.followup.send("料金を再読込しました。" + _panel_updated_note(done), ephemeral=True)


@bot.tree.command(name="料金設定", description="メニューの値段を変更します")
@app_commands.rename(menu="メニュー", price="料金")
@app_commands.describe(menu="料金を変更するメニュー", price="変更後の料金")
@app_commands.choices(menu=PRICE_MENU_CHOICES)
async def slash_set_price(interaction: discord.Interaction, menu: app_commands.Choice[str], price: int):
    if price < 0:
        await interaction.response.send_message(embed=notice_embed("料金は0円以上で入力してください。"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    key = menu.value
    MENU_PRICES[key] = price
    CONFIG.setdefault("menu_prices", {})[key] = price
    save_config()
    done = await refresh_panels("menu")
    lines = [f"{menu.name} を ¥{price:,} に変更しました。", "", "**現在の料金**"]
    lines += [f"・{label} … ¥{menu_price(k):,}" for k, label in MENU_ACTIONS]
    try:
        await interaction.followup.send("\n".join(lines) + _panel_updated_note(done), ephemeral=True)
    except discord.HTTPException as e:
        # 価格変更は上で完了済み。確認メッセージの送信失敗のみログに残す。
        _dbg(f"[料金設定] 確認送信失敗 (code={getattr(e,'code',None)}): {e}")


@bot.tree.command(name="無料代行", description="無料代行パネル(100万コイン・月1回)を設置します")
async def slash_free_daikou(interaction: discord.Interaction):
    await interaction.response.send_message(embed=free_daikou_embed(), view=FreeDaikouView())


@bot.tree.command(name="チケットカテゴリー", description="チケットを作成するカテゴリを設定します")
@app_commands.rename(category="カテゴリー")
@app_commands.describe(category="チケットを作るカテゴリー")
async def slash_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    set_guild_setting(interaction.guild_id, "ticket_category_id", str(category.id))
    await interaction.response.send_message(f"チケット作成カテゴリーを {category.mention} に設定しました。", ephemeral=True)


@bot.tree.command(name="チケットチャンネル設定", description="(予備)チケット用チャンネルを設定します")
@app_commands.rename(channel="チャンネル")
async def slash_ticket_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_setting(interaction.guild_id, "ticket_channel_id", str(channel.id))
    await interaction.response.send_message(f"チケットチャンネルを {channel.mention} に設定しました。", ephemeral=True)


@bot.tree.command(name="実績チャンネル設定", description="代行完了の実績を投稿するチャンネルを設定します")
@app_commands.rename(channel="チャンネル")
@app_commands.describe(channel="実績を出すチャンネル。空欄で解除")
async def slash_public_result(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if channel is None:
        set_guild_setting(interaction.guild_id, "public_result_channel_id", None)
        await interaction.response.send_message("実績の自動投稿を解除しました。", ephemeral=True)
        return
    set_guild_setting(interaction.guild_id, "public_result_channel_id", str(channel.id))
    await interaction.response.send_message(f"代行完了の実績を {channel.mention} に自動投稿します。", ephemeral=True)


@bot.tree.command(name="実績カウンター設定",
                  description="完了件数をチャンネル名の末尾(-1234)に表示するチャンネルを設定します")
@app_commands.rename(channel="チャンネル")
@app_commands.describe(channel="件数を名前に付けるチャンネル。空欄で解除")
async def slash_achievement_channel(interaction: discord.Interaction, channel: discord.abc.GuildChannel = None):
    if channel is None:
        CONFIG.pop("achievement_channel_id", None)
        save_config()
        await interaction.response.send_message("実績カウンターの表示を解除しました。", ephemeral=True)
        return
    CONFIG["achievement_channel_id"] = str(channel.id)
    save_config()
    request_achievement_update()
    await interaction.response.send_message(
        f"{channel.mention} の名前の末尾に完了件数(-{achievement_count()})を表示します。\n"
        "（名前変更は10分に2回までのため、反映まで数分かかることがあります）",
        ephemeral=True)


@bot.tree.command(name="実績カウント設定", description="実績カウンターの件数を手動で設定します")
@app_commands.rename(count="件数")
@app_commands.describe(count="設定する件数(0以上)")
async def slash_achievement_set(interaction: discord.Interaction, count: int):
    if count < 0:
        await interaction.response.send_message(embed=notice_embed("件数は0以上で入力してください。"), ephemeral=True)
        return
    set_achievement_count(count)
    request_achievement_update()
    await interaction.response.send_message(f"実績カウントを {count} 件に設定しました。", ephemeral=True)


@bot.tree.command(name="実績カウント", description="現在の実績カウント(完了件数)を表示します")
async def slash_achievement_show(interaction: discord.Interaction):
    n = achievement_count()
    cid = CONFIG.get("achievement_channel_id")
    where = f"<#{cid}>" if cid else "未設定"
    request_achievement_update()
    await interaction.response.send_message(
        f"現在の実績カウント: **{n} 件**\n表示チャンネル: {where}", ephemeral=True)


@bot.tree.command(name="注文状況", description="現在処理中の注文を表示します")
async def slash_orders(interaction: discord.Interaction):
    if not ACTIVE_ORDERS:
        await interaction.response.send_message("現在処理中の注文はありません。", ephemeral=True)
        return
    lines = []
    for o in list(ACTIVE_ORDERS.values()):
        ago = int(time.time() - o.get("ts", 0))
        lines.append(f"・{o.get('user')} … {o.get('action')} {o.get('val')}（{ago}秒経過）")
    await interaction.response.send_message("**処理中の注文**\n" + "\n".join(lines), ephemeral=True)


_APPCMD_BACKUP = os.path.join(HERE, "appcmd_perm_backup.json")

@bot.tree.command(name="コマンド禁止",
                  description="全ロール/チャンネルから『アプリコマンドを使う』を剥奪します(他のbotのスラッシュも止まる)")
async def slash_appcmd_deny(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = interaction.guild
    if g is None:
        return await interaction.followup.send(embed=notice_embed("サーバー内で実行してください。"), ephemeral=True)
    me = g.me
    if not me.guild_permissions.manage_roles:
        return await interaction.followup.send(
            embed=notice_embed("botに『ロールの管理』権限がありません。付与してから実行してください。"), ephemeral=True)
    backup = {"roles": [], "channels": []}
    done_roles, done_ch, skipped = [], [], []
    for role in g.roles:
        try:
            if role.managed or role.permissions.administrator:
                continue
            if not role.permissions.use_application_commands:
                continue
            if role != g.default_role and role >= me.top_role:
                skipped.append(f"{role.name}(botより上位で編集不可)")
                continue
            p = discord.Permissions(role.permissions.value)
            p.update(use_application_commands=False)
            await role.edit(permissions=p, reason="コマンド禁止: アプリコマンドを剥奪")
            backup["roles"].append(role.id)
            done_roles.append(role.name)
        except Exception as e:
            skipped.append(f"{role.name}({type(e).__name__})")
    for ch in g.channels:
        try:
            for target, ow in list(ch.overwrites.items()):
                if ow.use_application_commands is True:
                    ow.update(use_application_commands=None)
                    await ch.set_permissions(target, overwrite=ow, reason="コマンド禁止: allow上書きを解除")
                    backup["channels"].append({"ch": ch.id, "target": target.id})
                    done_ch.append(f"#{ch.name}/{getattr(target, 'name', target.id)}")
        except Exception as e:
            skipped.append(f"#{getattr(ch, 'name', '?')}({type(e).__name__})")
    try:
        with open(_APPCMD_BACKUP, "w", encoding="utf-8") as _f:
            json.dump(backup, _f)
    except Exception:
        pass
    msg = ["**アプリコマンドを剥奪しました**（管理者は元々バイパスするので影響なし）",
           f"・ロール {len(done_roles)}件: {', '.join(done_roles) or 'なし'}",
           f"・チャンネルのallow上書き解除 {len(done_ch)}件: {', '.join(done_ch[:8]) or 'なし'}"]
    if skipped:
        msg.append(f"⚠️触れなかった: {', '.join(skipped[:8])}")
    msg.append("※元に戻すには `/コマンド許可`")
    await interaction.followup.send("\n".join(msg)[:1900], ephemeral=True)


@bot.tree.command(name="コマンド許可", description="/コマンド禁止 で剥奪した『アプリコマンドを使う』を元に戻します")
async def slash_appcmd_allow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = interaction.guild
    if g is None:
        return await interaction.followup.send(embed=notice_embed("サーバー内で実行してください。"), ephemeral=True)
    try:
        backup = json.load(open(_APPCMD_BACKUP, encoding="utf-8-sig"))
    except Exception:
        return await interaction.followup.send(
            embed=notice_embed("復元データ(appcmd_perm_backup.json)がありません。"), ephemeral=True)
    done = []
    for rid in backup.get("roles", []):
        role = g.get_role(int(rid))
        if not role:
            continue
        try:
            p = discord.Permissions(role.permissions.value)
            p.update(use_application_commands=True)
            await role.edit(permissions=p, reason="コマンド許可: 復元")
            done.append(role.name)
        except Exception:
            pass
    for c in backup.get("channels", []):
        try:
            ch = g.get_channel(int(c["ch"]))
            target = g.get_role(int(c["target"])) or g.get_member(int(c["target"]))
            if ch and target:
                ow = ch.overwrites_for(target)
                ow.update(use_application_commands=True)
                await ch.set_permissions(target, overwrite=ow, reason="コマンド許可: 復元")
        except Exception:
            pass
    await interaction.followup.send(f"復元しました（ロール{len(done)}件: {', '.join(done) or 'なし'}）"[:1900],
                                    ephemeral=True)


@bot.tree.command(name="無料無制限", description="指定した人の『無料代行は月1回』制限を解除/解除取消/一覧します")
@app_commands.describe(操作="追加=制限解除する / 解除=元に戻す / 一覧=今の登録者を見る",
                       ユーザー="対象のユーザー(一覧のときは不要)")
@app_commands.choices(操作=[
    app_commands.Choice(name="追加(制限を解除する)", value="add"),
    app_commands.Choice(name="解除(月1回に戻す)", value="remove"),
    app_commands.Choice(name="一覧", value="list"),
])
async def slash_free_unlimited(interaction: discord.Interaction,
                               操作: app_commands.Choice[str],
                               ユーザー: discord.User = None):
    global CONFIG
    if not free_unlimited_enabled():
        return await interaction.response.send_message(
            embed=notice_embed("このbotでは無制限ユーザー機能は使用できません。"), ephemeral=True)
    ids = [int(x) for x in (CONFIG.get("free_unlimited_ids") or [])]
    op = 操作.value
    if op == "list":
        if not ids:
            msg = "無制限に設定されている人はいません。"
        else:
            msg = "**無料代行が無制限の人**\n" + "\n".join(f"・<@{i}>（{i}）" for i in ids)
        return await interaction.response.send_message(msg, ephemeral=True)
    if ユーザー is None:
        return await interaction.response.send_message(
            embed=notice_embed("対象のユーザーを指定してください。"), ephemeral=True)
    uid = int(ユーザー.id)
    if op == "add":
        if uid in ids:
            return await interaction.response.send_message(
                f"<@{uid}> はすでに無制限です。", ephemeral=True)
        ids.append(uid)
        CONFIG["free_unlimited_ids"] = ids
        save_config()
        return await interaction.response.send_message(
            f"<@{uid}> の無料代行の月1回制限を解除しました（何回でも利用できます）。", ephemeral=True)
    if uid not in ids:
        return await interaction.response.send_message(
            f"<@{uid}> は無制限に登録されていません。", ephemeral=True)
    ids = [i for i in ids if i != uid]
    CONFIG["free_unlimited_ids"] = ids
    save_config()
    await interaction.response.send_message(
        f"<@{uid}> を通常（月1回まで）に戻しました。", ephemeral=True)


@bot.tree.command(name="paypayログイン", description="このbotの受取PayPay口座にログインします(SMSにOTPが届きます)")
@app_commands.describe(phone="PayPayの電話番号(例 07012345678)", password="PayPayのパスワード")
async def slash_paypay_login(interaction: discord.Interaction, phone: str, password: str):
    await interaction.response.defer(ephemeral=True)
    target = paypay_guild_file(interaction.guild_id) if interaction.guild_id else PAYPAY_SHARED_FILE
    where = f"**{interaction.guild.name}**" if interaction.guild else "全体"
    out = await _run_pp_script(PP_RELOGIN_START, target, phone, password)
    if "DIRECT_OK" in out:
        msg = ("ログイン完了しました（OTP不要でトークン取得）。\n"
               f"{where} の受取口座: {phone}\nこのまま受け取りに使えます。")
    elif "OTP_SENT" in out:
        prefix = ""
        for line in out.splitlines():
            if "otp_prefix" in line:
                prefix = line.split(":", 1)[-1].strip()
        msg = ("SMSにOTPコードを送信しました。\n"
               + (f"OTPの接頭辞: `{prefix}`\n" if prefix else "")
               + "届いた数字を `/paypayotp` で入力してください。")
    elif "LOGIN_ERROR" in out:
        msg = "電話番号かパスワードが違う可能性があります。確認してもう一度お試しください。"
    else:
        msg = f"ログイン開始に失敗しました。\n```{out[:600]}```"
    await interaction.followup.send(embed=notice_embed(msg, color=0x3498db), ephemeral=True)


@bot.tree.command(name="paypayotp", description="paypayログインの後、SMSで届いたOTPコードを入力して完了します")
@app_commands.describe(code="SMSで届いたOTPコード(数字)")
async def slash_paypay_otp(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    target = paypay_guild_file(interaction.guild_id) if interaction.guild_id else PAYPAY_SHARED_FILE
    where = f"**{interaction.guild.name}**" if interaction.guild else "全体"
    out = await _run_pp_script(PP_RELOGIN_OTP, code.strip(), target)
    if "RELOGIN_OK" in out:
        msg = ("PayPayログインが完了しました。\n"
               f"保存先: `{os.path.basename(target)}`\n"
               f"以降、{where} の注文の受け取りはこの口座で行われます。")
    elif "OTP_FAIL" in out:
        msg = "OTPコードが違うか期限切れです。`/paypayログイン` からやり直してください。"
    else:
        msg = f"OTP確定に失敗しました。\n```{out[:600]}```"
    await interaction.followup.send(embed=notice_embed(msg, color=0x2ecc71), ephemeral=True)


@bot.tree.command(name="paypay状態", description="PayPay受取アカウントの安全な状態を確認します")
async def slash_paypay_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    pp_file = paypay_file_for(interaction.guild_id)
    if not pp_file:
        return await interaction.followup.send(
            embed=notice_embed("このサーバーのPayPayアカウントは未設定です。\n"
                               "`/paypayログイン` で設定してください。", color=0xe67e22),
            ephemeral=True,
        )
    try:
        with open(pp_file, encoding="utf-8") as fp:
            data = json.load(fp) or {}
    except Exception:
        return await interaction.followup.send(
            embed=notice_embed("PayPay設定ファイルを読み込めませんでした。", color=0xe74c3c),
            ephemeral=True,
        )

    phone = str(data.get("phone") or "")
    masked_phone = ("*" * max(0, len(phone) - 4) + phone[-4:]) if phone else "(未設定)"
    has_access = bool(data.get("access_token"))
    has_refresh = bool(data.get("refresh_token"))
    # トークンは時間が経つと無効になるので、取得からの経過時間も出す
    try:
        captured = float(data.get("captured_ts") or 0)
    except (TypeError, ValueError):
        captured = 0.0
    if captured:
        age = time.time() - captured
        token_line = (f"取得日時: {rental_fmt_jst(captured)}"
                      f"（{rental_fmt_duration(age)}前）")
        if age > 86400:
            token_line += "\n⚠ 取得から時間が経っています。受け取りに失敗する場合は "\
                          "`/paypayログイン` → `/paypayotp` で再ログインしてください。"
    else:
        token_line = "取得日時: 不明"
    await interaction.followup.send(
        embed=notice_embed(
            "**PayPay状態**\n"
            f"アカウント: `{masked_phone}`\n"
            f"アクセストークン: {'保存済み' if has_access else 'なし'}\n"
            f"更新トークン: {'保存済み' if has_refresh else 'なし'}\n"
            f"{token_line}\n"
            f"設定ファイル: `{os.path.basename(pp_file)}`（{paypay_file_label(pp_file)}）\n"
            "※トークン本体は表示していません。",
            color=0x3498db,
        ),
        ephemeral=True,
    )


class PayPayLogoutView(discord.ui.View):
    def __init__(self, requester_id: int, path: str = ""):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.path = path or PAYPAY_SHARED_FILE

    @discord.ui.button(label="ログアウトして削除", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message(
                "この確認ボタンを操作できるのはコマンド実行者だけです。",
                ephemeral=True,
            )
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
                msg = "PayPayの保存済み認証情報を削除しました。"
            else:
                msg = "PayPayの保存済み認証情報はありませんでした。"
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                embed=notice_embed(msg, color=0x2ecc71), view=self
            )
        except OSError:
            await interaction.response.send_message(
                "PayPay設定ファイルを削除できませんでした。",
                ephemeral=True,
            )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message(
                "この確認ボタンを操作できるのはコマンド実行者だけです。",
                ephemeral=True,
            )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=notice_embed("PayPayログアウトをキャンセルしました。", color=0x95a5a6),
            view=self,
        )


@bot.tree.command(name="paypayログアウト", description="保存済みPayPay認証情報を削除します")
async def slash_paypay_logout(interaction: discord.Interaction):
    pp_file = paypay_file_for(interaction.guild_id)
    if not pp_file:
        return await interaction.response.send_message(
            embed=notice_embed("このサーバーにはPayPayの保存済み認証情報がありません。"), ephemeral=True)
    if pp_file == PAYPAY_SHARED_FILE and not is_bot_owner(interaction.user):
        return await interaction.response.send_message(
            embed=notice_embed("全体共通のPayPay認証情報はオーナーのみ削除できます。"), ephemeral=True)
    await interaction.response.send_message(
        embed=notice_embed(
            f"保存済みのPayPay認証情報（{paypay_file_label(pp_file)}）を削除します。\n"
            "削除後は、受け取り処理の前に再ログインが必要です。\n"
            "実行する場合は下のボタンを押してください。",
            color=0xe74c3c,
        ),
        view=PayPayLogoutView(interaction.user.id, pp_file),
        ephemeral=True,
    )


@bot.tree.command(name="収益", description="今日/今週/今月の収益を確認します")
async def slash_sales(interaction: discord.Interaction):
    s = sales_summary()
    def line(label, t):
        amt, n = t
        return f"{label}: ¥{amt:,}（{n}件）"
    msg = ("**収益**\n" + line("今日", s["today"]) + "\n" + line("今週(7日)", s["week"]) + "\n"
           + line("今月(30日)", s["month"]) + "\n" + line("累計", s["total"]))
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="収益リセット", description="収益記録をリセットします")
async def slash_sales_reset(interaction: discord.Interaction):
    reset_sales()
    await interaction.response.send_message("収益記録をリセットしました。", ephemeral=True)


@bot.tree.command(name="状態", description="状態(IP/アカウント/トークン/CAPTCHA)を確認します")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    txt = await loop.run_in_executor(None, status_text)
    await interaction.followup.send(txt, ephemeral=True)


@bot.tree.command(name="ツムログイン確認", description="ツムツムへログインして安全なデバッグ情報を確認します")
async def slash_tsum_login_debug(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    txt = await loop.run_in_executor(None, tsum_login_debug_text)
    try:
        await interaction.user.send(txt)
        await interaction.followup.send(
            "確認結果をあなたのDMへ送信しました。", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "DMを送信できませんでした。サーバーのメンバー一覧からBotとのDMを許可してから、もう一度実行してください。",
            ephemeral=True,
        )




async def _rental_resolve_gid(interaction, サーバーid):
    """入力（招待リンク/ID）から対象サーバーを決める。無指定なら今いるサーバー。"""
    if not (サーバーid or "").strip():
        return interaction.guild_id, None
    return await resolve_guild_id(サーバーid)


async def _require_owner(interaction: discord.Interaction) -> bool:
    if is_bot_owner(interaction.user):
        return True
    await interaction.response.send_message(
        embed=notice_embed("このコマンドは bot のオーナーのみ使用できます。"), ephemeral=True)
    return False


@bot.tree.command(name="サーバー貸し出し", description="サーバーIDを指定してbotを貸し出します(期限付き)")
@app_commands.describe(操作="貸出/延長・上書き・永久・返却・確認・一覧",
                       サーバーid="貸し出し先の招待リンクまたはサーバーID(省略時は今いるサーバー)",
                       月数="貸し出す月数(1/3/6/12 など)",
                       日数="月数に追加する日数(任意)",
                       メモ="メモ(任意・管理用)")
@app_commands.choices(操作=[
    app_commands.Choice(name="貸出/延長(残り期間に追加)", value="add"),
    app_commands.Choice(name="期限を上書き(今から指定期間)", value="set"),
    app_commands.Choice(name="永久に貸し出す", value="unlimited"),
    app_commands.Choice(name="返却(貸し出しを取り消す)", value="remove"),
    app_commands.Choice(name="確認(残り期間を見る)", value="check"),
    app_commands.Choice(name="一覧(貸し出し中のサーバー)", value="list"),
])
async def slash_rental(interaction: discord.Interaction,
                       操作: app_commands.Choice[str],
                       サーバーid: str = "",
                       月数: int = 0,
                       日数: int = 0,
                       メモ: str = ""):
    op = 操作.value
    gid, gerr = await _rental_resolve_gid(interaction, サーバーid)

    # 「確認」だけは貸し出し先の管理者も使える（自分のサーバーの残り期間のみ）。
    if not is_bot_owner(interaction.user):
        if op != "check" or gid is None or gid != interaction.guild_id:
            return await _require_owner(interaction)
    if gerr and op != "list":
        return await interaction.response.send_message(
            embed=notice_embed(gerr), ephemeral=True)

    if not rental_enabled():
        return await interaction.response.send_message(
            embed=notice_embed("このbotでは貸し出し機能が無効になっています"
                               "（bot_config.json の rental_enabled）。"), ephemeral=True)

    if op == "list":
        rows = rental_list()
        if not rows:
            msg = "現在、貸し出し中のサーバーはありません。"
        else:
            lines = ["**貸し出し中のサーバー**"]
            for g, exp in rows[:25]:
                guild = bot.get_guild(g)
                name = guild.name if guild else "(bot未参加)"
                if exp == RENTAL_UNLIMITED:
                    lines.append(f"・{name}（{g}）… 永久")
                else:
                    lines.append(f"・{name}（{g}）… {rental_fmt_jst(exp)} まで"
                                 f"（残り {rental_fmt_duration(exp - time.time())}）")
            if len(rows) > 25:
                lines.append(f"… ほか {len(rows) - 25} 件")
            msg = "\n".join(lines)
        return await interaction.response.send_message(msg, ephemeral=True)

    if gid is None:
        return await interaction.response.send_message(
            embed=notice_embed("対象サーバーの招待リンク、またはサーバーIDを指定してください。"),
            ephemeral=True)

    guild = bot.get_guild(int(gid))
    gname = guild.name if guild else "(bot未参加)"

    if op == "check":
        return await interaction.response.send_message(
            f"**{gname}**（{gid}）\n{rental_status_text(gid)}", ephemeral=True)

    if op == "remove":
        if not rental_revoke(gid):
            return await interaction.response.send_message(
                f"{gname}（{gid}）は貸し出しされていません。", ephemeral=True)
        await interaction.response.send_message(
            f"{gname}（{gid}）の貸し出しを取り消しました。", ephemeral=True)
        return await _rental_notify(
            f"{interaction.user} がサーバー **{gname}**（{gid}）の貸し出しを取り消しました。", 0xe74c3c)

    if op == "unlimited":
        exp = rental_grant(gid, unlimited=True, by=interaction.user.id, note=メモ)
        span = "永久"
    else:
        if 月数 <= 0 and 日数 <= 0:
            return await interaction.response.send_message(
                embed=notice_embed("月数（または日数）を1以上で指定してください。"), ephemeral=True)
        exp = rental_grant(gid, months=月数, seconds=int(日数) * 86400,
                           by=interaction.user.id, note=メモ, extend=(op == "add"))
        span = (rental_plan_label(月数) if 月数 > 0 else "") + (f"{日数}日" if 日数 > 0 else "")
    word = "追加しました" if op == "add" else "設定しました"
    invite = bot_invite_url()
    lines = [f"**{gname}**（`{gid}`）に {span} の貸し出しを{word}。",
             f"有効期限: {rental_until_text(exp)}"]
    if guild is None and invite:
        lines += ["", "まだ bot がこのサーバーにいません。招待リンク:", invite]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
    await _rental_notify(
        f"{interaction.user} がサーバー **{gname}**（{gid}）へ {span} 貸し出しました"
        f"（{rental_until_text(exp)}）。" + (f"\nメモ: {メモ}" if メモ else ""), 0x2ecc71)


@bot.tree.command(name="貸出パネル設置", description="サーバー貸し出しの購入パネルを設置します")
async def slash_rental_panel(interaction: discord.Interaction):
    if not await _require_owner(interaction):
        return
    await interaction.response.send_message(embed=rental_panel_embed(), view=RentalPanelView())
    try:
        remember_panel("rental", await interaction.original_response())
    except Exception as e:
        _dbg(f"[panel] 貸し出しパネルの記録に失敗: {e}")


@bot.tree.command(name="貸出料金設定", description="貸し出しプラン(月数と料金)を追加・変更・削除します")
@app_commands.describe(月数="プランの月数。0 を指定すると『永久』プラン",
                       料金="料金(円)。-1 を指定するとそのプランを削除します")
async def slash_rental_price(interaction: discord.Interaction, 月数: int, 料金: int):
    if not await _require_owner(interaction):
        return
    if 月数 < 0:
        return await interaction.response.send_message(
            embed=notice_embed("月数は0以上で指定してください（0=永久）。"), ephemeral=True)
    prices = {str(k): v for k, v in rental_prices().items()}
    label = rental_plan_label(月数)
    if 料金 < 0:
        if prices.pop(str(月数), None) is None:
            return await interaction.response.send_message(
                f"{label} のプランはありません。", ephemeral=True)
        msg = f"{label} のプランを削除しました。"
    else:
        prices[str(月数)] = int(料金)
        msg = f"{label} の料金を {料金:,}円 にしました。"
    await interaction.response.defer(ephemeral=True)
    CONFIG["rental_prices"] = {k: prices[k] for k in sorted(prices, key=lambda s: (int(s) == 0, int(s)))}
    save_config()
    done = await refresh_panels("rental")
    lines = [msg, "", "**現在の貸し出しプラン**"]
    if rental_prices():
        lines += [f"・{rental_plan_label(m)} … {p:,}円" for m, p in rental_prices().items()]
    else:
        lines.append("・なし（販売停止中）")
    await interaction.followup.send("\n".join(lines) + _panel_updated_note(done), ephemeral=True)


@bot.tree.command(name="コマンド一覧", description="このサーバーで使えるコマンドの一覧を表示します")
async def slash_command_list(interaction: discord.Interaction):
    lines = ["**このサーバーで使えるコマンド**（サーバー管理者のみ）", ""]
    lines += guild_admin_command_lines()
    if is_bot_owner(interaction.user):
        lines += ["", "**オーナー専用**", "、".join(owner_command_lines())]
    msg = "\n".join(lines)
    await interaction.response.send_message(msg[:1990], ephemeral=True)


@bot.tree.command(name="自分のサーバー登録", description="このサーバーを『貸し出し不要で常に使える自分のサーバー』にします")
@app_commands.describe(操作="登録=常に使えるようにする / 解除=元に戻す / 一覧=登録済みを見る")
@app_commands.choices(操作=[
    app_commands.Choice(name="登録(このサーバーを自分用にする)", value="add"),
    app_commands.Choice(name="解除", value="remove"),
    app_commands.Choice(name="一覧", value="list"),
])
async def slash_home_guild(interaction: discord.Interaction, 操作: app_commands.Choice[str]):
    if not await _require_owner(interaction):
        return
    ids = []
    for x in (CONFIG.get("rental_home_guild_ids") or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    op = 操作.value
    if op == "list":
        extra = f"（bot_config.json の guild_id: {GUILD_ID or '未設定'}）"
        if not ids:
            return await interaction.response.send_message(
                "登録されている自分のサーバーはありません。" + extra, ephemeral=True)
        lines = []
        for g in ids:
            guild = bot.get_guild(g)
            lines.append(f"・{guild.name if guild else '(bot未参加)'}（{g}）")
        return await interaction.response.send_message(
            "**自分のサーバー**\n" + "\n".join(lines) + "\n" + extra, ephemeral=True)
    gid = interaction.guild_id
    if gid is None:
        return await interaction.response.send_message(
            embed=notice_embed("サーバー内で実行してください。"), ephemeral=True)
    if op == "add":
        if gid in ids:
            return await interaction.response.send_message(
                "このサーバーはすでに登録されています。", ephemeral=True)
        ids.append(gid)
        CONFIG["rental_home_guild_ids"] = [str(i) for i in ids]
        save_config()
        return await interaction.response.send_message(
            "このサーバーを自分のサーバーとして登録しました。"
            "貸し出しの期限に関係なく、いつでも利用できます。", ephemeral=True)
    if gid not in ids:
        return await interaction.response.send_message(
            "このサーバーは登録されていません。", ephemeral=True)
    CONFIG["rental_home_guild_ids"] = [str(i) for i in ids if i != gid]
    save_config()
    await interaction.response.send_message(
        "登録を解除しました。以降は貸し出しの期限が必要になります。", ephemeral=True)


@bot.tree.command(name="貸出ログチャンネル設定", description="貸し出しの購入・期限切れを記録するチャンネルを設定します")
@app_commands.rename(channel="チャンネル")
@app_commands.describe(channel="記録先チャンネル(未指定で解除)")
async def slash_rental_log_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await _require_owner(interaction):
        return
    CONFIG["rental_log_channel_id"] = str(channel.id) if channel else ""
    save_config()
    where = channel.mention if channel else "なし（記録しません）"
    await interaction.response.send_message(f"貸し出しの記録先を {where} にしました。", ephemeral=True)


@bot.event
async def on_guild_join(guild: discord.Guild):
    """新しいサーバーに入ったとき、貸し出し状況を確認して案内する。"""
    licensed = guild_allowed(guild.id)
    print(f"[rental] サーバー参加: {guild.name} ({guild.id}) 貸出={'あり' if licensed else 'なし'}",
          flush=True)
    exp = rental_expires(guild.id)
    if exp is None:
        state = "自分のサーバー（常時利用可）" if licensed else "なし（未貸出）"
    else:
        state = f"あり / {rental_until_text(exp)}"
    await _rental_notify(
        f"サーバー **{guild.name}**（{guild.id}）に参加しました。貸し出し: {state}",
        0x3498db if licensed else 0xf1c40f)
    try:
        me = guild.me
        ch = guild.system_channel
        if ch is None or not ch.permissions_for(me).send_messages:
            ch = next((c for c in guild.text_channels if c.permissions_for(me).send_messages), None)
        if ch is None:
            return
        if licensed:
            emb = paypay_setup_embed(guild)
            if exp is not None:
                emb.set_footer(text=f"利用期間: {rental_until_text(exp)}")
            await ch.send(embed=emb)
        else:
            await ch.send(embed=guild_block_embed(guild.id))
    except Exception as e:
        _dbg(f"[rental] 参加時の案内送信失敗 guild={guild.id}: {e}")
    if not licensed and bool(CONFIG.get("rental_leave_unlicensed", False)):
        try:
            await guild.leave()
            print(f"[rental] 未貸出のため退出: {guild.name} ({guild.id})", flush=True)
        except Exception as e:
            _dbg(f"[rental] 退出失敗 guild={guild.id}: {e}")

# RenderなどのWeb Serviceでポート待受を維持するための軽量HTTPサーバー
# Flaskは追加せず、標準ライブラリだけで / を200 OKにします。
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _RenderHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _start_render_health_server():
    port = int(os.environ.get("PORT", "5000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _RenderHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[render] HTTP health server started on 0.0.0.0:{port}", flush=True)
    return server


if __name__ == "__main__":
    _start_render_health_server()
    bot.run(CONFIG["token"])

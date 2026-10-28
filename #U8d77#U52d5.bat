@echo off
setlocal enabledelayedexpansion

rem このbatがある場所に移動する(どこから起動しても正しく動く)
cd /d "%~dp0"

echo ============================================
echo  ツムツム代行bot 起動
echo ============================================
echo.

rem ------------------------------------------------------------------
rem Tor / Privoxy を探す。見つからなくてもbotは起動する
rem (その場合は直接接続で動作する。bot側が自動で判定する)
rem ------------------------------------------------------------------
set "TOR_EXE="
for %%P in (
    "%~dp0tor\tor.exe"
    "%USERPROFILE%\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
    "%ProgramFiles%\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
    "%ProgramFiles(x86)%\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
    "%LOCALAPPDATA%\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
    "%~dp0tor\tor.exe"
    "%ProgramData%\chocolatey\bin\tor.exe"
    "%USERPROFILE%\scoop\apps\tor\current\tor.exe"
) do if not defined TOR_EXE if exist %%P set "TOR_EXE=%%~P"
if not defined TOR_EXE for %%P in (tor.exe) do if not defined TOR_EXE if exist "%%~$PATH:P" set "TOR_EXE=%%~$PATH:P"

set "PRIVOXY_EXE="
for %%P in (
    "%ProgramFiles(x86)%\Privoxy\privoxy.exe"
    "%ProgramFiles%\Privoxy\privoxy.exe"
    "%~dp0privoxy\privoxy.exe"
) do if not defined PRIVOXY_EXE if exist %%P set "PRIVOXY_EXE=%%~P"
if not defined PRIVOXY_EXE for %%P in (privoxy.exe) do if not defined PRIVOXY_EXE if exist "%%~$PATH:P" set "PRIVOXY_EXE=%%~$PATH:P"

rem GeoIPファイル(国の判定用)。tor.exe と同じ配布物の中にあることが多い
set "TOR_GEOIP="
if defined TOR_EXE (
    for %%D in ("!TOR_EXE!") do set "TOR_DIR=%%~dpD"
    if exist "!TOR_DIR!..\Data\Tor\geoip" set "TOR_GEOIP=!TOR_DIR!..\Data\Tor\geoip"
    if exist "!TOR_DIR!geoip" set "TOR_GEOIP=!TOR_DIR!geoip"
)
rem botフォルダに同梱したGeoIPがあれば最優先で使う(自己完結させるため)
if exist "%~dp0geoip" set "TOR_GEOIP=%~dp0geoip"

set "TOR_DATA=%~dp0tor-data"
set "TOR_LOG=%~dp0tor.log"
set "PRIVOXY_CONF=%~dp0privoxy-tsum.txt"

rem ------------------------------------------------------------------
rem Tor 起動
rem ------------------------------------------------------------------
set "TOR_ON=0"
tasklist /FI "IMAGENAME eq tor.exe" 2>nul | find /I "tor.exe" >nul
if not errorlevel 1 (
    echo [OK] Tor は既に起動中
    set "TOR_ON=1"
) else if defined TOR_EXE (
    echo [起動] Tor を起動します
    echo        !TOR_EXE!
    if not exist "!TOR_DATA!" mkdir "!TOR_DATA!"
    rem 前回のログが残っていると古い "Bootstrapped 100" を今回の分と誤認するので消す
    if exist "!TOR_LOG!" del /q "!TOR_LOG!" >nul 2>&1
    set "TOR_STARTED_BY_US=1"
    if defined TOR_GEOIP (
        start "Tor" /min "!TOR_EXE!" --SocksPort 9050 --DataDirectory "!TOR_DATA!" --Log "notice file !TOR_LOG!" --GeoIPFile "!TOR_GEOIP!" --ExcludeExitNodes "{nl}" --StrictNodes 1
    ) else (
        rem GeoIPが無いと国指定は使えないので付けない(付けると警告が出るだけ)
        start "Tor" /min "!TOR_EXE!" --SocksPort 9050 --DataDirectory "!TOR_DATA!" --Log "notice file !TOR_LOG!"
    )
    set "TOR_ON=1"
) else (
    echo [情報] tor.exe が見つかりません → 直接接続で動作します
)

rem ------------------------------------------------------------------
rem Privoxy 起動 (Discord を Tor 経由にする場合のみ必要)
rem ------------------------------------------------------------------
rem Discordを直接接続にしている(discord_proxy_url が空)なら Privoxy は不要
python -c "import json,sys;sys.exit(0 if json.load(open('bot_config.json',encoding='utf-8')).get('discord_proxy_url') else 1)" >nul 2>&1
if errorlevel 1 (
    echo [情報] Discordは直接接続の設定です → Privoxy は起動しません
    goto skip_privoxy
)
tasklist /FI "IMAGENAME eq privoxy.exe" 2>nul | find /I "privoxy.exe" >nul
if not errorlevel 1 (
    echo [OK] Privoxy は既に起動中
) else if defined PRIVOXY_EXE (
    if exist "!PRIVOXY_CONF!" (
        echo [起動] Privoxy を起動します
        start "Privoxy" /min "!PRIVOXY_EXE!" "!PRIVOXY_CONF!"
    ) else (
        echo [情報] privoxy-tsum.txt が無いので Privoxy は起動しません
        echo        Discordは直接接続になります
    )
) else (
    echo [情報] privoxy.exe が見つかりません → Discordは直接接続
)
:skip_privoxy

rem ------------------------------------------------------------------
rem Tor が使えるようになるまで待つ
rem (ポートが開くのと実際に通信できるのは別。実通信で確認する)
rem ------------------------------------------------------------------
if "!TOR_ON!"=="1" (
    echo.
    echo Tor の準備ができるまで待っています...
    echo (初回のみリレー情報の取得に5～10分かかります。2回目以降は数秒で終わります)
    set /a _w=0
    set "LAST_BS="

    :wait_tor
    rem 自分で起動した場合はログから進捗を読む(通信を繰り返すより速く、進捗も見える)
    if not "!TOR_STARTED_BY_US!"=="1" goto check_by_request
    findstr /C:"Bootstrapped 100" "!TOR_LOG!" >nul 2>&1
    if not errorlevel 1 goto check_by_request
    for /f "delims=" %%L in ('findstr /C:"Bootstrapped " "!TOR_LOG!" 2^>nul') do set "BS=%%L"
    if not "!BS!"=="!LAST_BS!" (
        set "LAST_BS=!BS!"
        echo    !BS:*] =!
    )
    goto tor_wait_next

    :check_by_request
    rem 準備できたはず。実際に通信できるか1回だけ確かめる
    python -c "import sys,requests;px={'http':'socks5h://127.0.0.1:9050','https':'socks5h://127.0.0.1:9050'};sys.exit(0 if requests.get('https://check.torproject.org/api/ip',proxies=px,timeout=15).json().get('IsTor') else 1)" >nul 2>&1
    if not errorlevel 1 goto tor_ready

    :tor_wait_next
    set /a _w+=1
    if !_w! GEQ 300 goto tor_timeout
    ping -n 3 127.0.0.1 >nul
    goto wait_tor

    :tor_ready
    echo [OK] Tor 経由で通信できることを確認しました
    goto tor_done

    :tor_timeout
    echo [警告] Tor がまだ使える状態になりません。
    echo        このまま起動します(繋がらない場合bot側が自動で直接接続に切り替えます)
    :tor_done
)

rem ------------------------------------------------------------------
rem bot 起動
rem ここから先は python の出力(UTF-8)を表示するため UTF-8 に切り替える。
rem 切り替え後は bat 内の日本語が化けるので、以降は英数字のみにしてある。
rem ------------------------------------------------------------------
echo.
chcp 65001 >nul

python main.py

echo.
echo ============================================
echo  bot stopped.
echo ============================================
pause

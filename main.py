"""Entry point for the attached Tsum bot bundle.

The uploaded source files have timestamped names, while tsum_bot.py expects
the original module names.  This launcher loads those files under the names
expected by the bot and then starts the Discord bot.

Put bot_config.json in the project root (or set TSUM_BOT_CONFIG to another
JSON file) before starting this program.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "attached_assets"


def _find_source(original_name: str) -> Path:
    """Find an uploaded source file by its original stem."""
    # Depending on how the files were uploaded, they may be in the project
    # root or inside attached_assets/.
    for directory in (ROOT, ASSETS):
        direct = directory / original_name
        if direct.exists():
            return direct

    stem = Path(original_name).stem
    matches = []
    for directory in (ROOT, ASSETS):
        matches.extend(directory.glob(f"{stem}_*.py"))
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(
            f"必要なファイルが見つかりません: {original_name} "
            f"(検索先: {ASSETS})"
        )
    raise RuntimeError(
        f"{original_name} に一致するファイルが複数あります: "
        + ", ".join(str(p.name) for p in matches)
    )


def _load_as(module_name: str, filename: str) -> ModuleType:
    """Load a source file and register it under its expected import name."""
    source = _find_source(filename)
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"モジュールを読み込めません: {source}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _config_path() -> Path:
    configured = os.environ.get("TSUM_BOT_CONFIG")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else ROOT / path

    # Prefer a user-created root config.  The second location supports running
    # the bundle directly without moving the uploaded files.
    root_config = ROOT / "bot_config.json"
    asset_config = ASSETS / "bot_config.json"
    if root_config.exists():
        return root_config
    return asset_config


def main() -> None:
    config = _config_path()
    if not config.exists():
        example = ASSETS / "bot_config.example_1787661856700.json"
        raise SystemExit(
            "bot_config.json がありません。\n"
            f"例: {example} を参考に {ROOT / 'bot_config.json'} を作成し、"
            "Discord Bot token などを設定してください。"
        )

    # tsum_bot.py resolves relative paths from the directory containing its
    # source file, so use an absolute config path here.
    os.environ["TSUM_BOT_CONFIG"] = str(config.resolve())
    if str(ASSETS) not in sys.path:
        sys.path.insert(0, str(ASSETS))

    # Load dependencies before tsum_bot.py imports them by their original
    # names.  The order matters because tsum and tsum_guest import
    # tsum_login.
    _load_as("tsum_login", "tsum_login.py")
    _load_as("line_password_login", "line_password_login.py")
    _load_as("tsum", "tsum.py")
    _load_as("tsum_guest", "tsum_guest.py")
    _load_as("tsum_forge", "tsum_forge.py")
    bot_module = _load_as("tsum_bot", "tsum_bot.py")

    # The bot launches these helpers as child processes.  Point them at the
    # timestamped uploaded files instead of requiring manual renames.
    bot_module.PAYPAY_HELPER = str(_find_source("paypay_helper.py"))
    bot_module.PP_RELOGIN_START = str(_find_source("_pp_relogin_start.py"))
    bot_module.PP_RELOGIN_OTP = str(_find_source("_pp_relogin_otp.py"))

    bot = getattr(bot_module, "bot", None)
    if bot is None:
        raise RuntimeError("tsum_bot.py に Discord bot インスタンスがありません。")

    token = bot_module.CONFIG.get("token")
    if not token:
        raise SystemExit(
            f"設定ファイル {config} の token が空です。Discord Bot token を設定してください。"
        )
    bot.run(token)


if __name__ == "__main__":
    main()
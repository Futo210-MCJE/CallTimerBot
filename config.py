import os
from pathlib import Path
from dotenv import load_dotenv

# プロジェクトルートディレクトリの特定
BASE_DIR = Path(__file__).resolve().parent

# .env ファイルの読み込み
load_dotenv(BASE_DIR / ".env")

# 環境変数の取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

# サーバーID（即時同期用、省略可）
GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID", "").strip()
DISCORD_GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None

# デフォルト通話制限時間 (分)
DEFAULT_DURATION_MINUTES = int(os.getenv("DEFAULT_DURATION_MINUTES", "60"))

# データベースファイルの保存先
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "timers.db"


def validate_config():
    """起動時に設定値が正しく入っているか検証する"""
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN == "your_bot_token_here":
        raise ValueError(
            "DISCORD_BOT_TOKEN が設定されていません。.env ファイルを作成し、有効なBotトークンを入力してください。"
        )

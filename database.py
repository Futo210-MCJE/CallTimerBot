import datetime
import json
import sqlite3
import time
from typing import Optional, List, Dict, Any
from config import DB_PATH


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """データベースとテーブルの初期化"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # 通話タイマーテーブル
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                text_channel_id INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                duration_minutes INTEGER NOT NULL,
                target_user_ids TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                warned_5m INTEGER NOT NULL DEFAULT 0,
                warned_1m INTEGER NOT NULL DEFAULT 0,
                log_message_id INTEGER,
                logged INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cursor.execute("PRAGMA table_info(timers)")
        columns = [col[1] for col in cursor.fetchall()]
        if "log_message_id" not in columns:
            cursor.execute("ALTER TABLE timers ADD COLUMN log_message_id INTEGER")
        if "logged" not in columns:
            cursor.execute("ALTER TABLE timers ADD COLUMN logged INTEGER NOT NULL DEFAULT 0")

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_guild_status ON timers(guild_id, status);
            """
        )

        # 通話履歴ログテーブル (終了した通話のアルバム・実績記録)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                timer_id INTEGER,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                duration_seconds INTEGER NOT NULL,
                planned_minutes INTEGER NOT NULL,
                target_user_ids TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_logs_guild ON call_logs(guild_id);
            """
        )

        # サーバー設定テーブル (ログ用チャンネルIDなど)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER
            );
            """
        )

        conn.commit()


def create_timer(
    guild_id: int,
    voice_channel_id: int,
    text_channel_id: int,
    duration_minutes: int,
    target_user_ids: List[int],
) -> Dict[str, Any]:
    """新規タイマーを作成し、既存のアクティブなタイマーがあればキャンセルする"""
    now = time.time()
    end_time = now + (duration_minutes * 60)
    target_json = json.dumps(target_user_ids)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE timers SET status = 'CANCELLED' WHERE guild_id = ? AND status = 'ACTIVE'",
            (guild_id,),
        )
        cursor.execute(
            """
            INSERT INTO timers (
                guild_id, voice_channel_id, text_channel_id,
                start_time, end_time, duration_minutes, target_user_ids, status, logged
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 0)
            """,
            (
                guild_id,
                voice_channel_id,
                text_channel_id,
                now,
                end_time,
                duration_minutes,
                target_json,
            ),
        )
        timer_id = cursor.lastrowid
        conn.commit()

        cursor.execute("SELECT * FROM timers WHERE id = ?", (timer_id,))
        row = cursor.fetchone()
        return _row_to_dict(row)


def get_active_timer(guild_id: int) -> Optional[Dict[str, Any]]:
    """指定ギルドのアクティブタイマーを取得"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM timers WHERE guild_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        row = cursor.fetchone()
        return _row_to_dict(row) if row else None


def get_all_active_timers() -> List[Dict[str, Any]]:
    """全てのアクティブなタイマーを取得"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM timers WHERE status = 'ACTIVE'")
        rows = cursor.fetchall()
        return [_row_to_dict(r) for r in rows]


def cancel_timer(guild_id: int) -> Optional[Dict[str, Any]]:
    """指定ギルドのアクティブタイマーをキャンセルし、そのタイマー情報を返す"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM timers WHERE guild_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        cursor.execute(
            "UPDATE timers SET status = 'CANCELLED' WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
        return _row_to_dict(row)


def complete_timer(timer_id: int):
    """タイマーを完了としてマーク"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE timers SET status = 'COMPLETED' WHERE id = ?",
            (timer_id,),
        )
        conn.commit()


def mark_logged(timer_id: int) -> bool:
    """ログ記録済みフラグをアトミックに更新（二重ログを完全遮断）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE timers SET logged = 1 WHERE id = ? AND logged = 0",
            (timer_id,),
        )
        affected = cursor.rowcount
        conn.commit()
        return affected > 0


def add_target_user(guild_id: int, user_id: int):
    """通話中に入室してきたユーザーをタイマーの対象メンバーに追加"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, target_user_ids FROM timers WHERE guild_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        row = cursor.fetchone()
        if not row:
            return

        try:
            ids = json.loads(row["target_user_ids"])
        except Exception:
            ids = []

        if user_id not in ids:
            ids.append(user_id)
            cursor.execute(
                "UPDATE timers SET target_user_ids = ? WHERE id = ?",
                (json.dumps(ids), row["id"]),
            )
            conn.commit()


def extend_timer(
    guild_id: int, additional_minutes: int, max_limit: int = 120
) -> Optional[Dict[str, Any]]:
    """アクティブタイマーを延長（最大時間制限を遵守）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM timers WHERE guild_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        current_duration = row["duration_minutes"]
        if current_duration >= max_limit:
            return None

        actual_addition = min(additional_minutes, max_limit - current_duration)
        if actual_addition <= 0:
            return None

        new_end_time = row["end_time"] + (actual_addition * 60)
        new_duration = current_duration + actual_addition

        cursor.execute(
            """
            UPDATE timers 
            SET end_time = ?, duration_minutes = ?, warned_5m = 0, warned_1m = 0 
            WHERE id = ?
            """,
            (new_end_time, new_duration, row["id"]),
        )
        conn.commit()

        cursor.execute("SELECT * FROM timers WHERE id = ?", (row["id"],))
        updated_row = cursor.fetchone()
        return _row_to_dict(updated_row)


def mark_warned(timer_id: int, warn_type: str):
    """警告通知フラグを更新"""
    col = "warned_5m" if warn_type == "5m" else "warned_1m"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE timers SET {col} = 1 WHERE id = ?", (timer_id,))
        conn.commit()


def set_monitor_message_id(timer_id: int, message_id: int):
    """モニターメッセージIDを保存"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE timers SET log_message_id = ? WHERE id = ?",
            (message_id, timer_id),
        )
        conn.commit()


set_log_message_id = set_monitor_message_id


# -------------------------------------------------------------
# 通話ログ操作関数
# -------------------------------------------------------------
def record_call_log(
    guild_id: int,
    timer_id: int,
    start_time: float,
    end_time: float,
    duration_seconds: int,
    planned_minutes: int,
    target_user_ids: List[int],
) -> Dict[str, Any]:
    """終了した通話を記録として永続化保存"""
    target_json = json.dumps(target_user_ids)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO call_logs (
                guild_id, timer_id, start_time, end_time, duration_seconds,
                planned_minutes, target_user_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                timer_id,
                start_time,
                end_time,
                duration_seconds,
                planned_minutes,
                target_json,
            ),
        )
        log_id = cursor.lastrowid
        conn.commit()

        cursor.execute("SELECT * FROM call_logs WHERE id = ?", (log_id,))
        row = cursor.fetchone()
        return _row_to_dict(row)


def get_call_stats(guild_id: int) -> Dict[str, Any]:
    """そのサーバーの通話統計（通算回数・合計時間、今週の回数・時間）を取得"""
    now = datetime.datetime.now()
    start_of_week = now - datetime.timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_ts = start_of_week.timestamp()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0) FROM call_logs WHERE guild_id = ?",
            (guild_id,),
        )
        total_count, total_seconds = cursor.fetchone()

        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0) FROM call_logs WHERE guild_id = ? AND start_time >= ?",
            (guild_id, week_start_ts),
        )
        week_count, week_seconds = cursor.fetchone()

        return {
            "total_count": total_count,
            "total_seconds": total_seconds,
            "week_count": week_count,
            "week_seconds": week_seconds,
        }


def set_guild_log_channel(guild_id: int, channel_id: int):
    """通話ログを記録するテキストチャンネルを設定"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO guild_settings (guild_id, log_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id
            """,
            (guild_id, channel_id),
        )
        conn.commit()


def get_guild_log_channel(guild_id: int) -> Optional[int]:
    """通話ログを記録するテキストチャンネルIDを取得"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT log_channel_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        row = cursor.fetchone()
        return row["log_channel_id"] if row and row["log_channel_id"] else None


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if isinstance(d.get("target_user_ids"), str):
        try:
            d["target_user_ids"] = json.loads(d["target_user_ids"])
        except Exception:
            d["target_user_ids"] = []
    return d

import asyncio
import datetime
import logging
import socket
import sys
import time
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import tasks

import config
import database

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("CallTimerBot")

# 多重起動防止用ロック
_lock_socket = None


def ensure_single_instance(port: int = 49299):
    """同一マシン上でBotが2重起動するのを完全に防止する"""
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", port))
        _lock_socket.listen(1)
    except (socket.error, OSError):
        logger.warning("Another bot process is already running. Exiting.")
        sys.stderr.write("\n" + "=" * 60 + "\n")
        sys.stderr.write("[WARNING] Another bot instance is already running!\n")
        sys.stderr.write("Exiting to prevent duplicate execution.\n")
        sys.stderr.write("=" * 60 + "\n\n")
        sys.exit(0)


intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True


class CallTimerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        database.init_db()
        logger.info("Database initialized.")

        # 常駐Viewの登録 (Bot再起動後もボタン操作を有効化)
        self.add_view(ControlPanelView())
        self.add_view(ActiveCallControlView())

        check_call_timers.start()

        # スラッシュコマンドの同期
        try:
            if config.DISCORD_GUILD_ID:
                guild_obj = discord.Object(id=config.DISCORD_GUILD_ID)
                self.tree.copy_global_to(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
                logger.info(f"Slash commands synced to guild: {config.DISCORD_GUILD_ID}")
            else:
                await self.tree.sync()
                logger.info("Global slash commands synced.")
        except discord.Forbidden as e:
            if e.code == 50001:
                logger.warning(
                    f"\n====================================================================\n"
                    f"⚠️ [重要] サーバー (ID: {config.DISCORD_GUILD_ID}) にBotが参加していません！\n"
                    f"BotをDiscordサーバーに招待してください。\n"
                    f"====================================================================\n"
                )
            else:
                logger.error(f"コマンド同期に失敗しました (Forbidden): {e}")
        except Exception as e:
            logger.error(f"コマンド同期エラー: {e}")


bot = CallTimerBot()


# -------------------------------------------------------------
# ユーティリティ: プログレスバー & Embed生成
# -------------------------------------------------------------
def generate_progress_bar(progress: float, length: int = 14) -> str:
    """進捗率 (0.0 ~ 1.0) に基づいてテキストプログレスバーを生成"""
    filled = int(round(length * progress))
    filled = max(0, min(length, filled))
    empty = length - filled
    return "█" * filled + "░" * empty


def create_monitor_embed(
    timer: dict, guild: discord.Guild, status: str = "RUNNING"
) -> discord.Embed:
    """通話中モニターEmbedを生成（アラートもこのEmbed内に集約）"""
    now = time.time()
    start_time = timer["start_time"]
    end_time = timer["end_time"]
    total_seconds = timer["duration_minutes"] * 60
    elapsed_seconds = max(0, int(now - start_time))
    remaining_seconds = max(0, int(end_time - now))

    progress = min(1.0, elapsed_seconds / total_seconds) if total_seconds > 0 else 1.0
    percent = int(progress * 100)
    bar = generate_progress_bar(progress, length=14)

    voice_ch = guild.get_channel(timer["voice_channel_id"])
    voice_name = voice_ch.name if voice_ch else "通話チャンネル"

    target_ids = timer.get("target_user_ids", [])
    members_text = (
        ", ".join(f"<@{uid}>" for uid in target_ids) if target_ids else "通話メンバー"
    )

    end_ts = int(end_time)
    start_ts = int(start_time)

    if status == "RUNNING":
        if remaining_seconds <= 60:  # 1分未満
            color = discord.Color.red()
            title = "🚨 通話中 【残り1分・まもなく自動切断】"
        elif remaining_seconds <= 300:  # 5分未満
            color = discord.Color.gold()
            title = "⚠️ 通話中 【残り5分・まもなく終了】"
        else:
            color = discord.Color.blurple()
            title = "🎙️ 通話中"

        embed = discord.Embed(
            title=title,
            description=f"`[{bar}]` `{percent}%`",
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name="残り時間",
            value=f"**{remaining_seconds // 60}分 {remaining_seconds % 60:02d}秒** (<t:{end_ts}:R>)",
            inline=True,
        )
        embed.add_field(
            name="経過 / 設定",
            value=f"{elapsed_seconds // 60}分 / {timer['duration_minutes']}分",
            inline=True,
        )
        embed.add_field(
            name="切断予定時刻",
            value=f"<t:{end_ts}:T>",
            inline=True,
        )
        embed.add_field(name="チャンネル", value=f"`{voice_name}`", inline=True)
        embed.add_field(name="参加者", value=members_text, inline=True)
        embed.set_footer(text="10秒毎自動更新")

    elif status == "COMPLETED":
        embed = discord.Embed(
            title="🏁 通話終了（自動切断完了）",
            description=f"設定時間（**{timer['duration_minutes']}分**）が経過したため、通話を切断しました。\n`[{generate_progress_bar(1.0, 14)}] 100%`",
            color=discord.Color.dark_grey(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="チャンネル", value=f"`{voice_name}`", inline=True)
        embed.add_field(name="切断対象", value=members_text, inline=True)
        embed.add_field(
            name="通話時間",
            value=f"<t:{start_ts}:t> 〜 <t:{end_ts}:t> ({timer['duration_minutes']}分間)",
            inline=False,
        )

    elif status == "CANCELLED":
        embed = discord.Embed(
            title="🛑 通話終了",
            description=f"通話タイマーを終了しました。（通話時間: 約 {elapsed_seconds // 60}分 {elapsed_seconds % 60:02d}秒）",
            color=discord.Color.dark_grey(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="チャンネル", value=f"`{voice_name}`", inline=True)

    return embed


async def update_monitor_message(timer: dict, status: str = "RUNNING"):
    """通話メッセージを安全にインライン更新"""
    guild = bot.get_guild(timer["guild_id"])
    if not guild:
        return

    text_ch = guild.get_channel(timer["text_channel_id"])
    if not text_ch or not isinstance(text_ch, discord.TextChannel):
        return

    msg_id = timer.get("log_message_id")
    if not msg_id:
        return

    embed = create_monitor_embed(timer, guild, status=status)
    view = ActiveCallControlView() if status == "RUNNING" else None

    try:
        msg = await text_ch.fetch_message(msg_id)
        await msg.edit(embed=embed, view=view)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.warning(f"Forbidden to edit message in channel {text_ch.id}")
    except discord.HTTPException as e:
        if e.status == 429:
            logger.warning("Rate limit hit while updating monitor message.")
        else:
            logger.warning(f"HTTPException while updating monitor: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in update_monitor_message: {e}")


# -------------------------------------------------------------
# 通話ログ投稿機能（2人揃っているときだけ1回だけ記録）
# -------------------------------------------------------------
async def post_call_log(timer: dict):
    """通話終了後に記録カードを作成し、ログ用テキストチャンネルへ投稿（2人揃っている時のみ）"""
    # 二重ログ記録をアトミックに完全防止（すでに記録済みなら即リターン）
    if not database.mark_logged(timer["id"]):
        return

    # 参加者リストの取得（重複排除）
    target_ids = list(set(timer.get("target_user_ids", [])))

    # ★要件: 2人そろっているときだけカウントする（1人だけのソロ通話・テスト時は記録しない）
    if len(target_ids) < 2:
        logger.info(
            f"Timer {timer['id']}: Participants count is {len(target_ids)} (< 2). Skipping call log."
        )
        return

    guild = bot.get_guild(timer["guild_id"])
    if not guild:
        return

    now = time.time()
    start_time = timer["start_time"]
    duration_seconds = max(0, int(now - start_time))

    # 10秒未満の誤作動はスキップ
    if duration_seconds < 10:
        return

    # DBに永続化記録
    log_data = database.record_call_log(
        guild_id=guild.id,
        timer_id=timer["id"],
        start_time=start_time,
        end_time=now,
        duration_seconds=duration_seconds,
        planned_minutes=timer["duration_minutes"],
        target_user_ids=target_ids,
    )

    # 統計取得
    stats = database.get_call_stats(guild.id)
    total_count = stats["total_count"]
    total_h = stats["total_seconds"] // 3600
    total_m = (stats["total_seconds"] % 3600) // 60

    week_count = stats["week_count"]
    week_h = stats["week_seconds"] // 3600
    week_m = (stats["week_seconds"] % 3600) // 60

    members_text = " & ".join(f"<@{uid}>" for uid in target_ids)

    # ログ用チャンネルの決定
    log_ch_id = database.get_guild_log_channel(guild.id)
    log_ch = guild.get_channel(log_ch_id) if log_ch_id else None

    # 未設定の場合、チャンネル名から自動検索
    if not log_ch or not isinstance(log_ch, discord.TextChannel):
        for ch in guild.text_channels:
            lower = ch.name.lower()
            if any(k in lower for k in ["log", "ログ", "記録", "history"]):
                log_ch = ch
                database.set_guild_log_channel(guild.id, ch.id)
                break

    # それでもなければ操作チャンネルに送信
    if not log_ch or not isinstance(log_ch, discord.TextChannel):
        log_ch = guild.get_channel(timer["text_channel_id"])

    if not log_ch or not isinstance(log_ch, discord.TextChannel):
        return

    start_ts = int(start_time)
    end_ts = int(now)
    dur_min = duration_seconds // 60
    dur_sec = duration_seconds % 60

    # 終了状況などの余分な項目は削り、シンプルで綺麗なログカードにする
    embed = discord.Embed(
        title=f"📜 通話記録 #{log_data['id']}",
        color=discord.Color.teal(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="⏱ 通話時間",
        value=f"**{dur_min}分 {dur_sec:02d}秒** (設定: {timer['duration_minutes']}分)",
        inline=True,
    )
    embed.add_field(name="👥 通話メンバー", value=members_text, inline=True)
    embed.add_field(
        name="🕒 通話日時",
        value=f"<t:{start_ts}:f> 〜 <t:{end_ts}:t>",
        inline=False,
    )
    embed.add_field(
        name="📈 累計実績",
        value=(
            f"📅 **今週:** {week_count}回目 (計 {week_h}時間{week_m:02d}分)\n"
            f"🏆 **通算:** {total_count}回目 (計 {total_h}時間{total_m:02d}分)"
        ),
        inline=False,
    )
    embed.set_footer(text="通話の思い出ログ ✨")

    try:
        await log_ch.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to post call log: {e}")


# -------------------------------------------------------------
# コア機能: タイマー開始処理 (GUI・コマンド共通)
# -------------------------------------------------------------
async def start_timer_core(
    interaction: discord.Interaction, minutes: int
) -> Optional[dict]:
    """タイマー開始の共通処理"""
    if minutes < 1 or minutes > 120:
        await interaction.response.send_message(
            "❌ 通話時間は **1分〜120分** の間で指定してください。", ephemeral=True
        )
        return None

    user = interaction.user
    if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
        await interaction.response.send_message(
            "❌ ボイスチャンネルに参加してから操作してください。", ephemeral=True
        )
        return None

    voice_ch = user.voice.channel
    guild = interaction.guild
    me = guild.me

    # 切断権限の事前チェック
    if not voice_ch.permissions_for(me).move_members:
        await interaction.response.send_message(
            "⚠️ Botに「メンバーを移動」権限がありません。サーバー設定を確認してください。",
            ephemeral=True,
        )
        return None

    participants = [m for m in voice_ch.members if not m.bot]
    target_ids = [m.id for m in participants]

    # 古いタイマーがあればメッセージを終了状態に更新（上書き時のログ送信は行わない）
    old_timer = database.get_active_timer(guild.id)
    if old_timer:
        await update_monitor_message(old_timer, status="CANCELLED")

    # DBにタイマー作成
    timer = database.create_timer(
        guild_id=guild.id,
        voice_channel_id=voice_ch.id,
        text_channel_id=interaction.channel_id,
        duration_minutes=minutes,
        target_user_ids=target_ids,
    )

    embed = create_monitor_embed(timer, guild, status="RUNNING")
    view = ActiveCallControlView()

    # チャンネルに1通だけ通話モニターカードを投稿
    if interaction.response.is_done():
        msg = await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

    database.set_monitor_message_id(timer["id"], msg.id)
    timer["log_message_id"] = msg.id
    return timer


# -------------------------------------------------------------
# GUI: カスタム時間入力モーダル
# -------------------------------------------------------------
class CustomDurationModal(discord.ui.Modal, title="通話時間の設定"):
    minutes_input = discord.ui.TextInput(
        label="通話時間 (分)",
        placeholder="1〜120の数字を入力 (例: 45)",
        min_length=1,
        max_length=3,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        val = self.minutes_input.value.strip()
        if not val.isdigit():
            await interaction.response.send_message(
                "❌ 半角数字で入力してください。（例: 45）", ephemeral=True
            )
            return

        minutes = int(val)
        if minutes < 1 or minutes > 120:
            await interaction.response.send_message(
                "❌ 通話時間は **1分〜120分** の範囲で入力してください。", ephemeral=True
            )
            return

        await start_timer_core(interaction, minutes)


# -------------------------------------------------------------
# GUI: 通話中カードに付属する操作ボタン（2つのみ）
# -------------------------------------------------------------
class ActiveCallControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="➕ 10分延長",
        style=discord.ButtonStyle.primary,
        custom_id="btn_active_extend_10m",
    )
    async def extend_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        timer = database.get_active_timer(interaction.guild_id)
        if not timer:
            await interaction.response.send_message(
                "❌ 稼働中の通話タイマーはありません。", ephemeral=True
            )
            return

        current_dur = timer["duration_minutes"]
        if current_dur >= 120:
            await interaction.response.send_message(
                "❌ 既に上限（120分）に達しているため延長できません。", ephemeral=True
            )
            return

        updated = database.extend_timer(interaction.guild_id, 10, max_limit=120)
        if updated:
            end_ts = int(updated["end_time"])
            await interaction.response.send_message(
                f"⏳ 通話時間を10分延長しました（合計: **{updated['duration_minutes']}分** / 終了予定: <t:{end_ts}:T>）",
                ephemeral=True,
            )
            await update_monitor_message(updated, status="RUNNING")
        else:
            await interaction.response.send_message(
                "延長処理に失敗しました。", ephemeral=True
            )

    @discord.ui.button(
        label="🛑 通話終了",
        style=discord.ButtonStyle.danger,
        custom_id="btn_active_stop_call",
    )
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        timer = database.get_active_timer(interaction.guild_id)
        if not timer:
            await interaction.response.send_message(
                "❌ 稼働中の通話タイマーはありません。", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🛑 通話を切断します。", ephemeral=True
        )
        await execute_disconnect(timer, reason="手動終了（ボタン操作）")


# -------------------------------------------------------------
# GUI: 常設コントロールパネル
# -------------------------------------------------------------
class DurationSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="15分", value="15"),
            discord.SelectOption(label="30分", value="30"),
            discord.SelectOption(label="45分", value="45"),
            discord.SelectOption(label="60分 (標準)", value="60"),
            discord.SelectOption(label="90分", value="90"),
            discord.SelectOption(label="120分 (最大)", value="120"),
            discord.SelectOption(
                label="✏️ 自由に入力 (1〜120分)", value="custom"
            ),
        ]
        super().__init__(
            placeholder="⏱️ 時間を選択して通話を開始...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="select_call_duration",
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "custom":
            await interaction.response.send_modal(CustomDurationModal())
        else:
            minutes = int(selected)
            await start_timer_core(interaction, minutes)


class ControlPanelView(discord.ui.View):
    """常設コントロールパネル用View"""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(DurationSelect())

    @discord.ui.button(
        label="▶️ 60分スタート",
        style=discord.ButtonStyle.success,
        row=1,
        custom_id="btn_panel_start_60",
    )
    async def quick_start_60(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await start_timer_core(interaction, 60)

    @discord.ui.button(
        label="➕ 10分延長",
        style=discord.ButtonStyle.primary,
        row=1,
        custom_id="btn_panel_extend_10",
    )
    async def panel_extend(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        timer = database.get_active_timer(interaction.guild_id)
        if not timer:
            await interaction.response.send_message(
                "❌ 稼働中の通話タイマーはありません。", ephemeral=True
            )
            return

        current_dur = timer["duration_minutes"]
        if current_dur >= 120:
            await interaction.response.send_message(
                "❌ 既に上限（120分）に達しているため延長できません。", ephemeral=True
            )
            return

        updated = database.extend_timer(interaction.guild_id, 10, max_limit=120)
        if updated:
            end_ts = int(updated["end_time"])
            await interaction.response.send_message(
                f"⏳ 通話時間を10分延長しました（合計: **{updated['duration_minutes']}分** / 終了予定: <t:{end_ts}:T>）",
                ephemeral=True,
            )
            await update_monitor_message(updated, status="RUNNING")
        else:
            await interaction.response.send_message(
                "延長処理に失敗しました。", ephemeral=True
            )

    @discord.ui.button(
        label="🛑 通話終了",
        style=discord.ButtonStyle.danger,
        row=1,
        custom_id="btn_panel_stop",
    )
    async def panel_stop(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        timer = database.get_active_timer(interaction.guild_id)
        if not timer:
            await interaction.response.send_message(
                "❌ 稼働中の通話タイマーはありません。", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🛑 通話を切断します。", ephemeral=True
        )
        await execute_disconnect(timer, reason="手動終了（パネル操作）")

    @discord.ui.button(
        label="📊 状況確認",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="btn_panel_status",
    )
    async def panel_status(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        timer = database.get_active_timer(interaction.guild_id)
        if not timer:
            await interaction.response.send_message(
                "ℹ️ 現在通話タイマーは稼働していません。", ephemeral=True
            )
            return

        now = time.time()
        rem_sec = max(0, int(timer["end_time"] - now))
        end_ts = int(timer["end_time"])
        await interaction.response.send_message(
            f"⏱ **残り時間:** 約 {rem_sec // 60}分 {rem_sec % 60:02d}秒 (<t:{end_ts}:R>)",
            ephemeral=True,
        )


def create_panel_embed() -> discord.Embed:
    """シンプルで洗練されたパネルEmbed"""
    embed = discord.Embed(
        title="🎙️ 通話コントロールパネル",
        description="通話時間の制限を設定・管理します。",
        color=discord.Color.blurple(),
    )
    return embed


# -------------------------------------------------------------
# イベントリスナー
# -------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user.name} (ID: {bot.user.id})")

    # 起動時の復元・期限切れチェック
    now = time.time()
    active_timers = database.get_all_active_timers()
    logger.info(f"Loaded {len(active_timers)} active timer(s) on startup.")

    for timer in active_timers:
        if now >= timer["end_time"]:
            logger.info(
                f"Timer {timer['id']} expired while bot was offline. Disconnecting now."
            )
            await execute_disconnect(timer, reason="制限時間超過（再起動復元）")


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    """VCの入退室を監視。パートナーの後から入室を追跡し、全員退出時は終了・記録"""
    if member.bot:
        return

    timer = database.get_active_timer(member.guild.id)
    if not timer:
        return

    voice_ch_id = timer["voice_channel_id"]

    # 1. 通話中に新しいパートナーが入室してきた場合、記録対象に追加
    if after.channel and after.channel.id == voice_ch_id:
        database.add_target_user(member.guild.id, member.id)
        # メモリ上のtimer情報も更新
        ids = timer.get("target_user_ids", [])
        if member.id not in ids:
            ids.append(member.id)
            timer["target_user_ids"] = ids
        await update_monitor_message(timer, status="RUNNING")
        return

    # 2. 通話チャンネルから退出した場合
    if before.channel and before.channel.id == voice_ch_id:
        remaining_humans = [m for m in before.channel.members if not m.bot]
        if len(remaining_humans) == 0:
            database.complete_timer(timer["id"])
            await update_monitor_message(timer, status="CANCELLED")
            # 2人揃っていた場合のみ1回だけ記録
            await post_call_log(timer)


# -------------------------------------------------------------
# 強制切断処理 (インライン更新 & 1回だけログ記録)
# -------------------------------------------------------------
async def execute_disconnect(timer: dict, reason: str = "制限時間到達（自動切断）"):
    guild = bot.get_guild(timer["guild_id"])
    if not guild:
        database.complete_timer(timer["id"])
        return

    voice_ch = guild.get_channel(timer["voice_channel_id"])
    target_ids = set(timer.get("target_user_ids", []))
    members_to_disconnect: List[discord.Member] = []

    if voice_ch and isinstance(voice_ch, discord.VoiceChannel):
        for m in voice_ch.members:
            if m.bot:
                continue
            if not target_ids or m.id in target_ids:
                members_to_disconnect.append(m)
    else:
        for uid in target_ids:
            m = guild.get_member(uid)
            if m and m.voice and m.voice.channel:
                members_to_disconnect.append(m)

    # メンバー切断実行 (VC接続チェック付きで 400 Bad Request を完全防止)
    for m in members_to_disconnect:
        try:
            if m.voice and m.voice.channel:
                await m.move_to(None, reason=reason)
        except discord.Forbidden:
            logger.error(
                f"Forbidden: Bot lacks 'Move Members' permission to disconnect {m.display_name}"
            )
        except Exception as e:
            logger.error(f"Error disconnecting member {m.display_name}: {e}")

    # DBを完了に更新
    database.complete_timer(timer["id"])

    # 通話モニターメッセージ自体を「🏁 通話終了」に更新
    await update_monitor_message(timer, status="COMPLETED")

    # 通話ログチャンネルへ思い出記録を投稿（2人揃っていた場合のみ1回だけ実行）
    await post_call_log(timer)


# -------------------------------------------------------------
# バックグラウンド監視ループ (10秒間隔)
# -------------------------------------------------------------
@tasks.loop(seconds=10.0)
async def check_call_timers():
    now = time.time()
    try:
        active_timers = database.get_all_active_timers()
    except Exception as e:
        logger.error(f"DB read error in timer loop: {e}")
        return

    for timer in active_timers:
        end_time = timer["end_time"]
        remaining_sec = end_time - now

        guild = bot.get_guild(timer["guild_id"])
        if not guild:
            continue

        # 1. 終了時刻到達 -> 強制切断
        if remaining_sec <= 0:
            logger.info(f"Timer {timer['id']} reached end_time. Executing disconnect.")
            await execute_disconnect(timer, reason="制限時間到達（自動切断）")
            continue

        # 2. 通話カードのリアルタイム更新
        await update_monitor_message(timer, status="RUNNING")


@check_call_timers.before_loop
async def before_check_timers():
    await bot.wait_until_ready()


# -------------------------------------------------------------
# スラッシュコマンドの実装
# -------------------------------------------------------------
@bot.tree.command(name="panel", description="通話コントロールパネルを設置します")
async def panel_command(interaction: discord.Interaction):
    embed = create_panel_embed()
    view = ControlPanelView()
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(
    name="start",
    description="通話タイマーを開始します（1〜120分、省略時は60分）",
)
@app_commands.describe(
    minutes="通話時間（1分〜120分で指定。省略時は60分）"
)
async def start_command(
    interaction: discord.Interaction,
    minutes: Optional[app_commands.Range[int, 1, 120]] = None,
):
    if minutes is None:
        minutes = config.DEFAULT_DURATION_MINUTES

    await start_timer_core(interaction, minutes)


@bot.tree.command(name="cancel", description="通話タイマーを終了します")
async def cancel_command(interaction: discord.Interaction):
    timer = database.get_active_timer(interaction.guild_id)
    if not timer:
        await interaction.response.send_message(
            "❌ 稼働中の通話タイマーはありません。", ephemeral=True
        )
        return

    database.cancel_timer(interaction.guild_id)
    await update_monitor_message(timer, status="CANCELLED")
    await post_call_log(timer)
    await interaction.response.send_message(
        "🛑 通話タイマーを終了しました。", ephemeral=True
    )


@bot.tree.command(name="extend", description="通話時間を延長します（最大120分まで）")
@app_commands.describe(
    minutes="延長する分数（1分〜60分で指定。省略時は10分）"
)
async def extend_command(
    interaction: discord.Interaction,
    minutes: Optional[app_commands.Range[int, 1, 60]] = 10,
):
    if minutes is None:
        minutes = 10

    timer = database.get_active_timer(interaction.guild_id)
    if not timer:
        await interaction.response.send_message(
            "❌ 稼働中の通話タイマーはありません。", ephemeral=True
        )
        return

    current_dur = timer["duration_minutes"]
    if current_dur >= 120:
        await interaction.response.send_message(
            "❌ 既に上限（120分）に達しているため延長できません。",
            ephemeral=True,
        )
        return

    updated = database.extend_timer(interaction.guild_id, minutes, max_limit=120)
    if updated:
        end_ts = int(updated["end_time"])
        await interaction.response.send_message(
            f"⏳ 通話時間を{minutes}分延長しました（合計: **{updated['duration_minutes']}分** / 終了予定: <t:{end_ts}:T>）",
            ephemeral=True,
        )
        await update_monitor_message(updated, status="RUNNING")
    else:
        await interaction.response.send_message(
            "延長処理に失敗しました。", ephemeral=True
        )


@bot.tree.command(
    name="set_log_channel",
    description="通話終了時の思い出記録を投稿するテキストチャンネルを設定します",
)
@app_commands.describe(channel="記録を投稿するテキストチャンネル")
async def set_log_channel_command(
    interaction: discord.Interaction, channel: discord.TextChannel
):
    database.set_guild_log_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(
        f"📜 通話記録の保存先を {channel.mention} に設定しました！\n今後は2人揃って通話が終了したときのみ、このチャンネルに記録が残ります。",
        ephemeral=True,
    )


@bot.tree.command(name="stats", description="これまでの通話記録・統計を確認します")
async def stats_command(interaction: discord.Interaction):
    stats = database.get_call_stats(interaction.guild_id)
    total_count = stats["total_count"]
    total_h = stats["total_seconds"] // 3600
    total_m = (stats["total_seconds"] % 3600) // 60

    week_count = stats["week_count"]
    week_h = stats["week_seconds"] // 3600
    week_m = (stats["week_seconds"] % 3600) // 60

    embed = discord.Embed(
        title="📊 通話実績レポート",
        color=discord.Color.teal(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="📅 今週の通話",
        value=f"回数: **{week_count} 回**\n合計時間: **{week_h}時間 {week_m:02d}分**",
        inline=True,
    )
    embed.add_field(
        name="🏆 通算の通話",
        value=f"回数: **{total_count} 回**\n合計時間: **{total_h}時間 {total_m:02d}分**",
        inline=True,
    )

    log_ch_id = database.get_guild_log_channel(interaction.guild_id)
    if log_ch_id:
        embed.set_footer(text=f"記録チャンネル: #{interaction.guild.get_channel(log_ch_id).name if interaction.guild.get_channel(log_ch_id) else '未設定'}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Botの稼働状態と通話状況を確認します")
async def status_command(interaction: discord.Interaction):
    timer = database.get_active_timer(interaction.guild_id)
    embed = discord.Embed(
        title="🤖 通話Bot ステータス",
        color=discord.Color.dark_grey(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="接続状態",
        value="🟢 正常稼働中 (Ping: {:.0f}ms)".format(bot.latency * 1000),
        inline=True,
    )
    if timer:
        now = time.time()
        rem_sec = max(0, int(timer["end_time"] - now))
        end_ts = int(timer["end_time"])
        embed.add_field(
            name="タイマー状態",
            value=f"🏃 **稼働中** (残り約 {rem_sec // 60}分 {rem_sec % 60:02d}秒 / <t:{end_ts}:R>)",
            inline=True,
        )
    else:
        embed.add_field(name="タイマー状態", value="⏸ 待機中（通話なし）", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


def main():
    # 多重起動の完全ブロック
    ensure_single_instance()

    try:
        config.validate_config()
    except ValueError as e:
        logger.error(str(e))
        print(f"\n[エラー] {e}\n", file=sys.stderr)
        sys.exit(1)

    try:
        bot.run(config.DISCORD_BOT_TOKEN)
    except discord.LoginFailure:
        logger.error("Discord Botトークンが無効です。正確なトークンを設定してください。")
        sys.exit(1)
    except Exception as e:
        logger.error(f"予期しないエラーが発生しました: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

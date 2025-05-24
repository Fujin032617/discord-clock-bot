import discord
from discord.ext import commands, tasks
import sqlite3
from datetime import datetime, timedelta
import pytz

# Replace with your timezone
ph_tz = pytz.timezone('Asia/Manila')

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = 'bot_data.db'

# In-memory store for active shifts: {user_id: clock_in_datetime_str}
active_shifts = {}

# Load excluded users from DB into set for quick check
excluded_users = set()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # Create tables if they don't exist
    c.execute('''
        CREATE TABLE IF NOT EXISTS active_shifts (
            user_id INTEGER PRIMARY KEY,
            clock_in TEXT NOT NULL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS excluded_users (
            user_id INTEGER PRIMARY KEY
        )
    ''')

    conn.commit()
    conn.close()

def load_active_shifts():
    global active_shifts
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, clock_in FROM active_shifts')
    rows = c.fetchall()
    active_shifts = {user_id: clock_in for user_id, clock_in in rows}
    conn.close()

def load_excluded_users():
    global excluded_users
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM excluded_users')
    rows = c.fetchall()
    excluded_users = set(user_id for (user_id,) in rows)
    conn.close()

async def log_to_google_sheets(user_id, action, timestamp):
    # Your Google Sheets logging logic here
    pass

def cleanup_stale_shifts():
    """Remove shifts active more than 14 hours ago."""
    conn = get_db_connection()
    c = conn.cursor()
    threshold = datetime.now(ph_tz) - timedelta(hours=14)
    threshold_str = threshold.strftime("%Y-%m-%d %H:%M:%S")
    c.execute('DELETE FROM active_shifts WHERE clock_in < ?', (threshold_str,))
    conn.commit()
    conn.close()

    # Remove from in-memory dict
    stale_users = [uid for uid, clock_in_str in active_shifts.items()
                   if datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S") < threshold]
    for uid in stale_users:
        del active_shifts[uid]

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    init_db()
    load_active_shifts()
    load_excluded_users()
    cleanup_stale_shifts()
    auto_clock_out_check.start()

@bot.event
async def on_voice_state_update(member, before, after):
    user_id = member.id

    # Skip bots and excluded users
    if member.bot or user_id in excluded_users:
        return

    # User joined a voice channel
    if before.channel is None and after.channel is not None:
        if user_id in active_shifts:
            # Already clocked in - do nothing
            return

        now = datetime.now(ph_tz)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        active_shifts[user_id] = now_str

        conn = get_db_connection()
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO active_shifts (user_id, clock_in) VALUES (?, ?)', (user_id, now_str))
        conn.commit()
        conn.close()

        await log_to_google_sheets(user_id, "IN", now_str)

    # User left voice channel
    elif before.channel is not None and after.channel is None:
        if user_id in active_shifts:
            now = datetime.now(ph_tz)
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")

            await log_to_google_sheets(user_id, "OUT", now_str)

            del active_shifts[user_id]

            conn = get_db_connection()
            c = conn.cursor()
            c.execute('DELETE FROM active_shifts WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()

# Background task: check for auto clock-out after 14 hours
@tasks.loop(minutes=10)
async def auto_clock_out_check():
    now = datetime.now(ph_tz)
    to_clock_out = []

    for user_id, clock_in_str in active_shifts.items():
        clock_in_time = datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S")
        if now >= clock_in_time + timedelta(hours=14):
            to_clock_out.append(user_id)

    for user_id in to_clock_out:
        user = bot.get_user(user_id)
        if not user:
            continue

        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        await log_to_google_sheets(user_id, "OUT", now_str)

        del active_shifts[user_id]

        conn = get_db_connection()
        c = conn.cursor()
        c.execute('DELETE FROM active_shifts WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

        try:
            await user.send(f"You have been automatically clocked out after 14 hours at {now_str}.")
        except:
            pass

# Admin command to clear all active shifts
@bot.command()
@commands.has_permissions(administrator=True)
async def clearshifts(ctx):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM active_shifts')
    conn.commit()
    conn.close()

    active_shifts.clear()

    await ctx.send("All active shifts have been reset.")

# New command to show active shifts
@bot.command()
@commands.has_permissions(administrator=True)
async def activeshifts(ctx):
    if not active_shifts:
        await ctx.send("No active shifts currently.")
        return

    lines = []
    for user_id, clock_in_str in active_shifts.items():
        user = bot.get_user(user_id)
        username = user.name if user else f"User ID {user_id}"
        # Show clock-in time in readable format
        dt_obj = datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S")
        display_time = dt_obj.strftime("%b %d %Y %I:%M %p")
        lines.append(f"**{username}** clocked in at {display_time}")

    msg = "\n".join(lines)
    # Discord message limit ~2000 chars; you may want to paginate if many users
    await ctx.send(f"**Active Shifts:**\n{msg}")

bot.run("YOUR_DISCORD_BOT_TOKEN")

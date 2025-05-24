import os
import discord
from discord.ext import commands, tasks
import sqlite3
from datetime import datetime, timedelta
import pytz
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask
import threading
import json

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)
ph_tz = pytz.timezone("Asia/Manila")

# ========== SQLite Setup ==========
conn = sqlite3.connect('botdata.db')
c = conn.cursor()

# Create tables if not exist
c.execute('''CREATE TABLE IF NOT EXISTS active_shifts (
    user_id INTEGER PRIMARY KEY,
    clock_in TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS excluded_users (
    user_id INTEGER PRIMARY KEY
)''')
conn.commit()

# ========== Google Sheets Setup ==========
scope = ['https://spreadsheets.google.com/feeds',
         'https://www.googleapis.com/auth/drive']

# Expect GOOGLE_CREDS env variable to contain JSON credentials string
creds_json = os.getenv("GOOGLE_CREDS")
if not creds_json:
    raise Exception("Missing GOOGLE_CREDS environment variable.")

creds_dict = json.loads(creds_json)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)
sheet = gc.open("Attendance").sheet1  # Change to your actual Google Sheet name

# ========== Helper functions ==========

def add_active_shift(user_id: int, clock_in_str: str):
    c.execute("INSERT OR REPLACE INTO active_shifts (user_id, clock_in) VALUES (?, ?)", (user_id, clock_in_str))
    conn.commit()

def remove_active_shift(user_id: int):
    c.execute("DELETE FROM active_shifts WHERE user_id=?", (user_id,))
    conn.commit()

def get_active_shifts():
    c.execute("SELECT user_id, clock_in FROM active_shifts")
    return dict(c.fetchall())

def is_user_excluded(user_id: int):
    c.execute("SELECT 1 FROM excluded_users WHERE user_id=?", (user_id,))
    return c.fetchone() is not None

def add_excluded_user(user_id: int):
    c.execute("INSERT OR IGNORE INTO excluded_users (user_id) VALUES (?)", (user_id,))
    conn.commit()

def remove_excluded_user(user_id: int):
    c.execute("DELETE FROM excluded_users WHERE user_id=?", (user_id,))
    conn.commit()

def append_sheet_row(username, action, timestamp_str):
    try:
        sheet.append_row([username, action, timestamp_str])
    except Exception as e:
        print(f"Failed to append to Google Sheets: {e}")

# ========== Load data into memory ==========
active_shifts = get_active_shifts()
excluded_user_ids = []
c.execute("SELECT user_id FROM excluded_users")
excluded_user_ids = [row[0] for row in c.fetchall()]

# ========== Keep Alive Flask Server for Render ==========
app = Flask('')

@app.route('/')
def home():
    return "Bot is running."

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()

# ========== Bot Events & Commands ==========

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    auto_clock_out_check.start()

@bot.event
async def on_voice_state_update(member, before, after):
    user_id = member.id
    now = datetime.now(ph_tz)

    # Ignore excluded users
    if is_user_excluded(user_id):
        return

    # Auto clock-in: user joins a voice channel (after.channel is not None, before.channel is None or different)
    if after.channel and (before.channel != after.channel):
        # Check if already clocked in
        if str(user_id) in active_shifts:
            return  # Already clocked in, ignore

        # Record clock-in
        clock_in_str = now.strftime("%Y-%m-%d %H:%M:%S")
        active_shifts[str(user_id)] = clock_in_str
        add_active_shift(user_id, clock_in_str)
        append_sheet_row(member.name, "Clock In", clock_in_str)
        channel = member.guild.system_channel or (member.guild.text_channels[0] if member.guild.text_channels else None)
        if channel:
            await channel.send(f"{member.mention} has clocked in (joined voice channel).")

    # Auto clock-out: user leaves all voice channels (after.channel is None)
    if before.channel and (after.channel is None):
        if str(user_id) not in active_shifts:
            return  # Not clocked in

        clock_in_time = datetime.strptime(active_shifts[str(user_id)], "%Y-%m-%d %H:%M:%S")
        # Prevent double clock-out if very quick rejoin
        clock_out_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # Remove active shift
        del active_shifts[str(user_id)]
        remove_active_shift(user_id)
        append_sheet_row(member.name, "Clock Out", clock_out_str)
        channel = member.guild.system_channel or (member.guild.text_channels[0] if member.guild.text_channels else None)
        if channel:
            await channel.send(f"{member.mention} has clocked out (left voice channel).")

# ========== Auto clock out after 14 hours ==========

@tasks.loop(minutes=5)
async def auto_clock_out_check():
    now = datetime.now(ph_tz)
    expired = []

    for uid, clock_in_str in active_shifts.items():
        clock_in_time = datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S")
        delta = now - clock_in_time
        if delta > timedelta(hours=14):
            expired.append(uid)

    for uid in expired:
        user = bot.get_user(int(uid))
        if user:
            channel = None
            guilds = bot.guilds
            for guild in guilds:
                member = guild.get_member(int(uid))
                if member:
                    channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
                    break

            if channel:
                try:
                    await channel.send(f"{user.mention} was automatically clocked out after 14 hours.")
                except Exception as e:
                    print(f"Failed to send auto clock-out message: {e}")

        # Remove active shift and log clock out
        del active_shifts[uid]
        remove_active_shift(int(uid))
        clock_out_str = now.strftime("%Y-%m-%d %H:%M:%S")
        append_sheet_row(user.name if user else uid, "Clock Out (Auto)", clock_out_str)

# ========== Commands ==========

@bot.command()
@commands.has_permissions(administrator=True)
async def onduty(ctx):
    """Show list of users currently clocked in (on duty)."""
    if not active_shifts:
        await ctx.send("No users are currently on duty.")
        return

    msg = "**Currently on duty:**\n"
    for uid, clock_in_str in active_shifts.items():
        user = bot.get_user(int(uid))
        name = user.name if user else uid
        msg += f"- {name} (clocked in at {clock_in_str})\n"
    await ctx.send(msg)

@bot.command()
@commands.has_permissions(administrator=True)
async def removeduty(ctx, user: discord.User):
    """Remove user from active shifts (force clock out)."""
    uid = str(user.id)
    if uid in active_shifts:
        del active_shifts[uid]
        remove_active_shift(user.id)
        now = datetime.now(ph_tz).strftime("%Y-%m-%d %H:%M:%S")
        append_sheet_row(user.name, "Clock Out (Force)", now)
        await ctx.send(f"{user.name} has been force clocked out and removed from active shifts.")
    else:
        await ctx.send(f"{user.name} is not currently clocked in.")

@bot.command()
@commands.has_permissions(administrator=True)
async def exclude(ctx, user: discord.User):
    """Exclude user from auto clock-in/out tracking."""
    add_excluded_user(user.id)
    if user.id not in excluded_user_ids:
        excluded_user_ids.append(user.id)
    await ctx.send(f"{user.name} is now excluded from time tracking.")

@bot.command()
@commands.has_permissions(administrator=True)
async def include(ctx, user: discord.User):
    """Remove user from exclusion list."""
    remove_excluded_user(user.id)
    if user.id in excluded_user_ids:
        excluded_user_ids.remove(user.id)
    await ctx.send(f"{user.name} is now included in time tracking.")

# ========== Run bot ==========

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("DISCORD_TOKEN"))

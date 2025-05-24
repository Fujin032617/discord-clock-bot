import discord
from discord.ext import commands, tasks
import asyncio
import sqlite3
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

# === Google Sheets Setup ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gc = gspread.authorize(creds)
sheet = gc.open("Employee Time Log").sheet1  # Your Google Sheet name

# === Discord Bot Setup ===
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# === SQLite Setup ===
conn = sqlite3.connect("shifts.db")
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS active_shifts (
    user_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    clock_in_time TEXT
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS excluded_users (
    user_id INTEGER PRIMARY KEY
)
""")
conn.commit()

# === Helper Functions ===
async def clock_in(user_id, channel_id, user_name):
    now = datetime.utcnow()
    c.execute("SELECT * FROM active_shifts WHERE user_id = ?", (user_id,))
    if c.fetchone():
        return  # Already clocked in

    c.execute("INSERT INTO active_shifts (user_id, channel_id, clock_in_time) VALUES (?, ?, ?)",
              (user_id, channel_id, now.isoformat()))
    conn.commit()

    sheet.append_row([user_name, "Clock In", now.strftime("%Y-%m-%d %H:%M:%S")])

async def clock_out(user_id, user_name):
    c.execute("SELECT clock_in_time FROM active_shifts WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        return  # Not clocked in

    clock_in_time = datetime.fromisoformat(row[0])
    now = datetime.utcnow()

    c.execute("DELETE FROM active_shifts WHERE user_id = ?", (user_id,))
    conn.commit()

    sheet.append_row([user_name, "Clock Out", now.strftime("%Y-%m-%d %H:%M:%S")])

@tasks.loop(minutes=1)
async def check_shifts():
    now = datetime.utcnow()
    c.execute("SELECT user_id, clock_in_time FROM active_shifts")
    for user_id, clock_in in c.fetchall():
        clock_in_time = datetime.fromisoformat(clock_in)
        if now - clock_in_time >= timedelta(hours=14):
            guild = discord.utils.get(bot.guilds)
            member = guild.get_member(user_id)
            if member:
                await clock_out(user_id, member.display_name)
                print(f"Auto-clocked out {member.display_name} after 14 hours.")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    check_shifts.start()

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    c.execute("SELECT 1 FROM excluded_users WHERE user_id = ?", (member.id,))
    if c.fetchone():
        return

    if after.channel and not before.channel:
        await clock_in(member.id, after.channel.id, member.display_name)
    elif not after.channel and before.channel:
        await clock_out(member.id, member.display_name)

# === Commands ===
@bot.command()
async def exclude(ctx, member: discord.Member):
    c.execute("INSERT OR IGNORE INTO excluded_users (user_id) VALUES (?)", (member.id,))
    conn.commit()
    await ctx.send(f"Excluded {member.display_name} from tracking.")

@bot.command()
async def include(ctx, member: discord.Member):
    c.execute("DELETE FROM excluded_users WHERE user_id = ?", (member.id,))
    conn.commit()
    await ctx.send(f"Included {member.display_name} in tracking.")

@bot.command()
async def on_duty(ctx):
    c.execute("SELECT user_id, clock_in_time FROM active_shifts")
    rows = c.fetchall()
    if not rows:
        await ctx.send("No one is currently clocked in.")
        return

    msg = "**Currently On Duty:**\n"
    for user_id, clock_in in rows:
        member = ctx.guild.get_member(user_id)
        if member:
            time = datetime.fromisoformat(clock_in).strftime("%Y-%m-%d %H:%M:%S")
            msg += f"- {member.display_name} (since {time})\n"
    await ctx.send(msg)

@bot.command()
async def force_clockout(ctx, member: discord.Member):
    c.execute("SELECT 1 FROM active_shifts WHERE user_id = ?", (member.id,))
    if not c.fetchone():
        await ctx.send(f"{member.display_name} is not currently clocked in.")
        return

    await clock_out(member.id, member.display_name)
    await ctx.send(f"Force clocked out {member.display_name}.")

# === Run Bot ===
bot.run("YOUR_BOT_TOKEN")

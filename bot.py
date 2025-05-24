import os
import json
import sqlite3
from flask import Flask, make_response
from threading import Thread
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from pytz import timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Use Render's persistent disk directory or fallback to current directory
data_dir = os.getenv('RENDER_DATA_DIR', '.')  # On Render, this is writable
os.makedirs(data_dir, exist_ok=True)
db_path = os.path.join(data_dir, 'bot_data.db')

# Flask App Setup
app = Flask('')

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

@app.route('/')
def home():
    response = make_response("I'm alive!")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# ========== SQLite Setup ==========

def get_db_connection():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # Create tables if they don't exist
    c.execute('''
        CREATE TABLE IF NOT EXISTS excluded_users (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS active_shifts (
            user_id INTEGER PRIMARY KEY,
            date TEXT,
            timestamp TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS last_clockouts (
            user_id INTEGER PRIMARY KEY,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ========== Data Access Functions ==========

def load_excluded_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM excluded_users')
    rows = c.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def save_excluded_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO excluded_users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_excluded_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM excluded_users WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def load_active_shifts():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, date, timestamp FROM active_shifts')
    rows = c.fetchall()
    conn.close()
    return {str(row['user_id']): {'date': row['date'], 'timestamp': row['timestamp']} for row in rows}

def save_active_shift(user_id, date, timestamp):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO active_shifts (user_id, date, timestamp)
        VALUES (?, ?, ?)
    ''', (user_id, date, timestamp))
    conn.commit()
    conn.close()

def remove_active_shift(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM active_shifts WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def load_last_clockouts():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, timestamp FROM last_clockouts')
    rows = c.fetchall()
    conn.close()
    return {str(row['user_id']): row['timestamp'] for row in rows}

def save_last_clockout(user_id, timestamp):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO last_clockouts (user_id, timestamp)
        VALUES (?, ?)
    ''', (user_id, timestamp))
    conn.commit()
    conn.close()

# ========== Discord Setup ==========

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets Setup
scope = [
    "https://spreadsheets.google.com/feeds",
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open("Employee Time Log").sheet1

# Load Data from DB
excluded_user_ids = load_excluded_users()
active_shifts = load_active_shifts()
last_clockouts = load_last_clockouts()

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    cleanup_old_shifts.start()
    public_clockout_reminder.start()

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or member.id in excluded_user_ids:
        return
    if before.channel is not None or after.channel is None:
        return  # Only trigger on join

    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    user_id_str = str(member.id)

    # Check last clockout
    if user_id_str in last_clockouts:
        last_out_time = datetime.strptime(last_clockouts[user_id_str], "%Y-%m-%d %H:%M:%S")
        last_out_time = ph_tz.localize(last_out_time)
        if (now - last_out_time) < timedelta(minutes=15):
            print(f"{member.name} tried to Clock In too soon after Clock Out.")
            return

    # Check last clock in
    previous_entry = active_shifts.get(user_id_str)
    allow_clock_in = False
    if previous_entry:
        last_clock_in_time = datetime.strptime(previous_entry['timestamp'], "%Y-%m-%d %H:%M:%S")
        last_clock_in_time = ph_tz.localize(last_clock_in_time)
        if (now - last_clock_in_time) >= timedelta(hours=14):
            allow_clock_in = True
    else:
        allow_clock_in = True

    if allow_clock_in:
        sheet.append_row([member.name, 'Clock In', timestamp_str])
        active_shifts[user_id_str] = {
            'date': now.strftime("%Y-%m-%d"),
            'timestamp': timestamp_str
        }
        save_active_shift(member.id, now.strftime("%Y-%m-%d"), timestamp_str)
        print(f'{member.name} Clock In at {timestamp_str}')

@bot.command()
async def clockout(ctx):
    if ctx.author.id in excluded_user_ids:
        await ctx.send(f'{ctx.author.mention}, you are not eligible for time tracking.')
        return

    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, 'Clock Out', timestamp])
    await ctx.send(f'{ctx.author.mention} has clocked out at {timestamp}')
    print(f'{ctx.author.name} Clock Out at {timestamp}')

    user_id_str = str(ctx.author.id)
    if user_id_str in active_shifts:
        del active_shifts[user_id_str]
        remove_active_shift(ctx.author.id)
    last_clockouts[user_id_str] = timestamp
    save_last_clockout(ctx.author.id, timestamp)

@bot.command(name='exclude')
@commands.has_permissions(administrator=True)
async def exclude_user(ctx, member: discord.Member):
    global excluded_user_ids
    if member.id in excluded_user_ids:
        await ctx.send(f'{member.mention} is already in the exclusion list.')
    else:
        excluded_user_ids.append(member.id)
        save_excluded_user(member.id)
        await ctx.send(f'{member.mention} has been excluded from time tracking.')

@bot.command(name='unexclude')
@commands.has_permissions(administrator=True)
async def unexclude_user(ctx, member: discord.Member):
    global excluded_user_ids
    if member.id not in excluded_user_ids:
        await ctx.send(f'{member.mention} is not in the exclusion list.')
    else:
        excluded_user_ids.remove(member.id)
        remove_excluded_user(member.id)
        await ctx.send(f'{member.mention} has been re-included in time tracking.')

@bot.command(name='listexcluded')
async def list_excluded(ctx):
    if not excluded_user_ids:
        await ctx.send("No users are currently excluded.")
    else:
        excluded_names = []
        for uid in excluded_user_ids:
            user = bot.get_user(uid)
            excluded_names.append(f"{user.display_name if user else 'Unknown'} ({uid})")
        await ctx.send("Excluded users:\n" + "\n".join(excluded_names))

# Background task to clean up expired shift entries
@tasks.loop(minutes=30)
async def cleanup_old_shifts():
    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    expired = []
    for uid, entry in active_shifts.items():
        shift_time = ph_tz.localize(datetime.strptime(entry['timestamp'], "%Y-%m-%d %H:%M:%S"))
        if (now - shift_time) >= timedelta(hours=14):
            expired.append(uid)

    for uid in expired:
        del active_shifts[uid]
        remove_active_shift(int(uid))
    if expired:
        print(f"Cleaned up {len(expired)} expired shift entries.")

# Reminder to clock out every 8 hours in general channels
@tasks.loop(hours=8)
async def public_clockout_reminder():
    for guild in bot.guilds:
        general_channel = discord.utils.get(guild.text_channels, name='general')
        if general_channel:
            try:
                await general_channel.send("\u23f0 Friendly reminder: Don't forget to clock out after your shift. Anyone who doesn't clock out will be marked as absent! -HRJEL ")
            except Exception as e:
                print(f"Failed to send reminder in {guild.name}: {e}")
        else:
            print(f"No #general channel found in {guild.name}")

if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])

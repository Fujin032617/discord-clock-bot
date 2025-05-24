import os
import sqlite3
from flask import Flask, make_response
from threading import Thread
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from pytz import timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# Setup persistent directory
data_dir = os.getenv('RENDER_DATA_DIR', '.')
os.makedirs(data_dir, exist_ok=True)
db_path = os.path.join(data_dir, 'bot_data.db')

# Flask app to keep bot alive
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

# SQLite database helper functions
def get_db_connection():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS excluded_users (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_shifts (user_id INTEGER PRIMARY KEY, clock_in TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS last_clockouts (user_id INTEGER PRIMARY KEY, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

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
    c.execute('SELECT user_id, clock_in FROM active_shifts')
    rows = c.fetchall()
    conn.close()
    return {str(row['user_id']): row['clock_in'] for row in rows}

def save_active_shift(user_id, clock_in):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO active_shifts (user_id, clock_in) VALUES (?, ?)', (user_id, clock_in))
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
    c.execute('INSERT OR REPLACE INTO last_clockouts (user_id, timestamp) VALUES (?, ?)', (user_id, timestamp))
    conn.commit()
    conn.close()

# Discord Bot Setup
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets Setup
scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open("YOUR_GOOGLE_SHEET_NAME").sheet1

excluded_user_ids = load_excluded_users()
active_shifts = load_active_shifts()
last_clockouts = load_last_clockouts()

ph_tz = timezone('Asia/Manila')

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    auto_clockout_expired_shifts.start()

def can_clock_in(user_id):
    now = datetime.now(ph_tz)
    if user_id in excluded_user_ids:
        return False
    clock_in_str = active_shifts.get(str(user_id))
    if clock_in_str:
        clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))
        diff = now - clock_in_time
        if diff < timedelta(hours=14):
            return False
    return True

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    user_id = member.id
    if user_id in excluded_user_ids:
        return
    if after.channel is not None and before.channel != after.channel:
        if can_clock_in(user_id):
            now = datetime.now(ph_tz)
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([member.name, "Clock In", timestamp_str])
            active_shifts[str(user_id)] = timestamp_str
            save_active_shift(user_id, timestamp_str)
            print(f"{member.name} clocked in at {timestamp_str}")

@bot.command()
async def clockout(ctx):
    user_id = ctx.author.id
    if user_id in excluded_user_ids:
        await ctx.send(f"{ctx.author.mention}, you are excluded from time tracking.")
        return
    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, "Clock Out", timestamp_str])
    if str(user_id) in active_shifts:
        del active_shifts[str(user_id)]
        remove_active_shift(user_id)
    last_clockouts[str(user_id)] = timestamp_str
    save_last_clockout(user_id, timestamp_str)
    await ctx.send(f"{ctx.author.mention} clocked out at {timestamp_str}")

@tasks.loop(minutes=15)
async def auto_clockout_expired_shifts():
    now = datetime.now(ph_tz)
    expired = []
    for uid, clock_in_str in active_shifts.items():
        clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))
        if now - clock_in_time >= timedelta(hours=14):
            user = bot.get_user(int(uid))
            name = user.name if user else uid
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([name, "Clock Out", timestamp_str])
            last_clockouts[uid] = timestamp_str
            save_last_clockout(int(uid), timestamp_str)
            expired.append(uid)
    for uid in expired:
        del active_shifts[uid]
        remove_active_shift(int(uid))

@bot.command()
@commands.has_permissions(administrator=True)
async def exclude(ctx, member: discord.Member = None, *, username: str = None):
    if member is not None:
        user_id = member.id
        if user_id in excluded_user_ids:
            await ctx.send(f"{member.name} is already excluded.")
            return
        save_excluded_user(user_id)
        excluded_user_ids.append(user_id)
        if str(user_id) in active_shifts:
            del active_shifts[str(user_id)]
            remove_active_shift(user_id)
        await ctx.send(f"{member.name} has been excluded from time tracking.")
        return
    if username:
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'No user found with username "{username}". Please mention the user or provide exact username.')
            return
        user_id = found_members[0].id
        if user_id in excluded_user_ids:
            await ctx.send(f"{found_members[0].name} is already excluded.")
            return
        save_excluded_user(user_id)
        excluded_user_ids.append(user_id)
        if str(user_id) in active_shifts:
            del active_shifts[str(user_id)]
            remove_active_shift(user_id)
        await ctx.send(f"{found_members[0].name} has been excluded from time tracking.")
        return
    await ctx.send("Please mention a user or provide a username to exclude.")

@bot.command()
@commands.has_permissions(administrator=True)
async def include(ctx, member: discord.Member = None, *, username: str = None):
    if member is not None:
        user_id = member.id
        if user_id not in excluded_user_ids:
            await ctx.send(f"{member.name} is not excluded.")
            return
        remove_excluded_user(user_id)
        excluded_user_ids.remove(user_id)
        await ctx.send(f"{member.name} has been included back in time tracking.")
        return
    if username:
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'No user found with username "{username}". Please mention the user or provide exact username.')
            return
        user_id = found_members[0].id
        if user_id not in excluded_user_ids:
            await ctx.send(f"{found_members[0].name}
::contentReference[oaicite:0]{index=0}
 

import os
import json
from flask import Flask, make_response
from threading import Thread
import discord
from discord.ext import commands, tasks
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from pytz import timezone

EXCLUDED_USERS_FILE = 'excluded_users.json'
ACTIVE_SHIFTS_FILE = 'active_shifts.json'
LAST_CLOCKOUT_FILE = 'last_clockout.json'

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

# Load/save functions
def load_json_file(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default

def save_json_file(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)

def load_excluded_users():
    return load_json_file(EXCLUDED_USERS_FILE, [])

def save_excluded_users(user_ids):
    save_json_file(EXCLUDED_USERS_FILE, user_ids)

def load_active_shifts():
    return load_json_file(ACTIVE_SHIFTS_FILE, {})

def save_active_shifts(data):
    save_json_file(ACTIVE_SHIFTS_FILE, data)

def load_last_clockouts():
    return load_json_file(LAST_CLOCKOUT_FILE, {})

def save_last_clockouts(data):
    save_json_file(LAST_CLOCKOUT_FILE, data)

# Discord Setup
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

# Load Data
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
        save_active_shifts(active_shifts)
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
        save_active_shifts(active_shifts)
    last_clockouts[user_id_str] = timestamp
    save_last_clockouts(last_clockouts)

@bot.command(name='exclude')
@commands.has_permissions(administrator=True)
async def exclude_user(ctx, member: discord.Member):
    global excluded_user_ids
    if member.id in excluded_user_ids:
        await ctx.send(f'{member.mention} is already in the exclusion list.')
    else:
        excluded_user_ids.append(member.id)
        save_excluded_users(excluded_user_ids)
        await ctx.send(f'{member.mention} has been excluded from time tracking.')

@bot.command(name='unexclude')
@commands.has_permissions(administrator=True)
async def unexclude_user(ctx, member: discord.Member):
    global excluded_user_ids
    if member.id not in excluded_user_ids:
        await ctx.send(f'{member.mention} is not in the exclusion list.')
    else:
        excluded_user_ids.remove(member.id)
        save_excluded_users(excluded_user_ids)
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
    if expired:
        save_active_shifts(active_shifts)
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

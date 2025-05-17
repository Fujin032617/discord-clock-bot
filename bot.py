import os
import json
from flask import Flask, make_response, request
from threading import Thread
from functools import wraps
import time
import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from pytz import timezone

# ----- Keep Alive Server -----
app = Flask('')
request_count = {}
RATE_LIMIT = 10  # requests
RATE_TIME = 60   # seconds

def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        now = time.time()
        ip = request.remote_addr
        if ip in request_count:
            if now - request_count[ip]['time'] >= RATE_TIME:
                request_count[ip] = {'count': 1, 'time': now}
            elif request_count[ip]['count'] >= RATE_LIMIT:
                return 'Rate limit exceeded', 429
            else:
                request_count[ip]['count'] += 1
        else:
            request_count[ip] = {'count': 1, 'time': now}
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
@rate_limit
def home():
    response = make_response("I'm alive!")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ----- Discord Bot Setup -----
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets API scope
scope = [
    "https://spreadsheets.google.com/feeds", 
    'https://www.googleapis.com/auth/spreadsheets', 
    'https://www.googleapis.com/auth/drive'
]

# Load Google credentials from environment variable
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)

# Open your Google Sheet (replace with your actual sheet name)
sheet = client.open("Employee Time Log").sheet1

daily_clock_ins = {}

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    print('Ready to track voice channel activity!')

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    if before.channel != after.channel and after.channel is not None:
        ph_tz = timezone('Asia/Manila')
        current_date = datetime.now(ph_tz).strftime("%Y-%m-%d")

        if member.name not in daily_clock_ins or daily_clock_ins[member.name] != current_date:
            timestamp = datetime.now(ph_tz).strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([member.name, 'Clock In', timestamp])
            daily_clock_ins[member.name] = current_date
            print(f'{member.name} Clock In at {timestamp}')

@bot.command()
async def clockout(ctx):
    ph_tz = timezone('Asia/Manila')
    timestamp = datetime.now(ph_tz).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, 'Clock Out', timestamp])
    await ctx.send(f'{ctx.author.mention} has clocked out at {timestamp}')
    print(f'{ctx.author.name} Clock Out at {timestamp}')

# Start keep_alive server and bot
if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])

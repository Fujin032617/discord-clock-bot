import os
import json
from flask import Flask, make_response, request
from threading import Thread
import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from pytz import timezone

# ----- Keep Alive Server -----
app = Flask('')

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
creds_json = json.loads(os.environ['GOOGLE_CREDS'])  # Set in your environment
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)

# Open your Google Sheet (replace with your actual sheet name)
sheet = client.open("Employee Time Log").sheet1

daily_clock_ins = {}

# ----- Flask Route -----
@app.route('/')
def home():
    response = make_response("I'm alive!")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# ----- Discord Bot Events -----
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

# ----- Command to Clock Out -----
@bot.command()
async def clockout(ctx):
    ph_tz = timezone('Asia/Manila')
    timestamp = datetime.now(ph_tz).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, 'Clock Out', timestamp])
    await ctx.send(f'{ctx.author.mention} has clocked out at {timestamp}')
    print(f'{ctx.author.name} Clock Out at {timestamp}')

# Start the keep_alive server and bot
if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])  # Make sure DISCORD_TOKEN is set in your environment

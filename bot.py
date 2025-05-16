from keep_alive import keep_alive
keep_alive()
import discord
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from discord.ext import commands
from datetime import datetime
from pytz import timezone

# Discord client setup
intents = discord.Intents.default()
intents.members = True  # Enable tracking of member updates (joins and leaves)
intents.message_content = True  # Enable privileged intents for message content

bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets API setup
scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Open your Google Sheet
sheet = client.open("Employee Time Log").sheet1  # Replace with your sheet name

# Track daily clock-ins
daily_clock_ins = {}

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    print('Ready to track voice channel activity!')

@bot.event
async def on_voice_state_update(member, before, after):
    # Ensure the member is not a bot
    if member.bot:
        return

    # Only handle Clock In when joining voice channel
    if before.channel != after.channel and after.channel is not None:
        ph_tz = timezone('Asia/Manila')
        current_date = datetime.now(ph_tz).strftime("%Y-%m-%d")
        
        # Check if user already clocked in today
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

bot.run('MTM3Mjg5MTk2OTMzNjExOTM1OA.GrMIW7.Fb5adfqoggEyjF2x3y9OHughFBF330zBOHbM3g')

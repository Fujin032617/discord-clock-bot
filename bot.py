import os
import json
import discord
from flask import Flask
import threading
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- Flask Web Server Setup ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=10000)

def keep_alive():
    thread = threading.Thread(target=run)
    thread.start()

# --- Google Sheets Setup ---
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_json = json.loads(os.environ['GOOGLE_CREDS'])  # Loads creds from environment variable
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open("Employee Time Log").sheet1  # Make sure this name matches your spreadsheet

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
client_bot = discord.Client(intents=intents)

@client_bot.event
async def on_ready():
    print(f'Logged in as {client_bot.user}')

@client_bot.event
async def on_message(message):
    if message.author == client_bot.user:
        return

    if message.content.startswith('!log'):
        username = str(message.author)
        timestamp = str(message.created_at)
        sheet.append_row([username, timestamp])
        await message.channel.send(f"{username}, your log has been recorded at {timestamp}.")

# --- Start Everything ---
keep_alive()
client_bot.run(os.environ['MTM3Mjg5MTk2OTMzNjExOTM1OA.GrMIW7.Fb5adfqoggEyjF2x3y9OHughFBF330zBOHbM3g'])

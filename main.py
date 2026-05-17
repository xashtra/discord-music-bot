"""
Discord Self-Bot Music Player
A hidden music self-bot running under a user token using discord.py-self.
"""

import os
import sys
import shutil
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN or TOKEN == "your_bot_token_here":
    print("❌ DISCORD_TOKEN is missing!")
    sys.exit(1)

# FFmpeg check
FFMPEG_PATH = shutil.which("ffmpeg")
if FFMPEG_PATH:
    print(f"OK: FFmpeg found at: {FFMPEG_PATH}")
else:
    print("Warning: FFmpeg not found!")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger("bot")

# Bot setup - User tokens don't require intents in discord.py-self
bot = commands.Bot(
    command_prefix="?",
    help_command=None,
)

@bot.event
async def on_ready():
    print("=======================")
    print(f"Self-Bot online: {bot.user}")
    print(f"User ID: {bot.user.id}")
    print("=======================")

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument: `{error.param.name}`\nUse `?help` for info.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Cooldown. Try again in `{error.retry_after:.1f}s`.")
    else:
        log.error(f"Error in {ctx.command}: {error}", exc_info=error)
        await ctx.send("❌ An unexpected error occurred.")

async def main():
    async with bot:
        await bot.load_extension("cogs.music")
        log.info("Loaded cog: cogs.music")
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot shut down.")

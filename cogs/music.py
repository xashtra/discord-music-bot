"""
Music Cog for Self-Bot using discord.py-self.
User accounts cannot send embeds, so all outputs are plain text.
Protected commands for Admin ID.
"""

import asyncio
import functools
import logging
import time
import os
import re
import shutil
import aiohttp
from collections import defaultdict

import discord
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("music")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMINS_FILE = "admins.txt"
FFMPEG_PATH = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"

def load_admins() -> set[int]:
    admins = {OWNER_ID}
    # Load default admins from env var (comma-separated IDs)
    # This survives Railway container restarts unlike admins.txt
    default_admins = os.getenv("DEFAULT_ADMINS", "")
    for aid in default_admins.split(","):
        aid = aid.strip()
        if aid.isdigit():
            admins.add(int(aid))
    if os.path.exists(ADMINS_FILE):
        with open(ADMINS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    admins.add(int(line))
    return admins

def save_admins(admins: set[int]):
    with open(ADMINS_FILE, "w") as f:
        for admin_id in admins:
            f.write(f"{admin_id}\n")

def extract_id(target: str) -> int:
    match = re.search(r'\d+', target)
    if match:
        return int(match.group())
    raise ValueError("Invalid user ID or mention")

ADMIN_IDS = load_admins()

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "nocheckcertificate": True,
}

FFMPEG_OPTIONS = {
    "options": "-vn",
}

def format_duration(seconds: int | float | None) -> str:
    if not seconds or seconds <= 0:
        return "🔴 Live"
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def create_progress_bar(elapsed: float, total: float, length: int = 20) -> str:
    if total <= 0:
        return "▬" * length
    ratio = min(elapsed / total, 1.0)
    pos = int(ratio * length)
    bar = "▬" * pos + "🔘" + "▬" * (length - pos - 1)
    return bar

class GuildMusicState:
    def __init__(self):
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.volume: float = 0.5
        self.start_time: float = 0.0
        self.text_channel: discord.TextChannel | None = None

    def clear(self):
        self.queue.clear()
        self.current = None
        self.start_time = 0.0

def is_admin():
    async def predicate(ctx):
        if ctx.author.id not in ADMIN_IDS and ctx.author.id != ctx.bot.user.id:
            await ctx.send("❌ This command is restricted to bot admins.")
            return False
        return True
    return commands.check(predicate)

def is_owner():
    async def predicate(ctx):
        if ctx.author.id != OWNER_ID and ctx.author.id != ctx.bot.user.id:
            await ctx.send("❌ This command is restricted to the bot owner.")
            return False
        return True
    return commands.check(predicate)

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = defaultdict(GuildMusicState)
        
        # Securely write cookies.txt from environment variable at runtime if provided
        cookies_content = os.getenv("COOKIES_CONTENT")
        if cookies_content:
            try:
                # Replace literal escaped newlines if passed in raw string format
                formatted_cookies = cookies_content.replace("\\n", "\n").replace("\\r", "")
                with open("cookies.txt", "w", encoding="utf-8") as f:
                    f.write(formatted_cookies.strip() + "\n")
                log.info("Successfully created cookies.txt from COOKIES_CONTENT env var.")
            except Exception as e:
                log.error(f"Failed to write cookies.txt from env var: {e}")

        # Dynamic yt-dlp options based on cookies availability
        ytdl_opts = YTDL_OPTIONS.copy()
        if os.path.exists("cookies.txt"):
            ytdl_opts["cookiefile"] = "cookies.txt"
            log.info("Cookies file detected. yt-dlp initialized with cookie authentication.")
        else:
            log.warning("No cookies.txt found. Running yt-dlp in unauthenticated mode.")
            
        self.ytdl = yt_dlp.YoutubeDL(ytdl_opts)

    def get_state(self, guild: discord.Guild) -> GuildMusicState:
        return self.states[guild.id]

    async def clear_bot_messages(self, channel):
        if not channel:
            return
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id == self.bot.user.id:
                    await msg.delete()
                    await asyncio.sleep(1.2)
        except Exception as e:
            log.error(f"Error clearing messages: {e}")

    async def extract_info(self, query: str) -> dict | None:
        import urllib.parse
        # Strip Discord embed-suppression brackets and whitespace
        query = query.strip().strip("<>")

        # Handle Spotify URLs by scraping the track title to search on YouTube
        if "spotify.com/track/" in query:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(query, ssl=False, headers={'User-Agent': 'Mozilla/5.0'}) as r:
                        html = await r.text()
                        match = re.search(r'<title>(.*?)</title>', html)
                        if match:
                            # Clean up the title to create a perfect YouTube search query
                            title = match.group(1).replace(" | Spotify", "").replace("- song and lyrics by", "")
                            query = f"ytsearch:{title.strip()}"
                            log.info(f"Converted Spotify URL to search query: {query}")
            except Exception as e:
                log.error(f"Failed to parse Spotify URL: {e}")
                return None

        # DuckDuckGo fallback for YouTube search queries to bypass Datacenter IP blocks (HTTP 403/429)
        is_search = False
        search_term = query
        if query.startswith("ytsearch:"):
            is_search = True
            search_term = query[9:]
        elif not query.startswith("http://") and not query.startswith("https://"):
            is_search = True

        if is_search:
            log.info(f"Search query detected: '{search_term}'. Running DuckDuckGo lookup to bypass datacenter blocks...")
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                ddg_url = f"https://html.duckduckgo.com/html/?q=site:youtube.com+{urllib.parse.quote(search_term)}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(ddg_url, headers=headers, ssl=False) as r:
                        if r.status == 200:
                            html = await r.text()
                            urls = re.findall(r'youtube\.com/watch\?v=([\w-]+)', html)
                            if urls:
                                query = f"https://www.youtube.com/watch?v={urls[0]}"
                                log.info(f"DuckDuckGo search successful. Resolved URL: {query}")
                            else:
                                log.warning(f"DuckDuckGo search returned no video URLs for query: '{search_term}'")
                        else:
                            log.warning(f"DuckDuckGo search failed with HTTP status: {r.status}")
            except Exception as e:
                log.error(f"DuckDuckGo search lookup exception: {e}")

        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None,
                functools.partial(self.ytdl.extract_info, query, download=False),
            )
        except Exception as e:
            err_str = str(e)
            log.error(f"yt-dlp extraction error: {err_str}")
            
            # If YouTube blocks us (Sign in to confirm you're not a bot or HTTP 403/429), fall back to SoundCloud!
            if any(indicator in err_str for indicator in ["confirm you're not a bot", "403", "429", "Sign in", "Forbidden", "Too Many Requests"]):
                log.info("YouTube blocked this request. Initiating automatic seamless fallback to SoundCloud...")
                
                sc_query = query
                # Convert YouTube URL back to search term if search_term exists in local variables
                if query.startswith("http"):
                    if 'search_term' in locals() and search_term:
                        sc_query = f"scsearch:{search_term}"
                    else:
                        sc_query = f"scsearch:{query}"
                else:
                    if not query.startswith("scsearch:"):
                        sc_query = f"scsearch:{query}"
                
                log.info(f"SoundCloud search query: '{sc_query}'")
                try:
                    data = await loop.run_in_executor(
                        None,
                        functools.partial(self.ytdl.extract_info, sc_query, download=False),
                    )
                    if data:
                        log.info("SoundCloud fallback successful! Streaming track.")
                    else:
                        return None
                except Exception as sc_err:
                    log.error(f"SoundCloud fallback search failed: {sc_err}")
                    return None
            else:
                return None

        if not data:
            return None

        if "entries" in data:
            entries = list(data["entries"])
            if not entries:
                return None
            data = entries[0]

        return {
            "title": data.get("title", "Unknown"),
            "url": data.get("webpage_url", data.get("url", "")),
            "stream_url": data.get("url", ""),
            "duration": data.get("duration", 0),
            "requester": "Unknown",
        }

    async def play_next(self, guild: discord.Guild):
        state = self.get_state(guild)

        if not state.queue:
            state.current = None
            if state.text_channel:
                await state.text_channel.send("🎉 Queue finished! Add more songs with `?play`.")
            return

        if not state.voice_client or not state.voice_client.is_connected():
            state.clear()
            return

        song = state.queue.pop(0)

        # Lazy Resolution for playlists
        if isinstance(song, dict) and "stream_url" not in song:
            query = song.get("url")
            requester = song.get("requester", "Unknown")
            
            extracted = await self.extract_info(query)
            if not extracted:
                if state.text_channel:
                    await state.text_channel.send(f"❌ Failed to resolve track `{query}`, skipping...")
                await self.play_next(guild)
                return
            
            song = extracted
            song["requester"] = requester

        state.current = song
        state.start_time = time.time()

        try:
            
            ffmpeg_kwargs = FFMPEG_OPTIONS.copy()
            # YouTube aggressively cuts streams midway to save bandwidth.
            # We MUST use reconnect options for standard HTTP streams to resume them instantly.
            # However, we must NOT use them for HLS (.m3u8) streams or ffmpeg crashes.
            url = song["stream_url"]
            if url.startswith("http") and ".m3u8" not in url:
                ffmpeg_kwargs["before_options"] = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                
            source = discord.FFmpegPCMAudio(url, executable=FFMPEG_PATH, **ffmpeg_kwargs)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)
        except Exception as e:
            log.error(f"FFmpeg error: {e}")
            if state.text_channel:
                await state.text_channel.send("❌ Failed to create audio source.")
            await self.play_next(guild)
            return

        def after_playback(error):
            if error:
                log.error(f"Playback error: {error}")
            future = asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)
            try:
                future.result(timeout=10)
            except Exception as e:
                log.error(f"Error scheduling next song: {e}")

        state.voice_client.play(source, after=after_playback)

        if state.text_channel:
            msg = f"🎶 **Now Playing:** {song['title']} [{format_duration(song['duration'])}]\n" \
                  f"🔗 {song['url']}"
            await state.text_channel.send(msg)

    # ── PUBLIC COMMANDS ──

    @commands.command(name="play", aliases=["p"])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def cmd_play(self, ctx: commands.Context, *, query: str):
        """Play a song. (Public)"""
        # A user token doesn't have an implicit voice.author so we find them via guild cache
        user_voice = ctx.author.voice
        if not user_voice or not user_voice.channel:
            return await ctx.send("❌ You must be in a voice channel to use this command!")

        voice_channel = user_voice.channel
        state = self.get_state(ctx.guild)
        state.text_channel = ctx.channel

        if not state.voice_client or not state.voice_client.is_connected():
            try:
                state.voice_client = await voice_channel.connect(self_deaf=True, timeout=10.0)
            except asyncio.TimeoutError:
                return await ctx.send("❌ Timed out connecting to the voice channel.")
            except discord.ClientException:
                if ctx.guild.voice_client:
                    await ctx.guild.voice_client.move_to(voice_channel)
                    state.voice_client = ctx.guild.voice_client

        searching_msg = await ctx.send(f"🔍 Searching for: `{query}`...")
        
        # Explicit Spotify Playlist Handling
        if "spotify.com/playlist/" in query:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(query, ssl=False, headers={'User-Agent': 'Mozilla/5.0'}) as r:
                        html = await r.text()
                        p_match = re.search(r'<meta property="og:title" content="(.*?)"', html)
                        p_title = p_match.group(1) if p_match else "Spotify Playlist"
                        
                        tracks = re.findall(r'<meta name="music:song" content="https://open.spotify.com/track/(.*?)"', html)
                        if not tracks:
                            return await searching_msg.edit(content="❌ No tracks found in this Spotify playlist.")
                        
                        await searching_msg.edit(content=f"🎶 Queued **{len(tracks)}** tracks from **{p_title}**!")
                        
                        for track_id in tracks:
                            state.queue.append({
                                "url": f"https://open.spotify.com/track/{track_id}",
                                "title": f"Spotify Track (Resolving...)",
                                "requester": ctx.author.name,
                                "duration": 0
                            })
                        
                        if not (state.voice_client.is_playing() or state.voice_client.is_paused()):
                            await self.play_next(ctx.guild)
                        return
            except Exception as e:
                log.error(f"Failed to parse Spotify Playlist: {e}")
                return await searching_msg.edit(content="❌ Failed to parse Spotify Playlist.")

        song = await self.extract_info(query)
        
        if not song:
            return await searching_msg.edit(content="❌ No results found or the video is unavailable.")

        song["requester"] = ctx.author.name

        if state.voice_client.is_playing() or state.voice_client.is_paused():
            state.queue.append(song)
            position = len(state.queue)
            await searching_msg.edit(content=f"➕ **Added to Queue [#{position}]:** {song['title']}\n🔗 {song['url']}")
        else:
            state.queue.append(song)
            await searching_msg.delete()
            await self.play_next(ctx.guild)

    @commands.command(name="join", aliases=["j"])
    async def cmd_join(self, ctx: commands.Context):
        """Join the user's voice channel. (Public)"""
        user_voice = ctx.author.voice
        if not user_voice or not user_voice.channel:
            return await ctx.send("❌ You must be in a voice channel to use this command!")
            
        voice_channel = user_voice.channel
        state = self.get_state(ctx.guild)
        state.text_channel = ctx.channel
        
        if not state.voice_client or not state.voice_client.is_connected():
            try:
                state.voice_client = await voice_channel.connect(self_deaf=True, timeout=10.0)
                await ctx.send(f"🔊 Joined **{voice_channel.name}**!")
            except asyncio.TimeoutError:
                await ctx.send("❌ Timed out connecting to the voice channel.")
            except discord.ClientException:
                if ctx.guild.voice_client:
                    await ctx.guild.voice_client.move_to(voice_channel)
                    state.voice_client = ctx.guild.voice_client
                    await ctx.send(f"🔊 Moved to **{voice_channel.name}**!")
        else:
            if state.voice_client.channel.id != voice_channel.id:
                await state.voice_client.move_to(voice_channel)
                await ctx.send(f"🔊 Moved to **{voice_channel.name}**!")
            else:
                await ctx.send("⚠️ Already connected to this voice channel.")

    @commands.command(name="pull")
    @is_admin()
    async def cmd_pull(self, ctx: commands.Context):
        """Force-move the bot to the admin's voice channel. (Admin)"""
        user_voice = ctx.author.voice
        if not user_voice or not user_voice.channel:
            return await ctx.send("❌ You must be in a voice channel to use `?pull`!")

        voice_channel = user_voice.channel
        state = self.get_state(ctx.guild)
        state.text_channel = ctx.channel

        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id == voice_channel.id:
                return await ctx.send("⚠️ Already in your voice channel!")
            # Stop current playback before moving
            if state.voice_client.is_playing() or state.voice_client.is_paused():
                state.voice_client.stop()
            await state.voice_client.move_to(voice_channel)
            await ctx.send(f"🔊 Force-moved to **{voice_channel.name}**!")
        else:
            try:
                state.voice_client = await voice_channel.connect(self_deaf=True, timeout=10.0)
                await ctx.send(f"🔊 Pulled into **{voice_channel.name}**!")
            except asyncio.TimeoutError:
                await ctx.send("❌ Timed out connecting to the voice channel.")
            except discord.ClientException as e:
                await ctx.send(f"❌ Failed to join: {e}")

    @commands.command(name="queue", aliases=["q"])
    async def cmd_queue(self, ctx: commands.Context):
        """Display the current song queue. (Public)"""
        state = self.get_state(ctx.guild)
        if not state.current and not state.queue:
            return await ctx.send("📜 The queue is empty. Use `?play` to add songs!")

        lines = ["**📜 Music Queue**"]
        if state.current:
            elapsed = time.time() - state.start_time
            lines.append(f"**Now Playing:** {state.current['title']} [{format_duration(elapsed)} / {format_duration(state.current.get('duration', 0))}]")
            lines.append(f"🔗 <{state.current['url']}>")
        if state.queue:
            lines.append("\n**Up Next:**")
            for i, song in enumerate(state.queue[:10], 1):
                lines.append(f"`{i}.` {song['title']} — {format_duration(song['duration'])} (Req: {song.get('requester')})")
            if len(state.queue) > 10:
                lines.append(f"\n...and **{len(state.queue) - 10}** more song(s)")
        
        await ctx.send("\n".join(lines))

    @commands.command(name="np", aliases=["nowplaying"])
    async def cmd_now_playing(self, ctx: commands.Context):
        """Show currently playing track. (Public)"""
        state = self.get_state(ctx.guild)
        if not state.current:
            return await ctx.send("⚠️ Nothing is playing right now.")

        song = state.current
        elapsed = time.time() - state.start_time
        duration = song.get("duration", 0)
        bar = create_progress_bar(elapsed, duration)

        msg = (
            f"🎵 **Now Playing:** {song['title']}\n"
            f"🔗 <{song['url']}>\n"
            f"`{format_duration(elapsed)}` {bar} `{format_duration(duration)}`\n"
            f"🔊 Volume: {int(state.volume * 100)}% | 🎧 Req: {song.get('requester')}"
        )
        await ctx.send(msg)

    @commands.command(name="clear")
    async def cmd_clear(self, ctx: commands.Context):
        """Clear all bot messages from the current channel. (Public)"""
        asyncio.create_task(self.clear_bot_messages(ctx.channel))

    async def _download_and_send(self, ctx: commands.Context, query: str, is_video: bool):
        state = self.get_state(ctx.guild)
        
        if not query:
            if not state.current:
                return await ctx.send("⚠️ Nothing is playing and no link was provided.")
            query = state.current.get("url", state.current.get("query"))
            if not query:
                return await ctx.send("⚠️ Cannot determine the URL of the current song.")
        else:
            query = query.strip().strip("<>")
            
        if "spotify.com" in query:
            return await ctx.send("⚠️ Direct Spotify downloads aren't supported. Please provide a YouTube link or search query.")

        msg = await ctx.send(f"⏳ Downloading **{'video' if is_video else 'audio'}**... This might take a minute.")
        
        loop = asyncio.get_running_loop()
        dl_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(dl_dir, exist_ok=True)
        

        
        opts = {
            "outtmpl": os.path.join(dl_dir, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "default_search": "ytsearch",
        }
        
        if is_video:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            opts["merge_output_format"] = "mp4"
            opts["ffmpeg_location"] = FFMPEG_PATH
        else:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            opts["ffmpeg_location"] = FFMPEG_PATH

        if os.path.exists("cookies.txt"):
            opts["cookiefile"] = "cookies.txt"

        dl_ytdl = yt_dlp.YoutubeDL(opts)
        
        try:
            info = await loop.run_in_executor(
                None,
                functools.partial(dl_ytdl.extract_info, query, download=True)
            )
            
            if not info:
                return await msg.edit(content="❌ Failed to extract video information.")
                
            if "entries" in info:
                entries = list(info["entries"])
                if not entries:
                    return await msg.edit(content="❌ No results found.")
                info = entries[0]
                
            filename = dl_ytdl.prepare_filename(info)
            if not is_video:
                base, _ = os.path.splitext(filename)
                filename = f"{base}.mp3"
                
            if not os.path.exists(filename):
                return await msg.edit(content="❌ Download finished but file not found.")
                
            filesize_mb = os.path.getsize(filename) / (1024 * 1024)
            await msg.edit(content=f"✅ Download complete ({filesize_mb:.1f} MB)! Uploading to Discord...")
            
            try:
                await ctx.send(file=discord.File(filename))
                await msg.delete()
                os.remove(filename)
            except discord.HTTPException:
                await msg.edit(content=f"❌ The file is too large to send over Discord ({filesize_mb:.1f} MB).\nIt has been saved locally on your PC at:\n`{filename}`")
                
        except Exception as e:
            log.error(f"Download error: {e}")
            await msg.edit(content=f"❌ Failed to download: {str(e)[:100]}")

    @commands.command(name="daudio")
    async def cmd_daudio(self, ctx: commands.Context, *, query: str = None):
        """Download audio of current song or query. (Public)"""
        await self._download_and_send(ctx, query, is_video=False)

    @commands.command(name="dvideo")
    async def cmd_dvideo(self, ctx: commands.Context, *, query: str = None):
        """Download video of current song or query. (Public)"""
        await self._download_and_send(ctx, query, is_video=True)

    @commands.command(name="help", aliases=["h"])
    async def cmd_help(self, ctx: commands.Context):
        """List all commands. (Public)"""
        msg = (
            "**🎵 Music Self-Bot Commands**\n"
            "**Public Actions:**\n"
            "`?play <url|search>` — Play or queue a song (Aliases: `?p`)\n"
            "`?join` — Join voice channel (Aliases: `?j`)\n"
            "`?queue` — Show the queue (Aliases: `?q`)\n"
            "`?np` — Show progress bar for current track\n"
            "`?daudio [url/search]` — Download audio (MP3)\n"
            "`?dvideo [url/search]` — Download video (MP4)\n"
            "`?clear` — Delete all bot messages in this chat\n"
            "`?help` — Show this message (Aliases: `?h`)\n\n"
            "**Admin-Only Actions:**\n"
            "`?pause` / `?resume` — Playback control (Aliases: `?r`)\n"
            "`?skip` — Skip track (Aliases: `?s`)\n"
            "`?stop` — Stop/clear queue\n"
            "`?pull` — Force-join admin's VC\n"
            "`?leave` — Disconnect bot (Aliases: `?dc`)\n"
            "`?volume <0-100>` — Set volume (Aliases: `?vol`, `?v`)\n\n"
            "**Owner-Only Actions:**\n"
            "`?admin <@user|id>` — Grant admin (Aliases: `?addadmin`)\n"
            "`?removeadmin <@user|id>` — Remove admin"
        )
        await ctx.send(msg)

    # ── ADMIN ONLY COMMANDS ──

    @commands.command(name="pause")
    @is_admin()
    async def cmd_pause(self, ctx: commands.Context):
        state = self.get_state(ctx.guild)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            await ctx.send("⏸️ Paused!")

    @commands.command(name="resume", aliases=["r"])
    @is_admin()
    async def cmd_resume(self, ctx: commands.Context):
        state = self.get_state(ctx.guild)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            await ctx.send("▶️ Resumed!")

    @commands.command(name="skip", aliases=["s"])
    @is_admin()
    async def cmd_skip(self, ctx: commands.Context):
        state = self.get_state(ctx.guild)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
            await ctx.send("⏭️ Skipped!")
        else:
            await ctx.send("⚠️ Nothing to skip.")

    @commands.command(name="stop")
    @is_admin()
    async def cmd_stop(self, ctx: commands.Context):
        state = self.get_state(ctx.guild)
        state.queue.clear()
        state.current = None
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
        await ctx.send("⏹️ Stopped playback and cleared the queue.")

    @commands.command(name="leave", aliases=["dc", "disconnect"])
    @is_admin()
    async def cmd_leave(self, ctx: commands.Context):
        state = self.get_state(ctx.guild)
        channel = state.text_channel or ctx.channel
        state.clear()
        if state.voice_client and state.voice_client.is_connected():
            await state.voice_client.disconnect(force=True)
            state.voice_client = None
        asyncio.create_task(self.clear_bot_messages(channel))

    @commands.command(name="volume", aliases=["vol", "v"])
    @is_admin()
    async def cmd_volume(self, ctx: commands.Context, vol: int = None):
        state = self.get_state(ctx.guild)
        if vol is None:
            return await ctx.send(f"🔊 Current volume: **{int(state.volume * 100)}%**")
        if vol < 0 or vol > 100:
            return await ctx.send("⚠️ Volume must be between **0** and **100**.")
        state.volume = vol / 100.0
        if state.voice_client and state.voice_client.source and isinstance(state.voice_client.source, discord.PCMVolumeTransformer):
            state.voice_client.source.volume = state.volume
        await ctx.send(f"🔊 Volume set to **{vol}%**")

    # ── OWNER ONLY COMMANDS ──

    @commands.command(name="admin", aliases=["addadmin"])
    @is_owner()
    async def cmd_add_admin(self, ctx: commands.Context, *, target: str):
        try:
            user_id = extract_id(target)
        except ValueError:
            return await ctx.send("❌ Please provide a valid user ID or mention.")
        if user_id in ADMIN_IDS:
            return await ctx.send("⚠️ This user is already an admin.")
        ADMIN_IDS.add(user_id)
        save_admins(ADMIN_IDS)
        await ctx.send(f"✅ Added user `{user_id}` as an admin!")

    @commands.command(name="removeadmin")
    @is_owner()
    async def cmd_remove_admin(self, ctx: commands.Context, *, target: str):
        try:
            user_id = extract_id(target)
        except ValueError:
            return await ctx.send("❌ Please provide a valid user ID or mention.")
        if user_id == OWNER_ID:
            return await ctx.send("❌ You cannot remove the owner from admins.")
        if user_id not in ADMIN_IDS:
            return await ctx.send("⚠️ This user is not an admin.")
        ADMIN_IDS.remove(user_id)
        save_admins(ADMIN_IDS)
        await ctx.send(f"✅ Removed user `{user_id}` from admins.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))

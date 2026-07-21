import asyncio
import os
import re
import sys
import urllib.parse
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CONSOLE_CHANNEL_ID = int(os.environ["CONSOLE_CHANNEL_ID"])
CHAT_CHANNEL_ID = int(os.environ["CHAT_CHANNEL_ID"])
ALLOWLIST_PATH = Path(os.environ["ALLOWLIST_PATH"])
SERVER_BIN = os.environ["SERVER_BIN"]
DATA_PATH = os.environ["DATA_PATH"]
MODS_PATH = Path(os.environ.get("MODS_PATH", str(Path(DATA_PATH) / "mods"))).resolve()
CHAT_REGEX = os.environ.get("CHAT_REGEX", "Server Chat")
CHAT_PATTERN = re.compile(CHAT_REGEX) if CHAT_REGEX else None

FLUSH_INTERVAL = 0.5
MAX_MSG_LEN = 2000
MAX_BUFFER_LINES = 200
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
DOWNLOAD_CHUNK = 64 * 1024


def read_allowlist():
    if not ALLOWLIST_PATH.exists():
        return set()
    ids = set()
    for line in ALLOWLIST_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ids.add(int(line))
        except ValueError:
            pass
    return ids


def safe_mod_path(filename):
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return None
    candidate = (MODS_PATH / filename).resolve()
    try:
        candidate.relative_to(MODS_PATH)
    except ValueError:
        return None
    return candidate


class ServerProcess:
    def __init__(self):
        self.proc = None
        self._pump_task = None

    @property
    def running(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        if self.running:
            return False, "already running"
        self.proc = await asyncio.create_subprocess_exec(
            SERVER_BIN, "--dataPath", DATA_PATH,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
        )
        self._pump_task = asyncio.create_task(self._pump_stdout())
        return True, "started"

    async def stop(self):
        if not self.running:
            self.proc = None
            return False, "not running"
        try:
            self.proc.stdin.write(b"/stop\n")
            await self.proc.stdin.drain()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        await self._drain_pump()
        self.proc = None
        return True, "stopped"

    async def send_command(self, cmd):
        if not self.running:
            return False, "not running"
        try:
            self.proc.stdin.write(cmd.encode() + b"\n")
            await self.proc.stdin.drain()
            return True, "sent"
        except Exception as e:
            return False, str(e)

    async def _pump_stdout(self):
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\n")
            await console_buffer.push(text)
            if CHAT_PATTERN and CHAT_PATTERN.search(text):
                idx = text.find("|")
                chat_text = text[idx + 1:].lstrip() if idx >= 0 else text
                if chat_text:
                    await chat_buffer.push(chat_text)

    async def _drain_pump(self):
        if self._pump_task is None:
            return
        try:
            await asyncio.wait_for(self._pump_task, timeout=5)
        except asyncio.TimeoutError:
            self._pump_task.cancel()


class Buffer:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.lines = []
        self.dropped = 0
        self._lock = asyncio.Lock()

    async def push(self, line):
        async with self._lock:
            if len(self.lines) >= MAX_BUFFER_LINES:
                self.dropped += 1
                return
            self.lines.append(line)

    async def drain(self):
        async with self._lock:
            if not self.lines:
                return None
            text = "\n".join(self.lines)
            dropped = self.dropped
            self.lines = []
            self.dropped = 0
        if dropped:
            text = f"[dropped {dropped} lines]\n" + text
        return text


console_buffer = Buffer(CONSOLE_CHANNEL_ID)
chat_buffer = Buffer(CHAT_CHANNEL_ID)
server = ServerProcess()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def flusher():
    await client.wait_until_ready()
    console_channel = client.get_channel(CONSOLE_CHANNEL_ID)
    chat_channel = client.get_channel(CHAT_CHANNEL_ID)
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        for buf, channel in [
            (console_buffer, console_channel),
            (chat_buffer, chat_channel),
        ]:
            text = await buf.drain()
            if text is None:
                continue
            if len(text) > MAX_MSG_LEN:
                text = text[-(MAX_MSG_LEN - 3):] + "..."
            try:
                await channel.send(f"```\n{text}\n```")
            except Exception:
                pass


@client.event
async def on_ready():
    print(f"bot ready as {client.user}", flush=True)
    client.loop.create_task(flusher())


@client.event
async def on_message(message):
    if message.author.bot:
        return
    if message.content.startswith("!"):
        await handle_bot_command(message)
        return
    if message.channel.id != CONSOLE_CHANNEL_ID:
        return
    if message.author.id not in read_allowlist():
        return
    ok, info = await server.send_command(message.content)
    if not ok:
        await message.channel.send(f"command failed: {info}")


async def handle_bot_command(message):
    parts = message.content.split(maxsplit=2)
    cmd = parts[0].lower()
    is_op = message.author.id in read_allowlist()

    if cmd == "!mod":
        await handle_mod_command(message, parts[1:] if len(parts) > 1 else [], is_op)
        return

    if cmd == "!start":
        if not is_op:
            return
        ok, info = await server.start()
        await message.channel.send(info)
    elif cmd == "!stop":
        if not is_op:
            return
        ok, info = await server.stop()
        await message.channel.send(info)
    elif cmd == "!status":
        await message.channel.send("running" if server.running else "stopped")
    else:
        await message.channel.send(
            "commands: !start, !stop, !status, !mod {add|remove|list|get} [filename]\n"
            "in #console, anything else (from auth'd users) is forwarded to the server"
        )


async def handle_mod_command(message, args, is_op):
    if not args:
        await message.channel.send("usage: !mod {add|remove|list|get} [filename]")
        return
    sub = args[0].lower()
    rest = args[1] if len(args) > 1 else ""

    if sub == "add":
        if not is_op:
            return
        rest = rest.strip()
        if rest.startswith("<") and rest.endswith(">"):
            rest = rest[1:-1]
        if rest.startswith(("http://", "https://")):
            await mod_add_url(message, rest)
        elif message.attachments:
            await mod_add_attachments(message)
        else:
            await message.channel.send(
                "attach a .zip file, or provide a URL: !mod add <url>"
            )
    elif sub == "remove":
        if not is_op:
            return
        if not rest:
            await message.channel.send("usage: !mod remove <filename>")
            return
        await mod_remove(message, rest)
    elif sub == "list":
        await mod_list(message)
    elif sub == "get":
        if not rest:
            await message.channel.send("usage: !mod get <filename>")
            return
        await mod_get(message, rest)
    else:
        await message.channel.send("usage: !mod {add|remove|list|get} [filename]")


async def mod_add(message):
    await mod_add_attachments(message)


async def mod_add_attachments(message):
    MODS_PATH.mkdir(parents=True, exist_ok=True)
    results = []
    for att in message.attachments:
        if not att.filename.lower().endswith(".zip"):
            results.append(f"{att.filename}: skipped (not a .zip)")
            continue
        target = safe_mod_path(att.filename)
        if target is None:
            results.append(f"{att.filename}: invalid filename")
            continue
        await att.save(target)
        results.append(f"{att.filename}: saved ({att.size} bytes)")
    await message.channel.send("\n".join(results))


async def mod_add_url(message, url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        await message.channel.send("only http(s) URLs are supported")
        return
    filename = os.path.basename(parsed.path)
    if not filename or not filename.lower().endswith(".zip"):
        await message.channel.send("couldn't determine a .zip filename from the URL")
        return
    target = safe_mod_path(filename)
    if target is None:
        await message.channel.send(f"invalid filename: {filename}")
        return
    MODS_PATH.mkdir(parents=True, exist_ok=True)
    if target.exists():
        await message.channel.send(f"{filename} already exists, not overwriting")
        return
    tmp = target.with_suffix(target.suffix + ".part")
    status = await message.channel.send(f"downloading {filename}...")
    total = 0
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await status.edit(content=f"download failed: HTTP {resp.status}")
                    return
                with open(tmp, "wb") as f:
                    async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            f.close()
                            tmp.unlink(missing_ok=True)
                            await status.edit(content=f"download exceeded {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB limit")
                            return
                        f.write(chunk)
        tmp.rename(target)
        await status.edit(content=f"{target.name}: downloaded ({total} bytes)")
    except Exception as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        await status.edit(content=f"download failed: {e}")


async def mod_remove(message, filename):
    target = safe_mod_path(filename)
    if target is None or not target.is_file():
        await message.channel.send(f"no mod named {filename}")
        return
    target.unlink()
    await message.channel.send(f"removed {target.name}")


async def mod_list(message):
    if not MODS_PATH.exists():
        await message.channel.send("no mods")
        return
    files = sorted(f.name for f in MODS_PATH.iterdir() if f.is_file())
    if not files:
        await message.channel.send("no mods")
        return
    text = "\n".join(files)
    if len(text) > MAX_MSG_LEN - 10:
        text = text[: MAX_MSG_LEN - 13] + "..."
    await message.channel.send(f"```\n{text}\n```")


async def mod_get(message, filename):
    target = safe_mod_path(filename)
    if target is None or not target.is_file():
        await message.channel.send(f"no mod named {filename}")
        return
    size = target.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        await message.channel.send(
            f"{target.name} is {size // (1024 * 1024)}MB, too big (limit 25MB)"
        )
        return
    await message.channel.send(file=discord.File(target))


def main():
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

import asyncio
import os
import re
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CONSOLE_CHANNEL_ID = int(os.environ["CONSOLE_CHANNEL_ID"])
CHAT_CHANNEL_ID = int(os.environ["CHAT_CHANNEL_ID"])
ALLOWLIST_PATH = Path(os.environ["ALLOWLIST_PATH"])
SERVER_BIN = os.environ["SERVER_BIN"]
DATA_PATH = os.environ["DATA_PATH"]
CHAT_REGEX = os.environ.get("CHAT_REGEX", "")
CHAT_PATTERN = re.compile(CHAT_REGEX) if CHAT_REGEX else None

FLUSH_INTERVAL = 0.5
MAX_MSG_LEN = 2000
MAX_BUFFER_LINES = 200


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
                await chat_buffer.push(text)

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
    if message.channel.id != CONSOLE_CHANNEL_ID:
        return
    if message.content.startswith("!"):
        await handle_bot_command(message)
        return
    if message.author.id not in read_allowlist():
        return
    ok, info = await server.send_command(message.content)
    if not ok:
        await message.channel.send(f"command failed: {info}")


async def handle_bot_command(message):
    if message.author.id not in read_allowlist():
        return
    parts = message.content.split(maxsplit=1)
    cmd = parts[0].lower()
    if cmd == "!start":
        ok, info = await server.start()
        await message.channel.send(info)
    elif cmd == "!stop":
        ok, info = await server.stop()
        await message.channel.send(info)
    elif cmd == "!status":
        await message.channel.send("running" if server.running else "stopped")
    else:
        await message.channel.send(
            "commands: !start, !stop, !status — anything else is forwarded to the server console"
        )


def main():
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

# (c) Anilyka Barry, 2022

from __future__ import annotations

from typing import Generator, Callable

import datetime
import random
import string
import base64
import json
import time
import sys
import re
import os

from twitchio.ext.commands import Cooldown as TCooldown, Bucket as TBucket, Bot as TBot
from twitchio.ext.routines import routine, Routine
from twitchio.ext.eventsub import EventSubClient, StreamOnlineData, StreamOfflineData
from twitchio.channel import Channel
from twitchio.chatter import Chatter
from twitchio.errors import HTTPException
from twitchio.models import Stream

import discord
from discord.ext.commands import Cooldown as DCooldown, BucketType as DBucket, Bot as DBot, command as _dcommand

from aiohttp_jinja2 import template
from aiohttp.web import Request, HTTPNotFound, Response, HTTPServiceUnavailable
from aiohttp import ClientSession

from nameinternal import get_relic, query, Base, Card, Relic
from sts_profile import get_profile, get_current_profile
from webpage import router, __botname__, __version__, __github__, __author__
from wrapper import wrapper
from twitch import TwitchCommand
from logger import logger
from utils import getfile, update_db, get_req_data
from disc import DiscordCommand
from save import get_savefile, Savefile
from runs import get_latest_run, get_run_stats

from typehints import ContextType, CommandType
import events

from configuration import config

TConn: TwitchConn = None
DConn: DiscordConn = None

logger.info("Setting up")

# Twitch bot server defined here - process started in main.py

_DEFAULT_BURST = 1
_DEFAULT_RATE  = 3.0

_consts = {
    "discord": config.discord.invite_links.main,
    "prefix": config.baalorbot.prefix,
    "website": config.server.url,
}

class Formatter(string.Formatter): # this does not support conversion or formatting
    def __init__(self):
        super().__init__()
        self._re = re.compile(r".+(\(.+\)).*")

    def parse(self, format_string: str) -> Generator[tuple[str, str | None, str | None, None]]:
        if format_string is None: # recursion
            yield ("", None, None, None)
            return
        start = 0
        while start < len(format_string):
            try:
                idx = format_string.index("$<", start)
                end = format_string.index(">", idx)
            except ValueError:
                yield (format_string[start:], None, None, None)
                break

            lit = format_string[start:idx]

            field = format_string[idx+2:end]

            called = None
            call_args = self._re.match(field)
            if call_args:
                called = call_args.group(1)
                call_idx = field.index(called)
                field = field[:call_idx]
                called = called[1:-1]

            yield (lit, field, called, None)
            start = end+1

    def format_field(self, value: Callable, call_args: str) -> str:
        if call_args:
            return value(call_args)
        return str(value)

_formatter = Formatter()

_perms = {
    "": "Everyone",
    "m": "Moderator",
    "e": "Editor", # this has no effect in discord
}

_cmds: dict[str, dict[str, list[str] | bool | int | float]] = {} # internal json

_to_add_twitch: list[TwitchCommand] = []
_to_add_discord: list[DiscordCommand] = []

_timers: dict[str, Routine] = {}

def _get_sanitizer(ctx: ContextType, name: str, args: list[str], mapping: dict):
    async def _sanitize(require_args: bool = True, in_mapping: bool = True) -> bool:
        """Verify that user input is sane. Return True if input is sane."""

        if require_args and not args:
            await ctx.reply("Error: no output provided.")
            return False

        if in_mapping and name not in mapping:
            await ctx.reply(f"Error: command {name} does not exist!")
            return False

        if not in_mapping and name in mapping:
            await ctx.reply(f"Error: command {name} already exists!")
            return False

        return True

    return _sanitize


def _create_cmd(output):
    async def inner(ctx: ContextType, *s, output: str=output):
        try:
            msg = output.format(user=ctx.author.display_name, text=" ".join(s), words=s, **_consts)
        except KeyError as e:
            msg = f"Error: command has unsupported formatting key {e.args[0]!r}"
        keywords = {"savefile": None, "profile": get_current_profile(), "readline": readline}
        if "$<savefile" in msg:
            keywords["savefile"] = await get_savefile(ctx)
            if keywords["savefile"] is None:
                return
        msg = _formatter.vformat(msg, (), keywords)
        # TODO: Add a flag to the command that says whethere it's a reply
        # or a regular message.
        await ctx.reply(msg)
    return inner

def readline(file: str) -> str:
    if ".." in file:
        return "Error: '..' in filename is not allowed."
    with open(os.path.join("text", file), "r") as f:
        return random.choice(f.readlines())

def load():
    _cmds.clear()
    with getfile("data.json", "r") as f:
        _cmds.update(json.load(f))
    for name, d in _cmds.items():
        c = command(
            name,
            *d.get("aliases", []),
            flag=d.get("flag", ""),
            burst=d.get("burst", _DEFAULT_BURST),
            rate=d.get("rate", _DEFAULT_RATE)
        )(_create_cmd(d["output"]))
        c.enabled = d.get("enabled", True)
    with getfile("disabled", "r") as f:
        for disabled in f.readlines():
            TConn.commands[disabled].enabled = False

def add_cmd(name: str, *, aliases: list[str] = None, source: str = None, flag: str = None, burst: int = None, rate: float = None, output: str):
    _cmds[name] = {"output": output}
    if aliases is not None:
        _cmds[name]["aliases"] = aliases
    if source is not None:
        _cmds[name]["source"] = source
    if flag is not None:
        _cmds[name]["flag"] = flag
    if burst is not None:
        _cmds[name]["burst"] = burst
    if rate is not None:
        _cmds[name]["rate"] = rate
    update_db()

def command(name: str, *aliases: str, flag: str = "", force_argcount: bool = False, burst: int = _DEFAULT_BURST, rate: float = _DEFAULT_RATE, twitch: bool = True, discord: bool = True):
    def inner(func, wrapper_func=None):
        wrapped = wrapper(func, force_argcount, wrapper_func, name)
        wrapped.__cooldowns__ = [TCooldown(burst, rate, TBucket.default)]
        #wrapped.__commands_cooldown__ = DCooldown(burst, rate, DBucket.default)
        if twitch:
            tcmd = TwitchCommand(name=name, aliases=list(aliases), func=wrapped, flag=flag)
            if TConn is None:
                _to_add_twitch.append(tcmd)
            else:
                TConn.add_command(tcmd)
        if discord:
            dcmd = _dcommand(name, DiscordCommand, aliases=list(aliases), flag=flag)(wrapped)
            if DConn is None:
                _to_add_discord.append(dcmd)
            else:
                DConn.add_command(dcmd)
        return tcmd
    return inner

def with_savefile(name: str, *aliases: str, **kwargs):
    """Decorator for commands that require a save."""
    def inner(func):
        async def _savefile_get(ctx) -> list:
            res = await get_savefile(ctx)
            if res is None:
                raise ValueError("No savefile")
            return [res]
        return command(name, *aliases, **kwargs)(func, wrapper_func=_savefile_get)
    return inner

class TwitchConn(TBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.esclient: EventSubClient = None
        self.live_channels: dict[str, bool] = {config.twitch.channel: False}
        self._session: ClientSession | None = None
        self._token: str = None
        self._expires_at: int | float = 0
        self._refresh_token: str = None
        try:
            with open(os.path.join("data", "spotify_refresh_token"), "r") as f:
                self._refresh_token = f.read()
        except OSError:
            pass

    async def get_new_token(self):
        if self._session is None:
            self._session = ClientSession()

        value = base64.urlsafe_b64encode(f"{config.spotify.id}:{config.spotify.secret}".encode("utf-8"))
        value = value.decode("utf-8")

        headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {value}",
            }

        if self._refresh_token:
            params = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }

        else:
            params={
                "grant_type": "authorization_code",
                "code": config.spotify.code,
                "redirect_uri": f"{config.server.url}/spotify",
            }

        async with self._session.post("https://accounts.spotify.com/api/token", headers=headers, params=params) as resp:
            if resp.ok:
                content = await resp.json()
                self._token = content["access_token"]
                self._expires_at = (datetime.datetime.now() + datetime.timedelta(seconds=content["expires_in"])).timestamp()
                if "refresh_token" in content:
                    self._refresh_token = content["refresh_token"]
                    with open(os.path.join("data", "spotify_refresh_token"), "w") as f:
                        f.write(self._refresh_token)
                return self._token
            return None

    async def spotify_call(self):
        if self._session is None:
            self._session = ClientSession()

        if not self._token or self._expires_at < time.time():
            token = await self.get_new_token()
            if not token:
                return None

        async with self._session.get(
            "https://api.spotify.com/v1/me/player/currently-playing",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
                }) as resp:
            return await resp.json()

    async def eventsub_setup(self):
        self.loop.create_task(self.esclient.listen(port=4000))
        channel = await self.fetch_users([config.twitch.channel])

        try:
            await self.esclient.subscribe_channel_stream_start(broadcaster=channel[0])
            await self.esclient.subscribe_channel_stream_end(broadcaster=channel[0])
        except HTTPException:
            pass

    async def event_ready(self):
        self.live_channels[config.twitch.channel] = live = bool(await self.fetch_streams(user_logins=[config.twitch.channel]))
        if live:
            try:
                await _timers["global"].start(config.baalorbot.timers.globals.commands, stop_on_error=False)
                await _timers["sponsored"].start(config.baalorbot.timers.sponsored.commands, stop_on_error=False)
            except RuntimeError: # already running; don't worry about it
                pass

    async def event_raw_usernotice(self, channel: Channel, tags: dict):
        user = Chatter(tags=tags, name=tags["login"], channel=channel, bot=self, websocket=self._connection)
        match tags["msg-id"]:
            case "sub" | "resub":
                total = 0
                consecutive = None
                subtype = ""
                try:
                    total = int(tags["msg-param-cumulative-months"])
                    if int(tags["msg-param-should-share-streak"]):
                        consecutive = int(tags["msg-param-streak-months"])
                    subtype: str = tags["msg-param-sub-plan"]
                    if subtype.isdigit():
                        subtype = f"Tier {subtype[0]}"
                except (KeyError, ValueError):
                    pass
                self.run_event("subscription", user, channel, total, consecutive, subtype)
            case "subgift" | "anonsubgift" | "submysterygift" | "giftpaidupgrade" | "rewardgift" | "anongiftpaidupgrade":
                self.run_event("gift_sub", user, channel, tags)
            case "raid":
                self.run_event("raid", user, channel, int(tags["msg-param-viewerCount"]))
            case "unraid":
                self.run_event("unraid", user, channel, tags)
            case "ritual":
                self.run_event("ritual", user, channel, tags)
            case "bitsbadgetier":
                self.run_event("bits_badge", user, channel, tags)

    async def event_subscription(self, user: Chatter, channel: Channel, total: int, consecutive: int | None, subtype: str):
        pass

    async def event_ritual(self, user: Chatter, channel: Channel, tags: dict):
        if tags["msg-param-ritual-name"] == "new_chatter":
            self.run_event("new_chatter", user, channel, tags["message"])

    async def event_new_chatter(self, user: Chatter, channel: Channel, message: str):
        if "youtube" in message.lower() or "yt" in message.lower():
            await channel.send(f"Hello {user.display_name}! Glad to hear that you're enjoying the YouTube content, and welcome along baalorLove")

    async def event_raid(self, user: Chatter, channel: Channel, viewer_count: int):
        if viewer_count < 10:
            return
        chan = await self.fetch_channel(user.id)
        await channel.send(f"Welcome along {user.display_name} with your {viewer_count} friends! "
                           f"Everyone, go give them a follow over at https://twitch.tv/{user.name} - "
                           f"last I checked, they were playing some {chan.game_name}!")

    def __getattr__(self, name: str):
        if name.startswith("event_"): # calling events -- insert our own event system in
            name = name[6:]
            evt = events.get(name)
            if evt:
                async def invoke(*args, **kwargs):
                    for e in evt:
                        e.invoke(args, kwargs)
                return invoke
        raise AttributeError(name)

class EventSubBot(TBot):
    async def event_eventsub_notification_stream_start(self, evt: StreamOnlineData):
        TConn.live_channels[evt.broadcaster.name] = True
        try:
            await _timers["global"].start(config.baalorbot.timers.globals.commands, stop_on_error=False)
            await _timers["sponsored"].start(config.baalorbot.timers.sponsored.commands, stop_on_error=False)
        except RuntimeError: # already running; don't worry about it
            pass

    async def event_eventsub_notification_stream_end(self, evt: StreamOfflineData):
        TConn.live_channels[evt.broadcaster.name] = False
        _timers["global"].stop()
        _timers["sponsored"].stop()

class DiscordConn(DBot):
    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        if message.content.startswith(config.baalorbot.prefix) or isinstance(message.channel, discord.DMChannel):
            content = message.content.lstrip(config.baalorbot.prefix).split()
            if not content:
                return
            ctx = await self.get_context(message)
            cmd: DiscordCommand = self.get_command(content[0])
            if cmd:
                await cmd(ctx, *content[1:])

    def dispatch(self, name, /, *args, **kwargs):
        evt = events.get(name)
        if evt is not None:
            for event in evt:
                self.add_listener(event, name)
        value = super().dispatch(name, *args, **kwargs)
        if evt is not None:
            for event in evt:
                self.remove_listener(event, name)
        return value

async def _timer(cmds: list[str]):
    chan = TConn.get_channel(config.twitch.channel)
    if not chan or not TConn.live_channels[config.twitch.channel]:
        return
    cmd = None
    i = 0
    while cmd is None:
        i += 1
        if i > len(cmds):
            return
        maybe_cmd = cmds.pop(0)
        if maybe_cmd not in _cmds and maybe_cmd not in TConn.commands:
            i -= 1 # we're not adding it back, so it's fine
            continue
        if not TConn.commands[maybe_cmd].enabled:
            cmds.append(maybe_cmd) # in case it gets enabled again
            continue
        if maybe_cmd == "current":
            save = await get_savefile()
            if save is None:
                cmds.append(maybe_cmd)
                continue
        cmd = maybe_cmd
    # don't use the actual command, just send the raw output
    msg: str = _cmds[cmd]["output"]
    try:
        msg = msg.format(**_consts)
    except KeyError:
        logger.error(f"Timer-command {cmd} needs non-constant formatting. Sending raw line.")
    await chan.send(msg)
    cmds.append(cmd)

@command("command", flag="me")
async def command_cmd(ctx: ContextType, action: str, name: str, *args: str):
    """Syntax: command <action> <name> <output>"""
    args = list(args)
    msg = " ".join(args)
    name = name.lstrip(config.baalorbot.prefix)
    cmds: dict[str, list[CommandType]] = {}
    if TConn is not None:
        for cname, cmd in TConn.commands.items():
            cmds[cname] = [cmd]
    if DConn is not None:
        for cmd in DConn.commands:
            if cmd.name not in cmds:
                cmds[cmd.name] = []
            cmds[cmd.name].append(cmd)
    aliases: dict[str, list[CommandType]] = {}
    if TConn is not None:
        for alias, cmd in TConn._command_aliases.items():
            aliases[alias] = [cmd]
    if DConn is not None:
        for dcmd in DConn.commands:
            for alias in dcmd.aliases:
                if alias not in aliases:
                    aliases[alias] = []
                aliases[alias].append(dcmd)

    sanitizer = _get_sanitizer(ctx, name, args, cmds)
    match action:
        case "add":
            if not await sanitizer(in_mapping=False):
                return
            if name in aliases:
                await ctx.reply(f"Error: {name} is an alias to {aliases[name][0].name}. Use 'unalias {aliases[name][0].name} {name}' first.")
                return
            flag = ""
            if args[0].startswith("+"):
                flag, *args = args
            flag = flag[1:]
            if flag not in _perms:
                await ctx.reply("Error: flag not recognized.")
                return
            if flag:
                add_cmd(name, flag=flag, output=msg)
            else:
                add_cmd(name, output=msg)
            command(name, flag=flag)(_create_cmd(msg))
            await ctx.reply(f"Command {name} added! Permission: {_perms[flag]}")

        case "edit":
            if not await sanitizer():
                return
            if name in aliases:
                await ctx.reply(f"Error: cannot edit alias. Use 'edit {aliases[name][0].name}' instead.")
                return
            if name not in _cmds:
                await ctx.reply(f"Error: cannot edit built-in command {name}.")
                return
            _cmds[name]["output"] = msg
            flag = ""
            if args[0].startswith("+"):
                flag, *args = args
            if flag not in _perms:
                await ctx.reply("Error: flag not recognized.")
                return
            if flag:
                _cmds[name]["flag"] = flag
            update_db()
            for cmd in cmds[name]:
                cmd._callback = _create_cmd(msg)
            await ctx.reply(f"Command {name} edited successfully! Permission: {_perms[flag]}")

        case "remove" | "delete":
            if not await sanitizer(require_args=False):
                return
            if name in aliases:
                await ctx.reply(f"Error: cannot delete alias. Use 'remove {aliases[name][0].name}' or 'unalias {aliases[name][0].name} {name}' instead.")
                return
            if name not in _cmds:
                await ctx.reply(f"Error: cannot delete built-in command {name}.")
                return
            del _cmds[name]
            update_db()
            if TConn is not None:
                TConn.remove_command(name)
            if DConn is not None:
                DConn.remove_command(name)
            await ctx.reply(f"Command {name} has been deleted.")

        case "enable":
            if not await sanitizer(require_args=False):
                return
            if name in aliases:
                await ctx.reply(f"Error: cannot enable alias. Use 'enable {aliases[name][0].name}' instead.")
                return
            if all(cmds[name]):
                await ctx.reply(f"Command {name} is already enabled.")
                return
            for cmd in cmds[name]:
                cmd.enabled = True
            if name in _cmds:
                _cmds[name]["enabled"] = True
                update_db()
            with getfile("disabled", "r") as f:
                disabled = f.readlines()
            disabled.remove(name)
            with getfile("disabled", "w") as f:
                f.writelines(disabled)
            if name == "sponsored_cmd" and "sponsored" in _timers:
                _timers["sponsored"].restart()
            await ctx.reply(f"Command {name} has been enabled.")

        case "disable":
            if not await sanitizer(require_args=False):
                return
            if name in aliases:
                await ctx.reply(f"Error: cannot disable alias. Use 'disable {aliases[name][0].name}' or 'unalias {aliases[name][0].name} {name}' instead.")
                return
            if not all(cmds[name]):
                await ctx.reply(f"Command {name} is already disabled.")
                return
            for cmd in cmds[name]:
                cmd.enabled = False
            if name in _cmds:
                _cmds[name]["enabled"] = False
                update_db()
            with getfile("disabled", "r") as f:
                disabled = f.readlines()
            disabled.append(name)
            with getfile("disabled", "w") as f:
                f.writelines(disabled)
            await ctx.reply(f"Command {name} has been disabled.")

        case "alias": # cannot sanely sanitize this
            if not args:
                if name not in _cmds and name in aliases:
                    await ctx.reply(f"Alias {name} is bound to {aliases[name][0].name}.")
                elif _cmds[name].get("aliases"):
                    await ctx.reply(f"Command {name} has the following aliases: {', '.join(_cmds[name]['aliases'])}")
                else:
                    await ctx.reply(f"Command {name} does not have any aliases.")
                return
            if name not in cmds and args[0] in cmds:
                await ctx.reply(f"Error: use 'alias {args[0]} {name}' instead.")
                return
            if name not in cmds:
                await ctx.reply(f"Error: command {name} does not exist.")
                return
            if name not in _cmds:
                await ctx.reply("Error: cannot alias built-in commands.")
                return
            if set(args) & cmds.keys():
                await ctx.reply(f"Error: aliases {set(args) & cmds.keys()} already exist as commands.")
                return
            for arg in args:
                if TConn is not None:
                    TConn._command_aliases[arg] = name
                if DConn is not None:
                    DConn.get_command(name).aliases.append(arg)
            if "aliases" not in _cmds[name]:
                _cmds[name]["aliases"] = []
            _cmds[name]["aliases"].extend(args)
            update_db()
            await ctx.reply(f"Command {name} now has aliases {', '.join(_cmds[name]['aliases'])}")

        case "unalias":
            if not args:
                await ctx.reply("Error: no alias specified.")
                return
            if len(args) > 1:
                await ctx.reply("Can only remove one alias at a time.")
                return
            if name not in cmds and args[0] in cmds:
                await ctx.reply(f"Error: use 'unalias {args[0]} {name}' instead.")
                return
            if name not in cmds:
                await ctx.reply(f"Error: command {name} does not exist.")
                return
            if args[0] not in aliases:
                await ctx.reply("Error: not an alias.")
                return
            if name not in _cmds:
                await ctx.reply("Error: cannot unalias built-in commands.")
                return
            if aliases[args[0]][0].name != name:
                await ctx.reply(f"Error: alias {args[0]} does not match command {name} (bound to {aliases[args[0]][0].name}).")
                return
            if TConn is not None:
                TConn._command_aliases.pop(args[0], None)
            if DConn is not None:
                dcmd: DiscordCommand = DConn.get_command(name)
                if args[0] in dcmd.aliases:
                    dcmd.aliases.remove(args[0])
            _cmds[name]["aliases"].remove(name)
            update_db()
            await ctx.reply(f"Alias {args[0]} has been removed from command {name}.")

        case "cooldown" | "cd":
            await ctx.reply("Cooldown cannot be changed currently")
            return
            if not await sanitizer(require_args=False):
                return
            if not args:
                if name not in cmds and name not in aliases:
                    await ctx.reply(f"Error: command {name} does not exist.")
                else:
                    if name in aliases:
                        name = aliases[name]
                    cd = cmds[name]._cooldowns[0]
                    await ctx.reply(f"Command {name} has a cooldown of {cd._per/cd._rate}s.")
                return
            if name in aliases:
                await ctx.reply(f"Error: cannot edit alias cooldown. Use 'cooldown {aliases[name]}' instead.")
                return
            cd: TCooldown = cmds[name]._cooldowns.pop()
            try:
                burst = int(args[0])
            except ValueError:
                try:
                    rate = float(args[0])
                except ValueError:
                    await ctx.reply("Error: invalid argument.")
                    return
                else:
                    burst = cd._rate # _rate is actually the burst, it's weird
            else:
                try:
                    rate = float(args[1])
                except IndexError:
                    rate = cd._per
                except ValueError:
                    await ctx.reply("Error: invalid argument.")
                    return

            cmds[name]._cooldowns.append(TCooldown(burst, rate, cd.bucket))
            if name in _cmds:
                _cmds[name]["burst"] = burst
                _cmds[name]["rate"] = rate
                update_db()
            await ctx.reply(f"Command {name} now has a cooldown of {rate/burst}s.") # this isn't 100% accurate, but close enough
            if name not in _cmds:
                await ctx.reply("Warning: settings on built-in commands do not persist past a restart.")

        case _:
            await ctx.reply(f"Unrecognized action {action}.")

@command("help")
async def help_cmd(ctx: ContextType, name: str = ""):
    """Find help on the various commands in the bot."""
    if not name:
        await ctx.reply(f"I am {__botname__} v{__version__}, made by {__author__}. I am running on Python "
                       f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}, "
                       f"my source code is available at {__github__}, and the website is {config.server.url}")
        return

    tcmd = dcmd = None
    if TConn is not None:
        tcmd = TConn.get_command(name)
    if DConn is not None:
        dcmd = DConn.get_command(name)
    cmd = (tcmd or dcmd)
    if cmd:
        await ctx.reply(f"Full information about this command can be viewed at {config.server.url}/commands/{cmd.name}")
        return

    await ctx.reply(f"Could not find matching command. You may view all existing commands here: {config.server.url}/commands")

@command("support", "shoutout", "so")
async def shoutout(ctx: ContextType, name: str):
    """Give a shoutout to a fellow streamer."""
    try:
        chan = await TConn.fetch_channel(name)
    except IndexError as e:
        await ctx.send(e.args[0])
        return
    except HTTPException as e:
        await ctx.send(e.message)
        return

    msg = [f"Go give a warm follow to https://twitch.tv/{chan.user.name} -"]

    live: list[Stream] = await TConn.fetch_streams([chan.user.id])
    if live:
        stream = live[0]
        game = stream.game_name
        viewers = stream.viewer_count
        started_at = stream.started_at
        # somehow, doing now() - started_at triggers an error due to conflicting timestamps
        td = datetime.timedelta(seconds=(datetime.datetime.now().timestamp() - started_at.timestamp()))
        msg.append(f"they are currently live with {viewers} viewers playing {game}! They have been live for {str(td).partition('.')[0]}")

    else:
        msg.append(f"last time they were live, they were seen playing {chan.game_name}!")

    await ctx.send(" ".join(msg))

@command("title")
async def stream_title(ctx: ContextType):
    """Display the current stream title."""
    live: list[Stream] = await TConn.fetch_streams(user_logins=[config.twitch.channel])

    if live:
        await ctx.reply(live[0].title)
    else:
        await ctx.reply("Could not connect to the Twitch API (or stream is offline).")

@command("uptime")
async def stream_uptime(ctx: ContextType):
    """Display the stream uptime."""
    live: list[Stream] = await TConn.fetch_streams(user_logins=[config.twitch.channel])

    if live:
        td = datetime.timedelta(seconds=(datetime.datetime.now().timestamp() - live[0].started_at.timestamp()))
        await ctx.reply(f"The stream has been live for {str(td).partition('.')[0]}")
    else:
        await ctx.reply("Stream is offline (if this is wrong, the Twitch API broke).")

@command("playing", "nowplaying", "spotify", "np")
async def now_playing(ctx: ContextType):
    """Return the currently-playing song on Spotify (if any)."""
    if not config.server.debug and not TConn.live_channels[config.twitch.channel]:
        # just in case
        TConn.live_channels[config.twitch.channel] = live = bool(await TConn.fetch_streams(user_logins=[config.twitch.channel]))
        if not live:
            await ctx.reply("That's kinda creepy, not gonna lie...")
            return

    j = await TConn.spotify_call()
    if j is None:
        await ctx.reply("Could not get token from Spotify API. Retry in a few seconds.")
    elif "error" in j:
        await ctx.reply(f"Something went wrong with the Spotify API ({j['status']}: {j['message']})")
    elif j["is_playing"]:
        await ctx.reply(f"We are listening to {j['item']['name']} on the album {j['item']['album']['name']}.")
    else:
        await ctx.reply("We are not currently listening to anything.")

@router.get("/playing")
async def now_playing_client(req: Request):
    await get_req_data(req) # just checking if key is OK

    data = await TConn.spotify_call()

    if data:
        return Response(text=json.dumps(data), content_type="application/json")
    raise HTTPServiceUnavailable(reason="Could not connect to the Spotify API")

_ongoing_giveaway = {
    "running": False,
    "count": 0,
    "users": set(),
    "starter": None,
}

@command("giveaway", flag="m")
async def giveaway_handle(ctx: ContextType, count: int = 1):
    """Manage a giveaway."""

    if not _ongoing_giveaway["running"]:
        _ongoing_giveaway["running"] = True
        _ongoing_giveaway["starter"] = ctx.author.name
        if count > 0:
            _ongoing_giveaway["count"] = count
        else:
            _ongoing_giveaway["count"] = 1
        await ctx.send(f"/announce A giveaway has started! Type {config.baalorbot.prefix}enter to enter!")

    elif _ongoing_giveaway["starter"] != ctx.author.name:
        await ctx.reply("Only the person who started the giveaway can resolve it!")

    else:
        _ongoing_giveaway["running"] = False
        _ongoing_giveaway["starter"] = None
        _ongoing_giveaway["users"].discard(None) # just in case
        if not _ongoing_giveaway["users"]:
            await ctx.reply("uhhh, no one entered??")
            return

        users = random.choices(list(_ongoing_giveaway["users"]), k=_ongoing_giveaway["count"])
        _ongoing_giveaway["users"].clear()
        if len(users) == 1:
            await ctx.send(f"Congratulations to {users[0]}, you have won the giveaway!")
        else:
            await ctx.send(f"Congratulations to the following users for winning the giveaway: {', '.join(users)}")

@command("enter", burst=25, rate=1.0)
async def giveaway_enter(ctx: ContextType):
    """Enter into the current giveaway."""
    # it's a set, dupes won't do matter. don't respond to not spam
    if not _ongoing_giveaway["running"]:
        await ctx.reply("No giveaway is happening")
        return

    _ongoing_giveaway["users"].add(ctx.author.name)

@command("info", "cardinfo", "relicinfo")
async def card_info(ctx: ContextType, *line: str):
    line = "".join(line)
    info: Base = query(line)
    if info is None:
        await ctx.reply(f"Could not find info for {line!r}")
        return

    mod = ""
    if info.mod:
        mod = f"(Mod: {info.mod})"
    match info.cls_name:
        case "card":
            card: Card = info
            await ctx.reply(f"{card.name} - [{card.cost}] {card.color} {card.rarity} {card.type}: {card.description} {mod}")
        case "relic":
            rel: Relic = info
            pool = " "
            if rel.pool:
                pool = f" ({rel.pool})"
            await ctx.reply(f"{rel.name} - {rel.tier}{pool}: {rel.description} {mod}")

@with_savefile("bluekey", "sapphirekey", "key") # JSON_FP_PROP
async def bluekey(ctx: ContextType, save: Savefile):
    """Display what was skipped for the Sapphire key."""
    if not save._data["has_sapphire_key"]:
        await ctx.reply("We do not have the Sapphire key.")
        return

    for node in save.path:
        try:
            if node.blue_key:
                await ctx.reply(f"We skipped {node.key_relic} on floor {node.floor} for the Sapphire key.")
                return
        except AttributeError:
            continue

    await ctx.reply("RunHistoryPlus is not running; cannot get data.")

@with_savefile("neow", "neowbonus")
async def neowbonus(ctx: ContextType, save: Savefile):
    """Display what the Neow bonus was."""
    await ctx.reply(f"Option taken: {save.neow_bonus.picked} {save.neow_bonus.as_str() if save.neow_bonus.has_info else ''}")

@with_savefile("seed", "currentseed")
async def seed_cmd(ctx: ContextType, save: Savefile):
    """Display the run's current seed."""
    await ctx.reply(f"Current seed: {save.seed}{' (set manually)' if save.is_seeded else ''}")

@with_savefile("seeded", "isthisseeded")
async def is_seeded(ctx: ContextType, save: Savefile):
    """Display whether the current run is seeded."""
    if save.is_seeded:
        await ctx.reply(f"This run is seeded! See '{config.baalorbot.prefix}seed' for the seed.")
    else:
        await ctx.reply("This run is not seeded! Everything you're seeing is unplanned!")

@with_savefile("playtime", "runtime", "time", "played")
async def run_playtime(ctx: ContextType, save: Savefile):
    """Display the current playtime for the run."""
    # TODO: get run start time and compare to now, rather than the save data
    minutes, seconds = divmod(save.playtime, 60)
    hours, minutes = divmod(minutes, 60)
    await ctx.reply(f"This run has been going on for {hours}:{minutes:>02}:{seconds:>02}")

@with_savefile("shopremoval", "cardremoval", "removal")
async def shop_removal_cost(ctx: ContextType, save: Savefile):
    """Display the current shop removal cost."""
    await ctx.reply(f"Current card removal cost: {save.current_purge} (removed {save.purge_totals} card{'' if save.purge_totals == 1 else 's'})")

@with_savefile("shopprices", "shopranges", "shoprange", "ranges", "range", "shop", "prices")
async def shop_prices(ctx: ContextType, save: Savefile):
    """Display the current shop price ranges."""
    cards, colorless, relics, potions = save.shop_prices
    cc, uc, rc = cards
    ul, rl = colorless
    cr, ur, rr = relics
    cp, up, rp = potions
    await ctx.reply(
        f"Cards: Common {cc.start}-{cc.stop}, Uncommon {uc.start}-{uc.stop}, Rare {rc.start}-{rc.stop} | "
        f"Colorless: Uncommon {ul.start}-{ul.stop}, Rare {rl.start}-{rl.stop} | "
        f"Relics: Common/Shop {cr.start}-{cr.stop}, Uncommon {ur.start}-{ur.stop}, Rare {rr.start}-{rr.stop} | "
        f"Potions: Common {cp.start}-{cp.stop}, Uncommon {up.start}-{up.stop}, Rare {rp.start}-{rp.stop} | "
        f"Card removal: {save.current_purge}"
    )

@with_savefile("rest", "heal", "restheal")
async def campfire_heal(ctx: ContextType, save: Savefile):
    """Display the current heal at campfires."""
    base = int(save.max_health * 0.3)
    for relic in save.relics:
        if relic.name == "Regal Pillow":
            base += 15
            break

    lost = max(0, (base + save.current_health) - save.max_health)
    extra = ""
    if lost:
        extra = f" (extra healing lost: {lost} HP)"

    await ctx.reply(f"Current campfire heal: {base} HP{extra}")

@with_savefile("nloth")
async def nloth_traded(ctx: ContextType, save: Savefile): # JSON_FP_PROP
    """Display which relic was traded for N'loth's Gift."""
    if "Nloth's Gift" not in save._data["relics"]:
        await ctx.reply("We do not have N'loth's Gift.")
        return

    for evt in save._data["metric_event_choices"]:
        if evt["event_name"] == "N'loth":
            await ctx.reply(f"We traded {get_relic(evt['relics_lost'][0])} for N'loth's Gift.")
            return
    else:
        await ctx.reply("Something went terribly wrong.")

@with_savefile("eventchances", "event") # note: this does not handle pRNG calls like it should - event_seed_count might have something? though only appears to be count of seen ? rooms
async def event_likelihood(ctx: ContextType, save: Savefile):
    """Display current event chances for the various possibilities in ? rooms."""
    elite, hallway, shop, chest = save._data["event_chances"] # JSON_FP_PROP
    # elite likelihood is only for the "Deadly Events" custom modifier

    await ctx.reply(
        f"Event type likelihood: "
        f"Normal fight: {hallway:.0%} - "
        f"Shop: {shop:.0%} - "
        f"Treasure: {chest:.0%} - "
        f"Event: {hallway+shop+chest:.0%} - "
        f"See {config.baalorbot.prefix}eventrng for more information."
    )

@with_savefile("rare", "rarecard", "rarechance") # see comment in save.py -- this is not entirely accurate
async def rare_card_chances(ctx: ContextType, save: Savefile):
    """Display the current chance to see rare cards in rewards and shops."""
    regular, elites, shops = save.rare_chance
    await ctx.reply(
        f"The current chance of seeing a rare card is {regular:.2%} "
        f"in normal fight card rewards, {elites:.2%} in elite fight "
        f"card rewards, and {shops:.2%} in shops."
    )

@with_savefile("relic")
async def relic_info(ctx: ContextType, save: Savefile, index: int):
    """Display information about the current relics."""
    if index < 0:
        await ctx.reply("Why do you insist on breaking me?")
        return
    l = list(save.relics)
    if index > len(l):
        await ctx.reply(f"We only have {len(l)} relics!")
        return
    if not index:
        await ctx.reply(f"We have {len(l)} relics.")
        return

    await ctx.reply(f"The relic at position {index} is {l[index-1].name}.")

@with_savefile("allrelics", "offscreen", "page2")
async def relics_page2(ctx: ContextType, save: Savefile):
    """Display the relics on page 2."""
    l = list(save.relics)
    if len(l) <= 25:
        await ctx.reply("We only have one page of relics!")
        return

    relics = []
    for relic in l[25:]:
        relics.append(relic.name)

    await ctx.reply(f"The relics past page 1 are {', '.join(relics)}")

@with_savefile("skipped", "picked", "skippedboss", "bossrelic")
async def skipped_boss_relics(ctx: ContextType, save: Savefile): # JSON_FP_PROP
    """Display the boss relics that were taken and skipped."""
    l: list[dict] = save._data["metric_boss_relics"]

    if not l:
        await ctx.reply("We have not picked any boss relics yet.")
        return

    template = "We picked {1} at the end of Act {0}, and skipped {2} and {3}."
    msg = []
    i = 1
    for item in l:
        msg.append(
            template.format(
                i,
                get_relic(item["picked"]),
                get_relic(item["not_picked"][0]),
                get_relic(item["not_picked"][1]),
            )
        )
        i += 1

    await ctx.reply(" ".join(msg))

@with_savefile("bottle", "bottled", "bottledcards", "bottledcard")
async def bottled_cards(ctx: ContextType, save: Savefile):
    """List all bottled cards."""
    emoji_dict = {
        "Bottled Flame": "\N{FIRE}",
        "Bottled Lightning": "\N{HIGH VOLTAGE SIGN}",
        "Bottled Tornado": "\N{CLOUD WITH TORNADO}"
    }
    bottle_strings: list[str] = []
    for bottle in save.bottles:
        bottle_strings.append(f"{emoji_dict[bottle.bottle_id]} {bottle.card}")

    if bottle_strings:
        await ctx.reply(", ".join(bottle_strings))
    else:
        await ctx.reply("We do not have any bottled cards.")

@with_savefile("custom", "modifiers")
async def modifiers(ctx: ContextType, save: Savefile):
    """List all custom modifiers for the run."""
    if save.modifiers:
        await ctx.reply(", ".join(save.modifiers))
    else:
        await ctx.reply("This is a standard run.")

@with_savefile("score")
async def score(ctx: ContextType, save: Savefile):
    """Display the current score of the run"""
    if save.modded:
        await ctx.reply(f'Current Score: ~{save.score} points')
    else:
        await ctx.reply(f'Current Score: {save.score} points')

@command("last")
async def get_last(ctx: ContextType, arg1: str = "", arg2: str = ""):
    """Get the last run/win/loss."""
    char = None
    won = None
    value = None
    if arg2:
        value = [arg1.lower(), arg2.lower()]
    elif arg1:
        value = [arg1.lower()]
    match value:
        case None:
            pass # nothing to worry about
        case ["win"] | ["victory"] | ["w"]:
            won = True
        case ["loss"] | ["death"] | ["l"]:
            won = False
        case [a]:
            char = a
        case [a, b]:
            match a:
                case "win" | "victory" | "w":
                    won = True
                    char = b
                case "loss" | "death" | "l":
                    won = False
                    char = b
                case _:
                    char = a
                    match b:
                        case "win" | "victory" | "w":
                            won = True
                        case "loss" | "death" | "l":
                            won = False

    match char:
        case "ironclad" | "ic" | "i":
            char = "Ironclad"
        case "silent" | "s":
            char = "Silent"
        case "defect" | "d":
            char = "Defect"
        case "watcher" | "wa":
            char = "Watcher"
        case _:
            if char is not None:
                char = char.capitalize() # might be a mod character

    await _last_run(ctx, char, won)

@command("lastrun")
async def get_last_run(ctx: ContextType):
    """Get the last run."""
    await _last_run(ctx, None, None)

@command("lastwin", "lastvictory")
async def get_last_win(ctx: ContextType):
    """Get the last win."""
    await _last_run(ctx, None, True)

@command("lastloss", "lastdeath")
async def get_last_loss(ctx: ContextType):
    """Get the last loss."""
    await _last_run(ctx, None, False)

async def _last_run(ctx: ContextType, character: str | None, arg: bool | None):
    try:
        latest = get_latest_run(character, arg)
    except KeyError:
        await ctx.reply(f"Could not understand character {character}.")
        return
    value = "run"
    if character is not None:
        value = f"{character} run"
    if arg:
        value = f"winning {value}"
    elif arg is not None:
        value = f"lost {value}"
    await ctx.reply(f"The last {value}'s history can be viewed at {config.server.url}/runs/{latest.name}")

@command("wall")
async def wall_card(ctx: ContextType):
    """Fetch the card in the wall for the ladder savefile."""
    for i in range(2):
        try:
            p = get_profile(i)
        except KeyError:
            continue
        if "ladder" in p.name.lower():
            break
    else:
        await ctx.reply("Error: could not find Ladder savefile.")
        return

    await ctx.reply(f"Current card in the {config.baalorbot.prefix}hole in the wall for the ladder savefile: {p.hole_card}")

@command("kills", "wins") # TODO: Read game files for this
async def kills_cmd(ctx: ContextType):
    """Display the cumulative number of wins for the year-long challenge."""
    msg = "A20 Heart kills in 2022: Total: {1} - Ironclad: {0[0]} - Silent: {0[1]} - Defect: {0[2]} - Watcher: {0[3]}"
    with getfile("kills", "r") as f:
        kills = [int(x) for x in f.read().split()]
    await ctx.reply(msg.format(kills, sum(kills)))

@command("winstest")
async def killstest_cmd(ctx: ContextType):
    """Display the cumulative number of wins for the year-long challenge."""
    msg = "A20 Heart kills in 2022: Total: {0} - Ironclad: {1} - Silent: {2} - Defect: {3} - Watcher: {4}"
    run_stats = get_run_stats()
    await ctx.reply(msg.format(run_stats.wins.all_character_count, run_stats.wins.ironclad_count, run_stats.wins.silent_count, run_stats.wins.defect_count, run_stats.wins.watcher_count))

@command("losses")
async def losses_cmd(ctx: ContextType):
    """Display the cumulative number of losses for the year-long challenge."""
    msg = "A20 Heart losses in 2022: Total: {1} - Ironclad: {0[0]} - Silent: {0[1]} - Defect: {0[2]} - Watcher: {0[3]}"
    with getfile("losses", "r") as f:
        losses = [int(x) for x in f.read().split()]
    await ctx.reply(msg.format(losses, sum(losses)))

@command("lossestest")
async def lossestest_cmd(ctx: ContextType):
    """Display the cumulative number of losses for the year-long challenge."""
    msg = "A20 Heart losses in 2022: Total: {0} - Ironclad: {1} - Silent: {2} - Defect: {3} - Watcher: {4}"
    run_stats = get_run_stats()
    await ctx.reply(msg.format(run_stats.losses.all_character_count, run_stats.losses.ironclad_count, run_stats.losses.silent_count, run_stats.losses.defect_count, run_stats.losses.watcher_count))

@command("streak")
async def streak_cmd(ctx: ContextType):
    """Display Baalor's current streak for Ascension 20 Heart kills."""
    msg = "Current streak: Rotating: {0[0]} - Ironclad: {0[1]} - Silent: {0[2]} - Defect: {0[3]} - Watcher: {0[4]}"
    with getfile("streak", "r") as f:
        streak = f.read().split()
    await ctx.reply(msg.format(streak))

@command("streaktest")
async def streaktest_cmd(ctx: ContextType):
    """Display Baalor's current streak for Ascension 20 Heart kills."""
    msg = "Current streak: Rotating: {0} - Ironclad: {1} - Silent: {2} - Defect: {3} - Watcher: {4}"
    run_stats = get_run_stats()
    await ctx.reply(msg.format(run_stats.streaks.all_character_count, run_stats.streaks.ironclad_count, run_stats.streaks.silent_count, run_stats.streaks.defect_count, run_stats.streaks.watcher_count))

@command("pb")
async def pb_cmd(ctx: ContextType):
    """Display Baalor's Personal Best streaks for Ascension 20 Heart kills."""
    msg = "Baalor's PB A20H Streaks | Rotating: {0[0]} - Ironclad: {0[1]} - Silent: {0[2]} - Defect: {0[3]} - Watcher: {0[4]}"
    with getfile("pb", "r") as f:
        pb = f.read().split()
    await ctx.reply(msg.format(pb))

@command("winrate")
async def winrate_cmd(ctx: ContextType):
    """Display the current winrate for Baalor's 2022 A20 Heart kills."""
    with getfile("kills", "r") as f:
        kills = [int(x) for x in f.read().split()]
    with getfile("losses", "r") as f:
        losses = [int(x) for x in f.read().split()]
    rate = [a/(a+b) for a, b in zip(kills, losses)]
    await ctx.reply(f"Baalor's winrate: Ironclad: {rate[0]:.2%} - Silent: {rate[1]:.2%} - Defect: {rate[2]:.2%} - Watcher: {rate[3]:.2%}")

@command("winratetest")
async def winrate_cmd(ctx: ContextType):
    """Display the current winrate for Baalor's 2022 A20 Heart kills."""
    run_stats = get_run_stats()
    wins = [run_stats.wins.ironclad_count, run_stats.wins.silent_count, run_stats.wins.defect_count, run_stats.wins.watcher_count]
    losses = [run_stats.losses.ironclad_count, run_stats.losses.silent_count, run_stats.losses.defect_count, run_stats.losses.watcher_count]
    rate = [a/(a+b) for a, b in zip(wins, losses)]
    await ctx.reply(f"Baalor's winrate: Ironclad: {rate[0]:.2%} - Silent: {rate[1]:.2%} - Defect: {rate[2]:.2%} - Watcher: {rate[3]:.2%}")

async def edit_counts(ctx: ContextType, arg: str, *, add: bool):
    if arg.lower().startswith("i"):
        i = 0
    elif arg.lower().startswith("s"):
        i = 1
    elif arg.lower().startswith("d"):
        i = 2
    elif arg.lower().startswith("w"):
        i = 3
    else:
        await ctx.reply(f"Unrecognized character {arg}")
        return

    with getfile("kills", "r") as f:
        kills = [int(x) for x in f.read().split()]
    with getfile("losses", "r") as f:
        losses = [int(x) for x in f.read().split()]
    with getfile("streak", "r") as f:
        streak = [int(x) for x in f.read().split()]
    cur = streak.pop(0)
    with getfile("pb", "r") as f:
        pb = [int(x) for x in f.read().split()]
    rot = pb.pop(0)

    pb_changed = False

    if add:
        kills[i] += 1
        streak[i] += 1
        cur += 1
        if cur > rot:
            rot = cur
            pb_changed = True
        if streak[i] > pb[i]:
            pb[i] = streak[i]
            pb_changed = True

        with getfile("kills", "w") as f:
            f.write(" ".join(str(x) for x in kills))
        if pb_changed:
            with getfile("pb", "w") as f:
                f.write(f"{rot} {pb[0]} {pb[1]} {pb[2]} {pb[3]}")

    else:
        losses[i] += 1
        streak[i] = 0
        cur = 0

        with getfile("losses", "w") as f:
            f.write(" ".join(str(x) for x in losses))

    with getfile("streak", "w") as f:
        f.write(f"{cur} {streak[0]} {streak[1]} {streak[2]} {streak[3]}")

    d = ("Ironclad", "Silent", "Defect", "Watcher")

    if pb_changed:
        await ctx.reply(f"[NEW PB ATTAINED] Win #{kills[i]} recorded for the {d[i]}. Total wins: {sum(kills)}")
    elif add:
        await ctx.reply(f"Win #{kills[i]} recorded for the {d[i]}. Total wins: {sum(kills)}")
    else:
        await ctx.reply(f"Loss #{losses[i]} recorded for the {d[i]}. Total losses: {sum(losses)}")

@command("win", flag="me", burst=1, rate=60.0) # 1:60.0 means we can't accidentally do it twice in a row
async def win_cmd(ctx: ContextType, arg: str):
    """Register a win for a given character."""
    await edit_counts(ctx, arg, add=True)

@command("loss", flag="me", burst=1, rate=60.0)
async def loss_cmd(ctx: ContextType, arg: str):
    """Register a loss for a given character."""
    await edit_counts(ctx, arg, add=False)

@router.get("/commands")
@template("commands.jinja2")
async def commands_page(req: Request):
    d = {"prefix": config.baalorbot.prefix, "commands": []}
    cmds = set()
    if TConn is not None:
        cmds.update(TConn.commands)
    if DConn is not None:
        cmds.update(DConn.all_commands)
    d["commands"].extend(cmds)
    d["commands"].sort()
    return d

@router.get("/commands/{name}")
@template("command_single.jinja2")
async def individual_cmd(req: Request):
    name = req.match_info["name"]
    d = {"name": name}
    tcmd = dcmd = None
    if TConn is not None:
        tcmd: TwitchCommand = TConn.get_command(name)
    if DConn is not None:
        dcmd: DiscordCommand = DConn.get_command(name)
    cmd = (tcmd or dcmd)
    if cmd is None:
        raise HTTPNotFound()
    if name in _cmds:
        d["builtin"] = False
        output: str = _cmds[name]["output"]
        try:
            output = output.format(user="<username>", text="<text>", words="<words>", **_consts)
        except KeyError:
            pass
        out = []
        for word in output.split():
            if "<" in word:
                word = word.replace("<", "&lt")
            if word.startswith("http"):
                word = f'<a href="{word}">{word}</a>'
            out.append(word)
        d["output"] = " ".join(out)
        d["enabled"] = _cmds[name].get("enabled", True) is True
        d["aliases"] = _cmds[name].get("aliases", [])
        # Just in case it's not a list...
        if not isinstance(d["aliases"], list):
            d["aliases"] = [d["aliases"]]
    else:
        d["builtin"] = True
        d["fndoc"] = cmd.__doc__
        d["enabled"] = cmd.enabled

    # d["twitch"] = ("No" if tcmd is None else "Yes")
    # d["discord"] = ("No" if dcmd is None else "Yes")
    d["twitch"] = tcmd
    d["discord"] = dcmd

    d["permissions"] = ", ".join(_perms[x] for x in cmd.flag) or _perms[""]
    d["prefix"] = config.baalorbot.prefix

    return d

async def Twitch_startup():
    global TConn
    TConn = TwitchConn(token=config.twitch.oauth_token, prefix=config.baalorbot.prefix, initial_channels=[config.twitch.channel], case_insensitive=True)
    for cmd in _to_add_twitch:
        TConn.add_command(cmd)
    load()

    esbot = EventSubBot.from_client_credentials(
        config.server.websocket_client.id,
        config.server.websocket_client.secret,
        prefix=config.baalorbot.prefix
    )
    TConn.esclient = EventSubClient(esbot, config.server.webhook.secret, f"{config.server.url}/eventsub")
    await TConn.eventsub_setup()

    glob = config.baalorbot.timers.globals
    if glob.interval and glob.commands:
        _timers["global"] = _global_timer = routine(seconds=glob.interval)(_timer)
        @_global_timer.before_routine
        async def before_global():
            await TConn.wait_for_ready()

        @_global_timer.error
        async def error_global(e):
            logger.error(f"Timer global error with {e}")

        if TConn.live_channels[config.twitch.channel]:
            await _global_timer.start(glob.commands, stop_on_error=False)

    sponsored = config.baalorbot.timers.sponsored
    if sponsored.interval and sponsored.commands:
        _timers["sponsored"] = _sponsored_timer = routine(seconds=sponsored.interval)(_timer)
        @_sponsored_timer.before_routine
        async def before_sponsored():
            await TConn.wait_for_ready()

        @_sponsored_timer.error
        async def error_sponsored(e):
            logger.error(f"Timer sponsored error with {e}")

        if TConn.live_channels[config.twitch.channel]:
            await _sponsored_timer.start(sponsored.commands, stop_on_error=False)

    await TConn.connect()

async def Twitch_cleanup():
    await TConn.close()

async def Discord_startup():
    global DConn
    DConn = DiscordConn(config.baalorbot.prefix, case_insensitive=True, owner_ids=config.baalorbot.owners, help_command=None)
    for cmd in _to_add_discord:
        DConn.add_command(cmd)
    await DConn.start(config.discord.oauth_token)

async def Discord_cleanup():
    await DConn.close()

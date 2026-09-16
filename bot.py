#!/usr/bin/env python3
"""Bot da Verdade - Discord fact-checking bot.

Two jobs:
  1. Automatically fact-check every X/Twitter link posted by a watched user.
  2. Fact-check on demand - reply to a suspicious message and @-mention the bot,
     use /factcheck, or right-click a message > Apps > Fact-check.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import time
from collections import OrderedDict, deque
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from factchecker.claude import FactChecker, FactCheckResult, detect_request_language
from factchecker.config import Config, ConfigError, load_config
from factchecker.limits import ALLOWED, NO_CHARGES, ChargeLimiter
from factchecker.media import (
    ImageBlock,
    MediaBundle,
    collect_from_tweet,
    collect_from_urls,
    ffmpeg_path,
)
from factchecker.statuses import status_for
from factchecker.render import (
    build_embed,
    button_label,
    detail_embed,
    error_embed,
    has_detail,
)
from factchecker.transcribe import Transcriber
from factchecker.twitter import Tweet, fetch_tweet, find_status_links, status_id_of
from factchecker.watchlist import Watchlist

log = logging.getLogger("botdaverdade")

MAX_CONCURRENT_CHECKS = 3
# Touched once a minute while the gateway is alive. The container healthcheck
# reads its timestamp, so a wedged bot shows as unhealthy rather than "running".
HEARTBEAT_FILE = Path(os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat"))
# How many trimmed verdicts stay clickable. Small on purpose - nobody expands a
# verdict from last week, and at 3 checks a day this is well over a week's worth.
LONG_VERDICT_MEMORY = 30
# The status rotates once a day. Lisbon, because that is where the joke lives.
STATUS_TZ = ZoneInfo("Europe/Lisbon")
MIDNIGHT_LISBON = dt_time(hour=0, minute=0, tzinfo=STATUS_TZ)
# Shown while a slash command or the right-click menu is working. Discord's stock
# "<bot> is thinking..." cannot be customised, so we post our own message and edit
# it into the verdict once it is ready.
THINKING = "Hm, deixa-me averiguar\N{HORIZONTAL ELLIPSIS} \N{FACE WITH MONOCLE}"
DEDUPE_MEMORY = 500


@asynccontextmanager
async def safe_typing(channel: discord.abc.Messageable):
    """channel.typing(), but a channel that refuses it is not a reason to give up."""
    handle = None
    try:
        handle = channel.typing()
        await handle.__aenter__()
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.debug("no typing indicator in %s: %s", channel, exc)
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                await handle.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - closing the indicator must never raise
                pass


class DetailButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"verdade:more:(?P<token>[0-9a-f]{16})",
):
    """Sends the long version of a verdict by DM.

    A DynamicItem rather than a plain button so it still works after a restart -
    discord.py rebuilds it from the custom_id. The verdicts themselves live in
    memory, so a restarted bot has forgotten the older ones and says so.
    """

    def __init__(self, token: str, label: str = "Read the full story") -> None:
        self.token = token
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji="\N{LEFT-POINTING MAGNIFYING GLASS}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"verdade:more:{token}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["token"], label=item.label or "Read the full story")

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        stored = getattr(bot, "long_verdicts", {}).get(self.token)
        if not stored:
            await interaction.response.send_message(
                "Já não tenho esta verificação guardada — o bot reiniciou entretanto.\n"
                "*I no longer have this one stored — the bot restarted since.*",
                ephemeral=True,
            )
            return

        result, subject_url = stored
        portuguese = result.language.lower() in ("pt", "pt-pt", "pt-br", "por")
        embed = detail_embed(result, bot_name=bot.config.bot_name, subject_url=subject_url)

        try:
            await interaction.user.send(embed=embed)
        except discord.Forbidden:
            # Their DMs are shut. Hand it over here instead, privately - and say
            # nothing about DMs, because none was delivered.
            log.info("%s has DMs closed - answering in the channel instead", interaction.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        except discord.HTTPException as exc:
            log.warning("could not DM %s: %s", interaction.user, exc)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Only reached when a DM genuinely landed.
        await interaction.response.send_message(
            "\N{ENVELOPE WITH DOWNWARDS ARROW ABOVE} Ups, escorreguei para as tuas DMs \N{SPEAK-NO-EVIL MONKEY}"
            if portuguese
            else "\N{ENVELOPE WITH DOWNWARDS ARROW ABOVE} I just slid into your DMs \N{SMIRKING FACE}",
            ephemeral=True,
        )


class SeenSet:
    """Small bounded 'have I already handled this' memory."""

    def __init__(self, size: int = DEDUPE_MEMORY) -> None:
        self._order: deque = deque(maxlen=size)
        self._items: set = set()

    def add(self, key) -> bool:
        """True if the key is new."""
        if key in self._items:
            return False
        if len(self._order) == self._order.maxlen and self._order:
            self._items.discard(self._order[0])
        self._order.append(key)
        self._items.add(key)
        return True


class BotDaVerdade(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.watchlist = Watchlist(config.watchlist_path, config.seed_watch_user_ids)
        self.checker = FactChecker(
            config.anthropic_api_key,
            model=config.model,
            max_searches=config.max_web_searches,
            reply_language=config.reply_language,
        )
        self.transcriber = Transcriber(
            config.transcribe_backend,
            openai_api_key=config.openai_api_key,
            openai_model=config.openai_transcribe_model,
            local_model=config.whisper_model,
            max_seconds=config.max_video_seconds,
            max_chars=config.max_transcript_chars,
            timeout=config.http_timeout,
        )
        self.limiter = ChargeLimiter(
            capacity=config.max_charges,
            refill_seconds=int(config.charge_refill_hours * 3600),
            cooldown_seconds=config.cooldown_seconds,
        )
        self.session: aiohttp.ClientSession | None = None
        self._gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
        self._seen_auto = SeenSet()
        self._seen_manual = SeenSet()
        # token -> full verdict text, for the "Ver mais" button. Deliberately small
        # and in-memory: nobody re-opens a verdict from last week, and this is not
        # worth a database. Older buttons still respond, they just say it's gone.
        self.long_verdicts: OrderedDict[str, tuple] = OrderedDict()

    def _detail_view(self, result, subject_url: str = "") -> discord.ui.View | None:
        """The 'see the full check' button, when there's a longer version to send."""
        if not has_detail(result):
            return None
        token = secrets.token_hex(8)
        self.long_verdicts[token] = (result, subject_url)
        while len(self.long_verdicts) > LONG_VERDICT_MEMORY:
            self.long_verdicts.popitem(last=False)
        view = discord.ui.View(timeout=None)
        view.add_item(DetailButton(token, label=button_label(result)))
        return view

    # ---------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        self.watchlist.load()
        if not self._heartbeat.is_running():
            self._heartbeat.start()
        if not self._rotate_status.is_running():
            self._rotate_status.start()
        log.info(
            "budget: %d charges, one back every %gh, %ds per-author cooldown",
            self.config.max_charges, self.config.charge_refill_hours,
            self.config.cooldown_seconds,
        )
        log.info(
            "media: images %s · video frames %s · audio %s",
            "on" if self.config.analyse_media and self.config.max_images else "off",
            "on" if ffmpeg_path() else "no ffmpeg",
            self.transcriber.describe(),
        )

        menu = app_commands.ContextMenu(name="Fact-check this", callback=self._context_menu_check)
        self.tree.add_command(menu)
        # Lets the "Saber mais" buttons on older messages keep working after a restart.
        self.add_dynamic_items(DetailButton)

        # Commands can be registered per-guild (instant) or globally (cached by
        # Discord for up to an hour). Doing BOTH publishes each command twice in
        # that guild, so this picks one.
        guild_only = self.config.dev_guild_id and not self.config.sync_global

        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "synced %d command(s) to guild %s, live immediately: %s",
                    len(synced), self.config.dev_guild_id, ", ".join(c.name for c in synced),
                )
            except discord.Forbidden:
                log.error(
                    "Discord refused to register commands in guild %s. The bot was invited "
                    "without the applications.commands scope - run `python invite.py` and "
                    "re-authorise with the URL it prints.",
                    self.config.dev_guild_id,
                )

        try:
            if guild_only:
                # Withdraw any global copies from an earlier run, so the commands
                # do not show up twice once Discord's cache catches up.
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                log.info("global commands cleared - this guild's copies are the only ones")
            else:
                synced = await self.tree.sync()
                log.info(
                    "synced %d global command(s) - Discord can take up to an hour to publish "
                    "them. Set DEV_GUILD_ID to skip that wait.",
                    len(synced),
                )
        except discord.Forbidden:
            log.error(
                "Discord refused the global command sync - the bot is missing the "
                "applications.commands scope. Run `python invite.py` and re-authorise."
            )

    async def _apply_status(self) -> None:
        """Set today's status. Discord shows the name without the verb in most
        places, so each line has to read on its own."""
        today = datetime.now(STATUS_TZ).date()
        line = status_for(today)
        if not line:
            return
        try:
            await self.change_presence(
                # Swap for discord.CustomActivity(name=line) if you'd rather have
                # no activity type at all - support for bots is patchy.
                activity=discord.Activity(type=discord.ActivityType.watching, name=line)
            )
            log.info("status for %s: %s", today.isoformat(), line)
        except discord.HTTPException as exc:
            log.warning("could not set the status: %s", exc)

    @tasks.loop(time=MIDNIGHT_LISBON)
    async def _rotate_status(self) -> None:
        await self._apply_status()

    @_rotate_status.before_loop
    async def _before_rotate(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=60)
    async def _heartbeat(self) -> None:
        try:
            HEARTBEAT_FILE.touch()
        except OSError as exc:
            log.debug("could not write the heartbeat file: %s", exc)

    @_heartbeat.before_loop
    async def _before_heartbeat(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        self._heartbeat.cancel()
        self._rotate_status.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, discord.Forbidden):
            log.error(
                "Discord refused an action in %s (%s). The bot is missing a permission in "
                "that channel - check View Channel, Send Messages, Embed Links and "
                "Read Message History on it.",
                event_method, exc.text or exc,
            )
            return
        log.exception("unhandled exception in %s", event_method)

    async def on_ready(self) -> None:
        log.info("logged in as %s (id %s), %d guild(s)", self.user, self.user.id, len(self.guilds))
        for guild in self.guilds:
            log.info("  in %s (id %s)", guild.name, guild.id)
        if not self.guilds:
            log.warning("not in any server yet - run `python invite.py` for the invite URL")
        elif not self.config.dev_guild_id:
            log.warning(
                "DEV_GUILD_ID is not set. Slash commands were synced globally, which Discord "
                "can take up to an hour to publish. Set DEV_GUILD_ID=%s in .env and restart "
                "to make them appear immediately.",
                self.guilds[0].id,
            )
        await self._apply_status()

    # ------------------------------------------------------------- core helpers

    async def _gather_tweets(self, urls: list[str]) -> list[Tweet]:
        assert self.session is not None
        results = await asyncio.gather(
            *(fetch_tweet(self.session, url, timeout=self.config.http_timeout) for url in urls[:2]),
            return_exceptions=True,
        )
        return [t for t in results if isinstance(t, Tweet)]

    @staticmethod
    def missing_permissions(channel: discord.abc.Messageable) -> list[str]:
        """Which permissions the bot still needs in this channel to answer here."""
        guild = getattr(channel, "guild", None)
        if guild is None or guild.me is None:
            return []  # DMs and group channels have no overwrites
        perms = channel.permissions_for(guild.me)
        needed = {
            "View Channel": perms.view_channel,
            "Embed Links": perms.embed_links,
            "Read Message History": perms.read_message_history,
        }
        if isinstance(channel, discord.Thread):
            needed["Send Messages in Threads"] = perms.send_messages_in_threads
        else:
            needed["Send Messages"] = perms.send_messages
        return [name for name, granted in needed.items() if not granted]

    def _blocked_here(self, channel: discord.abc.Messageable) -> bool:
        missing = self.missing_permissions(channel)
        if not missing:
            return False
        log.warning(
            "cannot answer in #%s (%s): missing %s. Grant them on the channel, or give the "
            "bot's role access to it.",
            getattr(channel, "name", channel.id),
            getattr(getattr(channel, "guild", None), "name", "?"),
            ", ".join(missing),
        )
        return True

    @staticmethod
    def _message_payload(message: discord.Message) -> str:
        """Everything worth checking in a message: text, embed text, attachment names."""
        parts: list[str] = []
        if message.content:
            parts.append(message.content)
        for embed in message.embeds:
            for value in (embed.title, embed.description):
                if value:
                    parts.append(value)
        for attachment in message.attachments:
            parts.append(f"[attachment: {attachment.filename} {attachment.url}]")
        return "\n".join(parts).strip()

    async def _collect_media(
        self, tweets: list[Tweet], message: discord.Message | None
    ) -> MediaBundle:
        """Pictures, video keyframes and speech from the linked posts, plus attachments."""
        cfg = self.config
        bundle = MediaBundle()
        if not cfg.analyse_media or self.session is None:
            return bundle
        if cfg.max_images <= 0 and not self.transcriber.enabled:
            return bundle

        for tweet in tweets:
            try:
                bundle.extend(await collect_from_tweet(
                    self.session, tweet,
                    budget=max(0, cfg.max_images - len(bundle.images)),
                    video_frames=cfg.video_frames,
                    max_video_bytes=cfg.max_video_bytes,
                    max_video_seconds=cfg.max_video_seconds,
                    timeout=cfg.http_timeout,
                    transcriber=self.transcriber,
                ))
            except Exception:  # noqa: BLE001 - media is a bonus, never a blocker
                log.exception("media extraction failed for %s", tweet.url)

        if message is not None and len(bundle.images) < cfg.max_images:
            attached = [
                (a.url, f'Image attached to the Discord message ("{a.filename}"):')
                for a in message.attachments
                if (a.content_type or "").startswith("image/")
            ]
            if attached:
                try:
                    bundle.images.extend(await collect_from_urls(
                        self.session, attached,
                        budget=cfg.max_images - len(bundle.images), timeout=cfg.http_timeout,
                    ))
                except Exception:  # noqa: BLE001
                    log.exception("could not read Discord attachments")

        bundle.images = bundle.images[: cfg.max_images]
        if bundle.images or bundle.transcripts:
            log.info(
                "attached %d image(s) and %d transcript(s) to the check",
                len(bundle.images), len(bundle.transcripts),
            )
        return bundle

    async def _build_subject(
        self, message: discord.Message
    ) -> tuple[str, str, list[ImageBlock], str]:
        """Content, primary URL, images and a language hint. Transcripts go in the content."""
        text = self._message_payload(message)
        urls = find_status_links(text)
        primary = urls[0] if urls else ""

        tweets = await self._gather_tweets(urls) if urls else []
        blocks: list[str] = [tweet.as_prompt_block() for tweet in tweets]

        stripped = text
        for url in urls:
            stripped = stripped.replace(url, "")
        stripped = " ".join(stripped.split())

        if stripped:
            author = message.author.display_name
            blocks.insert(0, f'Discord message from "{author}":\n{stripped}')
        elif not blocks:
            blocks.append(f'Discord message from "{message.author.display_name}": (no text)')

        bundle = await self._collect_media(tweets, message)
        blocks.extend(bundle.transcripts)
        return "\n\n".join(blocks), primary, bundle.images, self._language_hint(tweets)

    def _budget_message(self, reason: str, user_id: int) -> str:
        """What to tell someone whose check was refused."""
        if reason == NO_CHARGES:
            when = self.limiter.next_charge_hours_pt
            return (
                "🪫 Estou sem créditos sociais."
                + (f" Volto daqui a {when}." if when else "")
            )
        return (
            "\N{HOURGLASS WITH FLOWING SAND} Espera "
            f"{self.limiter.cooldown_remaining(user_id)}s."
        )

    async def _run_check(
        self,
        content: str,
        *,
        context: str = "",
        images: list[ImageBlock] | None = None,
        language_hint: str = "",
        asker_language: str = "",
    ) -> FactCheckResult:
        async with self._gate:
            return await self.checker.check(
                content,
                context=context,
                images=images,
                language_hint=language_hint,
                asker_language=asker_language,
            )

    @staticmethod
    def _language_hint(tweets: list[Tweet]) -> str:
        """X tells us the post's language - far more reliable than letting the model guess."""
        for tweet in tweets:
            if tweet.ok and tweet.lang and tweet.lang.lower() not in ("und", "qme", "zxx"):
                return tweet.lang
        return ""

    # -------------------------------------------------------------- auto checks

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or (self.user and message.author.id == self.user.id):
            return

        mentioned = bool(
            self.user
            and (f"<@{self.user.id}>" in message.content or f"<@!{self.user.id}>" in message.content)
        )
        if mentioned:
            await self._handle_mention(message)
            return

        if not self.watchlist.is_watched(
            message.channel.id,
            message.author.id,
            parent_id=getattr(message.channel, "parent_id", None),
        ):
            return

        urls = find_status_links(self._message_payload(message))
        if not urls:
            return
        if self._blocked_here(message.channel):
            return
        if not self._seen_auto.add((message.channel.id, status_id_of(urls[0]))):
            log.debug("skipping already-checked link in channel %s", message.channel.id)
            return

        reason = self.limiter.allow(message.author.id, automatic=True)
        if reason != ALLOWED:
            log.info("auto-check skipped for %s: %s budget", message.author, reason)
            return
        self.limiter.spend(message.author.id)

        log.info("auto-checking link from %s: %s", message.author, urls[0])
        async with safe_typing(message.channel):
            content, primary, images, hint = await self._build_subject(message)
            result = await self._run_check(
                content,
                context="This link was posted in a Discord chat. Check the post it points to.",
                images=images,
                language_hint=hint,
            )
        await self._respond(message, result, primary)

    # ------------------------------------------------------------ manual checks

    async def _handle_mention(self, message: discord.Message) -> None:
        if not self._seen_manual.add(message.id):
            return
        if self._blocked_here(message.channel):
            return

        ask = message.content
        if self.user:
            ask = ask.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "")
        ask = " ".join(ask.split())

        target: discord.Message | None = None
        if message.reference and message.reference.message_id:
            target = message.reference.resolved if isinstance(message.reference.resolved, discord.Message) else None
            if target is None:
                try:
                    target = await message.channel.fetch_message(message.reference.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("could not fetch replied-to message: %s", exc)

        if target is None and not find_status_links(ask) and len(ask) < 15:
            await message.reply(
                f"Responde a uma mensagem suspeita e menciona-me, ou usa `/factcheck`.\n"
                f"*Reply to a suspicious message and @-mention me, or use `/factcheck`.*",
                mention_author=False,
            )
            return

        reason = self.limiter.allow(message.author.id, automatic=False)
        if reason != ALLOWED:
            log.info("manual check refused for %s: %s budget", message.author, reason)
            await message.reply(
                self._budget_message(reason, message.author.id), mention_author=False
            )
            return
        self.limiter.spend(message.author.id)

        log.info("manual check requested by %s", message.author)
        async with safe_typing(message.channel):
            if target is not None:
                content, primary, images, hint = await self._build_subject(target)
            else:
                urls = find_status_links(ask)
                primary = urls[0] if urls else ""
                tweets = await self._gather_tweets(urls) if urls else []
                blocks = [t.as_prompt_block() for t in tweets]
                cleaned = ask
                for url in urls:
                    cleaned = cleaned.replace(url, "")
                if cleaned.strip():
                    blocks.insert(0, f"Statement to check:\n{cleaned.strip()}")
                bundle = await self._collect_media(tweets, message)
                blocks.extend(bundle.transcripts)
                content = "\n\n".join(blocks)
                images = bundle.images
                hint = self._language_hint(tweets)

            context = (
                f'A member of the server asked you to check this. Their words were: "{ask}". '
                "Focus on what they are questioning if that is clear."
                if ask
                else "A member of the server asked you to check this."
            )
            # Somebody who types a question in Portuguese wants an answer in
            # Portuguese, whatever language the post they're pointing at is in.
            result = await self._run_check(
                content,
                context=context,
                images=images,
                language_hint=hint,
                asker_language=detect_request_language(ask),
            )

        await self._respond(target or message, result, primary, ping_target=message)

    async def _context_menu_check(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        reason = self.limiter.allow(interaction.user.id, automatic=False)
        if reason != ALLOWED:
            await interaction.response.send_message(
                self._budget_message(reason, interaction.user.id), ephemeral=True
            )
            return
        await interaction.response.send_message(THINKING)
        self.limiter.spend(interaction.user.id)

        content, primary, images, hint = await self._build_subject(message)
        if not content.strip():
            await interaction.edit_original_response(
                content="Não há nada para verificar nessa mensagem."
            )
            return
        result = await self._run_check(
            content,
            context="A member of the server asked you to check this Discord message.",
            images=images,
            language_hint=hint,
        )
        embed = (
            error_embed(result.error, bot_name=self.config.bot_name)
            if result.failed
            else build_embed(result, bot_name=self.config.bot_name, subject_url=primary)
        )
        await interaction.edit_original_response(
            content=None, embed=embed, view=self._detail_view(result, primary)
        )

    # ------------------------------------------------------------------ replies

    async def _respond(
        self,
        message: discord.Message,
        result: FactCheckResult,
        primary_url: str,
        ping_target: discord.Message | None = None,
    ) -> None:
        embed = (
            error_embed(result.error, bot_name=self.config.bot_name)
            if result.failed
            else build_embed(result, bot_name=self.config.bot_name, subject_url=primary_url)
        )
        view = self._detail_view(result, primary_url)
        destination = ping_target or message
        kwargs = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        try:
            await destination.reply(mention_author=False, **kwargs)
        except discord.HTTPException as exc:
            log.error("could not reply: %s", exc)
            try:
                await destination.channel.send(**kwargs)
            except discord.HTTPException:
                log.error("could not send to channel either")


# ---------------------------------------------------------------- slash commands


def register_commands(bot: BotDaVerdade) -> None:
    @bot.tree.command(name="factcheck", description="Fact-check a link, a quote or a claim.")
    @app_commands.describe(subject="An X/Twitter link, or the claim you want checked.")
    async def factcheck(interaction: discord.Interaction, subject: str) -> None:
        reason = bot.limiter.allow(interaction.user.id, automatic=False)
        if reason != ALLOWED:
            await interaction.response.send_message(
                bot._budget_message(reason, interaction.user.id), ephemeral=True
            )
            return
        await interaction.response.send_message(THINKING)
        bot.limiter.spend(interaction.user.id)

        urls = find_status_links(subject)
        tweets = await bot._gather_tweets(urls) if urls else []
        blocks = [t.as_prompt_block() for t in tweets]
        remainder = subject
        for url in urls:
            remainder = remainder.replace(url, "")
        if remainder.strip():
            blocks.insert(0, f"Statement to check:\n{remainder.strip()}")
        bundle = await bot._collect_media(tweets, None)
        blocks.extend(bundle.transcripts)
        result = await bot._run_check(
            "\n\n".join(blocks) or subject,
            context="A member of the server asked you to check this directly.",
            images=bundle.images,
            language_hint=bot._language_hint(tweets),
            asker_language=detect_request_language(remainder),
        )
        embed = (
            error_embed(result.error, bot_name=bot.config.bot_name)
            if result.failed
            else build_embed(result, bot_name=bot.config.bot_name, subject_url=urls[0] if urls else "")
        )
        await interaction.edit_original_response(
            content=None, embed=embed, view=bot._detail_view(result, urls[0] if urls else "")
        )

    @bot.tree.command(
        name="watch",
        description="Auto-check every X link this user posts IN THIS CHANNEL.",
    )
    @app_commands.describe(user="The user to watch.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def watch(interaction: discord.Interaction, user: discord.User) -> None:
        if user.bot:
            await interaction.response.send_message("Não vale a pena vigiar bots.", ephemeral=True)
            return
        added = await bot.watchlist.add(interaction.channel_id, user.id)
        await interaction.response.send_message(
            f"\N{EYE}\N{VARIATION SELECTOR-16} \N{EYE}\N{VARIATION SELECTOR-16} "
            f"A partir de agora vou estar atento ao {user.mention}, neste chat."
            if added
            else f"\N{EYE}\N{VARIATION SELECTOR-16} Já estava de olho nele.",
            ephemeral=not added,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="unwatch",
        description="Stop auto-checking this user's X links in this channel.",
    )
    @app_commands.describe(user="The user to stop watching.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def unwatch(interaction: discord.Interaction, user: discord.User) -> None:
        removed = await bot.watchlist.remove(interaction.channel_id, user.id)
        note = ""
        if not removed and bot.watchlist.is_seeded(user.id):
            note = " Está fixado no `.env` (WATCH_USER_IDS), que se aplica a todos os chats — remove-o lá."
        await interaction.response.send_message(
            f"Parei de stalkar o {user.mention}."
            if removed
            else f"{user.mention} não estava a ser vigiado neste chat.{note}",
            ephemeral=not removed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="watchlist",
        description="Who is being auto-checked in this channel.",
    )
    @app_commands.guild_only()
    async def watchlist_cmd(interaction: discord.Interaction) -> None:
        ids = bot.watchlist.members(interaction.channel_id)
        if not ids:
            await interaction.response.send_message(
                "Ninguém a ser vigiado neste chat. Usa `/watch @alguém` aqui.",
                ephemeral=True,
            )
            return
        lines = [
            f"<@{uid}>" + (" *(fixo no .env, todos os chats)*" if bot.watchlist.is_seeded(uid) else "")
            for uid in ids
        ]
        await interaction.response.send_message(
            "\N{EYE}\N{VARIATION SELECTOR-16} Estou a policiar neste chat: " + ", ".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="status",
        description="Bot health, usage this hour, and whether it can post in this channel.",
    )
    async def status(interaction: discord.Interaction) -> None:
        cfg = bot.config
        # latency is NaN until the gateway heartbeat lands, and round(nan) raises.
        latency = bot.latency
        latency_text = f"{round(latency * 1000)}ms" if latency == latency else "—"
        charges = (
            f"**{bot.limiter.available}/{bot.limiter.capacity}** fact checks available"
        )
        if bot.limiter.next_charge_in_text:
            charges += f". {bot.limiter.next_charge_in_text} until the next charge"
        lines = [
            f"**{cfg.bot_name}** · model `{cfg.model}` · language `{cfg.reply_language}` · "
            f"latency {latency_text}",
            charges,
            f"Media: images `{'on' if cfg.analyse_media and cfg.max_images else 'off'}` · "
            f"video `{'on' if ffmpeg_path() else 'no ffmpeg'}` · "
            f"audio `{bot.transcriber.describe()}`",
        ]
        missing = bot.missing_permissions(interaction.channel) if interaction.channel else []
        if missing:
            lines.append(
                "\n\N{WARNING SIGN}\N{VARIATION SELECTOR-16} I can't reply in this channel. "
                "Missing: **" + "**, **".join(missing) + "**.\n"
                "Edit Channel → Permissions → add the bot's role with those."
            )
        else:
            lines.append("\n\N{WHITE HEAVY CHECK MARK} I can reply in this channel.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @watch.error
    @unwatch.error
    async def perms_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "Precisas da permissão **Gerir Servidor** para isso."
        else:
            log.exception("slash command failed", exc_info=error)
            message = "Correu mal alguma coisa. Vê os logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


MIN_PYTHON = (3, 10)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        running = ".".join(str(n) for n in sys.version_info[:3])
        print(
            f"Bot da Verdade needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer "
            f"(the Claude SDK does). You are on {running} at {sys.executable}.\n"
            "On macOS the system python3 is 3.9 - install a newer one:\n"
            "  brew install python@3.12\n"
            "  /opt/homebrew/bin/python3.12 -m venv .venv\n"
            "  source .venv/bin/activate && pip install -r requirements.txt\n"
            "Or skip Python entirely: docker compose up -d --build",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    bot = BotDaVerdade(config)
    register_commands(bot)

    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected the token. Check DISCORD_TOKEN in .env.", file=sys.stderr)
        return 3
    except discord.PrivilegedIntentsRequired:
        print(
            "Discord refused the MESSAGE CONTENT / SERVER MEMBERS intents.\n"
            "Enable MESSAGE CONTENT INTENT under Bot > Privileged Gateway Intents "
            "in the Discord Developer Portal.",
            file=sys.stderr,
        )
        return 4
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

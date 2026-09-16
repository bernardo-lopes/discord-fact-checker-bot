# Bot da Verdade

A Discord fact-checking bot powered by Claude with live web search.

Two features:

0. **Sees and hears the media.** Photos and screenshots go to Claude's vision, videos are
   sampled into keyframes, and what's said in them is transcribed.
   See [Images, video and audio](#images-video-and-audio).

1. **Automatic** — every X/Twitter link posted by a watched user gets fact-checked, unprompted.
2. **On demand** — reply to a suspicious message and `@Bot da Verdade`, or use `/factcheck`,
   or right-click a message → **Apps → Fact-check this**.

Replies are a compact embed: a colour-coded verdict, 2–3 lines of reasoning, and numbered
source links. Roughly five lines, so it doesn't bury the conversation.

```
✅  Verdade
Alegação: Portugal produced 71% of its electricity from renewables in 2024.
Correct. REN's 2024 annual data puts renewables at 71% of consumption, the
highest since 1978. The remaining 29% came from gas and imports.
Fontes / Sources  1. REN · 2. Reuters · 3. Público
Bot da Verdade · confiança: high · 3 pesquisas · verifica sempre as fontes
```

## Two lengths, one check

Every check produces two versions of the same answer:

- **The channel reply** — the claim, then the verdict, and nothing else. No sources, no
  footnotes. About five lines, so it doesn't bury the conversation.
- **The full check** — a sectioned DM: the claim, 📊 what the evidence shows, 🧭 context,
  ⚠️ caveats, and 🔗 the sources one per line. Written as four separate sections rather than
  one essay, so it can be skimmed.

Under the short version sits a **Saber mais** / **See the full check** button — wording follows
the verdict's language. Clicking it DMs the full version to that person alone. Nobody else sees
it, and the channel stays readable. If they have DMs from server members turned off, the bot
shows it to them privately in the channel instead.

The long versions live in memory — the last 30. After a restart, or thirty checks later, older
buttons say the verdict is no longer stored rather than failing silently. That's a deliberate
trade: nobody reopens a verdict from last week, and it isn't worth a database.

## Verdicts

| | Verdict | Meaning |
|---|---|---|
| ✅ | `TRUE` | Accurate and well supported |
| ✔️ | `MOSTLY_TRUE` | Right in substance, one caveat |
| ⚠️ | `MISLEADING` | Real facts, framed to imply something false |
| ❌ | `FALSE` | Contradicted by the evidence |
| ❔ | `UNVERIFIABLE` | No adequate evidence either way |
| 💬 | `OPINION` | A judgement or prediction, not a checkable claim |
| 🎭 | `SATIRE` | Parody account, not meant literally |

---

## Setup

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application**, name it *Bot da Verdade*.
2. **Bot** tab → **Reset Token** → copy it. This is `DISCORD_TOKEN`.
3. Still on the **Bot** tab, under **Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**.
   Without it the bot cannot read the links it is supposed to check.
4. **OAuth2 → URL Generator**: tick **both** scopes — `bot` *and* `applications.commands` —
   then bot permissions **View Channels**, **Send Messages**, **Embed Links**,
   **Read Message History**. Open the generated URL and invite it.

   Easier: once `.env` has the token, run `python invite.py` and it prints the exact URL.

   `applications.commands` is granted at invite time and **cannot be added later from
   inside the server** — no role or channel permission substitutes for it. Without it the
   bot joins and reads messages fine, but no slash commands appear and it never shows up
   under **Apps**. The fix is to open the invite URL again and re-authorise the same
   server; you don't need to kick it first.

### 2. Get a Claude API key

<https://console.anthropic.com> → API Keys. This is `ANTHROPIC_API_KEY`. Note this is a
Claude **API** key (pay-as-you-go, billed per call) — a Claude.ai subscription is a separate thing.

### 3. Configure

```bash
cp .env .env
$EDITOR .env          # paste both keys
```

### 4. Run

**Requires Python 3.10 or newer** — the Claude SDK does. macOS ships 3.9.6 as `python3`,
which fails with `Could not find a version that satisfies the requirement anthropic`.
Check with `python3 -V`.

```bash
brew install python@3.12          # only if python3 -V says 3.9.x
python3.12 -m venv .venv          # or python3 -m venv .venv if yours is already 3.10+
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Or skip Python entirely — the image brings its own:

```bash
docker compose up -d --build
docker compose logs -f
```

---

## Using it

| Command | Who | What |
|---|---|---|
| `/watch @user` | Manage Server | Auto-check that user's X links **in this channel** |
| `/unwatch @user` | Manage Server | Stop, in this channel |
| `/watchlist` | anyone | Who is being auto-checked in this channel |
| `/factcheck <link or claim>` | anyone | Check something directly |
| `/status` | anyone | Model, language, checks used this hour |
| `@Bot da Verdade` in a reply | anyone | Check the message you replied to |
| Right-click → Apps → Fact-check this | anyone | Same, without typing |

When you @-mention it in a reply you can add a question — *"@Bot da Verdade is the bit about
the 2019 vote true?"* — and it will aim at that specifically instead of picking its own claim.

**Watches are per-channel.** `/watch @someone` in #links-suspeitos only fires there — the same
person can post freely in #general without the bot chiming in. Run `/watch` again in each channel
where you want it. Threads inherit their parent channel's watches, so a discussion spun off a
watched channel stays covered.

The list lives in `data/watchlist.json`. `WATCH_USER_IDS` in `.env` is a separate, blunter
instrument: those users are checked in every channel of every server, and `/unwatch` can't remove
them — edit `.env` instead.

---

## Images, video and audio

A screenshot of a fabricated headline is the most common shape misinformation takes on X, and
text alone can't catch it. So the bot sends Claude what the post actually shows:

- **Photos** go straight to Claude's vision, requested at X's `medium` size (~1200px) so you
  aren't paying for pixels that add nothing.
- **Videos** get sampled — the Claude API takes images but not video, so `ffmpeg` pulls
  `VIDEO_FRAMES` evenly spaced keyframes (a 12s clip → frames at 2s, 6s, 10s), scaled to
  1280px. Enough to read on-screen text and see who's present.
- **Images attached to the Discord message itself** are read too, so replying to a mate's
  screenshot and mentioning the bot works.

Claude is told these are samples, not the whole video, so it shouldn't claim to know what
happens between frames.

`ffmpeg` is needed for video only:

```bash
brew install ffmpeg      # macOS. The Docker image already has it.
```

Without it the bot falls back to the video's thumbnail — degraded, never broken. Same for
videos over `MAX_VIDEO_SECONDS`, downloads over `MAX_VIDEO_MB`, and any mirror that won't
serve the file.

`MAX_IMAGES` is the hard ceiling per check (photos first, then frames). Set `MAX_IMAGES=0` or
`ANALYSE_MEDIA=false` to turn all of it off. Media adds roughly 1–2¢ to a check.

### Audio

A claim that is only spoken aloud never appears in the text or the frames, so the bot
transcribes a video's speech and hands that to Claude alongside everything else. Claude
has no audio input, so this goes through a separate transcriber. Two backends, one switch:

| `TRANSCRIBE_BACKEND` | What it does |
|---|---|
| `auto` (default) | OpenAI if `OPENAI_API_KEY` is set, else local if faster-whisper is installed, else off |
| `openai` | OpenAI's transcription API. Fast and accurate, ~0.3¢/min, needs the key |
| `local` | faster-whisper on your machine. Free and private, slower |
| `off` | Never transcribe |

`auto` means you can leave it alone: set no key and install nothing, and audio simply stays
off. Setting a key is what turns it on.

**Free and local:**

```bash
pip install -r requirements-local-whisper.txt
```

The model downloads itself on first use (`WHISPER_MODEL=base` is ~150 MB; `tiny` is faster
and worse, `small` slower and better). It's loaded once and kept in memory. On a Mac,
`base` transcribes a 60s clip in roughly 10–20 seconds — noticeable, so if the bot feels
sluggish in a busy server, that's the knob to change.

**Paid and fast:** put an `OPENAI_API_KEY` in `.env`. Default model is
`gpt-4o-mini-transcribe` at $0.003/min — about 0.3¢ for a one-minute clip, roughly a tenth
of what the fact-check itself costs.

Transcripts are machine-made, so Claude is told to treat names and numbers in them as
unreliable and confirm anything load-bearing against a source rather than the transcript's
spelling. A video with no audio track, a failed transcription, or an expired key all
degrade to no transcript rather than an error.

Run `/status` to see which backend is live — it reports `áudio: openai (...)`,
`local faster-whisper (base)` or `off`. The startup log says the same.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DISCORD_TOKEN` | — | Required |
| `ANTHROPIC_API_KEY` | — | Required |
| `CLAUDE_MODEL` | `claude-sonnet-5` | `claude-haiku-4-5-20251001` is cheaper, `claude-opus-5` sharper |
| `MAX_WEB_SEARCHES` | `5` | Searches per check. Main cost lever alongside the model |
| `REPLY_LANGUAGE` | `auto` | `auto` = Portuguese for Portuguese content, English for everything else; or force `pt` / `en` |
| `BOT_NAME` | `Bot da Verdade` | Shown in the embed footer |
| `DEV_GUILD_ID` | empty | Your server ID. Registers commands there instantly instead of Discord's ~1h global cache |
| `SYNC_GLOBAL` | `false` | Also publish commands globally. Only needed if the bot is in more than one server |
| `WATCH_USER_IDS` | empty | Comma-separated. Watched in *every* channel, ignoring `/unwatch` |
| `MAX_CHARGES` | `3` | Size of the charge pool. Every check spends one |
| `CHARGE_REFILL_HOURS` | `8` | One charge back this often while the pool isn't full |
| `COOLDOWN_SECONDS` | `30` | Minimum gap between *automatic* checks of the same author |
| `ANALYSE_MEDIA` | `true` | Look at images and video keyframes |
| `MAX_IMAGES` | `4` | Ceiling on images per check. `0` disables media |
| `VIDEO_FRAMES` | `3` | Keyframes sampled per video |
| `MAX_VIDEO_MB` | `25` | Bigger videos fall back to the thumbnail |
| `MAX_VIDEO_SECONDS` | `180` | Longer videos fall back to the thumbnail. Also caps transcribed audio |
| `TRANSCRIBE_BACKEND` | `auto` | `auto` / `openai` / `local` / `off` |
| `OPENAI_API_KEY` | empty | Only for the `openai` backend |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | $0.003/min |
| `WHISPER_MODEL` | `base` | Local model: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `MAX_TRANSCRIPT_CHARS` | `3000` | Longer transcripts are truncated |
| `HTTP_TIMEOUT` | `15` | Per-request timeout when fetching tweets |
| `LOG_LEVEL` | `INFO` | `DEBUG` when something misbehaves |

### Cost and rate limiting

The bot holds a pool of **fact-check charges**. Every check spends one — automatic links,
`@mentions`, `/factcheck` and the right-click menu alike. There is no unmetered path.

- **`MAX_CHARGES=3`** — the size of the pool.
- **`CHARGE_REFILL_HOURS=8`** — when the pool isn't full, one charge comes back this often.
  They refill one at a time, and the clock starts at the first spend, not the last.
- **The pool starts full every time the bot starts**, and isn't written to disk. Restart it
  and you have three again.
- **`COOLDOWN_SECONDS=30`** — a separate per-author cooldown that applies *only* to automatic
  checks, so one person posting three links in a row doesn't drain the pool instantly. It
  never blocks someone who deliberately asks; they get an answer or a clear refusal.

Run out and the bot replies `Fiquei sem verificações (0/3). A próxima fica pronta daqui a
6h 12m.` — refusals to a deliberate request are always visible, never silent. Automatic
checks that hit a limit are skipped quietly and logged instead, so the channel stays clean.
`/status` shows the pool at any time.

Steady state at the defaults is **3 checks per 24 hours**, roughly 12–18¢ a day at worst.
Each check is one Claude call plus its searches (web search is $10 per 1,000 searches on top
of tokens), plus 1–2¢ if there is media and a fraction of a cent for audio. To loosen it,
raise `MAX_CHARGES` or lower `CHARGE_REFILL_HOURS` — `MAX_CHARGES=10` with
`CHARGE_REFILL_HOURS=2` gives you about 12/day.

## How the tweet reading works

X blocks unauthenticated reads, so the bot pulls the post's text through the public
[FixTweet](https://github.com/FixTweet/FixTweet) JSON API (`api.fxtwitter.com`), falling back to
`api.vxtwitter.com`. No X API key, no scraping, no cost.

**This is the fragile part.** Those mirrors are volunteer-run and occasionally rate-limit or go
down. When every mirror fails the bot degrades rather than erroring: it hands the bare URL to
Claude and lets web search find what the post said, which usually works for anything that got
attention. Run `python test_fetch.py <url>` to check the mirrors from your machine.

If you outgrow it, `factchecker/twitter.py` is the only file that needs to change — swap
`_MIRRORS` for the official X API v2 (`/2/tweets/:id`, needs a paid Basic plan) and keep the
`Tweet` dataclass the same.

## Layout

```
bot.py                    Discord client, events, slash commands, rate limiting
factchecker/config.py     .env loading and validation
factchecker/twitter.py    X link detection + tweet retrieval
factchecker/media.py      Photos and ffmpeg keyframes, ready for Claude's vision
factchecker/transcribe.py Speech-to-text, OpenAI API or local faster-whisper
factchecker/claude.py     The Claude call, prompt, and JSON verdict parsing
factchecker/render.py     Verdict → Discord embed
factchecker/limits.py     The charge pool and the per-author cooldown
factchecker/statuses.py   The daily rotating Discord status
data/statuses.txt         Your status lines (gitignored; see statuses.example.txt)
factchecker/watchlist.py  Per-guild watchlist, persisted to data/watchlist.json
test_fetch.py             Offline sanity checks + live mirror test
```

## Troubleshooting

**Slash commands don't appear, and the bot isn't listed under Apps.**
It was invited without the `applications.commands` scope. Run `python invite.py`, open the
URL it prints, pick the same server, authorise. No need to kick it. Then set `DEV_GUILD_ID`
in `.env` and restart — the startup log should say `synced N command(s) to guild ...`.

**Commands registered but take ages to appear.** That's Discord caching global commands for
up to an hour. `DEV_GUILD_ID` sidesteps it entirely — guild commands are live immediately.
Restart Discord (Ctrl/Cmd+R) after a sync to refresh its local cache.

**Every command appears twice in the picker.** Commands were published both to your server
and globally, and Discord shows both. With `DEV_GUILD_ID` set the bot now registers to that
server only and actively withdraws the global copies — the log says `global commands
cleared`. If you genuinely run the bot in several servers, set `SYNC_GLOBAL=true` and drop
`DEV_GUILD_ID`, or accept the duplicates in your main server.

**A reply came back as raw JSON.** Fixed — but if you ever see it again, that's the
verdict parsing failing. The bot now asks for the verdict through a tool call, which the
API validates, so quotes inside a claim can't break it. A malformed JSON block is still
salvaged field by field, a truncated one is rejected outright, and nothing that looks like
a payload is ever posted to the channel — you get "Claude answered in a format I could not
read" instead. `LOG_LEVEL=DEBUG` shows the raw reply.

**It answered in the wrong language.** On `auto`, three things decide, in order:

1. **How you asked.** Ask in Portuguese — *"isto é mm verdade?"* — and you get Portuguese, even
   if the post is in English. Somebody who types a question chose a language, and that's the
   language the answer is for. Applies to `@mentions` and `/factcheck`.
2. **The post's language**, from X's own `lang` field, when nobody asked in words — an automatic
   check, or the right-click menu. Portuguese content gets Portuguese.
3. **English otherwise.** A German or French article is *checked* in German but *answered* in
   English, with quotations translated, so the whole server can read it.

`REPLY_LANGUAGE=en` or `pt` overrides all three.

**Nobody gets the DM.** The button DMs the full check to whoever clicks. If they have DMs from
server members switched off, Discord refuses and the bot shows the same thing privately in the
channel instead — nothing is lost, it just doesn't land in their inbox.

**The button says the verdict is gone.** Long versions are held in memory, the last 30 of them.
After a restart, or 30 checks later, older buttons say so rather than failing silently. Raise
`LONG_VERDICT_MEMORY` in `bot.py` if you want a deeper history.

**It stopped answering and says it has no charges left.** Working as intended — the pool is
empty. `/status` says when the next one lands. Restarting the bot refills it immediately, or
raise `MAX_CHARGES` / lower `CHARGE_REFILL_HOURS`.

**It stopped auto-checking after an update.** Watches used to be server-wide and are now
per-channel, and a guild id can't be turned into a channel id — so the old list can't be
migrated. The startup log says so outright. Re-run `/watch` in the channels you want.

**The bot is offline in the member list.** It isn't running, or the token is wrong. The log
prints `logged in as ...` and every server it can see when the connection is good.

**`403 Forbidden (error code: 50001): Missing Access`.**
The bot can see that channel but not act in it — a channel-level permission overwrite is
denying it. Channel → Edit Channel → Permissions → add the bot's role with **View Channel**,
**Send Messages**, **Embed Links**, **Read Message History** (plus **Send Messages in
Threads** if it's a thread). Run `/status` in the channel and it names exactly what's
missing. The bot now checks before spending a Claude call and logs the channel and the
missing permissions instead of raising.

**It ignores links but slash commands work.** MESSAGE CONTENT INTENT is off in the Developer
Portal, so `message.content` arrives empty. Enable it under Bot → Privileged Gateway Intents.

**Videos aren't being analysed.** Check `ffmpeg -version`. Without it the bot logs
`ffmpeg not installed - falling back to the video thumbnail` and uses the thumbnail instead.
`LOG_LEVEL=DEBUG` shows every media fetch.

**Audio isn't being transcribed.** Run `/status`. `áudio: off` means neither backend
resolved — set `OPENAI_API_KEY`, or `pip install -r requirements-local-whisper.txt`. If you
set `TRANSCRIBE_BACKEND` explicitly and the requirement is missing, the startup log says so
outright. A silent video, or one with no audio track, produces no transcript by design.

**`Could not find a version that satisfies the requirement anthropic`.** Your Python is older
than 3.10 — see step 4 above.

## The status

The bot's Discord status changes once a day at **midnight Lisbon time**, cycling through a
list so nothing comes round again for weeks.

The lines live in `data/statuses.txt`, one per line, blank lines and `#` comments ignored.
That file is **gitignored** — a server's in-jokes have no business in version control — so
copy the starter and write your own:

```bash
cp data/statuses.example.txt data/statuses.txt
```

Without it the bot falls back to a handful of dull built-ins, so a fresh clone still runs.

Two things worth knowing. Discord hides the activity verb in most places, so each line has to
read on its own — *"checking the sources"*, not *"sources"*. And the order matters: keep
lines from the same family far apart, **including the wrap from the last line back to the
first**, or the rotation feels repetitive. The choice is derived from the date, so restarting
the bot mid-afternoon doesn't change that day's line, and every line is used exactly once per
cycle.

## Security notes

**Nothing listens.** The bot opens no inbound ports — it only makes outbound connections to
Discord, Anthropic, and the tweet mirrors. There is no service here for anyone to reach from
the network, and the compose file deliberately has no `ports:` key. Don't add one.

**The container is kept weak.** It runs as a non-root user, drops all Linux capabilities, can't
gain new privileges, and is capped at 2 GB of memory and 256 processes. This matters because
the bot feeds attacker-influenced media through `ffmpeg` — a video linked from a post is
untrusted input parsed by a large C codebase. That's the one place where a bug could turn into
code execution, and containment is why it wouldn't get far.

**Nothing local is committed.** `.gitignore` covers `.env`, `data/watchlist.json` (real
Discord channel and user IDs) and `data/statuses.txt`. Check with `git status --ignored`
before your first push.

**`.env` is the crown jewel.** It holds a Discord bot token and a Claude API key in plain text.
Lock it down on whatever machine it lives on:

```bash
chmod 600 .env
```

If it ever leaks: reset the Discord token in the Developer Portal, and revoke the Claude key
in the console. Both are one click, and both are worth doing at the first hint of doubt.

**On a repurposed laptop**, the weak points are the machine, not the network:

- **Automatic login needs FileVault off**, so the disk — including `.env` — is readable to
  anyone who walks off with the laptop. Acceptable for a home machine that holds nothing else;
  not acceptable if it still has your personal files on it. Wipe it first.
- **An unsupported macOS gets no security patches.** This is the real long-term risk of an old
  Mac, and the reason to move to Ubuntu Server eventually.
- **Don't port-forward SSH** to the internet from your router. Everything here works over your
  home network. If you genuinely need access from outside, use Tailscale — it gives you a
  private network without opening anything to the world.
- **Use an SSH key, not a password** (`ssh-copy-id`), and the login stops being guessable.

## Licence

MIT — see `LICENSE`.

## Caveats

It is an LLM with a search engine, a pair of eyes and a pair of ears, not an oracle. It will occasionally be confidently wrong,
and the footer says so on every reply. Treat the verdict as a prompt to read the sources, not
as the last word — and expect the automatic mode to feel pointed if the watched person doesn't
know they're on the list.

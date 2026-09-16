# TODO

## Follow-up questions in DMs

Right now the DM is a dead end. Someone reads the full check, thinks *"what does 'net' mean
here?"*, types it — and nothing happens. The bot only listens in servers.

**What it should do:** treat a reply in the DM as a question about the check just sent, and
answer it with that check already in context.

**What it needs**

- `on_message` currently returns early for DMs. It would need a branch for
  `isinstance(message.channel, discord.DMChannel)`.
- A short conversation memory per user: the last check sent to them, plus the last few
  turns. Same shape as `long_verdicts` — bounded, in-memory, lost on restart.
- A different Claude call from `check()`: a conversational one that answers a question
  against the stored verdict and its sources, with web search available for genuinely new
  ground. It should not re-run a full fact-check.
- A charge decision. A follow-up is much cheaper than a check — probably free up to 3 or 4
  turns per verdict, then it costs a charge. Otherwise one curious person drains the pool.
- An end condition, so a stale thread doesn't answer a question about something from
  yesterday. Expire the context after ~30 minutes of silence.

**Worth deciding first:** whether follow-ups should also work in the channel (reply to the
bot's verdict and @-mention it). Same machinery, more noise, and it competes with the
existing mention handler.

## Smaller things

- The rescue call and the follow-up call would share a "talk to Claude without doing a full
  check" helper. Build it once.
- `data/watchlist.json` has no pruning — users who leave the server stay on the list.

"""Turn a FactCheckResult into a compact Discord embed (~5 visible lines)."""
from __future__ import annotations

import re

import discord

from .claude import FactCheckResult

# verdict -> (emoji, English label, Portuguese label, colour)
VERDICT_META: dict[str, tuple[str, str, str, int]] = {
    "TRUE":         ("\N{WHITE HEAVY CHECK MARK}", "True", "Verdade", 0x2ECC71),
    "MOSTLY_TRUE":  ("\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}", "Mostly true", "Quase verdade", 0x8BC34A),
    "MISLEADING":   ("\N{WARNING SIGN}\N{VARIATION SELECTOR-16}", "Misleading", "Enganador", 0xF1C40F),
    "FALSE":        ("\N{CROSS MARK}", "False", "Falso", 0xE74C3C),
    "UNVERIFIABLE": ("\N{WHITE QUESTION MARK ORNAMENT}", "Unverifiable", "Não verificável", 0x95A5A6),
    "OPINION":      ("\N{SPEECH BALLOON}", "Opinion, not fact", "Opinião, não facto", 0x5865F2),
    "SATIRE":       ("\N{PERFORMING ARTS}", "Satire", "Sátira", 0x9B59B6),
}

_PT = {"pt", "pt-pt", "pt-br", "por"}


def _label(verdict: str, language: str) -> tuple[str, str, int]:
    emoji, en, pt, colour = VERDICT_META.get(verdict, VERDICT_META["UNVERIFIABLE"])
    return emoji, (pt if language.lower() in _PT else en), colour


# How much of the summary the embed shows before it needs a "see more" button.
# Roughly four lines on a desktop client, which keeps the reply skimmable.
SUMMARY_BUDGET = 430
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")


def _truncate(text: str, limit: int) -> str:
    """Hard cut. For anything the reader sees, prefer _fit()."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fit(text: str, limit: int) -> tuple[str, bool]:
    """Trim to `limit`, preferring a sentence boundary. Returns (text, was_cut).

    Cutting mid-word leaves the ugly "and the figure com…" that makes a verdict
    look broken. Ending on the last complete sentence reads like a deliberate
    summary instead, and the rest is one click away.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text, False

    window = text[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(window)]
    # Only honour a sentence break if it keeps at least half the budget - otherwise
    # one long opening sentence would collapse the summary to almost nothing.
    if ends and ends[-1] >= limit * 0.5:
        return window[: ends[-1]].rstrip(), True

    space = window.rfind(" ")
    cut = window[:space] if space > limit * 0.5 else window[: limit - 1]
    return cut.rstrip().rstrip(",;:") + "…", True


def _tidy_multiline(text: str, limit: int) -> str:
    """Trim to `limit` while keeping the paragraph structure.

    _fit() flattens whitespace, which is right for a one-line summary and wrong
    for the long version - the blank lines between paragraphs are the formatting.
    """
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]

    # Collapse runs of blank lines down to one.
    tidied: list[str] = []
    for line in lines:
        if not line and (not tidied or not tidied[-1]):
            continue
        tidied.append(line)
    while tidied and not tidied[-1]:
        tidied.pop()

    out: list[str] = []
    used = 0
    for line in tidied:
        cost = len(line) + 1
        if used + cost > limit:
            break
        out.append(line)
        used += cost
    if not out:
        return _fit(text, limit)[0]
    return "\n".join(out).strip()


def has_detail(result: FactCheckResult) -> bool:
    """True when there is a longer write-up worth clicking through for."""
    if result.failed:
        return False
    return bool(result.evidence.strip() or result.context.strip() or result.caveats.strip())


def button_label(result: FactCheckResult) -> str:
    """The wording on the button, in the verdict's own language."""
    return "Saber mais" if result.language.lower() in _PT else "Read the full story"


def _sources_block(sources: list[dict]) -> str:
    """One source per line. _truncate would flatten the newlines, so don't use it."""
    lines = [
        f"**{index}.** [{_truncate(src['title'], 70)}]({src['url']})"
        for index, src in enumerate(sources, start=1)
    ]
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > 1020:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def detail_embed(
    result: FactCheckResult, *, bot_name: str, subject_url: str = ""
) -> discord.Embed:
    """The long version, sent by DM. Sectioned, so it can be skimmed."""
    emoji, label, colour = _label(result.verdict, result.language)
    pt = result.language.lower() in _PT

    embed = discord.Embed(title=f"{emoji}  {label}", colour=colour)
    if subject_url:
        embed.url = subject_url

    # The short verdict leads, so the DM opens with the answer.
    embed.description = _tidy_multiline(result.summary, 900)

    sections = [
        ("\N{LEFT-POINTING MAGNIFYING GLASS}  " + ("A alegação" if pt else "The claim"),
         result.claim),
        ("\N{BAR CHART}  " + ("O que a evidência mostra" if pt else "What the evidence shows"),
         result.evidence),
        ("\N{COMPASS}  " + ("Contexto" if pt else "Context"),
         result.context),
        ("\N{WARNING SIGN}\N{VARIATION SELECTOR-16}  " + ("Ressalvas" if pt else "Caveats"),
         result.caveats),
    ]
    for name, body in sections:
        body = (body or "").strip()
        if body:
            embed.add_field(name=name, value=_tidy_multiline(body, 1020), inline=False)

    if result.sources:
        embed.add_field(
            name="\N{LINK SYMBOL}  " + ("Fontes" if pt else "Sources"),
            value=_sources_block(result.sources),
            inline=False,
        )

    embed.set_footer(
        text=_truncate(
            f"{bot_name} · "
            + (
                "gerado por IA com pesquisa web — confirma nas fontes"
                if pt
                else "AI-generated with web search — check the sources"
            ),
            2040,
        )
    )
    return embed


def build_embed(result: FactCheckResult, *, bot_name: str, subject_url: str = "") -> discord.Embed:
    """The channel reply: claim, verdict, nothing else. Sources live behind the button."""
    emoji, label, colour = _label(result.verdict, result.language)
    pt = result.language.lower() in _PT

    embed = discord.Embed(title=f"{emoji}  {label}", colour=colour)

    blocks: list[str] = []
    if result.claim:
        blocks.append(f"**{'Alegação' if pt else 'Claim'}:** {_fit(result.claim, 170)[0]}")
    if result.summary:
        blocks.append(f"**{'Veredito' if pt else 'Verdict'}:** {_fit(result.summary, SUMMARY_BUDGET)[0]}")
    # Blank line between them, so the claim and the answer don't run together.
    embed.description = "\n\n".join(blocks) or ("Sem conclusão." if pt else "No conclusion reached.")

    footer = f"{bot_name} · {'confiança' if pt else 'confidence'}: {result.confidence}"
    if result.searches:
        footer += f" · {result.searches} " + ("pesquisas" if pt else "searches")
    embed.set_footer(text=_truncate(footer, 2040))

    if subject_url:
        embed.url = subject_url
    return embed


def error_embed(message: str, *, bot_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="\N{ELECTRIC PLUG}  Não consegui verificar",
        description=_truncate(message, 380),
        colour=0x4F545C,
    )
    embed.set_footer(text=bot_name)
    return embed

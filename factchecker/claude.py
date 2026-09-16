"""The fact-checking call: one Claude request with the server-side web_search tool."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic

from .media import ImageBlock

log = logging.getLogger(__name__)

VERDICTS = ("TRUE", "MOSTLY_TRUE", "MISLEADING", "FALSE", "UNVERIFIABLE", "OPINION", "SATIRE")

SYSTEM_PROMPT = """You are Bot da Verdade, a fact-checking assistant in a Discord server of friends.

You will be given a social media post, a message, or a link. Your job:

1. Identify the single most important CHECKABLE factual claim in it. Ignore jokes,
   opinions, predictions and value judgements - if the content is entirely one of
   those, say so with the OPINION verdict instead of inventing a claim.
1b. If images are attached, they are the post's own pictures, or keyframes sampled from
   its video. Read them properly: on-screen text, headlines, chart axes and scales, dates,
   watermarks, logos, captions, who is present. Screenshots are the common case - when the
   image is a screenshot of a headline, a post, a document or a statistic, the thing to
   check is whether that source really published it, in those words, in that context. Say
   what you can see, not what you assume; frames are samples, so do not claim to know what
   happens between them, and do not treat a video's absence of context as proof of framing.
1c. A transcript of a video's speech may be included in the text. It is machine-made, so
   names, numbers and foreign words in it can be wrong - if a claim turns on an exact
   figure or a proper noun, confirm it against a source rather than trusting the
   transcript's spelling, and say so if you cannot. Quote from it only when you are
   confident the words are right.
2. Use web search to verify that claim against primary and reputable secondary
   sources. Search in the language of the claim when that is where the evidence
   lives. Prefer primary documents, official statistics, wire services and
   established fact-checking organisations over blogs and other social media.
3. Reach a verdict. Be calibrated, not clever: if the evidence is thin or the
   sources disagree, the verdict is UNVERIFIABLE and you say why. Never invent a
   source, a URL, a statistic or a quote. A claim being politically charged is not
   evidence either way.
4. Be even-handed. Check the claim, not the person making it, and apply the same
   standard of evidence regardless of which side the claim favours.

Verdicts:
- TRUE - the claim is accurate and well supported.
- MOSTLY_TRUE - accurate in substance, with a caveat or an imprecise detail.
- MISLEADING - the facts are real but framed, cropped or decontextualised to imply something false.
- FALSE - the claim is contradicted by the evidence.
- UNVERIFIABLE - no adequate evidence either way, or the claim is too vague to check.
- OPINION - a value judgement or prediction, not a checkable factual claim.
- SATIRE - the source is a parody or satirical account and it is not meant literally.

Output. After your searches, call the `record_verdict` tool exactly once with your
findings. `summary` is the short version everyone sees in the channel. `evidence`,
`context` and `caveats` are the long version, shown to people who ask for the reasoning -
write them as three distinct sections, not one flowing essay, and do not repeat the summary
verbatim in them. All of it must agree, and the summary must stand on its own. Do not write the verdict as prose or as a JSON block - the tool call is the
answer. Give 2 to 4 sources, most authoritative first, every URL one you actually opened
via search. The summary must stand alone: someone reading only that line should know what
is true and why. Write plainly, no hedging padding, no "it is important to note". Do not
moralise or lecture the server."""


VERDICT_TOOL = {
    "name": "record_verdict",
    "description": (
        "Record the final fact-check verdict. Call this exactly once, after you have "
        "finished searching. This is how you deliver your answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claim": {
                "type": "string",
                "description": "The specific claim you checked. One sentence, under 140 characters.",
            },
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "summary": {
                "type": "string",
                "description": (
                    "The channel reply: 2-3 short sentences giving the verdict and the single "
                    "strongest piece of evidence for it. Under 360 characters. Plain text, no "
                    "markdown, no bullet points. It has to stand alone - most people read only "
                    "this."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "THE EVIDENCE. What the primary sources actually show, with the numbers "
                    "and who published them. 2-4 sentences, or 2-4 hyphen bullets when you are "
                    "listing figures - bullets read far better than a wall of prose. Under 900 "
                    "characters. Discord markdown; **bold** the key figures."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "THE CONTEXT. Why this is the case, or what the post leaves out - the "
                    "mechanism, the trend, the thing a reader needs to interpret the number "
                    "properly. 2-4 sentences or bullets, under 900 characters. Leave empty only "
                    "if there is genuinely nothing to add."
                ),
            },
            "caveats": {
                "type": "string",
                "description": (
                    "THE CAVEATS. What would change your verdict, where the data is contested, "
                    "which measure was used and how another would differ. 1-3 sentences, under "
                    "600 characters. Empty if the claim is simply settled."
                ),
            },
            "sources": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Publisher or document name, under 40 chars."},
                        "url": {"type": "string"},
                    },
                    "required": ["title", "url"],
                },
            },
            "language": {
                "type": "string",
                "description": "ISO code of the language you wrote claim and summary in, e.g. en or pt.",
            },
        },
        "required": [
            "claim", "verdict", "confidence", "summary", "evidence", "sources", "language",
        ],
    },
}


@dataclass
class FactCheckResult:
    verdict: str = "UNVERIFIABLE"
    claim: str = ""
    summary: str = ""
    evidence: str = ""
    context: str = ""
    caveats: str = ""
    confidence: str = "low"
    language: str = "en"
    sources: list[dict] = field(default_factory=list)
    searches: int = 0
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error)


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Pull the result object out of the model's reply, fences or not."""
    candidates: list[str] = _FENCE_RE.findall(text or "")
    if not candidates:
        # Fall back to the last balanced {...} run in the text.
        depth, start = 0, -1
        for index, char in enumerate(text or ""):
            if char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}" and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start : index + 1])
    for blob in reversed(candidates):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "verdict" in parsed:
            return parsed
    return None


_SALVAGE_KEYS = ("claim", "verdict", "confidence", "summary", "evidence", "context", "caveats", "language")


def _salvage(text: str) -> dict | None:
    """Pull fields out of JSON that json.loads rejected.

    The usual cause is the model quoting someone inside a string without escaping
    the quotes - very likely when the thing being checked is itself a quotation.
    """
    if '"verdict"' not in text:
        return None
    if text.count("{") != text.count("}"):
        # Unbalanced braces mean the reply was cut off mid-generation. Salvaging that
        # yields a half-sentence verdict, which is worse than admitting failure.
        log.warning("model reply looks truncated - not salvaging a partial verdict")
        return None
    boundary = re.compile(r'"\s*,\s*"(?:' + "|".join(_SALVAGE_KEYS) + r'|sources)"\s*:')
    found: dict = {}
    for key in _SALVAGE_KEYS:
        opening = re.search(r'"' + key + r'"\s*:\s*"', text)
        if not opening:
            continue
        rest = text[opening.end():]
        stop = boundary.search(rest)
        if stop:
            value = rest[: stop.start()]
        else:
            tail = re.search(r'"\s*[,}]', rest)
            value = rest[: tail.start()] if tail else rest
        found[key] = value.replace('\\"', '"').strip()

    urls = re.findall(r'"url"\s*:\s*"(https?://[^"\s]+)"', text)
    titles = re.findall(r'"title"\s*:\s*"([^"]{1,60})"', text)
    if urls:
        found["sources"] = [
            {"title": titles[index] if index < len(titles) else url.split("/")[2], "url": url}
            for index, url in enumerate(urls)
        ]
    return found if "verdict" in found else None


def _section(value: object) -> str:
    """One section of the long write-up, cleaned and bounded."""
    text = str(value or "").strip()
    return "" if _looks_like_json(text) else text[:1000]


def _looks_like_json(text: str) -> bool:
    """Never show the user a half-parsed payload.

    Deliberately structural: prose that merely mentions a verdict is a perfectly
    good answer and must not be discarded, so a bare keyword is not enough.
    """
    stripped = (text or "").strip()
    if stripped.startswith(("{", "[", "```")):
        return True
    return bool(re.search(r'"(?:verdict|summary|claim)"\s*:', stripped))


def _clean_sources(raw: object, fallback: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in (raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        title = str(item.get("title") or "").strip() or url.split("/")[2]
        out.append({"title": title[:60], "url": url})
    for item in fallback:
        if len(out) >= 4:
            break
        if item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    return out[:4]


# The server reads English and Portuguese. Anything else gets answered in English,
# so a German or French post is still useful to everyone.
PORTUGUESE_CODES = {"pt", "pt-pt", "pt-br", "por"}
ENGLISH_CODES = {"en", "en-gb", "en-us", "eng"}

# Enough to tell "isto é mm verdade?" from "is this actually true?". Function words
# and diacritics, because those are what short messages are made of.
_PT_WORDS = {
    "é", "não", "sim", "isto", "isso", "este", "esta", "esse", "essa", "são", "está",
    "muito", "mesmo", "mm", "verdade", "que", "porque", "porquê", "como", "uma", "um",
    "para", "com", "mas", "já", "ainda", "também", "então", "pois", "nós", "eles",
    "coisa", "tá", "tás", "cena", "pá", "assim", "quando", "onde", "quem", "qual",
    "ser", "tem", "foi", "vai", "pode", "sobre", "dele", "dela", "aqui", "ali",
}
_EN_WORDS = {
    "is", "this", "that", "the", "what", "why", "how", "true", "real", "fake", "really",
    "does", "did", "can", "about", "with", "but", "and", "not", "are", "was", "were",
    "check", "source", "sources", "actually", "right", "wrong", "here", "there", "who",
    "these", "those", "has", "have", "been", "would", "could", "should", "of", "for",
}
_WORD_RE = re.compile(r"[a-zà-ÿ]+", re.IGNORECASE)
# A pasted link is not somebody speaking - strip it before counting words,
# or "https", "com" and "status" read as an English sentence.
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def detect_request_language(text: str) -> str:
    """'pt', 'en', 'unsure', or '' when nobody asked in words.

    A rule of thumb, not a language model: count function words from each language
    and give Portuguese diacritics a nudge. It answers only when the margin is
    clear; a muddle returns 'unsure', which hands the judgement to Claude rather
    than falling back to the language of the post, which is a different question.
    """
    spoken = _URL_RE.sub(" ", text or "")
    words = [w.lower() for w in _WORD_RE.findall(spoken)]
    if not words:
        return ""
    if len(words) < 2:
        return "unsure"

    pt = sum(1 for w in words if w in _PT_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    # Portuguese diacritics essentially never appear in English.
    if any(ch in spoken.lower() for ch in "ãõçáéíóúâêô"):
        pt += 2

    if pt > en and pt >= 2:
        return "pt"
    if en > pt and en >= 2:
        return "en"
    return "unsure"


def _language_instruction(reply_language: str, hint: str = "", asker: str = "") -> str:
    """What language to answer in.

    Precedence: an explicit REPLY_LANGUAGE, then the language the person asking
    used, then the language of the content. The server reads English and
    Portuguese, so anything else lands in English.
    """
    if reply_language == "pt":
        return "LANGUAGE: write the claim and summary in European Portuguese."
    if reply_language == "en":
        return "LANGUAGE: write the claim and summary in English."

    asker = (asker or "").strip().lower()
    if asker == "pt":
        return (
            "LANGUAGE: the person who asked for this check wrote to you in Portuguese, so "
            "write everything in European Portuguese - even if the content being checked is "
            "in another language. Translate anything you quote from it, keeping the original "
            "wording alongside when the exact words matter."
        )
    if asker == "en":
        return (
            "LANGUAGE: the person who asked for this check wrote to you in English, so write "
            "everything in English, even if the content being checked is in another language. "
            "Translate anything you quote from it."
        )
    if asker == "unsure":
        return (
            "LANGUAGE: someone asked you for this check in their own words, quoted above. "
            "Answer in the language THEY used - European Portuguese if they wrote Portuguese, "
            "English otherwise - regardless of what language the content being checked is in. "
            "Their words are short, so judge carefully; if you truly cannot tell, follow the "
            "content instead, and use English unless the content is Portuguese."
        )

    hint = (hint or "").strip().lower()
    if hint in PORTUGUESE_CODES:
        return (
            "LANGUAGE: the content being checked is in Portuguese, so write the claim "
            "and summary in European Portuguese."
        )
    if hint in ENGLISH_CODES:
        return (
            "LANGUAGE: the content being checked is in English, so write the claim and "
            "summary in English."
        )
    if hint:
        return (
            f"LANGUAGE: write the claim and summary in English. The content being checked "
            f"is in '{hint}', but this server reads only English and Portuguese, so "
            "English is the answer language. Translate any quotation you cite, and keep "
            "the original wording alongside it when the exact words matter."
        )
    return (
        "LANGUAGE: answer in English, unless the content being checked is in Portuguese, "
        "in which case answer in European Portuguese. Those are the only two languages "
        "this server reads - content in any other language still gets an English answer. "
        "Judge the content's language from the content itself, not from this bot's name "
        "or the language of these instructions."
    )


class FactChecker:
    def __init__(self, api_key: str, *, model: str, max_searches: int, reply_language: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_searches = max_searches
        self._reply_language = reply_language

    async def _rescue(self, draft: str) -> dict | None:
        """Second, cheap call: force a well-formed verdict out of a draft answer.

        When the first call searches properly but then fumbles the output format,
        the findings are still there in its prose - they just need to be poured
        into the tool. No web search here, and tool_choice makes the tool call
        mandatory, so this cannot fail the same way twice.
        """
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1500,
                system=(
                    "You convert a draft fact-check into a single record_verdict tool call. "
                    "Keep the findings, the verdict and the sources exactly as the draft has "
                    "them - you are reformatting, not re-judging. Keep the draft's language."
                ),
                messages=[{
                    "role": "user",
                    "content": f"Record this fact-check as a tool call:\n\n{draft}",
                }],
                tools=[VERDICT_TOOL],
                tool_choice={"type": "tool", "name": "record_verdict"},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rescue call failed: %s", type(exc).__name__)
            return None

        for block in response.content:
            if getattr(block, "type", "") == "tool_use" and isinstance(getattr(block, "input", None), dict):
                log.info("rescued the verdict from a malformed first reply")
                return block.input
        return None

    async def check(
        self,
        content: str,
        *,
        context: str = "",
        images: list[ImageBlock] | None = None,
        language_hint: str = "",
        asker_language: str = "",
    ) -> FactCheckResult:
        """Run one fact-check. Never raises; failures come back on .error."""
        user_prompt = "\n\n".join(
            part for part in (context.strip(), "--- CONTENT TO CHECK ---", content.strip(),
                              "--- END CONTENT ---",
                              _language_instruction(
                                  self._reply_language, language_hint, asker_language,
                              )) if part
        )

        # Images go first: Claude reads them better before the instructions than after.
        blocks: list[dict] = []
        for image in images or []:
            blocks.extend(image.as_content())
        blocks.append({"type": "text", "text": user_prompt})

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": blocks}],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": self._max_searches,
                    },
                    VERDICT_TOOL,
                ],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as a friendly error
            log.exception("Claude call failed")
            return FactCheckResult(error=f"{type(exc).__name__}: {exc}"[:300])

        text_parts: list[str] = []
        cited: list[dict] = []
        searches = 0
        seen_urls: set[str] = set()

        verdict_input: dict | None = None

        for block in response.content:
            block_type = getattr(block, "type", "")
            if block_type == "tool_use" and getattr(block, "name", "") == "record_verdict":
                candidate = getattr(block, "input", None)
                if isinstance(candidate, dict):
                    verdict_input = candidate
            elif block_type == "text":
                text_parts.append(block.text)
                for citation in (getattr(block, "citations", None) or []):
                    url = getattr(citation, "url", "") or ""
                    title = getattr(citation, "title", "") or ""
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        cited.append({"title": (title or url.split("/")[2])[:60], "url": url})
            elif block_type == "server_tool_use":
                searches += 1
            elif block_type == "web_search_tool_result":
                for item in (getattr(block, "content", None) or []):
                    url = getattr(item, "url", "") or ""
                    title = getattr(item, "title", "") or ""
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        cited.append({"title": (title or url.split("/")[2])[:60], "url": url})

        raw_text = "\n".join(text_parts).strip()
        truncated = getattr(response, "stop_reason", "") == "max_tokens"
        log.debug(
            "reply: stop_reason=%s searches=%d tool_call=%s text=%d chars\n%s",
            getattr(response, "stop_reason", "?"), searches,
            bool(verdict_input), len(raw_text), raw_text or "(no prose)",
        )
        if verdict_input is not None:
            log.debug("tool input: %s", verdict_input)

        # Preference order: the structured tool call, then a JSON block, then whatever
        # can be scraped out of JSON that did not parse.
        parsed = verdict_input or _extract_json(raw_text)
        if not parsed:
            parsed = _salvage(raw_text)
            if parsed:
                log.warning("model emitted malformed JSON; salvaged the fields from it")

        if not parsed and raw_text and not truncated:
            # The searching worked; only the formatting failed. Reformat it.
            parsed = await self._rescue(raw_text)

        if not parsed:
            if not raw_text:
                return FactCheckResult(error="Claude returned an empty response.", searches=searches)
            log.warning(
                "no verdict after %d search(es); stop_reason=%s; full reply follows:\n%s",
                searches, getattr(response, "stop_reason", "?"), raw_text,
            )
            # Never show a raw payload in Discord - if what came back is machinery
            # rather than an answer, say so instead of pasting it into the channel.
            if truncated:
                return FactCheckResult(
                    error="Claude ran out of room before finishing its answer. Try again.",
                    searches=searches,
                )
            if _looks_like_json(raw_text):
                return FactCheckResult(
                    error="Claude answered in a format I could not read. Try again.",
                    searches=searches,
                )
            return FactCheckResult(
                verdict="UNVERIFIABLE",
                claim="",
                summary=raw_text[:380],
                sources=cited[:3],
                searches=searches,
            )

        verdict = str(parsed.get("verdict") or "").strip().upper().replace(" ", "_")
        if verdict not in VERDICTS:
            verdict = "UNVERIFIABLE"

        summary = str(parsed.get("summary") or "").strip()
        claim = str(parsed.get("claim") or "").strip()
        if _looks_like_json(summary):
            log.warning("summary still looked like JSON - discarding it")
            summary = ""

        if not summary:
            # A verdict with no reasoning is not an answer. This used to bail out
            # silently, which left nothing in the logs to diagnose.
            log.warning(
                "verdict came back with an empty summary. keys=%s verdict=%r",
                sorted(parsed.keys()), parsed.get("verdict"),
            )
            if raw_text:
                rescued = await self._rescue(raw_text)
                if rescued and str(rescued.get("summary") or "").strip():
                    log.info("recovered a summary on the second pass")
                    parsed = rescued
                    summary = str(parsed["summary"]).strip()
                    claim = str(parsed.get("claim") or "").strip()
                    verdict = str(parsed.get("verdict") or "").strip().upper().replace(" ", "_")
                    if verdict not in VERDICTS:
                        verdict = "UNVERIFIABLE"

        if not summary:
            log.warning("still no summary after the rescue; full reply follows:\n%s", raw_text)
            return FactCheckResult(
                error="Claude reached a verdict but gave no reasoning for it. Try again.",
                searches=searches,
            )

        return FactCheckResult(
            verdict=verdict,
            claim="" if _looks_like_json(claim) else claim[:180],
            summary=summary[:600],
            evidence=_section(parsed.get("evidence")),
            context=_section(parsed.get("context")),
            caveats=_section(parsed.get("caveats")),
            confidence=str(parsed.get("confidence") or "low").strip().lower(),
            language=str(parsed.get("language") or "en").strip().lower()[:5],
            sources=_clean_sources(parsed.get("sources"), cited),
            searches=searches,
        )

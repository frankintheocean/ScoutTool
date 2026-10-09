"""On-demand BetterBanned activity snapshots, also accepting copied page text."""
import asyncio
from datetime import date
from html.parser import HTMLParser
import re

import aiohttp

MAX_TEXT = 65536
MAX_RESPONSE = 1024 * 1024
_slots = asyncio.Semaphore(2)


class ProviderError(ValueError):
    pass


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'head'}:
            self.hidden.append(tag)
        elif not self.hidden and tag in {'br', 'p', 'div', 'li', 'tr', 'section', 'h1', 'h2', 'h3', 'h4'}:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        else:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def profile_url(username):
    if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_]{1,25}', username):
        raise ValueError('Invalid Twitch username.')
    return 'https://betterbanned.com/en/streamer/' + username.lower()


def parse_activity(text, *, html=False):
    if html:
        parser = _VisibleText()
        parser.feed(text)
        text = ''.join(parser.parts)
    text = re.sub(r'\s+', ' ', text).strip()
    if re.search(r'just a moment|verify you are human|checking your browser', text, re.I):
        raise ProviderError('BetterBanned requires a browser verification. Open its page and copy the activity text instead. Saved history was preserved.')
    heading = re.search(r'\bRecent Activity\b', text, re.I)
    if heading is None:
        raise ProviderError('No Recent Activity section was found. Copy its heading and dated rows from BetterBanned; saved history was preserved.')
    count = re.search(r'\bTotal Bans\s*:\s*(\d{1,9})\b', text[:heading.start()], re.I)
    section = re.split(r'\b(?:Socials|Find similar streamer names|Streamer Profile)\b', text[heading.end():], maxsplit=1, flags=re.I)[0]
    matches = list(re.finditer(r'\b\d{4}-\d{2}-\d{2}\b', section))
    if len(matches) > 500:
        raise ProviderError('Activity is limited to 500 rows per snapshot.')
    events = []
    for i, match in enumerate(matches):
        try:
            date.fromisoformat(match.group())
        except ValueError as exc:
            raise ProviderError('An activity row has an invalid date; saved history was preserved.') from exc
        description = section[match.end():matches[i+1].start() if i+1 < len(matches) else len(section)].strip(' |')
        if not description or len(description) > 2000:
            raise ProviderError('An activity row is missing details or is too long; saved history was preserved.')
        if re.search(r'\bunbanned\b', description, re.I):
            kind = 'unbanned'
        elif re.search(r'\bbanned\b', description, re.I):
            kind = 'banned'
        elif re.search(r'profile\s+(?:pic|picture)\s+changed', description, re.I):
            kind = 'profile_picture'
        elif re.search(r'offline\s+(?:pic|picture)\s+changed', description, re.I):
            kind = 'offline_picture'
        else:
            kind = 'other'
        event = {'date': match.group(), 'kind': kind, 'description': description}
        if event not in events:
            events.append(event)
    if not events and not (count and int(count.group(1)) == 0) and not re.search(r'no (?:recent )?activity', section, re.I):
        raise ProviderError('No dated activity was found. Copy the full Recent Activity section; saved history was preserved.')
    return {'total_bans': int(count.group(1)) if count else None, 'events': events}


async def fetch_activity(username):
    url = profile_url(username)
    try:
        # Includes queue time so repeated requests cannot wait indefinitely.
        async with asyncio.timeout(20):
            async with _slots:
                async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=15)) as session:
                    async with session.get(url, allow_redirects=False, headers={'Accept': 'text/html', 'User-Agent': 'ScoutTool activity lookup'}) as response:
                        if response.status in {403, 429}:
                            raise ProviderError('BetterBanned blocked or rate-limited this request. Open its page and copy the activity text instead. Saved history was preserved.')
                        if response.status != 200:
                            raise ProviderError(f'BetterBanned returned HTTP {response.status}. Saved history was preserved.')
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(16384):
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE:
                                raise ProviderError('BetterBanned response was too large. Saved history was preserved.')
                        text = data.decode(response.charset or 'utf-8', errors='replace')
                        return await asyncio.to_thread(parse_activity, text, html=True)
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise ProviderError('BetterBanned could not be reached. Retry or copy its page text; saved history was preserved.') from exc

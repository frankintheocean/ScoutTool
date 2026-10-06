"""Failure, scalability, and zero-viewer discovery tests; no external network."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_regressions as fixtures
import discover_cache
import main
import nobody_discovery as nobody
import scoring
import twitch_api as twitch


class Content:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]


class Response:
    def __init__(self, status=200, payload=None, headers=None, body=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        self.content = Content(json.dumps(payload).encode() if body is None else body)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def __aenter__(self):
        self.closed = False
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses)


def stream(username='alice', game='Mario', tags=None, viewers=0):
    return {'user_login': username, 'user_name': username.upper(),
            'game_name': game, 'tags': tags, 'viewer_count': viewers,
            'title': 'A stream', 'started_at': '2026-10-06T00:00:00Z'}


class LocalTests(unittest.TestCase):
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)

    def test_cache_update_at_capacity_preserves_other_entries(self):
        now = time.time()
        cache = {str(i): {'timestamp': now, 'value': i} for i in range(3)}
        with patch.object(twitch, '_CACHE_MAX_ENTRIES', 3):
            twitch._store_cache(cache, '2', {'timestamp': now, 'value': 99}, 3600)
            self.assertEqual(set(cache), {'0', '1', '2'})
            twitch._store_cache(cache, '3', {'timestamp': now, 'value': 3}, 3600)
            self.assertEqual(set(cache), {'1', '2', '3'})

    def test_cache_expiry_is_swept_without_scanning_on_every_update(self):
        class CountingCache(dict):
            scans = 0
            def __iter__(self):
                self.scans += 1
                return super().__iter__()
        cache = CountingCache({str(i): {'timestamp': 100, 'value': i} for i in range(128)})
        with patch.dict(twitch._cache_write_counts, {}, clear=True), patch.object(twitch.time, 'time', return_value=200):
            for _ in range(256):
                twitch._store_cache(cache, '0', {'timestamp': 200, 'value': 1}, 10)
        self.assertEqual(set(cache.keys()), {'0'})
        self.assertLessEqual(cache.scans, 2)

    def test_discover_cache_refresh_at_capacity_does_not_evict_a_peer(self):
        discover_cache.invalidate_all()
        try:
            for i in range(100):
                discover_cache.set_cached_discover(i, category=str(i))
            discover_cache.set_cached_discover('updated', category='99')
            self.assertEqual(discover_cache.get_cached_discover(category='0'), 0)
            self.assertEqual(discover_cache.get_cached_discover(category='99'), 'updated')
            self.assertEqual(len(discover_cache._cache), 100)
        finally:
            discover_cache.invalidate_all()

    def test_top_k_preserves_ties_and_legacy_limit_slicing(self):
        rows = [{'username': name, 'score': score, 'average_viewers': 1}
                for name, score in [('a', 10), ('b', 9), ('c', 10), ('d', 1), ('e', 10)]]
        expected = ['a', 'c', 'e', 'b', 'd']
        with patch.object(scoring, 'get_all', return_value=rows), \
             patch.object(scoring, 'get_average_viewers_bulk', return_value={}), \
             patch.object(scoring, 'calculate_raid_score', new=lambda row, _: row['score']):
            for limit in (0, 1, 3, 50, None, -1, -50):
                with self.subTest(limit=limit):
                    self.assertEqual([r['username'] for r in scoring.get_raid_candidates(limit)], expected[:limit])
            for limit in (0.0, 3.2, '3', {}):
                with self.subTest(limit=limit), self.assertRaises(TypeError):
                    scoring.get_raid_candidates(limit)

    def test_download_result_retention_starts_at_completion_and_preserves_trim_precision(self):
        outputs = []
        class Downloader:
            def __init__(self, opts):
                outputs.append(opts['outtmpl'])
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _): return 0
        from types import SimpleNamespace
        with patch.dict(main.VOD_DOWNLOAD_JOBS, {}, clear=True), \
             patch.dict(fixtures.sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=Downloader)}), \
             patch.object(main, '_downloads_dir', return_value=fixtures.Path(fixtures.DATA.name)), \
             patch.object(main, '_fastest_download_opts', return_value={}):
            for index, start in enumerate((10.000001, 10.000002)):
                job_id = str(index)
                main.VOD_DOWNLOAD_JOBS[job_id] = {'id': job_id, 'status': 'queued', '_created_ts': time.time() - 7200}
                main._run_vod_download(job_id, 'https://twitch.tv/videos/123', start, None)
            self.assertNotEqual(outputs[0], outputs[1])
            main.prune_stale_vod_jobs()
            self.assertEqual(len(main.VOD_DOWNLOAD_JOBS), 2)
            self.assertNotIn('_finished_ts', main.get_vod_download('0'))
            with patch.object(main.time, 'time', return_value=time.time() + 3601):
                main.prune_stale_vod_jobs()
            self.assertEqual(main.VOD_DOWNLOAD_JOBS, {})

    def test_download_numeric_overflow_returns_a_validation_error(self):
        response = self.client.post('/api/vod-downloads', json={'url': 'https://twitch.tv/videos/123', 'start_time': 10 ** 400})
        self.assertEqual(response.status_code, 400)

    def test_zero_viewer_endpoint_validation_tracking_blacklist_and_cache_control(self):
        fixtures.db.add_streamer(['alice', 'https://twitch.tv/alice'])
        fixtures.db.add_to_blacklist('bob')
        result = [dict(username='alice', live_viewers=0), dict(username='carol', live_viewers=0)]
        with patch.object(nobody, 'search_zero_viewers', AsyncMock(return_value=result)) as search:
            response = self.client.get('/api/discover/zero-viewers?include=mario&match=any')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertTrue(response.json()['items'][0]['already_tracked'])
            self.assertFalse(response.json()['items'][1]['already_tracked'])
            self.assertEqual(search.call_args.kwargs['blacklist'], {'bob'})
            self.assertEqual(search.call_args.args, ('mario', 'any', 25))
            for query in ('limit=0', 'limit=66', 'match=none', 'include=' + 'x' * 66):
                self.assertEqual(self.client.get('/api/discover/zero-viewers?' + query).status_code, 422)
            self.assertEqual(search.await_count, 1)

    def test_zero_viewer_endpoint_outage_is_actionable(self):
        with patch.object(nobody, 'search_zero_viewers', AsyncMock(side_effect=nobody.NobodyDiscoveryError('Try again', 503, 5))):
            response = self.client.get('/api/discover/zero-viewers')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers['retry-after'], '5')
            self.assertIn('Try again', response.json()['detail'])

    def test_zero_viewer_blacklist_change_during_provider_fetch_is_respected(self):
        with patch.object(main.db, 'get_blacklist_set', side_effect=[set(), {'alice'}]), \
             patch.object(nobody, 'search_zero_viewers', AsyncMock(return_value=[{'username': 'alice', 'live_viewers': 0}])):
            response = self.client.get('/api/discover/zero-viewers')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'], [])


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.slots = patch.object(nobody, '_REQUEST_SLOTS', asyncio.Semaphore(2))
        self.slots.start()
        self.addCleanup(self.slots.stop)

    async def test_bulk_scrape_uses_eight_workers_and_counts_partial_failures(self):
        rows = [{'username': 'u' + str(i)} for i in range(1000)]
        calls = []; tasks = set(); active = 0; peak = 0
        async def scrape(username):
            nonlocal active, peak
            calls.append(username); tasks.add(asyncio.current_task())
            active += 1; peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                if username == 'u5': raise RuntimeError('fixture failure')
                return {'_failed': int(username[1:]) % 3 == 0}
            finally: active -= 1
        with patch.object(main.db, 'get_tracking_snapshot', return_value=(rows, main.db.database_generation)), \
             patch.object(main.twitch_api, 'get_channel_social', new=scrape), \
             patch.object(main.db, 'set_scraped_social'), patch.object(main.db, 'backfill_scraped_platform_links'):
            result = await main.scrape_all_social_links()
        self.assertEqual(result, {'ok': True, 'total': 1000, 'succeeded': 665, 'failed': 335})
        self.assertEqual(len(set(calls)), 1000)
        self.assertEqual(len(tasks), 8); self.assertLessEqual(peak, 8); self.assertEqual(active, 0)

    async def test_bulk_scrape_cancellation_joins_workers(self):
        ready = asyncio.Event(); hold = asyncio.Event(); active = 0
        async def scrape(_):
            nonlocal active
            active += 1
            if active == 8: ready.set()
            try: await hold.wait()
            finally: active -= 1
        rows = [{'username': str(i)} for i in range(100)]
        with patch.object(main.db, 'get_tracking_snapshot', return_value=(rows, main.db.database_generation)), \
             patch.object(main.twitch_api, 'get_channel_social', new=scrape):
            work = asyncio.create_task(main.scrape_all_social_links())
            try:
                await asyncio.wait_for(ready.wait(), 3)
            finally:
                work.cancel()
                with self.assertRaises(asyncio.CancelledError): await work
        self.assertEqual(active, 0)

    async def test_replaced_database_stops_bulk_network_work(self):
        rows = [{'username': str(i)} for i in range(1000)]
        with patch.object(main.db, 'get_tracking_snapshot', return_value=(rows, main.db.database_generation - 1)), \
             patch.object(main.twitch_api, 'get_channel_social', AsyncMock()) as scrape:
            result = await main.scrape_all_social_links()
        self.assertEqual(result['failed'], 1000); scrape.assert_not_awaited()

    async def test_rate_limit_releases_response_before_sleep_and_handles_bad_headers(self):
        responses = [Response(429, headers={'Ratelimit-Reset': 'invalid'}), Response(429)]
        session = Session(responses)
        async def sleep(_): self.assertTrue(responses[0].closed)
        with patch.object(twitch, 'get_session', AsyncMock(return_value=session)), \
             patch.object(twitch, 'get_access_token', AsyncMock(return_value='fixture')), \
             patch.object(twitch.asyncio, 'sleep', new=sleep):
            with self.assertRaises(twitch.TwitchRateLimitedError) as error:
                await twitch.twitch_request('streams', {})
        self.assertEqual(error.exception.retry_after, 5)
        self.assertTrue(all(response.closed for response in responses))
        self.assertEqual(len(session.requests), 2)

    async def test_unauthorized_request_does_not_discard_an_already_refreshed_token(self):
        responses = [Response(401), Response(200, {'data': []})]
        session = Session(responses); calls = 0
        async def token():
            nonlocal calls
            calls += 1
            if calls == 1:
                twitch.access_token = 'fresh'
                return 'old'
            self.assertTrue(responses[0].closed)
            self.assertEqual(twitch.access_token, 'fresh')
            return 'fresh'
        with patch.object(twitch, 'get_session', AsyncMock(return_value=session)), \
             patch.object(twitch, 'get_access_token', new=token), patch.object(twitch, 'access_token', 'old'):
            self.assertEqual(await twitch.twitch_request('streams', {}), {'data': []})
        self.assertEqual(session.requests[1][1]['headers']['Authorization'], 'Bearer fresh')

    async def test_rate_limit_on_final_attempt_is_reported(self):
        session = Session([Response(401), Response(401), Response(429)])
        with patch.object(twitch, 'get_session', AsyncMock(return_value=session)), \
             patch.object(twitch, 'get_access_token', AsyncMock(return_value='fixture')):
            with self.assertRaises(twitch.TwitchRateLimitedError): await twitch.twitch_request('streams', {})
        self.assertEqual(len(session.requests), 3)

    async def test_zero_viewer_all_any_game_tag_matching_dedup_and_blacklist(self):
        data = [stream(tags=['Speedrun']), stream('bob', 'Chess', ['speedrun']),
                stream('one', tags=['speedrun'], viewers=1), stream('alice', tags=['speedrun']),
                stream('title_only', 'Art', None), stream('bad<script>', tags=['speedrun']),
                stream('boolean', tags=['speedrun'], viewers=False)]
        data[4]['title'] = 'Mario speedrun'
        session = Session([Response(payload=data) for _ in range(3)])
        with patch.object(nobody.aiohttp, 'ClientSession', return_value=session) as factory:
            all_terms = await nobody.search_zero_viewers('MARIO, speedrun', 'all')
            any_terms = await nobody.search_zero_viewers('mario speedrun', 'any', blacklist={'ALICE'})
            blank = await nobody.search_zero_viewers('', 'any', limit=1)
        self.assertTrue(factory.call_args.kwargs['trust_env'])
        self.assertTrue(session.closed)
        self.assertEqual([r['username'] for r in all_terms], ['alice'])
        self.assertEqual([r['username'] for r in any_terms], ['bob'])
        self.assertEqual([r['username'] for r in blank], ['alice'])
        self.assertTrue(all(r['live_viewers'] == 0 for r in all_terms + any_terms + blank))
        self.assertEqual(session.requests[-1][1]['params']['search_operator'], 'all')
        for url, options in session.requests:
            self.assertEqual(url, 'https://nobody.live/stream')
            self.assertEqual(options['params']['max_viewers'], 0)
            self.assertFalse(options['allow_redirects'])
            self.assertEqual(options['headers'], {'Accept': 'application/json'})

    async def test_zero_viewer_errors_empty_and_bounded_response(self):
        cases = [(Response(payload=[]), None), (Response(503), 503), (Response(429), 503),
                 (Response(413), 422), (Response(302), 502), (Response(500), 502),
                 (Response(body=b'not JSON'), 502), (Response(payload={'error': 'bad'}), 502),
                 (Response(payload=[stream()] * 66), 502), (Response(body=b'x' * (1024 * 1024 + 1)), 502)]
        for response, expected in cases:
            with self.subTest(expected=expected), patch.object(nobody.aiohttp, 'ClientSession', return_value=Session([response])):
                if expected is None: self.assertEqual(await nobody.search_zero_viewers(), [])
                else:
                    with self.assertRaises(nobody.NobodyDiscoveryError) as error: await nobody.search_zero_viewers()
                    self.assertEqual(error.exception.status_code, expected)
                self.assertTrue(response.closed)

    async def test_zero_viewer_unicode_punctuation_and_missing_tags(self):
        data = [stream('unicode', 'Pokémon', ['日本語']), stream('missing', 'Mario', None)]
        session = Session([Response(payload=data) for _ in range(3)])
        with patch.object(nobody.aiohttp, 'ClientSession', return_value=session):
            self.assertEqual([r['username'] for r in await nobody.search_zero_viewers('POKÉMON,日本語', 'all')], ['unicode'])
            self.assertEqual([r['username'] for r in await nobody.search_zero_viewers('mario missing', 'any')], ['missing'])
            self.assertEqual(await nobody.search_zero_viewers('%'), [])

    async def test_zero_viewer_requests_have_only_two_active_fetches(self):
        ready = asyncio.Event(); release = asyncio.Event(); active = 0; peak = 0
        class SlowContent:
            async def iter_chunked(self, _):
                nonlocal active, peak
                active += 1; peak = max(peak, active)
                if active == 2: ready.set()
                try:
                    await release.wait()
                    yield b'[]'
                finally: active -= 1
        responses = [Response(payload=[]) for _ in range(6)]
        for response in responses: response.content = SlowContent()
        with patch.object(nobody.aiohttp, 'ClientSession', return_value=Session(responses)):
            tasks = [asyncio.create_task(nobody.search_zero_viewers()) for _ in range(6)]
            try:
                await asyncio.wait_for(ready.wait(), 3)
                self.assertEqual(active, 2)
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
        self.assertEqual(results, [[]] * 6)
        self.assertEqual(peak, 2); self.assertEqual(active, 0)
        self.assertTrue(all(response.closed for response in responses))

    async def test_zero_viewer_timeout_is_bounded_and_cancellation_closes_response(self):
        with patch.object(nobody.aiohttp, 'ClientSession', side_effect=asyncio.TimeoutError):
            with self.assertRaises(nobody.NobodyDiscoveryError) as error: await nobody.search_zero_viewers()
            self.assertEqual(error.exception.status_code, 504)
        entered = asyncio.Event(); hold = asyncio.Event()
        response = Response(payload=[])
        class WaitingContent:
            async def iter_chunked(self, _):
                entered.set(); await hold.wait()
                yield b'[]'
        response.content = WaitingContent()
        with patch.object(nobody.aiohttp, 'ClientSession', return_value=Session([response])):
            work = asyncio.create_task(nobody.search_zero_viewers())
            try: await asyncio.wait_for(entered.wait(), 3)
            finally:
                work.cancel()
                with self.assertRaises(asyncio.CancelledError): await work
        self.assertTrue(response.closed)

    async def test_zero_viewer_queue_wait_has_a_deadline(self):
        with patch.object(nobody, '_REQUEST_SLOTS', asyncio.Semaphore(0)), \
             patch.object(nobody, '_REQUEST_DEADLINE', .01), \
             patch.object(nobody.aiohttp, 'ClientSession') as session:
            with self.assertRaises(nobody.NobodyDiscoveryError) as error:
                await nobody.search_zero_viewers()
        self.assertEqual(error.exception.status_code, 504)
        session.assert_not_called()


if __name__ == '__main__': unittest.main()

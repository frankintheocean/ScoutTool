"""BetterBanned screenshot contract and saved snapshot failure handling."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_regressions as fixtures
import betterbanned
import database as db
import main
from fastapi.testclient import TestClient

PAGE = '''Total Bans: 2
Recent Activity
2026-09-21 unbanned 2 days banned
2026-09-19 banned [banned due to copyright violations]
2026-09-11 unbanned 2 days banned
2026-09-09 banned [banned due to copyright violations]
2026-07-31 profile pic changed
2026-03-28 profile pic changed
2026-03-11 profile pic changed
2026-02-09 profile pic changed
2025-08-30 offline pic changed
2025-08-30 profile pic changed
2025-06-02 offline pic changed
2025-06-02 profile pic changed
2024-03-29 profile pic changed
Socials
Find similar streamer names like isthiscaleb'''


class ActivityTests(unittest.TestCase):
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        db.add_streamer(['isthiscaleb','https://twitch.tv/isthiscaleb'])
        self.client = TestClient(main.app)
        self.endpoint = '/api/streamers/isthiscaleb/activity'

    def test_screenshot_contract(self):
        payload = betterbanned.parse_activity(PAGE)
        self.assertEqual(payload['total_bans'], 2)
        self.assertEqual(len(payload['events']), 13)
        self.assertEqual([e['kind'] for e in payload['events'][:4]], ['unbanned','banned','unbanned','banned'])
        self.assertEqual(payload['events'][1]['description'], 'banned [banned due to copyright violations]')
        self.assertEqual(payload['events'][0]['description'], 'unbanned 2 days banned')
        self.assertEqual(payload['events'][8]['kind'], 'offline_picture')

    def test_html_whitespace_hidden_scripts_and_empty_unknown_counts(self):
        html = '<head><title>Ignore</title></head><script>Recent Activity 2020-01-01 banned</script><div>Total Bans: <b>2</b></div><h3>Recent Activity</h3><div>2026-09-21 <span>unbanned</span> 2 days banned</div><h3>Socials</h3>'
        self.assertEqual(betterbanned.parse_activity(html, html=True)['events'][0]['kind'], 'unbanned')
        self.assertEqual(betterbanned.parse_activity('Recent Activity 2026-01-01 profile pic changed')['total_bans'], None)
        self.assertEqual(betterbanned.parse_activity('Total Bans: 0 Recent Activity Socials')['events'], [])
        self.assertEqual(betterbanned.parse_activity('Recent Activity No recent activity')['events'], [])
        for text in ['Just a moment...', 'Recent Activity', 'Recent Activity 2026-02-30 banned', 'Total Bans: 3 Recent Activity Socials']:
            with self.assertRaises(betterbanned.ProviderError): betterbanned.parse_activity(text)
        with self.assertRaises(ValueError): betterbanned.profile_url('../etc/passwd')

    def test_copy_persistence_refresh_error_and_cleanup(self):
        self.assertIsNone(self.client.get(self.endpoint).json()['snapshot'])
        response = self.client.post(self.endpoint+'/copied',json={'text':PAGE, 'provider_username':'isthiscaleb'})
        self.assertEqual(response.status_code,200,response.text)
        snapshot = response.json()['snapshot']
        self.assertEqual(snapshot['source_type'],'copied')
        with patch.object(betterbanned,'fetch_activity',AsyncMock(side_effect=betterbanned.ProviderError('HTTP 403; saved history was preserved.'))):
            self.assertEqual(self.client.post(self.endpoint+'/refresh').status_code,502)
        self.assertEqual(self.client.get(self.endpoint).json()['snapshot'],snapshot)
        self.assertEqual(self.client.post(self.endpoint+'/copied',json={'text':'bad','provider_username':'isthiscaleb'}).status_code,422)
        self.assertEqual(self.client.get(self.endpoint).json()['snapshot'],snapshot)
        db.close_all_connections();db.setup()
        self.assertEqual(db.get_activity_snapshot('isthiscaleb')['snapshot'],snapshot)
        db.remove_streamer('isthiscaleb')
        self.assertEqual(self.client.get(self.endpoint).status_code,404)
        self.assertIsNone(db.db().execute('SELECT * FROM streamer_activity').fetchone())

    def test_refresh_success_and_identity_race(self):
        payload = betterbanned.parse_activity(PAGE)
        with patch.object(betterbanned,'fetch_activity',AsyncMock(return_value=payload)):
            response = self.client.post(self.endpoint+'/refresh')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['snapshot']['source_type'],'fetched')
        db.save_twitch_identity('isthiscaleb','123','isthiscaleb')
        async def renamed(_):
            db.save_twitch_identity('isthiscaleb','123','newname')
            return payload
        with patch.object(betterbanned,'fetch_activity',renamed):
            self.assertEqual(self.client.post(self.endpoint+'/refresh').status_code,409)
        self.assertEqual(self.client.post(self.endpoint+'/copied',json={'text':PAGE,'provider_username':'isthiscaleb'}).status_code,409)
        self.assertEqual(db.get_activity_snapshot('isthiscaleb')['snapshot']['provider_username'],'isthiscaleb')

    def test_bounded_input_and_rows(self):
        self.assertEqual(self.client.post(self.endpoint+'/copied',json={'text':'x'*65537,'provider_username':'isthiscaleb'}).status_code,422)
        with self.assertRaises(betterbanned.ProviderError):
            betterbanned.parse_activity('Recent Activity ' + '2026-01-01 banned '*501)
        with self.assertRaises(betterbanned.ProviderError):
            betterbanned.parse_activity('Recent Activity 2026-01-01 '+ 'x'*2001)

    def test_transport_limits_and_response_cleanup(self):
        closed = []
        class Context:
            def __init__(self, value): self.value = value
            async def __aenter__(self): return self.value
            async def __aexit__(self, *args): closed.append(self.value)
        async def body(data):
            yield data
        for status, data, valid in [(200,PAGE.encode(),True),(403,b'',False),(429,b'',False),(302,b'',False),
                                    (200,b'x'*(betterbanned.MAX_RESPONSE+1),False),(200,b'Just a moment...',False)]:
            response = SimpleNamespace(status=status,charset='utf-8',content=SimpleNamespace(iter_chunked=lambda _:body(data)))
            session = SimpleNamespace(get=lambda *args,**kwargs:Context(response))
            with patch.object(betterbanned.aiohttp,'ClientSession',return_value=Context(session)):
                if valid:
                    self.assertEqual(asyncio.run(betterbanned.fetch_activity('isthiscaleb'))['total_bans'],2)
                else:
                    with self.assertRaises(betterbanned.ProviderError): asyncio.run(betterbanned.fetch_activity('isthiscaleb'))
            self.assertIn(response,closed)
            self.assertIn(session,closed)
        with patch.object(betterbanned.aiohttp,'ClientSession',side_effect=TimeoutError):
            with self.assertRaises(betterbanned.ProviderError): asyncio.run(betterbanned.fetch_activity('isthiscaleb'))

    def test_slow_refresh_cannot_overwrite_newer_copied_snapshot(self):
        payload = betterbanned.parse_activity(PAGE)
        async def copy_during_refresh(_):
            db.save_activity_snapshot('isthiscaleb','isthiscaleb',{'total_bans':3,'events':[]},'copied')
            return payload
        with patch.object(betterbanned,'fetch_activity',copy_during_refresh):
            self.assertEqual(self.client.post(self.endpoint+'/refresh').status_code,409)
        self.assertEqual(db.get_activity_snapshot('isthiscaleb')['snapshot']['total_bans'],3)


if __name__ == '__main__':
    unittest.main()

"""Run with python -m unittest discover -s tests -v (no live credentials needed).
Install httpx alongside the application dependencies for FastAPI TestClient.
"""
import asyncio
import base64
import concurrent.futures
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
DATA = tempfile.TemporaryDirectory(prefix='scout-audit-')
os.environ['DASHBOARD_SCOUT_DATA_DIR'] = DATA.name
os.environ['SCOUTBOT_STANDALONE'] = '1'
sys.path.insert(0, str(ROOT / 'scout-backend'))
import database as db
import main
import twitch_api as twitch
import notifier
import auth
import config
import alerts
import desktop_launcher
from scoring import calculate_raid_score
from fastapi.testclient import TestClient
from fastapi import HTTPException


def legacy_bytes(username='legacy'):
    with tempfile.NamedTemporaryFile(suffix='.db') as file:
        con = sqlite3.connect(file.name)
        con.execute('CREATE TABLE streamers(id INTEGER PRIMARY KEY, username TEXT UNIQUE, url TEXT)')
        con.execute('INSERT INTO streamers(username,url) VALUES (?,?)', (username, 'https://twitch.tv/'+username))
        con.execute('CREATE TABLE streamer_metadata(username TEXT PRIMARY KEY, alias TEXT)')
        con.execute('INSERT INTO streamer_metadata VALUES (?,?)', (username, 'LegacyAlias'))
        con.commit(); con.close()
        return Path(file.name).read_bytes()


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        db.close_all_connections()
        for suffix in ('', '-wal', '-shm'):
            Path(db.DB_NAME + suffix).unlink(missing_ok=True)
        db._invalidate_all_cache()
        db.setup()
        self.client = TestClient(main.app)

    def add(self, username='alice'):
        db.add_streamer([username, 'https://twitch.tv/'+username])

    def test_populated_legacy_migration_and_search(self):
        staged, info = main._stage_database(legacy_bytes())
        self.assertEqual(info['streamers'], 1)
        main._replace_database(staged)
        self.assertEqual(db.search_streamers(query='LegacyAlias')[1], 1)
        self.assertEqual(db.get_streamer_metadata('legacy')['priority'], 'Watch')
        self.assertEqual(self.client.get('/api/streamers/legacy').status_code, 200)

    def test_fts_updates_removal_reused_id_and_rebuild(self):
        self.add()
        db.set_alias('alice', 'UniqueAlias')
        self.assertEqual(db.search_streamers(query='UniqueAlias')[1], 1)
        db.set_alias('alice', 'NewAlias')
        self.assertEqual(db.search_streamers(query='UniqueAlias')[1], 0)
        db.set_scraped_social('alice', 'bio', {}, 'Oslo', 'Europe/Oslo', 30)
        self.assertEqual(db.search_streamers(query='Oslo')[1], 1)
        db.rebuild_fts_index()
        self.assertEqual(db.search_streamers(query='NewAlias')[1], 1)
        db.remove_streamer('alice'); self.add('bob')
        self.assertEqual(db.search_streamers(query='NewAlias')[1], 0)
        self.assertEqual(db.search_streamers(query='bob')[1], 1)

    def test_old_contentless_index_migrates(self):
        self.add()
        with db.db() as con:
            triggers = con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%streamers_fts%'").fetchall()
            for row in triggers: con.execute('DROP TRIGGER '+row[0])
            con.execute('DROP TABLE streamers_fts')
            con.execute("CREATE VIRTUAL TABLE streamers_fts USING fts5(username,alias,notes,category,tags,location,content='')")
        db.setup()
        self.assertEqual(db.search_streamers(query='alice')[1], 1)
        db.rebuild_fts_index()

    def test_unknown_followers_preserve_known_count_and_cache(self):
        self.add(); db.update_twitch_data('alice', 'Art', 42, 0, 'Live')
        db.get_all()
        db.update_twitch_data('alice', 'Music', None, 3, 'Live')
        self.assertEqual(db.get_streamer('alice')['followers'], 42)
        self.assertEqual(db.get_all()[0]['category'], 'Music')

    def test_toggle_is_atomic_under_concurrency(self):
        self.add()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: db.toggle_favourite('alice'), range(100)))
        self.assertFalse(db.get_streamer_metadata('alice')['favourite'])

    def test_removed_streamer_has_no_orphaned_history_or_scrape(self):
        self.add()
        db.add_streamer_response('alice', 'replied', 'hello')
        db.add_recently_viewed('alice')
        db.remove_streamer('alice')
        self.assertFalse(db.set_scraped_social('alice', 'late', {}))
        with db.db() as con:
            for table in ('streamer_metadata', 'recently_viewed', 'streamer_responses'):
                self.assertEqual(con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)

    def test_response_delete_scoped_to_username(self):
        self.add(); self.add('bob')
        rid = db.add_streamer_response('bob', 'replied', '')
        self.assertFalse(db.delete_streamer_response(rid, 'alice'))
        self.assertEqual(len(db.get_streamer_responses('bob')), 1)

    def test_invalid_paired_import_preserves_credentials_and_database(self):
        self.add()
        Path(config.ENV_PATH).write_text('TWITCH_CLIENT_ID=old\n')
        response = self.client.post('/api/system/import', files={
            'env_file': ('.env', b'TWITCH_CLIENT_ID=new\n'),
            'db_file': ('streamers.db', b'not a database')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Path(config.ENV_PATH).read_text(), 'TWITCH_CLIENT_ID=old\n')
        self.assertTrue(db.streamer_exists('alice'))
        self.assertFalse(list(Path(DATA.name).glob('scout-import-*')))

    def test_preview_migrates_without_touching_live_database(self):
        self.add()
        info = main._preview_database(legacy_bytes())
        self.assertEqual(info['streamers'], 1)
        self.assertTrue(db.streamer_exists('alice'))
        self.assertFalse(db.streamer_exists('legacy'))

    def test_concurrent_reads_during_replacement(self):
        self.add()
        staged, _ = main._stage_database(legacy_bytes())
        def reads():
            for _ in range(30): db.get_paginated(page=1,page_size=25)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(reads) for _ in range(5)]
            main._replace_database(staged)
            for future in futures: future.result()
        self.assertTrue(db.streamer_exists('legacy'))

    def test_bulk_metadata_matches_single_lookup(self):
        self.add(); db.set_tags('alice', ['one','two']); db.set_alias('alice', 'Alias')
        db.set_scraped_social('alice', 'bio', {'YouTube':'https://youtube.com/@alice'}, 'Oslo','Europe/Oslo',23)
        self.assertEqual(db.get_metadata_bulk(['alice'])['alice'], db.get_streamer_metadata('alice'))

    def test_staged_migration_does_not_block_live_reads(self):
        self.add()
        entered=threading.Event();release=threading.Event()
        original=db.migrate_metadata_table
        def pause(con):
            entered.set();release.wait(timeout=3);return original(con)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(db,'migrate_metadata_table',pause):
                future=pool.submit(main._stage_database,legacy_bytes())
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    start=time.monotonic();self.assertTrue(db.streamer_exists('alice'))
                    self.assertLess(time.monotonic()-start,.5)
                finally:release.set()
                staged,_=future.result()
        Path(staged).unlink()

    def test_native_readiness_accepts_own_authenticated_server(self):
        from urllib.error import HTTPError
        from types import SimpleNamespace
        with patch('urllib.request.urlopen',side_effect=HTTPError('http://localhost',401,'Authentication',{},None)):
            self.assertTrue(desktop_launcher._wait_for_server('127.0.0.1',1,timeout=.1,server=SimpleNamespace(started=True)))

    def test_large_dataset_search_paging_and_api(self):
        with db.db() as con:
            con.executemany('INSERT INTO streamers(username,url,followers,category) VALUES (?,?,?,?)',
                [('user%05d'%i, 'https://twitch.tv/user%05d'%i, i, 'Art' if i%2 else 'Music') for i in range(10000)])
        db.setup(); db._invalidate_all_cache()
        start=time.monotonic()
        first=self.client.get('/api/streamers?page_size=200&sort_by=username&ascending=true').json()
        last=self.client.get('/api/streamers?page=50&page_size=200&sort_by=username&ascending=true').json()
        self.assertEqual(first['total'],10000); self.assertEqual(len(last['items']),200)
        self.assertEqual(last['items'][-1]['username'],'user09999')
        self.assertEqual(self.client.get('/api/streamers/search?category=Art').json()['total'],5000)
        self.assertEqual(self.client.get('/api/streamers/search?q=missing').json()['total'],0)
        self.assertLess(time.monotonic()-start,10)

    def test_download_workers_are_bounded_and_queued_work_starts_next(self):
        with patch.object(main.threading,'Thread') as thread:
            jobs=[main.start_vod_download({'url':'https://twitch.tv/videos/'+str(i)}) for i in range(5)]
            self.assertEqual(thread.call_count,2)
            with main.VOD_JOB_LOCK:
                main.VOD_DOWNLOAD_JOBS[jobs[0]['id']]['status']='complete'
                main._start_queued_vod_downloads()
            self.assertEqual(thread.call_count,3)
        with main.VOD_JOB_LOCK:
            for job in jobs:main.VOD_DOWNLOAD_JOBS.pop(job['id'])

    def test_failed_downloader_result_is_reported_and_open_ended_trim_is_finite_safe(self):
        from types import SimpleNamespace
        options={}
        class Downloader:
            def __init__(self, opts):options.update(opts)
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def download(self, urls):return 1
        job={'id':'failure-fixture','status':'queued','_started':True,'_request_key':('https://twitch.tv/videos/123',10,None)}
        with main.VOD_JOB_LOCK:main.VOD_DOWNLOAD_JOBS[job['id']]=job
        try:
            with patch.dict(sys.modules,{'yt_dlp':SimpleNamespace(YoutubeDL=Downloader)}),patch.object(main,'_downloads_dir',return_value=Path(DATA.name)),patch.object(main,'_fastest_download_opts',return_value={}):
                main._run_vod_download(job['id'],job['_request_key'][0],10,None)
            self.assertEqual(job['status'],'failed')
            self.assertEqual(list(options['download_ranges'](None,None))[0]['start_time'],10)
            self.assertEqual(list(options['download_ranges'](None,None))[0]['end_time'],float('inf'))
            self.assertIn('[10-end]',options['outtmpl'])
        finally:
            with main.VOD_JOB_LOCK:main.VOD_DOWNLOAD_JOBS.pop(job['id'])

    def test_empty_error_and_validation_states(self):
        self.assertEqual(self.client.get('/api/streamers').json()['items'], [])
        self.assertEqual(self.client.get('/api/streamers/missing').status_code,404)
        self.assertEqual(self.client.get('/api/streamers?page_size=0').status_code,422)
        self.assertEqual(self.client.post('/api/system/import').status_code,400)

    def test_credential_save_preserves_other_settings(self):
        Path(config.ENV_PATH).write_text('WEB_PORT=8888\n')
        config.save_twitch_credentials('id','secret')
        text=Path(config.ENV_PATH).read_text()
        self.assertIn('WEB_PORT=8888',text); self.assertIn('TWITCH_CLIENT_SECRET=',text)
        self.assertFalse(list(Path(DATA.name).glob('.credentials-*')))

    def test_failed_replacement_rolls_back_paired_env(self):
        self.add()
        Path(config.ENV_PATH).write_text('TWITCH_CLIENT_ID=old\n')
        with patch.object(main,'_replace_database',side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError): main._import_files(b'TWITCH_CLIENT_ID=new\n',legacy_bytes())
        self.assertEqual(Path(config.ENV_PATH).read_text(),'TWITCH_CLIENT_ID=old\n')
        self.assertTrue(db.streamer_exists('alice'))

    def test_restore_selected_backup_survives_retention(self):
        self.add()
        with tempfile.TemporaryDirectory() as directory, patch.object(main,'BACKUP_DIR',Path(directory)), patch.object(main,'BACKUP_RETENTION',1):
            backup=main._create_backup(lambda *_:None)
            db.remove_streamer('alice'); self.add('bob')
            response=self.client.post('/api/maintenance/restore/'+backup['filename'])
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(db.streamer_exists('alice'))
            self.assertFalse(db.streamer_exists('bob'))
            self.assertEqual(len(list(Path(directory).glob('*.db'))),1)

    def test_stale_background_update_rejected_after_import(self):
        self.add()
        generation=db.database_generation
        staged,_=main._stage_database(legacy_bytes('alice'))
        main._replace_database(staged)
        with self.assertRaises(db.StaleDatabaseOperationError):
            db.update_twitch_data('alice','stale category',10,10,'Live',_generation=generation)
        self.assertNotEqual(db.get_streamer('alice')['category'],'stale category')

    def test_legacy_column_order_does_not_change_raid_scoring(self):
        staged,_=main._stage_database(legacy_bytes('alice'));main._replace_database(staged)
        with db.db() as con:
            con.execute("UPDATE streamers SET notes='text',followers=1000,average_viewers=50,current_viewers=25,community_rating=2,content_rating=5,raid_rating=4,peak_viewers=100,raid_score=50,manual_raid_score=50")
        self.assertEqual(calculate_raid_score(db.get_streamer('alice')),70)

    def test_automatic_raid_calculations_do_not_overwrite_manual_bonus(self):
        self.add()
        self.client.put('/api/streamers/alice/raidscore',json={'score':90})
        self.client.put('/api/streamers/alice/rating',json={'category':'community','score':4})
        one=self.client.post('/api/streamers/alice/calculate_raid').json()['raid_score']
        two=self.client.post('/api/streamers/alice/calculate_raid').json()['raid_score']
        self.assertEqual(one,two)
        self.assertEqual(db.get_streamer('alice')['manual_raid_score'],90)

    def test_failed_social_lookup_preserves_previous_scrape(self):
        self.add();db.set_scraped_social('alice','old bio',{},'Oslo','Europe/Oslo',25)
        with patch.object(twitch,'get_channel_social',AsyncMock(return_value={'_failed':True,'bio':'','social_links':{}})):
            response=self.client.post('/api/streamers/alice/social/scrape')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['bio'],'old bio')
        self.assertEqual(response.json()['scraped_age'],25)

    def test_validation_context_serializes_and_import_errors_remain_400(self):
        response=self.client.post('/api/discover/history',json={'filters':{}})
        self.assertEqual(response.status_code,422,response.text)
        from fastapi.exceptions import RequestValidationError
        error=RequestValidationError([{'type':'value_error','loc':['body','filters'],'msg':'invalid','ctx':{'error':ValueError('invalid')}}])
        response=asyncio.run(main.app.exception_handlers[RequestValidationError](None,error))
        self.assertEqual(response.status_code,422)
        self.assertIn(b'"error":"invalid"',response.body)
        with tempfile.NamedTemporaryFile(suffix='.db') as file:
            con=sqlite3.connect(file.name);con.execute('CREATE TABLE streamers(id INTEGER,username TEXT)');con.commit();con.close()
            response=self.client.post('/api/maintenance/import-preview',files={'db_file':('streamers.db',Path(file.name).read_bytes())})
        self.assertEqual(response.status_code,400,response.text)

    def test_duplicate_maintenance_actions_share_active_job(self):
        release=threading.Event()
        def work(progress):
            release.wait(timeout=3)
            return {'ok':True}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                jobs=list(pool.map(lambda _:main._submit_job('regression-fixture',work),range(20)))
            self.assertEqual(len(set(jobs)),1)
        finally:release.set()
        deadline=time.monotonic()+3
        while main.MAINTENANCE_JOBS[jobs[0]]['status'] in ('queued','running') and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(main.MAINTENANCE_JOBS[jobs[0]]['status'],'complete')

    def test_duplicate_vod_requests_share_job_and_nonfinite_times_rejected(self):
        with patch.object(main.threading,'Thread') as thread:
            one=main.start_vod_download({'url':'https://twitch.tv/videos/123','start_time':10})
            two=main.start_vod_download({'url':'https://twitch.tv/videos/123','start_time':10})
            self.assertEqual(one['id'],two['id']);self.assertEqual(thread.call_count,1)
        with main.VOD_JOB_LOCK:main.VOD_DOWNLOAD_JOBS.pop(one['id'])
        for value in ('NaN','Infinity','-Infinity'):
            self.assertEqual(self.client.post('/api/vod-downloads',json={'url':'https://twitch.tv/videos/123','start_time':value}).status_code,400)

    def test_websocket_relay_and_subscribers_clean_up(self):
        notifier._recent.clear()
        with self.client.websocket_connect('/api/ws') as first:
            with self.client.websocket_connect('/api/ws') as second:
                first.send_json({'type':'test','value':42})
                self.assertEqual(second.receive_json(),{'type':'test','value':42})
        self.assertEqual(len(notifier._ws_clients),0)
        self.assertEqual(len(notifier._subscribers),0)

    def test_lifespan_startup_and_shutdown_close_resources(self):
        async def idle():await asyncio.sleep(3600)
        with patch.object(main,'tracker_wrapper',idle),patch.object(main,'discover_alert_wrapper',idle),patch.object(main,'category_watch_cron_wrapper',idle):
            with TestClient(main.app) as client:
                self.assertEqual(client.get('/api/version').status_code,200)
        self.assertEqual(db._all_connections,[])


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_viewer_live_and_failed_stream_fetch(self):
        users={'alice':dict(user_id='1',username='alice',display_name='Alice',profile_image='')}
        with patch.object(twitch,'get_bulk_users',AsyncMock(return_value=users)), patch.object(twitch,'get_followers',AsyncMock(return_value=None)):
            with patch.object(twitch,'twitch_request',AsyncMock(side_effect=[{'data':[]},{'data':[{'user_id':'1','viewer_count':0}]}])):
                data=await twitch.get_bulk_streamer_data(['alice'])
                self.assertEqual(data['alice']['live_status'],'Live')
                self.assertIsNone(data['alice']['followers'])
            with patch.object(twitch,'twitch_request',AsyncMock(side_effect=[{'data':[]},None])):
                self.assertEqual(await twitch.get_bulk_streamer_data(['alice']),{})

    async def test_discover_does_not_drop_overfetched_matches(self):
        twitch._search_pages.clear()
        entries=[{'username':'u%d'%i,'user_id':str(i),'followers':None} for i in range(105)]
        with patch.object(twitch,'get_followers',AsyncMock(return_value=7)):
            page,cursor=await twitch._search_page(entries,None,25,False)
            names=[r['username'] for r in page]
            while cursor:
                page,cursor=await twitch.search_streams(cursor=cursor,limit=25)
                names.extend(r['username'] for r in page)
        self.assertEqual(names,['u%d'%i for i in range(105)])

    async def test_subscriber_is_bounded_and_ws_has_single_sender(self):
        q=notifier.subscribe(); ws=object(); other=object(); other_q=notifier.subscribe()
        notifier.register_ws(ws,q); notifier.register_ws(other,other_q)
        try:
            for i in range(250): await notifier.broadcast_client_event({'n':i},sender=other)
            self.assertEqual(q.qsize(),100); self.assertEqual(other_q.qsize(),0)
            self.assertEqual((await q.get())['n'],150)
        finally:
            notifier.unregister_ws(ws); notifier.unregister_ws(other)
            notifier.unsubscribe(q); notifier.unsubscribe(other_q)

    async def test_follower_permission_failure_is_cached(self):
        twitch.follower_cache.clear()
        mock=AsyncMock(return_value=None)
        with patch.object(twitch,'twitch_request',mock):
            self.assertIsNone(await twitch.get_followers('no-permission'))
            self.assertIsNone(await twitch.get_followers('no-permission'))
            self.assertEqual(mock.await_count,1)

    async def test_real_discover_pipeline_retains_all_matches(self):
        twitch._search_pages.clear()
        streams=[{'user_login':'u%d'%i,'user_name':'U%d'%i,'user_id':str(i),'viewer_count':100-i,'game_name':'Art'} for i in range(60)]
        users={str(i):{'broadcaster_type':'','profile_image_url':''} for i in range(60)}
        with patch.object(twitch,'twitch_request',AsyncMock(return_value={'data':streams,'pagination':{}})),patch.object(twitch,'get_users_by_id',AsyncMock(return_value=users)),patch.object(twitch,'get_followers',AsyncMock(return_value=None)):
            page,cursor=await twitch.search_streams(limit=25)
            names=[r['username'] for r in page]
            while cursor:
                page,cursor=await twitch.search_streams(limit=25,cursor=cursor)
                names.extend(r['username'] for r in page)
        self.assertEqual(names,['u%d'%i for i in range(60)])

    async def test_alert_search_passes_exclusions_dates_and_blacklist(self):
        filters={'category':'Art','exclude_tags':['vtuber'],'created_after':'2020-01-01','created_before':'2025-01-01'}
        rule={'id':1,'kind':'discover_match','target':__import__('json').dumps(filters),'last_result_hash':None}
        search=AsyncMock(return_value=([],None))
        with patch.object(alerts.db,'get_alert_rules',return_value=[rule]),patch.object(alerts.db,'get_blacklist_set',return_value={'blocked'}),patch.object(twitch,'search_streams',search):
            await alerts.check_discover_match_rules()
        self.assertEqual(search.call_args.kwargs['exclude_tags'],['vtuber'])
        self.assertEqual(search.call_args.kwargs['created_after'],'2020-01-01')
        self.assertEqual(search.call_args.kwargs['blacklist'],{'blocked'})

    async def test_invalid_candidate_credentials_never_replace_working_settings(self):
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def json(self):return {'error':'invalid client'}
        class Session:
            def post(self,*_,**__):return Response()
        with patch.object(twitch,'get_session',AsyncMock(return_value=Session())),patch.object(config,'save_twitch_credentials') as save,patch.object(config,'TWITCH_CLIENT_ID','working'):
            with self.assertRaises(HTTPException) as error:
                await main.set_twitch_settings(main.TwitchCredentialsBody(client_id='bad',client_secret='bad'))
            self.assertEqual(error.exception.status_code,400)
            self.assertEqual(config.TWITCH_CLIENT_ID,'working');save.assert_not_called()

    async def test_token_for_superseded_credentials_is_discarded(self):
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def json(self):
                twitch.set_credentials('new','new-secret')
                return {'access_token':'old-token','expires_in':3600}
        class Session:
            def post(self,*_,**__):return Response()
        twitch.set_credentials('old','old-secret')
        with patch.object(twitch,'get_session',AsyncMock(return_value=Session())):
            self.assertIsNone(await twitch.get_access_token())
        self.assertIsNone(twitch.access_token)
        twitch.set_credentials('','')


class AuthTests(unittest.TestCase):
    def test_unicode_credentials_and_invalid_utf8(self):
        with patch.object(config,'WEB_USERNAME','näme'),patch.object(config,'WEB_PASSWORD','påss'):
            header='Basic '+base64.b64encode('näme:påss'.encode()).decode()
            self.assertTrue(auth._is_authorized(header))
            self.assertFalse(auth._is_authorized('Basic /w=='))
            self.assertFalse(auth._is_authorized('Basic '+base64.b64encode('wrong:päss'.encode()).decode()))


if __name__=='__main__': unittest.main()

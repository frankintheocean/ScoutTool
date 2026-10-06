"""Persistent Twitch identity and rename regressions; no external requests."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_regressions as fixtures
import database as db
import discover_cache
import main
import twitch_api as twitch


class IdentityDatabaseTests(unittest.TestCase):
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        db.add_streamer(['oldname','https://twitch.tv/oldname'])
        discover_cache.invalidate_all()

    def test_observed_rename_preserves_record_and_local_data(self):
        db.update_notes('oldname','precious notes')
        db.set_alias('oldname','Alias')
        before = db.get_streamer('oldname')['id']
        db.save_twitch_identity('oldname','123','oldname')
        db.save_twitch_identity('oldname','123','newname')
        db.save_twitch_identity('oldname','123','newname')
        row = db.get_streamer('oldname')
        self.assertEqual(row['id'],before)
        self.assertEqual(row['notes'],'precious notes')
        self.assertEqual(row['url'],'https://twitch.tv/newname')
        self.assertEqual(db.get_streamer_metadata('oldname')['alias'],'Alias')
        history = db.get_username_identity('oldname')['history']
        self.assertEqual(len(history),1)
        self.assertEqual((history[0]['previous_username'],history[0]['new_username'],history[0]['source']),('oldname','newname','observed'))
        self.assertTrue(history[0]['observed_at'])
        self.assertEqual(db.search_streamers(query='newname')[1],1)
        db.save_twitch_identity('oldname','123','oldname')
        self.assertEqual(len(db.get_username_identity('oldname')['history']),2)

    def test_initial_binding_does_not_fabricate_confirmed_past_history(self):
        db.save_twitch_identity('oldname','123','newname')
        entry = db.get_username_identity('oldname')['history'][0]
        self.assertEqual(entry['source'],'manual')
        self.assertIsNone(entry['observed_at'])
        self.assertIsNone(entry['new_username'])

    def test_older_tracker_response_cannot_undo_newer_identity_check(self):
        db.save_twitch_identity('oldname','123','oldname')
        async def lookup(_):
            db.save_twitch_identity('oldname','123','newname')
            return {'123':{'id':'123','login':'oldname','display_name':'Old'}}
        with patch.object(twitch,'get_users_by_id',lookup):
            result = asyncio.run(twitch.get_tracked_users(['oldname']))
        self.assertEqual(result,{})
        self.assertEqual(db.get_username_identity('oldname')['current_username'],'newname')

    def test_five_thousand_saved_ids_are_resolved_in_fifty_requests(self):
        with db.db() as con:
            con.executemany('INSERT INTO streamers(username,twitch_id,current_username) VALUES(?,?,?)',
                            [('bulk'+str(i),str(10000+i),'bulk'+str(i)) for i in range(5000)])
        async def lookup(endpoint,params):
            self.assertEqual(endpoint,'users')
            return {'data':[{'id':uid,'login':'bulk'+str(int(uid)-10000),'display_name':'Bulk'} for _,uid in params]}
        with patch.object(twitch,'twitch_request',AsyncMock(side_effect=lookup)) as requests:
            result = asyncio.run(twitch.get_tracked_users(['bulk'+str(i) for i in range(5000)]))
        self.assertEqual(len(result),5000)
        self.assertEqual(requests.await_count,50)

    def test_same_id_cannot_create_duplicate_record_and_insert_is_atomic(self):
        db.save_twitch_identity('oldname','123','newname')
        with self.assertRaises(ValueError): db.add_streamer(['newname','https://twitch.tv/newname'],twitch_id='123')
        self.assertFalse(db.streamer_exists('newname'))
        self.assertIsNone(db.db().execute("SELECT username FROM streamer_metadata WHERE username='newname'").fetchone())
        self.assertEqual(db.find_twitch_identity_owner('123'),'oldname')

    def test_reused_old_username_cannot_rebind_existing_account(self):
        db.save_twitch_identity('oldname','123','newname')
        with self.assertRaises(ValueError): db.save_twitch_identity('oldname','999','oldname')
        self.assertEqual(db.get_streamer('oldname')['twitch_id'],'123')
        self.assertEqual(db.get_tracked_identity_names(['oldname','newname']),{'newname':'oldname'})

    def test_manual_names_are_deduplicated_removable_and_preserved_on_restart(self):
        db.add_previous_username('oldname','@EvenOlder')
        db.add_previous_username('oldname','evenolder')
        history = db.get_username_identity('oldname')['history']
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]['previous_username'],'evenolder')
        db.close_all_connections(); db.setup()
        self.assertEqual(db.get_username_identity('oldname')['history'],history)
        self.assertTrue(db.remove_previous_username('oldname',history[0]['id']))
        db.save_twitch_identity('oldname','123','oldname'); db.save_twitch_identity('oldname','123','newname')
        entry = db.get_username_identity('oldname')['history'][0]
        self.assertFalse(db.remove_previous_username('oldname',entry['id']))

    def test_legacy_migration_and_deletion_cleanup(self):
        staged,_ = main._stage_database(fixtures.legacy_bytes())
        main._replace_database(staged)
        self.assertIsNone(db.get_streamer('legacy')['twitch_id'])
        db.add_previous_username('legacy','older')
        db.remove_streamer('legacy')
        self.assertEqual(db.db().execute('SELECT COUNT(*) FROM streamer_username_history').fetchone()[0],0)

    def test_api_check_uses_saved_id_and_preserves_identity_on_failure(self):
        db.save_twitch_identity('oldname','123','oldname')
        with patch.object(twitch,'twitch_request',AsyncMock(return_value={'data':[{'id':'123','login':'newname'}]})) as lookup:
            result = self.client.post('/api/streamers/oldname/identity/check')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['current_username'],'newname')
        self.assertEqual(lookup.call_args.args,('users',{'id':'123'}))
        before = db.get_username_identity('oldname')
        for failure,status in ((None,502),({'data':[]},404),({'data':[{'id':'999','login':'wrong'}]},502),({'data':'invalid'},502)):
            with patch.object(twitch,'twitch_request',AsyncMock(return_value=failure)):
                self.assertEqual(self.client.post('/api/streamers/oldname/identity/check').status_code,status)
            self.assertEqual(db.get_username_identity('oldname'),before)

    def test_api_id_validation_manual_binding_and_history_controls(self):
        for value in (0,123,'0','-1','１２３','123\n','1'*21,'https://twitch.tv/name',''):
            self.assertEqual(self.client.put('/api/streamers/oldname/identity',json={'twitch_id':value}).status_code,422,value)
        numeric_id='9007199254740993'
        with patch.object(twitch,'twitch_request',AsyncMock(return_value={'data':[{'id':numeric_id,'login':'newname'}]})):
            result=self.client.put('/api/streamers/oldname/identity',json={'twitch_id':numeric_id})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['twitch_id'],numeric_id)
        self.assertEqual(result.json()['history'][0]['source'],'manual')
        self.assertEqual(self.client.put('/api/streamers/oldname/identity',json={'twitch_id':'999'}).status_code,409)
        entry=self.client.post('/api/streamers/oldname/identity/history',json={'username':'known_old_name'}).json()['history'][0]
        self.assertEqual(self.client.delete('/api/streamers/oldname/identity/history/'+str(entry['id'])).status_code,200)
        self.assertEqual(self.client.get('/api/streamers/unknown/identity').status_code,404)


class IdentityAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixtures.DatabaseTests.setUp(self)
        discover_cache.invalidate_all()
        db.add_streamer(['oldname','https://twitch.tv/oldname'],twitch_id='123')

    async def asyncTearDown(self):
        await discover_cache.close_pending_searches()
        discover_cache.invalidate_all()

    async def test_tracker_resolves_saved_ids_in_batches_and_keeps_original_keys(self):
        with patch.object(twitch,'get_bulk_users',AsyncMock(return_value={})) as by_name, \
             patch.object(twitch,'get_users_by_id',AsyncMock(return_value={'123':{'id':'123','login':'newname','display_name':'NewName','profile_image_url':''}})) as by_id, \
             patch.object(twitch,'get_followers',AsyncMock(return_value=None)), \
             patch.object(twitch,'twitch_request',AsyncMock(side_effect=[{'data':[]},{'data':[{'user_id':'123','viewer_count':0}]}])):
            result = await twitch.get_bulk_streamer_data(['oldname'])
        self.assertEqual(by_name.call_args.args,([],))
        self.assertEqual(by_id.call_args.args,(['123'],))
        self.assertEqual(result['oldname']['username'],'newname')
        self.assertEqual(result['oldname']['live_status'],'Live')
        self.assertIsNone(result['oldname']['followers'])
        self.assertEqual(db.get_username_identity('oldname')['history'][0]['source'],'observed')

    async def test_missing_saved_id_never_falls_back_to_reused_username(self):
        with patch.object(twitch,'get_bulk_users',AsyncMock(return_value={})) as by_name, \
             patch.object(twitch,'get_users_by_id',AsyncMock(return_value={})):
            self.assertEqual(await twitch.get_bulk_streamer_data(['oldname']),{})
            self.assertIsNone(await twitch.get_user_id('oldname'))
        self.assertEqual(by_name.call_args.args,([],))
        self.assertEqual(db.get_streamer('oldname')['current_username'],'oldname')

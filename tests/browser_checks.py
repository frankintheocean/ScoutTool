"""Optional browser regression checks: python tests/browser_checks.py.
Requires Playwright and a Chromium executable (SCOUT_CHROMIUM overrides).
Uses disposable data; never contacts Twitch or edits an installed database.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse, parse_qs

ROOT=Path(__file__).resolve().parents[1]

async def checks(url):
    from playwright.async_api import async_playwright
    passed=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=os.getenv('SCOUT_CHROMIUM','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
        page=await browser.new_page(viewport={'width':1400,'height':900},service_workers='block')
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        await page.route('**/api/settings/twitch',lambda route:route.fulfill(json={'configured':True,'client_id':'fixture','rate_limit':{}}))
        await page.goto(url,wait_until='networkidle')
        assert await page.locator('.streamer-card').count()==30
        passed.append('startup and normal roster')
        await page.evaluate('state.virtualScroll=true; loadView()')
        await page.wait_for_function('state.items.length===10000')
        await page.wait_for_timeout(150)
        assert await page.locator('.streamer-card').count()<100
        for _ in range(6):
            await page.evaluate('document.querySelector(".main").scrollTop=document.querySelector(".main").scrollHeight')
            await page.wait_for_timeout(100)
        assert await page.locator('.streamer-card').last.get_attribute('data-username')=='user00000'
        passed.append('10,000-record Show all with bounded DOM and reachable final row')
        await page.route('https://player.twitch.tv/**', lambda route: route.abort())
        preview=page.locator('[data-preview-toggle="user00001"]')
        await preview.click()
        assert await page.locator('[data-preview-embed="user00001"] iframe').count()==1
        await preview.click()
        assert await page.locator('[data-preview-embed="user00001"] iframe').count()==0
        passed.append('virtual-grid live preview opens, resizes its row, and cleans up')
        await page.set_viewport_size({'width':900,'height':600})
        await page.evaluate('selectStreamer("user00001")')
        await page.wait_for_selector('#detailClose',state='visible')
        await page.locator('#detailNotes').fill('saved before close')
        await page.locator('#detailClose').click()
        await page.wait_for_timeout(300)
        result=await page.request.get(url+'/api/streamers/user00001')
        assert (await result.json())['notes']=='saved before close'
        passed.append('small-window detail controls and autosave on close')
        await page.set_viewport_size({'width':1400,'height':900})
        async def slow_detail(route):
            response=await route.fetch()
            await asyncio.sleep(.35)
            await route.fulfill(response=response)
        await page.route('**/api/streamers/user00002',slow_detail)
        await page.evaluate('Promise.all([selectStreamer("user00002"),selectStreamer("user00003")])')
        assert await page.evaluate('state.selectedUsername')=='user00003'
        assert 'user00003' in await page.locator('#detailContent').inner_text()
        passed.append('rapid detail selection ignores older response')
        await page.evaluate('setDetailPanelEmpty(true);state.selectedUsername=null')
        first=page.locator('[data-select-streamer]').first
        await first.check()
        assert await page.evaluate('state.selectedUsername') is None
        assert await page.evaluate('state.selectedUsernames.size')==1
        await page.evaluate('state.selectedUsernames.add("user00001"); updateSelectionToolbar()')
        await page.locator('#btnCompare').click()
        await page.wait_for_selector('#compareModal.open')
        assert await page.locator('#compareBody th').count()>=3
        await page.keyboard.press('Escape')
        assert not await page.locator('#compareModal').evaluate('el=>el.classList.contains("open")')
        passed.append('card selection and comparison across pages; Escape closes modal')
        await page.route('**/api/failing-test',lambda route:route.fulfill(status=500,json={'detail':'expected failure'}))
        await page.evaluate('Promise.all([api("/failing-test").catch(()=>{}),api("/failing-test").catch(()=>{})])')
        await page.wait_for_timeout(50)
        assert errors==[],errors
        passed.append('failed deduplicated GET has no unhandled rejection')
        # A roster response started before dashboard navigation cannot replace it.
        await page.route('**/api/streamers?*',slow_detail)
        await page.evaluate('state.virtualScroll=false;state.view="roster";loadView();setTimeout(()=>{state.view="dashboard";loadView()},10)')
        await page.wait_for_timeout(800)
        assert await page.evaluate('state.view')=='dashboard'
        assert await page.locator('#cardGrid').evaluate('el=>getComputedStyle(el).display')=='none'
        passed.append('roster/dashboard navigation race')
        await page.evaluate('currentPrefs.theme="system";applyPrefs(currentPrefs)')
        await page.emulate_media(color_scheme='light');await page.wait_for_timeout(50)
        assert await page.locator('html').get_attribute('data-theme')=='light'
        await page.emulate_media(color_scheme='dark');await page.wait_for_timeout(50)
        assert await page.locator('html').get_attribute('data-theme')=='dark'
        passed.append('live system theme changes')
        result=await page.evaluate('parseRosterSearch(\'category:"Just Chatting" followers:>100\')')
        assert result['parsed']['category']=='Just Chatting' and result['parsed']['minFollowers']==101
        assert await page.evaluate('timezoneOptionsHtml("US/Eastern").includes(\'value="US/Eastern" selected\')')
        passed.append('quoted search operators and saved timezone aliases')
        # Exercise the existing 422 fallback: all 125 results must survive paging.
        async def paging_fixture(route):
            query = parse_qs(urlparse(route.request.url).query)
            if int(query['page_size'][0]) > 50:
                await route.fulfill(status=422, json={'detail':'limit'})
                return
            page_number = int(query['page'][0])
            items = [{'username':'fixture%03d'%i} for i in range((page_number-1)*50, min(page_number*50,125))]
            await route.fulfill(json={'total':125,'items':items})
        await page.route('**/api/paging-fixture?*', paging_fixture)
        total=await page.evaluate('loadAllStreamerPages("/paging-fixture",{},new AbortController().signal).then(data=>data.items.length)')
        assert total==125,total
        passed.append('Show all fallback page size keeps every result')
        await page.evaluate('applyDiscoverFilters({tags:["Art","Chat"],exclude_tags:"vtuber",created_after:"2020-01-01"})')
        assert await page.locator('#discoverTags').input_value()=='Art,Chat'
        passed.append('Discover history restores all filters')
        await page.unroute('**/api/streamers?*', slow_detail)
        await page.evaluate('state.view="roster";state.virtualScroll=false;loadView()')
        await page.request.put(url+'/api/streamers/user00001/location',data={'location':'London','timezone':'Europe/London'})
        await page.evaluate('selectStreamer("user00001")')
        await page.locator('#detailNotes').fill('timezone toggle keeps this edit')
        toggle=page.locator('#detailContent [data-tz-toggle]')
        await toggle.evaluate('el=>{el.checked=!el.checked;el.dispatchEvent(new Event("change",{bubbles:true}))}')
        assert await page.locator('#detailNotes').input_value()=='timezone toggle keeps this edit'
        await page.evaluate('setDetailPanelEmpty(true);state.selectedUsername=null')
        await page.wait_for_timeout(100)
        passed.append('timezone display changes preserve unsaved form fields')
        await page.evaluate('state.view="dashboard";loadView()')
        await page.wait_for_timeout(100)
        reordered=await page.evaluate("""() => {
          const tiles=[...document.querySelectorAll('.dashboard-widget')];
          if (tiles.length<2) return false;
          const first=tiles[0].dataset.widget, target=tiles.at(-1).dataset.widget;
          const transfer=new DataTransfer();
          tiles[0].dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:transfer}));
          tiles.at(-1).dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}));
          return dashboardLayout.indexOf(first)===dashboardLayout.length-1 && dashboardLayout.includes(target);
        }""")
        assert reordered
        passed.append('dashboard drag/drop persists the new order')
        async def videos(route):
            if '/vods?' in route.request.url: await asyncio.sleep(.25)
            kind='clip' if '/clips?' in route.request.url else 'vod'
            await route.fulfill(json={'items':[{'title':kind,'url':'https://clips.twitch.tv/Fixture','duration':30}]})
        await page.route('**/api/streamers/user00001/vods?*',videos)
        await page.route('**/api/streamers/user00001/clips?*',videos)
        await page.evaluate('openVodModal("user00001");vodState.tab="clips";loadVodItems()')
        await page.wait_for_timeout(400)
        video_text=await page.locator('#vodList').inner_text()
        assert 'clip' in video_text and 'vod' not in video_text
        posts=[]
        async def download(route):
            posts.append(route.request.url)
            await asyncio.sleep(.15)
            await route.fulfill(json={'id':'fixture-job'})
        await page.route('**/api/vod-downloads',download)
        await page.evaluate('selectVod({title:"clip",url:"https://clips.twitch.tv/Fixture"});startVodDownload();startVodDownload();setTimeout(()=>closeModal("vodModal"),25)')
        await page.wait_for_timeout(350)
        assert len(posts)==1
        assert await page.evaluate('vodState.poll===null')
        passed.append('rapid VOD tab switches and duplicate downloads; close cleans polling')
        async def archived(route):
            query=parse_qs(urlparse(route.request.url).query)
            offset=int(query.get('offset',['0'])[0]);limit=int(query['limit'][0])
            await route.fulfill(json={'total':750,'items':[{'username':'archived%04d'%i,'archived':True} for i in range(offset,min(offset+limit,750))]})
        await page.route('**/api/archived?*',archived)
        await page.evaluate('state.view="archived";state.virtualScroll=true;loadView()')
        await page.wait_for_function('state.items.length===750')
        passed.append('Show all archived fetches beyond the first 500 rows')
        await page.evaluate('state.view="roster";state.virtualScroll=false;loadView()')
        await page.wait_for_timeout(150)
        await page.evaluate('selectStreamer("user00001")')
        await page.locator('#detailNotes').fill('saved before database replacement')
        await page.evaluate('window.fixtureCommitted=false;undoToast("fixture",()=>{window.fixtureCommitted=true});prepareDatabaseReplacement()')
        assert await page.evaluate('_pendingUndos.size')==0
        assert await page.evaluate('state.selectedUsername') is None
        before=await page.request.get(url+'/api/streamers/user00001')
        assert (await before.json())['notes']=='saved before database replacement'
        passed.append('database replacement flushes saves and cancels delayed Undo actions')
        await page.evaluate('currentPrefs.theme="light";savePrefs(currentPrefs);applyPrefs(currentPrefs)')
        await page.reload(wait_until='networkidle')
        assert await page.locator('html').get_attribute('data-theme')=='light'
        passed.append('settings persist across reload')
        assert errors==[],errors
        await browser.close()
    print(json.dumps({'passed':passed,'page_errors':errors},indent=2))


def main():
    with tempfile.TemporaryDirectory(prefix='scout-browser-') as temp:
        env=os.environ.copy();env.update(DASHBOARD_SCOUT_DATA_DIR=temp,SCOUTBOT_STANDALONE='1',PYTHONPATH=str(ROOT/'scout-backend'))
        seed='''import database as db; db.setup()
with db.db() as con:
 con.executemany("INSERT INTO streamers(username,url,followers,category,live_status) VALUES (?,?,?,?,?)",[("user%05d"%i,"https://twitch.tv/user%05d"%i,i,"Art" if i%2 else "Music","Live" if i==1 else "Offline") for i in range(10000)])
db.setup()
'''
        subprocess.run([sys.executable,'-c',seed],env=env,check=True)
        import socket
        with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url='http://127.0.0.1:'+str(port)
        with open(Path(temp)/'server.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','main:app','--host','127.0.0.1','--port',str(port)],env=env,cwd=ROOT/'scout-backend',stdout=log,stderr=log)
            try:
                for _ in range(100):
                    if server.poll() is not None:raise RuntimeError('Server failed: '+Path(temp,'server.log').read_text())
                    try:
                        with urllib.request.urlopen(url+'/api/version',timeout=.5):break
                    except Exception:time.sleep(.1)
                else:raise RuntimeError('Server did not start')
                asyncio.run(checks(url))
            finally:
                server.terminate()
                try:server.wait(timeout=15)
                except subprocess.TimeoutExpired:server.kill();server.wait();raise RuntimeError('Shutdown did not complete')

if __name__=='__main__':main()

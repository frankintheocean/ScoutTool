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
        assert await page.evaluate('firstVirtualOffsetAtLeast([0,10,35,80],11)')==2
        assert await page.evaluate('firstVirtualOffsetAtLeast([0,10,35,80],10)')==1
        assert await page.evaluate('firstVirtualOffsetAtLeast([0,10,35,80],100)')==3
        await page.wait_for_function('document.getElementById("cardGrid")._virtualLayout !== null')
        assert await page.evaluate('''() => { const grid=document.getElementById("cardGrid");const offsets=grid._virtualLayout.offsets;renderVirtualGrid();return grid._virtualLayout?.offsets===offsets; }''')
        passed.append('virtual-scroll offset boundaries and reuse without rebuilding')
        await page.route('https://player.twitch.tv/**', lambda route: route.abort())
        preview=page.locator('[data-preview-toggle="user00001"]')
        await preview.click()
        assert await page.locator('[data-preview-embed="user00001"] iframe').count()==1
        await page.evaluate('''() => { window.fixturePreviewLoads=0;document.querySelector('[data-preview-embed="user00001"] iframe').srcdoc="<script>parent.fixturePreviewLoads++<"+"/script>"; }''')
        await page.wait_for_function('window.fixturePreviewLoads===1')
        await page.evaluate('renderVirtualGrid();renderVirtualGrid();renderVirtualGrid()')
        await page.wait_for_timeout(150)
        assert await page.evaluate('window.fixturePreviewLoads')==1
        await preview.click()
        assert await page.locator('[data-preview-embed="user00001"] iframe').count()==0
        passed.append('virtual-grid live preview opens, resizes its row, and cleans up')
        await page.set_viewport_size({'width':900,'height':600})
        await page.evaluate('selectStreamer("user00001")')
        await page.wait_for_selector('#detailClose',state='visible')
        betterbanned_link = page.locator('#detailBetterBannedLink')
        assert await betterbanned_link.get_attribute('href')=='https://betterbanned.com/en/streamer/user00001'
        assert await betterbanned_link.get_attribute('target')=='_blank'
        assert 'noopener' in await betterbanned_link.get_attribute('rel')
        assert 'noreferrer' in await betterbanned_link.get_attribute('rel')
        passed.append('BetterBanned profile link uses the channel username and opens a separate tab safely')
        await page.locator('#detailNotes').fill('saved before close')
        await page.locator('#detailClose').click()
        await page.wait_for_timeout(300)
        result=await page.request.get(url+'/api/streamers/user00001')
        assert (await result.json())['notes']=='saved before close'
        passed.append('small-window detail controls and autosave on close')
        await page.set_viewport_size({'width':1400,'height':900})
        from test_betterbanned import PAGE
        await page.evaluate('selectStreamer("user00001")')
        await page.wait_for_selector('#detailActivitySave',state='attached')
        await page.locator('#detailActivitySection summary').click()
        await page.locator('#detailActivityText').fill(PAGE)
        await page.locator('#detailActivitySave').click()
        await page.wait_for_function('document.getElementById("detailActivityStatus").textContent==="Copied activity saved."')
        assert await page.locator('#detailActivityRows .identity-history-row').count()==13
        assert 'Total bans: 2' in await page.locator('#detailActivitySummary').inner_text()
        assert 'Copied page text' in await page.locator('#detailActivitySummary').inner_text()
        await page.locator('#detailActivityFilter').select_option('bans')
        assert await page.locator('#detailActivityRows .identity-history-row').count()==4
        refresh_calls=[]
        async def failed_refresh(route):
            refresh_calls.append(1)
            await asyncio.sleep(.1)
            await route.fulfill(status=502,json={'detail':'BetterBanned blocked access. Saved history was preserved.'})
        await page.route('**/api/streamers/user00001/activity/refresh',failed_refresh)
        await page.evaluate('document.getElementById("detailActivityRefresh").click();document.getElementById("detailActivityRefresh").click()')
        await page.wait_for_function('document.getElementById("detailActivityStatus").textContent.includes("blocked access")')
        assert len(refresh_calls)==1
        assert await page.locator('#detailActivityRows .identity-history-row').count()==4
        passed.append('copied activity persists, filters 13 events to four ban/unban records, and survives a failed duplicate refresh')
        identity_calls=[]
        async def rename_identity(route):
            identity_calls.append(1)
            await asyncio.sleep(.1)
            await route.fulfill(json={'twitch_id':'9007199254740993','current_username':'renameduser','checked_at':'2026-10-06T00:00:00Z','history':[{'id':999,'previous_username':'user00001','new_username':'renameduser','source':'observed','observed_at':'2026-10-06T00:00:00Z'}]})
        await page.route('**/api/streamers/user00001/identity/check',rename_identity)
        await page.evaluate('document.getElementById("detailCheckUsername").click();document.getElementById("detailCheckUsername").click()')
        await page.wait_for_function('document.getElementById("detailCurrentUsername").textContent==="renameduser"')
        assert len(identity_calls)==1
        assert await page.locator('#detailTwitchId').input_value()=='9007199254740993'
        assert await page.locator('#detailTwitchLink').get_attribute('href')=='https://twitch.tv/renameduser'
        assert await page.locator('#detailBetterBannedLink').get_attribute('href')=='https://betterbanned.com/en/streamer/renameduser'
        assert await page.locator('#detailNotes').input_value()=='saved before close'
        assert await page.locator('#detailUsernameHistory [data-remove-identity-history]').count()==0
        passed.append('confirmed rename keeps precise numeric ID, notes and immutable record actions; updates both profile links and deduplicates checks')
        await page.locator('#detailClose').click()
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
        zero_requests=[]
        async def zero_streams(route):
            query=parse_qs(urlparse(route.request.url).query)
            zero_requests.append(query)
            phrase=query.get('include',[''])[0]
            if phrase=='slow': await asyncio.sleep(.25)
            if phrase=='error':
                await route.fulfill(status=503,json={'detail':'nobody.live is busy. Try again.'})
                return
            names=['mario_runner','chess_runner'] if query.get('match',['all'])[0]=='any' else ['mario_runner']
            await route.fulfill(json={'source':'nobody.live','items':[{'username':name,'display_name':name,'category':'Mario','tags':['speedrun', 'x'*180, '日本語'*70], 'title':'<img src=x onerror=alert(1)>' + ' long title '*40,'live_viewers':0,'already_tracked':False} for name in names]})
        await page.route('**/api/discover/zero-viewers?*',zero_streams)
        await page.route('https://static-cdn.jtvnw.net/**',lambda route:route.abort())
        await page.locator('#btnDiscover').click()
        await page.locator('#discoveryTabZero').click()
        await page.locator('#nobodySearchPhrase').fill('mario speedrun')
        await page.locator('#nobodyRememberFilters').check()
        await page.locator('#nobodyDiscoverSubmit').click()
        await page.wait_for_function('document.querySelectorAll(".zero-viewer-card").length===1')
        assert zero_requests[-1]['include']==['mario speedrun']
        assert zero_requests[-1]['match']==['all']
        assert await page.locator('.zero-viewer-card img').count()==1
        assert '<img src=x' in await page.locator('.zero-viewer-card').inner_text()
        await page.locator('#nobodySearchMatch').select_option('any')
        await page.locator('#nobodyDiscoverSubmit').click()
        await page.wait_for_function('document.querySelectorAll(".zero-viewer-card").length===2')
        passed.append('separate zero-viewer module sends game/tag all/any filters and escapes results')
        await page.set_viewport_size({'width':900,'height':650})
        box=await page.locator('#nobodyDiscoverSubmit').bounding_box()
        assert box and box['x']>=0 and box['x']+box['width']<=900 and box['y']+box['height']<=650
        await page.set_viewport_size({'width':1400,'height':900})
        await page.keyboard.press('Escape')
        await page.reload(wait_until='networkidle')
        await page.locator('#btnDiscover').click()
        await page.locator('#discoveryTabZero').click()
        assert await page.locator('#nobodySearchPhrase').input_value()=='mario speedrun'
        assert await page.locator('#nobodySearchMatch').input_value()=='any'
        assert await page.locator('#nobodyRememberFilters').is_checked()
        await page.locator('#nobodyRememberFilters').uncheck()
        assert await page.evaluate('localStorage.getItem("scoutbot_zero_viewer_filters")') is None
        passed.append('zero-viewer Remember filters survives reload and unchecking removes it')
        await page.locator('#nobodySearchPhrase').fill('slow')
        await page.evaluate('document.getElementById("nobodyDiscoverForm").requestSubmit()')
        await page.locator('#nobodySearchPhrase').fill('mario')
        await page.evaluate('document.getElementById("nobodyDiscoverForm").requestSubmit()')
        await page.wait_for_timeout(350)
        assert await page.locator('.zero-viewer-card').count()==2
        await page.locator('#nobodySearchPhrase').fill('slow')
        await page.evaluate('document.getElementById("nobodyDiscoverForm").requestSubmit()')
        await page.evaluate('closeModal("discoverModal")')
        await page.wait_for_timeout(350)
        assert not await page.locator('#discoverModal').evaluate('(el)=>el.classList.contains("open")')
        assert await page.locator('.zero-viewer-card').count()==0
        assert not await page.locator('#nobodyDiscoverSubmit').is_disabled()
        passed.append('zero-viewer rapid searches and close discard stale results and cancel work')
        await page.locator('#btnDiscover').click()
        await page.locator('#discoveryTabZero').click()
        await page.locator('#nobodySearchPhrase').fill('error')
        await page.locator('#nobodyDiscoverSubmit').click()
        await page.wait_for_function('document.getElementById("nobodyDiscoverStatus").textContent.includes("busy")')
        assert not await page.locator('#nobodyDiscoverSubmit').is_disabled()
        await page.locator('#nobodySearchPhrase').fill('mario')
        await page.locator('#nobodyDiscoverSubmit').click()
        await page.wait_for_function('document.querySelectorAll(".zero-viewer-card").length===2')
        await page.evaluate('''() => { const set=Storage.prototype.setItem; window.fixtureStorageSet=set; Storage.prototype.setItem=function(key,value){if(key==="scoutbot_zero_viewer_filters")throw new DOMException("fixture quota","QuotaExceededError");return set.call(this,key,value)} }''')
        await page.locator('#nobodyRememberFilters').check()
        assert 'could not save' in await page.locator('#nobodyDiscoverStatus').inner_text()
        await page.locator('#nobodyDiscoverSubmit').click()
        await page.wait_for_function('document.getElementById("nobodyDiscoverStatus").textContent.includes("streams found")')
        assert 'could not save' in await page.locator('#nobodyDiscoverStatus').inner_text()
        passed.append('zero-viewer service errors are retryable and storage quota does not break search')
        await page.evaluate('prepareDatabaseReplacement()')
        assert await page.locator('.zero-viewer-card').count()==0
        assert 'Database changed' in await page.locator('#nobodyDiscoverStatus').inner_text()
        await page.evaluate('closeModal("discoverModal");Storage.prototype.setItem=window.fixtureStorageSet;localStorage.setItem("scoutbot_zero_viewer_filters","[]")')
        await page.reload(wait_until='networkidle')
        assert await page.locator('#nobodySearchPhrase').input_value()==''
        assert not await page.locator('#nobodyRememberFilters').is_checked()
        passed.append('malformed remembered zero-viewer filter data is safely ignored')
        assert await page.evaluate('''() => { const grid=document.getElementById("cardGrid");const virtual=state.virtualScroll;state.virtualScroll=true;state.view="dashboard";teardownVirtualGrid(grid);renderVirtualGrid();const clean=!grid.classList.contains("card-grid-virtual");state.view="roster";state.virtualScroll=virtual;renderGrid();return clean; }''')
        passed.append('queued virtual rendering cannot resurrect the grid after switching to Dashboard')
        # Shared discovery preserves independent filters/results and chips.
        await page.route('**/api/discover?*', lambda route: route.fulfill(json={'items':[], 'next_cursor':None}))
        await page.locator('#btnDiscover').click()
        await page.locator('#discoveryTabZero').click()
        await page.locator('#nobodySearchPhrase').fill('mario speedrun')
        await page.locator('#nobodyRememberFilters').check()
        await page.locator('#nobodyDiscoverSubmit').click()
        await page.wait_for_function('document.querySelectorAll(".zero-viewer-card").length===1')
        await page.locator('#discoveryTabTwitch').click()
        await page.locator('#discoverMinViewers').fill('0')
        await page.locator('#discoverTags').fill('Art,Chat')
        await page.locator('#discoverSubmit').click()
        await page.wait_for_function('document.getElementById("discoverResults").textContent.includes("No live streamers")')
        assert 'Min viewers: 0' in await page.locator('#discoverFilterChips').inner_text()
        await page.locator('#discoverFilterChips button',has_text='Clear filters').click()
        assert await page.locator('#discoverTags').input_value()==''
        assert await page.locator('#nobodySearchPhrase').input_value()=='mario speedrun'
        await page.locator('#discoveryTabTwitch').focus()
        await page.keyboard.press('ArrowRight')
        assert await page.locator('#discoveryTabZero').get_attribute('aria-selected')=='true'
        assert await page.locator('.zero-viewer-card').count()==1
        passed.append('shared discovery tabs preserve results and independent filters; Twitch chips include zero and clear only their tab')
        for width in (1360,900,600):
            await page.set_viewport_size({'width':width,'height':800})
            for size in ('smaller','normal','larger'):
                await page.evaluate('(size)=>applyPrefs({...currentPrefs,fontSize:size})',size)
                metrics=await page.evaluate("""() => {
                  const header=document.querySelector('.topbar').getBoundingClientRect();
                  const controls=[...document.querySelectorAll('.topbar-actions > .btn,.tools-menu summary')];
                  const modal=document.querySelector('#discoverModal .modal').getBoundingClientRect();
                  const card=document.querySelector('.zero-viewer-card').getBoundingClientRect();
                  const tags=[...document.querySelectorAll('.zero-viewer-tags > span')].map(el=>el.getBoundingClientRect());
                  return {headerContains:controls.every(el=>{const r=el.getBoundingClientRect();return r.top>=header.top&&r.bottom<=header.bottom&&r.right<=innerWidth;}),
                    modalFits:modal.top>=0&&modal.bottom<=innerHeight&&modal.left>=0&&modal.right<=innerWidth,
                    tagsFit:tags.every(r=>r.left>=card.left&&r.right<=card.right),
                    titleSize:parseFloat(getComputedStyle(document.querySelector('.zero-viewer-title')).fontSize)};
                }""")
                assert metrics['headerContains'],(width,size,metrics)
                assert metrics['modalFits'],(width,size,metrics)
                assert metrics['tagsFit'],(width,size,metrics)
                assert metrics['titleSize']==12,(width,size,metrics)
        await page.evaluate('applyPrefs(currentPrefs)')
        await page.set_viewport_size({'width':1360,'height':800})
        await page.screenshot(path='/workspace/scout-discovery-v42.png')
        passed.append('header bounds, modal viewport fit, long titles and Unicode/unbroken tags at 600/900/1360px in three text scales')
        await page.locator('#nobodyFilterChips button',has_text='Search:').click()
        await page.wait_for_function('document.getElementById("nobodySearchPhrase").value==="" && !document.getElementById("nobodyDiscoverSubmit").disabled')
        assert await page.evaluate('JSON.parse(localStorage.getItem("scoutbot_zero_viewer_filters")).include')==''
        assert await page.locator('#nobodyRememberFilters').is_checked()
        await page.locator('#nobodySearchMatch').select_option('any')
        await page.locator('#nobodyFilterChips button',has_text='Clear filters').click()
        await page.wait_for_function('!document.getElementById("nobodyDiscoverSubmit").disabled')
        assert await page.locator('#nobodySearchMatch').input_value()=='all'
        assert await page.locator('#nobodyFilterChips button').count()==0
        passed.append('zero-viewer removable chips and Clear filters update remembered filters')
        await page.keyboard.press('Escape')
        await page.evaluate('state.view="roster";state.search="old";state.priority="High";loadView()')
        await page.wait_for_selector('#rosterFilterChips button',state='visible')
        await page.evaluate("""() => { const input=document.getElementById('searchInput');input.value='stale pending';input.dispatchEvent(new Event('input'));clearRosterFilters(); }""")
        await page.wait_for_timeout(250)
        assert await page.evaluate('state.search==="" && state.priority===""')
        assert await page.locator('#rosterFilterChips button').count()==0
        assert await page.locator('#btnSortDir').evaluate('el=>el.getBoundingClientRect().width<200')
        assert await page.locator('#btnRefreshView').evaluate('el=>el.getBoundingClientRect().width>=30')
        assert await page.evaluate('''() => { const heights=['btnRefreshView','btnSortDir','densityToggle','sortBy'].map(id=>document.getElementById(id).getBoundingClientRect().height);return Math.max(...heights)-Math.min(...heights)<2; }''')
        await page.screenshot(path='/workspace/scout-roster-v42.png')
        passed.append('roster Clear filters cancels pending search debounce and toolbar direction button stays compact')
        await page.locator('#toolsMenu summary').click()
        assert await page.locator('#btnSettings').is_visible()
        await page.keyboard.press('Escape')
        assert not await page.locator('#toolsMenu').evaluate('el=>el.open')
        await page.locator('#toolsMenu summary').click()
        await page.locator('#btnChangelog').click()
        assert await page.locator('#changelogModal').evaluate('el=>el.classList.contains("open")')
        assert not await page.locator('#toolsMenu').evaluate('el=>el.open')
        await page.keyboard.press('Escape')
        assert await page.locator('#toolsMenu summary').evaluate('el=>el===document.activeElement')
        passed.append('Tools menu retains secondary actions, closes on selection/Escape, and restores visible focus')
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

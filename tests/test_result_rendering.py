"""Real Chromium rendering; local assets only, no model or external HTTP."""
import functools
import hashlib
import json
import os
from pathlib import Path
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

STATIC=Path(__file__).resolve().parents[1]/'src/agentlab_app/static'


def test_vendored_renderer_assets_match_frozen_manifest():
    manifest=json.loads((STATIC/'vendor/manifest.json').read_text())
    for package in manifest['packages']:
        assert package['integrity'].startswith('sha512-')
        assert any('LICENSE' in name for name in package['files'])
        for name,expected in package['files'].items():
            assert hashlib.sha256((STATIC/'vendor'/name).read_bytes()).hexdigest()==expected


@pytest.fixture(scope='module')
def browser_page():
    playwright=pytest.importorskip('playwright.sync_api')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/fixture':
                body=b'''<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/vendor/katex.css"><div id="result" class="rich-answer"></div><script src="/vendor/marked.js"></script><script src="/vendor/purify.js"></script><script src="/vendor/katex.js"></script><script src="/result.js"></script>'''
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(body)
            else:super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(STATIC)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with playwright.sync_playwright() as pw:
            kwargs={'headless':True,'args':['--no-sandbox']}
            if os.environ.get('AGENTLAB_CHROMIUM'):kwargs['executable_path']=os.environ['AGENTLAB_CHROMIUM']
            browser=pw.chromium.launch(**kwargs);page=browser.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}/fixture')
            yield page
            browser.close()
    finally:server.shutdown();server.server_close();thread.join()


def draw(page,text):
    page.evaluate('text=>AgentResult.render(document.getElementById("result"),text)',text)


def test_nested_replay_answer_unwraps_without_rewriting_content(browser_page):
    p=browser_page
    raw={'id':'run-private','profile':'live','result':{'answer':'**Hello**\n\n`read_file`','children':[{'answer':'not the root answer'}]}}
    p.evaluate('raw=>AgentResult.render(document.getElementById("result"),AgentResult.answer(raw))',raw)
    assert p.locator('#result strong').inner_text()=='Hello'
    assert p.locator('#result code').inner_text()=='read_file'
    assert 'run-private' not in p.locator('#result').inner_text()
    assert p.evaluate('AgentResult.answer({result:{answer:""}})')=='本次没有返回可用回答。'


def test_markdown_tables_code_and_compact_headings(browser_page):
    p=browser_page
    draw(p,'# Title\n\n**bold** and `inline`\n\n- first\n- second\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint("$x^2$")\n```')
    assert p.locator('#result h1').inner_text()=='Title'
    assert p.locator('#result li').count()==2 and p.locator('#result table').count()==1
    assert p.locator('#result pre code').inner_text()=='print("$x^2$")\n'
    assert p.locator('#result pre .katex').count()==0
    assert p.locator('#result h1').evaluate('n=>parseFloat(getComputedStyle(n).fontSize)')<=16


@pytest.mark.parametrize('source,display',[(r'Inline $x_i^2+1$ text',False),(r'Inline \(\frac{1}{2}\) text',False),('$$\nx^2+y^2=z^2\n$$',True),(r'\[\sum_{i=1}^{n}i\]',True)])
def test_math_delimiters_render_without_markdown_corruption(browser_page,source,display):
    p=browser_page;draw(p,source)
    assert p.locator('#result .katex').count()==1
    assert bool(p.locator('#result .katex-display').count())==display
    assert p.locator('#result .katex-error').count()==0
    assert p.locator('#result math').count()==1


def test_untrusted_html_links_tex_and_partial_stream(browser_page):
    p=browser_page;external=[]
    def track(request):
        if not request.url.startswith('http://127.0.0.1:'):external.append(request.url)
    p.on('request',track)
    draw(p,'<img src="https://evil.invalid/a" onerror="window.pwned=1"><script>window.pwned=1</script><svg onload="window.pwned=1"></svg><a href="javascript:alert(1)">bad</a>\n\n$\\href{javascript:alert(1)}{click}$\n\n<span data-agent-math="0">original</span>')
    assert not p.evaluate('Boolean(window.pwned)')
    assert p.locator('#result img,#result script,#result [onload],#result [onerror],#result a[href^="javascript:"]').count()==0
    assert not external
    draw(p,'Partial **bold and $x_')
    draw(p,'Complete **bold** and $x_i$')
    assert p.locator('#result strong').inner_text()=='bold' and p.locator('#result .katex').count()==1
    draw(p,r'Cost $5 and $10; malformed $\unknownmacro{x}$')
    assert 'Cost $5 and $10' in p.locator('#result').inner_text()
    assert p.locator('#result').inner_text()
    p.remove_listener('request',track)


def test_history_is_readable_and_tools_folded(browser_page):
    p=browser_page
    data={'messages':[{'role':'system','content':'INTERNAL_SYSTEM'}, {'role':'user','content':'Question'},
        {'role':'assistant','content':'','tool_calls':[{'id':'call'}]},
        {'role':'tool','content':'RAW_TOOL_JSON'}, {'role':'assistant','content':'**Answer** $x^2$'}]}
    p.evaluate('d=>AgentResult.history(document.getElementById("result"),d)',data)
    assert p.locator('#result .chat-message').count()==4
    assert p.locator('#result details').count()==2 and p.locator('#result details[open]').count()==0
    assert p.locator('#result .assistant strong').inner_text()=='Answer'
    assert p.locator('#result .katex').count()==1 and 'INTERNAL_SYSTEM' not in p.locator('#result').inner_text()


def test_replay_groups_preserve_all_events_and_errors(browser_page):
    p=browser_page
    p.add_script_tag(url=p.url.replace('/fixture','/replay.js'))
    events=[{'seq':i,'event':'execution.progress','instance':'model' if i%2 else 'child',
             'detail':{'kind':'text_delta','text':'x'}} for i in range(1300)]
    error={'seq':1300,'event':'execution.failed','instance':'workspace',
           'error':{'cause':{'code':'WORKSPACE.NOT_FILE'}}}
    complete={'seq':1301,'event':'execution.progress','detail':{'kind':'complete'}}
    events.extend([error,complete])
    result=p.evaluate('rows=>({groups:AgentReplay.groups(rows),raw:AgentReplay.groups(rows,true),label:AgentReplay.label([rows[1300]])})',events)
    assert [r for group in result['groups'] for r in group]==events
    assert [len(g) for g in result['groups']]==[512,512,276,1,1]
    assert len(result['raw'])==len(events) and all(len(g)==1 for g in result['raw'])
    assert 'WORKSPACE.NOT_FILE' in result['label']
    assert p.evaluate('rows=>AgentReplay.visible(rows)',events)==[error]
    assert p.evaluate('rows=>AgentReplay.visible(rows,true)',events)==events
    response={'event':'plugin.detail','topic':'model.response','instance':'model','detail':{'answer':'assembled'}}
    assert p.evaluate('rows=>AgentReplay.visible(rows)',events+[response])==[error,response]

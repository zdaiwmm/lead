"""Real Chrome + pinned BrowserSession tools. No model, account or database."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api',type=Path,required=True)
    parser.add_argument('--sdk-site',type=Path,required=True)
    parser.add_argument('--chrome',type=Path,required=True)
    args=parser.parse_args()
    scope={}
    authorizations=[]

    class Board(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_GET(self):
            body=(b'<!doctype html><title>Actual next page</title><main><h1 id="receipt">Tool navigation succeeded</h1></main>'
                if self.path=='/second' else b'<!doctype html><title>Actual tool fixture</title><main><h1>Original page</h1><a id="actual-link" href="/second">Read next page</a></main>')
            self.send_response(200)
            self.send_header('Content-Type','text/html')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if request['operation']=='scope':
                result=scope
            elif request['operation']=='tool_authorize':
                authorizations.append(request['args'])
                result={'allowed':True}
            else:
                result={'error':'unexpected-manager-operation'}
            body=json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server=ThreadingHTTPServer(('127.0.0.1',0),Board)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    with tempfile.TemporaryDirectory(prefix='codex-browser-worker-') as folder:
        chrome=subprocess.Popen([str(args.chrome),'--headless=new','--no-sandbox','--disable-dev-shm-usage',
            '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--no-first-run',
            '--user-data-dir='+folder,url+'/'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        worker=None
        try:
            portfile=Path(folder)/'DevToolsActivePort'
            deadline=time.monotonic()+10
            while not portfile.exists() and time.monotonic()<deadline:
                time.sleep(.05)
            port=portfile.read_text().splitlines()[0]
            endpoint='http://127.0.0.1:'+port
            with urlopen(endpoint+'/json/list',timeout=5) as response:
                targets=json.load(response)
            target=next(row['id'] for row in targets if row.get('type')=='page')
            scope.update(task_id='original',generation=1,target_id=target,targets={target:target},origins=[url])
            env={key:value for key,value in os.environ.items() if key in ('PATH','HOME','LANG','LC_ALL','TZ')}
            env.update(PYTHONPATH=str(args.sdk_site)+os.pathsep+str(args.api),ANONYMIZED_TELEMETRY='false',
                BROWSER_USE_CLOUD_SYNC='false',BROWSER_USE_VERSION_CHECK='false',BROWSER_USE_SETUP_LOGGING='false')
            worker=subprocess.Popen([sys.executable,'-B','-S','-m','lead_app.local_executor_browser','worker'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
            serial=0
            def rpc(name,arguments):
                nonlocal serial
                serial+=1
                worker.stdin.write(json.dumps({'id':serial,'name':name,'arguments':arguments})+'\n')
                worker.stdin.flush()
                while line:=worker.stdout.readline():
                    try:
                        result=json.loads(line)
                    except ValueError:
                        continue
                    if result.get('id')==serial:
                        assert 'error' not in result,result
                        return result['result']
                raise AssertionError({'worker_exit':worker.poll(),'stderr':worker.stderr.read()[-3000:]})
            rpc('initialize',{'scope':{**scope,'cdp_url':endpoint,'headers':{}},'kind':'discovery',
                'gateway_endpoint':url,'gateway_token':'fixture-only','agent_id':'fixture','run_id':'fixture',
                'command_path':'native/task-command','sensitive_data':{}})
            first=rpc('browser_observe',{})
            assert first['structuredContent']['title']=='Actual tool fixture',first['structuredContent']
            assert any(item['type']=='image' for item in first['content']), 'real screenshot missing'
            next_page=rpc('browser_navigate',{'url':url+'/second'})
            assert not next_page.get('isError'),next_page
            second=rpc('browser_observe',{})
            assert second['structuredContent']['url']==url+'/second',second['structuredContent']
            assert 'Tool navigation succeeded' in second['structuredContent']['dom'],second['structuredContent']
            assert authorizations and authorizations[-1]['name']=='navigate'
            assert not any(key in json.dumps(first) for key in ('cdp_url','gateway_token'))
            worker.stdin.close()
            worker.wait(timeout=10)
            assert worker.returncode==0,worker.stderr.read()[-2000:]
            print(json.dumps({'real_chrome':True,'real_browser_session':True,'real_screenshot':True,
                'guarded_navigation':True,'model_requests':0,'database_access':False,'worker_exited':True}))
        finally:
            if worker and worker.poll() is None:
                worker.kill()
                worker.wait()
            chrome.terminate()
            chrome.wait(timeout=10)
            server.shutdown()


if __name__=='__main__':
    main()

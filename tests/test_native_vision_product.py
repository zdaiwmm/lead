"""Official Flash request shape, actual SDK/Chrome, loopback-only model I/O."""
import hashlib
import json
import os
import shlex

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_actual_sdk_current_page_image_reaches_official_flash_user_parts(monkeypatch):
    from lead_app import account_profiles
    from lead_app.config import STATE
    from native_connection_product import verify_login

    python=os.environ['LEAD_BROWSER_RUNTIME_PYTHON']
    bootstrap=STATE/'vision-fixture-bootstrap.py'
    interpreter=STATE/'vision-fixture-python'
    def transport(loopback):
        bootstrap.write_text('''import runpy,sys,httpx
real=httpx.AsyncHTTPTransport
class LocalOnly(httpx.AsyncBaseTransport):
 async def handle_async_request(self,request):
  if request.url.scheme=='http' and request.url.host=='127.0.0.1':
   async with httpx.AsyncClient(transport=real(trust_env=False),trust_env=False,timeout=45) as client:
    return await client.send(request)
  assert request.url.scheme=='https' and request.url.host=='api.deepseek.com'
  async with httpx.AsyncClient(transport=real(trust_env=False),trust_env=False,timeout=45) as client:
   response=await client.post(LOOPBACK,content=request.content,headers={'Content-Type':'application/json'})
   return httpx.Response(response.status_code,headers=response.headers,content=response.content,request=request)
httpx.AsyncHTTPTransport=lambda **kwargs:LocalOnly()
path=next(value for value in sys.argv[1:] if value not in ('-B','-S'))
runpy.run_path(path,run_name='__main__')
'''.replace('LOOPBACK',repr(loopback)))
        bootstrap.chmod(0o600)
        interpreter.write_text('#!/bin/sh\nexec '+shlex.quote(python)+' -B -S '+shlex.quote(str(bootstrap))+' "$@"\n')
        interpreter.chmod(0o700)
        monkeypatch.setenv('LEAD_BROWSER_RUNTIME_PYTHON',str(interpreter))
        return {'provider':'deepseek','key':'synthetic-transport-only','model':'deepseek-flash','base_url':'https://api.deepseek.com'}
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    result=await verify_login(monkeypatch,mode='vision_login',model_transport=transport)
    safe={'account_state':result['account']['state'],'usage':result['usage'],
        'request_image_counts':[row['image_parts'] for row in result['synthetic_model_diagnostics']],
        'authentication_posts':sum(row['method']=='POST' and row['path']=='/session' for row in result['oracle']['requests']),
        'bootstrap_sha256':hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
        'plaintext_literal_checks':result['privacy']}
    (STATE/'vision-sdk-product.json').write_text(json.dumps(safe,indent=2))
    assert safe['account_state']=='connected' and safe['authentication_posts']==1
    assert safe['usage']['request_count']==4 and safe['usage']['billing_unknown'] is False
    assert 1<=safe['usage']['vision_images_sent']<=4
    assert sum(safe['request_image_counts'])==safe['usage']['vision_images_sent']
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}

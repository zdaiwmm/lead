"""Known write/admin control values cannot ride a genuine credential endpoint."""
import os

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_authentication_control_values_are_checked_in_actual_dom_and_outbound_body(monkeypatch):
    import test_native_boundary_product as boundary

    async def check(data):
        actor,controller,page,site=(data[key] for key in ('actor','controller','page','site'))
        cdp=await actor.context.new_cdp_session(page)
        try:
            for name,value in (('action','delete_account'),('operation','purchase'),('role','admin')):
                await page.evaluate('''([name,value])=>{const form=document.createElement('form');form.id='login-control-test';
                    form.method='post';form.action='/session';form.innerHTML='<h2>Sign in</h2><input name="username"><input type="password" name="password">';
                    const field=document.createElement('input');field.type='hidden';field.name=name;field.value=value;form.append(field);document.body.append(form);
                }''',[name,value])
                document=(await cdp.send('DOM.getDocument'))['root']['nodeId']
                selected=await cdp.send('DOM.querySelector',{'nodeId':document,'selector':'#login-control-test input[name=username]'})
                described=(await cdp.send('DOM.describeNode',{'nodeId':selected['nodeId']}))['node']
                with pytest.raises(ValueError,match='authentication_form_dangerous_control'):
                    await controller.authorize({'name':'input','values':{'text':'saved_username'},'node':{
                        'target_id':controller.lease['target_id'],'backend_node_id':described['backendNodeId']}})
                await page.locator('#login-control-test').evaluate('node=>node.remove()')
                denied=await page.evaluate('''async values=>{try{await fetch('/session',{method:'POST',body:new URLSearchParams(values)});return false}
                    catch{return true}}''',{'username':site.username,'password':site.password,'nonce':'synthetic-control-test',name:value})
                assert denied and data['source_count']('POST','/session')==1
        finally:
            await cdp.detach()
        return {'unsafe_controls_denied_before_input_and_wire':3,'source_login_posts':1}

    await boundary.test_connection_session_keeps_sw_cache_and_nonpublication_posts(monkeypatch,boundary_hook=check)

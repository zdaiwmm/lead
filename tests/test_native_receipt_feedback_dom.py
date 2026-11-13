"""Real B DOM reproduces the model's failed paragraph selectors without a model."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lead_app.native_action import NativeAction
from test_native_member_automation_dom import observed_b_rules  # noqa: F401

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',
    reason='Explicit isolated B receipt DOM verification required')]


@pytest.mark.parametrize('paragraph,code',[(3,'receipt_body_selector_unavailable'),
    (2,'receipt_body_value_mismatch')])
async def test_actual_model_receipt_selectors_fail_with_fixed_body_feedback(observed_b_rules,paragraph,code):  # noqa: F811
    page,_,_,_,board=observed_b_rules
    text='An isolated real DOM publication for value-selector diagnosis.'
    await page.locator('#community-words').fill(text)
    await page.locator('button[type=submit]').click()
    await page.wait_for_url(board.origin+'/bulletin/7001')
    controller=NativeAction.__new__(NativeAction)
    controller.action={'type':'publish','intent':{'body':text,'browser_method':{'target_url':board.origin+'/house-rules'}}}
    controller.actor=SimpleNamespace()
    controller.page=page
    controller.snapshot=lambda:({}, {'external_account_id':board.state['username']}, {})
    controller.descriptor=AsyncMock()
    async def document(current=None):
        return (current or page).url
    controller.document=document
    selectors={'author':f'#note-7001 a[href="{board.origin}/members/{board.state["account_id"]}"]',
        'body':f'#note-7001 > p:nth-of-type({paragraph})','url':'#note-7001 a[rel="bookmark"]'}
    assert await controller.read_receipt(page,selectors) is None
    with pytest.raises(ValueError) as caught:
        await controller.report_receipt({'selectors':selectors})
    assert getattr(caught.value,'code',None)==code
    correct={**selectors,'body':'#note-7001 > p:first-of-type'}
    proven=await controller.read_receipt(page,correct)
    assert proven['body']==text and proven['author']==board.state['username']
    assert proven['url']==page.url
    assert sum(row['method']=='POST' for row in board.oracle()['requests'])==2

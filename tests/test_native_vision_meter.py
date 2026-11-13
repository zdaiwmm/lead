"""Current PNG accounting permits official vision without weakening text checks."""
import base64
import io
import json
import random

import httpx
import pytest
from lead_app.native_model_meter import ModelCallMeter
from PIL import Image


@pytest.mark.parametrize('text_secret',[False,True])
@pytest.mark.asyncio
async def test_current_png_then_sdk_text_prompt_is_counted_once_without_scanning_base64(text_secret):
    raw=io.BytesIO()
    Image.frombytes('RGB',(128,128),random.Random(1).randbytes(128*128*3)).save(raw,format='PNG')
    image=base64.b64encode(raw.getvalue()).decode()
    assert '7' in image
    meter=ModelCallMeter(max_requests=8,max_total_tokens=40000,max_output_tokens=1800,
        sensitive_data={'subject':'7'},image_model='deepseek-flash')
    async def current():
        return True
    meter.approve_image(image,current)
    body={'model':'deepseek-flash','messages':[{'role':'user','content':[
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+image}}]},
        {'role':'user','content':'7' if text_secret else 'Continue the current plan.'}]}
    assert len(json.dumps(body).encode())>40000
    reached=[]
    async def upstream(request):
        reached.append(True)
        return httpx.Response(200,json={'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            event_hooks={'request':[meter.measure_request],'response':[meter.measure_response]}) as client:
        if text_secret:
            with pytest.raises(ValueError,match='Credential redaction boundary'):
                await client.post('https://api.deepseek.com/chat/completions',json=body)
        else:
            await client.post('https://api.deepseek.com/chat/completions',json=body)
    assert len(reached)==meter.vision_images_sent==len(meter.provider_requests)==(0 if text_secret else 1)
    assert not meter.billing_unknown

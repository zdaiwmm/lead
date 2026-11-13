"""Small, shared wire limits for current authorized native screenshots."""
import base64
import hashlib
import io
import json
from urllib.parse import urlsplit

IMAGE_MODEL='deepseek-flash'
IMAGE_TOKENS=1024
MAX_MODEL_BODY_BYTES=1048576
MAX_IMAGE_BYTES=524288
MAX_IMAGE_EDGE=2048
MAX_IMAGE_PIXELS=4194304


def supported_model(model):
    if not isinstance(model,dict) or model.get('provider')!='deepseek' or model.get('model')!=IMAGE_MODEL:
        return False
    try:
        parsed=urlsplit(model.get('base_url',''))
        return (parsed.scheme=='https' and parsed.hostname=='api.deepseek.com' and parsed.port in (None,443)
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path.rstrip('/') in ('','/v1'))
    except ValueError:
        return False


def checked_image(value):
    from PIL import Image
    if not isinstance(value,str) or not value or len(value)>4*((MAX_IMAGE_BYTES+2)//3):
        raise ValueError('native_image_rejected')
    try:
        raw=base64.b64decode(value,validate=True)
        if len(raw)>MAX_IMAGE_BYTES or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError
        with Image.open(io.BytesIO(raw)) as image:
            if (image.format!='PNG' or image.width<1 or image.height<1
                    or max(image.size)>MAX_IMAGE_EDGE or image.width*image.height>MAX_IMAGE_PIXELS
                    or getattr(image,'n_frames',1)!=1):
                raise ValueError
            size=image.size
            image.verify()
        return hashlib.sha256(raw).hexdigest(),size
    except Exception:
        raise ValueError('native_image_rejected') from None


def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:
            raise ValueError('native_image_request_rejected')
        value[key]=item
    return value


def request_projection(content,*,image_model=None,approved_image=None):
    """Keep raw-byte text accounting; replace only one verified image value."""
    if len(content)>MAX_MODEL_BODY_BYTES:
        raise ValueError('native_model_body_limit')
    try:
        data=json.loads(content,object_pairs_hook=_unique)
    except ValueError:
        if image_model or content.lstrip().startswith((b'{',b'[')) or b'image' in content.lower():
            raise ValueError('native_image_request_rejected') from None
        return len(content),0,content
    if not isinstance(data,dict) or not isinstance(data.get('messages'),list):
        if image_model:
            raise ValueError('native_image_request_rejected')
        return len(content),0,content
    found=[]
    for index,message in enumerate(data['messages']):
        if not isinstance(message,dict):
            raise ValueError('native_image_request_rejected')
        parts=message.get('content')
        if parts is None or isinstance(parts,str):
            continue
        if not isinstance(parts,list):
            raise ValueError('native_image_request_rejected')
        for part in parts:
            if not isinstance(part,dict):
                raise ValueError('native_image_request_rejected')
            if part.get('type')=='text' and isinstance(part.get('text'),str):
                continue
            if (part.get('type')!='image_url' or set(part)!= {'type','image_url'}
                    or not isinstance(part['image_url'],dict)
                    or not set(part['image_url'])<= {'url','detail'}
                    or part['image_url'].get('detail','auto') not in ('auto','low','high','original')):
                raise ValueError('native_image_request_rejected')
            found.append((index,message.get('role'),part['image_url']))
    if not found:
        return len(content),0,content
    if (len(found)!=1 or image_model!=IMAGE_MODEL or data.get('model')!=IMAGE_MODEL or found[0][1]!='user'):
        raise ValueError('native_image_request_rejected')
    image=found[0][2]
    uri=image.get('url')
    prefix='data:image/png;base64,'
    if not isinstance(uri,str) or not uri.startswith(prefix):
        raise ValueError('native_image_request_rejected')
    digest,_=checked_image(uri[len(prefix):])
    if not approved_image or digest!=approved_image:
        raise ValueError('native_image_not_current')
    image['url']=prefix
    projected=len(json.dumps(data,ensure_ascii=True,separators=(',',':')).encode())
    scan=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
    return max(projected,len(content)-len(uri.encode())+len(prefix))+IMAGE_TOKENS,1,scan


def request_units(content,*,image_model=None,approved_image=None):
    units,count,_=request_projection(content,image_model=image_model,approved_image=approved_image)
    return units,count

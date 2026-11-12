"""Finite observed authentication popup roles on one original connection.

No platform recipe, new OAuth client, credential store or model authorization.
Only the actual opener, observed static control, response redirect and current
person-submitted form may extend this short-lived in-memory role.
"""
import hashlib
import json
import re
import time
from urllib.parse import parse_qsl, urljoin

from .browser_execution import origin
from .native_session_facts import reject_dangerous_endpoint


def check_auth_form(url,fields):
    reject_dangerous_endpoint(url)
    if any(re.search(r'delete|destroy|billing|payment|checkout|purchase|subscribe|admin|permission|body|content|amount|currency',name,re.I)
            for name in fields):
        raise ValueError('authentication_form_dangerous_field')


def popup_destination(facts,page_url):
    if not isinstance(facts,dict) or facts.get('form'):
        return None
    text=facts.get('text','')
    if (not re.search(r'sign[\s-]*in|log[\s-]*in|登录|登入',text,re.I)
            or not re.search(r'identity|single[ -]sign[ -]on|\bsso\b|身份|统一认证',text,re.I)
            or re.search(r'purchase|payment|billing|checkout|subscribe|delete|购买|支付|续费|删除',text,re.I)):
        return None
    if facts.get('tag')=='A' and facts.get('target') not in ('','_self') and not facts.get('onclick'):
        url=facts.get('href')
    else:
        match=re.fullmatch(r"\s*(?:return\s+)?window\.open\(\s*(['\"])([^'\"\r\n]+)\1\s*(?:,\s*(['\"])[\w-]+\3\s*)?\)\s*;?\s*",facts.get('onclick',''))
        url=match[2] if match else None
    if not url:
        return None
    url=urljoin(page_url,url)
    origin(url)
    try:
        reject_dangerous_endpoint(url)
    except ValueError:
        return None
    return url


def reserve(scope,url):
    flow=scope.get('auth_flow')
    if flow and not flow.get('completed') and flow.get('expires',0)>time.monotonic():
        raise ValueError('connection_auth_flow_active')
    scope['auth_flow']={'opener_target':scope['target_id'],'start_url':url,'urls':[url],
        'expires':time.monotonic()+120,'hops':0,'popup_target':None,'foreign_seen':False,'callback':False}
    return scope['auth_flow']


def attach_popup(scope,info):
    flow=scope.get('auth_flow') or {}
    if (not scope.get('connection_ref') or flow.get('completed') or flow.get('expires',0)<=time.monotonic()
            or info.get('openerId')!=scope.get('target_id') or flow.get('opener_target')!=scope.get('target_id')
            or flow.get('popup_target') not in (None,info['targetId'])):
        raise ValueError('popup_not_reserved')
    flow['popup_target']=info['targetId']


def note_redirect(scope,info,event):
    flow=scope.get('auth_flow') or {}
    response=event.get('redirectResponse') or {}
    if (flow.get('completed') or flow.get('expires',0)<=time.monotonic()
            or info.get('targetId')!=flow.get('popup_target') or flow.get('hops',0)>=6
            or response.get('url') not in flow.get('urls',[]) or response.get('status') not in (301,302,303,307,308)):
        return
    location=next((value for key,value in response.get('headers',{}).items() if key.lower()=='location'),None)
    if not isinstance(location,str):
        return
    url=urljoin(response['url'],location)
    try:
        origin(url)
        reject_dangerous_endpoint(url)
    except ValueError:
        return
    if url!=event['request']['url'] or url in flow['urls']:
        return
    flow['urls'].append(url)
    flow['hops']+=1


def body_hash(values):
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def authorize(scope,target,request):
    flow=scope.get('auth_flow') or {}
    if (flow.get('completed') or flow.get('expires',0)<=time.monotonic() or target.get('type')!='page'
            or target.get('targetId')!=flow.get('popup_target') or target.get('openerId')!=flow.get('opener_target')
            or request['url'] not in flow.get('urls',[])):
        return None
    if request['method'] in ('GET','HEAD'):
        used=flow.setdefault('read_urls_used',[])
        if request['url'] in used:
            return None
        used.append(request['url'])
        if origin(request['url']) not in {origin(url) for url in scope['origins']}:
            flow['foreign_seen']=True
        elif flow.get('foreign_seen'):
            flow['callback']=True
        return {'kind':'authentication'}
    proof=flow.get('owner_form') or {}
    if (request['method']!='POST' or scope.get('control_mode')!='owner' or proof.get('used')
            or proof.get('expires',0)<=time.monotonic() or proof.get('url')!=request['url']
            or proof.get('version')!=scope.get('challenge_version')
            or proof.get('document')!=flow.get('document') or proof.get('document')!=flow.get('current_document')):
        return None
    try:
        fields=parse_qsl(request.get('postData',''),keep_blank_values=True,strict_parsing=True)
    except ValueError:
        return None
    if len(set(key for key,_ in fields))!=len(fields) or body_hash(dict(fields))!=proof.get('body_hash'):
        return None
    proof['used']=True
    return {'kind':'authentication'}

"""Conservative facts from the currently compiled worker and page scripts.

This is a deliberately small syntax subset, never a JavaScript evaluator or a
URL heuristic. Unknown code leaves a local session-maintenance capability gap.
"""
import ast
import re
from typing import NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

from .browser_execution import origin


class Token(NamedTuple):
    value: str
    start: int
    end: int
    kind: str


_LEXER=re.compile(r'''\s+|//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|===|!==|=>|&&|\|\||[^\s]''')


def tokens(source):
    if not isinstance(source,str) or len(source)>200000:
        raise ValueError('session_script_outside_subset')
    result=[]
    for match in _LEXER.finditer(source):
        value=match[0]
        if value.isspace() or value.startswith(('//','/*')):
            continue
        kind='word'
        if value.startswith(('"',"'")):
            try:
                value=ast.literal_eval(value)
            except (ValueError,SyntaxError):
                raise ValueError('session_script_outside_subset') from None
            kind='string'
        result.append(Token(value,match.start(),match.end(),kind))
    return result


def end(tokens,start):
    pairs={'(':')','{':'}','[':']'}
    stack=[]
    for index in range(start,len(tokens)):
        value=tokens[index].value
        if tokens[index].kind=='string':
            continue
        if value in pairs:
            stack.append(pairs[value])
        elif value in pairs.values():
            if not stack or stack.pop()!=value:
                raise ValueError('session_script_outside_subset')
            if not stack:
                return index
    raise ValueError('session_script_outside_subset')


def parts(value):
    result=[]
    start=0
    index=0
    while index<len(value):
        if value[index].kind!='string' and value[index].value in ('(','{','['):
            index=end(value,index)
        elif value[index].kind!='string' and value[index].value==',':
            result.append(value[start:index])
            start=index+1
        index+=1
    if value[start:]:
        result.append(value[start:])
    return result


def obj(value):
    if not value or value[0].value!='{' or end(value,0)!=len(value)-1:
        raise ValueError('session_script_outside_subset')
    result={}
    for field in parts(value[1:-1]):
        if len(field)<3 or field[1].value!=':' or field[0].value in result or not re.fullmatch(r'[A-Za-z_][\w]*',field[0].value):
            raise ValueError('session_script_outside_subset')
        result[field[0].value]=field[2:]
    return result


def values(value):
    return [token.value for token in value]


def message_bridges(source):
    ts=tokens(source)
    found=[]
    for index in range(6,len(ts)-2):
        if values(ts[index-6:index+1])!=['navigator','.','serviceWorker','.','controller','.','postMessage'] or ts[index+1].value!='(':
            continue
        fields=obj(ts[index+2:end(ts,index+1)])
        mapping={}
        variable=None
        for name,expression in fields.items():
            if len(expression)!=5 or values(expression[1:4])!=['.','dataset','.']:
                raise ValueError('session_message_bridge_outside_subset')
            variable=variable or expression[0].value
            if expression[0].value!=variable:
                raise ValueError('session_message_bridge_outside_subset')
            mapping[name]=expression[4].value
        selectors=[]
        for before in range(index):
            if (values(ts[before:before+4])==['document','.','querySelectorAll','('] and before+5<len(ts)
                    and ts[before+4].kind=='string' and ts[before+5].value==')'):
                prefix=values(ts[max(0,before-5):before])
                after=values(ts[before+6:index])
                if variable in prefix and 'of' in prefix and any(after[i:i+5]==[variable,'.','addEventListener','(','click'] for i in range(len(after))):
                    selectors.append(ts[before+4].value)
        if len(selectors)!=1 or len(mapping)>4 or not mapping:
            raise ValueError('session_message_bridge_outside_subset')
        found.append({'selector':selectors[0],'fields':mapping})
    return found


def worker_envelope(source,*,script_url,line,column,message):
    ts=tokens(source)
    lines=source.splitlines(keepends=True)
    if not isinstance(line,int) or not 0<=line<len(lines):
        raise ValueError('session_initiator_unproved')
    offset=sum(map(len,lines[:line]))+column
    matches=[]
    for index in range(len(ts)-1):
        if ts[index].value=='fetch' and ts[index+1].value=='(':
            closing=end(ts,index+1)
            if ts[index].start<=offset<=ts[closing].end:
                matches.append((index,closing))
    if len(matches)!=1:
        raise ValueError('session_initiator_unproved')
    index,closing=matches[0]
    calls=parts(ts[index+2:closing])
    if len(calls)!=2:
        raise ValueError('session_script_outside_subset')
    expression,options=calls
    handlers=[]
    for start in range(index):
        if (values(ts[start:start+5])==['self','.','addEventListener','(','message'] and ts[start+4].kind=='string'
                and start+9<len(ts) and ts[start+5].value==',' and ts[start+7].value=='=>' and ts[start+8].value=='{'):
            stop=end(ts,start+8)
            if start+8<index<stop:
                handlers.append((ts[start+6].value,start+9))
    if len(handlers)!=1:
        raise ValueError('session_message_handler_unproved')
    event,body_start=handlers[0]
    prefix=ts[body_start:index]
    if any(item.value in ('function','=>') for item in prefix):
        raise ValueError('session_script_outside_subset')
    if len(expression)==1 and expression[0].kind=='string':
        path=expression[0].value
    elif (len(expression)==7 and expression[1].value=='===' and expression[2].kind=='string'
            and expression[3].value=='?' and expression[4].kind=='string' and expression[5].value==':' and expression[6].kind=='string'):
        variable=expression[0].value
        bindings=[]
        for before in range(max(0,len(prefix)-8)):
            candidate=prefix[before:before+9]
            if values(candidate[:7])==['const',variable,'=',event,'.','data','.'] and candidate[8].value==';':
                bindings.append(candidate[7].value)
        if len(bindings)!=1 or bindings[0] not in message:
            raise ValueError('session_message_selector_unproved')
        path=expression[4 if message[bindings[0]]==expression[2].value else 6].value
    else:
        raise ValueError('session_dynamic_endpoint_unproved')
    options=obj(options)
    if (set(options)!={'method','credentials','body'} or len(options['method'])!=1 or options['method'][0].kind!='string'
            or values(options['method'])!=['POST'] or len(options['credentials'])!=1 or options['credentials'][0].kind!='string' or values(options['credentials'])!=['include']):
        raise ValueError('session_request_outside_subset')
    body=options['body']
    if values(body[:3])!=['new','URLSearchParams','('] or end(body,2)!=len(body)-1:
        raise ValueError('session_request_outside_subset')
    fields=obj(body[3:-1])
    expected={}
    for name,expression in fields.items():
        if not re.fullmatch(r'(?:[A-Za-z_]*nonce|csrf(?:_[A-Za-z]+)?)',name,re.I) or values(expression[:4])!=[event,'.','data','.'] or len(expression)!=5:
            raise ValueError('session_nonmaintenance_field')
        value=message.get(expression[4].value)
        if not isinstance(value,str) or not 8<=len(value)<=4000:
            raise ValueError('session_current_nonce_unproved')
        expected[name]=value
    if not expected:
        raise ValueError('session_current_nonce_unproved')
    url=urljoin(script_url,path)
    reject_dangerous_endpoint(url)
    if origin(url)!=origin(script_url):
        raise ValueError('session_endpoint_outside_origin')
    return {'method':'POST','url':url,'fields':expected}


def reject_dangerous_endpoint(url):
    parsed=urlsplit(url)
    if re.search(r'(?:^|[/_.?&=-])(?:billing|checkout|pay(?:ment)?|charge|purchase|subscribe|subscriptions?|invoice|admin|delete|destroy|unfollow|upgrade|amount|currency)(?:$|[/_.?&=-])',unquote(parsed.path)+'?'+unquote(parsed.query),re.I):
        raise ValueError('session_endpoint_dangerous_operation')

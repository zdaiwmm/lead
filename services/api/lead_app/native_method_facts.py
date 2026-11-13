"""Read a bounded member editor contract from current DOM; never navigate or grant.

A proposal supplies selectors, not request facts, identity, permission or receipt
proof. The caller owns the TaskRef/page lease and replaces the candidate self URL
with its separately verified private account endpoint. Unrecognized forms/rules
remain local capability gaps.
"""
import re
from contextlib import AsyncExitStack
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from .browser_execution import canonical_numeric_id, origin, safe_parent_field
from .native_method_errors import SELECTOR_ERROR_CODES, MethodFactError

_DYNAMIC = re.compile(r'^(?:_?csrf(?:[_-]?token|middlewaretoken)?|_?nonce)$', re.I)
_CREDENTIAL = re.compile(r'(?:password|secret|credential|authorization|session|token|csrf|nonce|api[_-]?key|authenticity)', re.I)
_DANGEROUS = re.compile(r'(?:^|[_-])(?:admin|moderator|role|privilege|permission|grant|delete|remove|destroy|ban|invite|join|follow|like|share|scope)(?:$|[_-])', re.I)
_CONTROL = {'action', 'operation', 'mode', '_method', 'method', 'command', 'cmd', 'intent', 'action_type', 'operation_type'}
_ACTION_VALUES = {'publish', 'post', 'reply', 'comment', 'create_post', 'create_reply', 'create_comment', 'submit', 'POST'}


def _text(value):
    return ' '.join(str(value).split())


def _url(value, base=None):
    if not isinstance(value,str) or not value or len(value)>2000:
        raise MethodFactError('browser_method_url_missing')
    result=urljoin(base or '',value)
    origin(result)
    if urlsplit(result).fragment:
        raise MethodFactError('browser_method_url_fragment')
    return result


async def _node(page, selector, *, field=None):
    if not isinstance(selector,str) or not 1<=len(selector)<=300:
        raise MethodFactError('browser_method_selector_missing',selector_issues=[{'field':field,'issue':'invalid_selector'}])
    node=page.locator(selector)
    count=await node.count()
    if count!=1:
        raise MethodFactError('browser_method_element_ambiguous',
            selector_issues=[{'field':field,'issue':'not_found' if count==0 else 'not_unique'}])
    if not await node.is_visible():
        raise MethodFactError('browser_method_element_ambiguous',selector_issues=[{'field':field,'issue':'not_visible'}])
    return node


async def _nodes(page,selectors,names):
    nodes,issues={},[]
    first=None
    for name in names:
        try:
            nodes[name]=await _node(page,selectors[name],field=name)
        except MethodFactError as error:
            if error.code not in SELECTOR_ERROR_CODES:
                raise
            first=first or error
            issues.extend(error.selector_issues)
    if first:
        raise MethodFactError(first.code,selector_issues=issues)
    return nodes


def _permission(context, quote, operation):
    """Conservative supported rule language, rather than model-asserted approval."""
    normalized=_text(context)
    if not quote or _text(quote) not in normalized:
        raise MethodFactError('browser_method_permission_quote_missing')
    for sentence in re.split(r'[.!?。！？\n]',context):
        # A local rule can qualify the preceding permission in its next sentence.
        for requirement in re.finditer(r'\b(?:approval|authorization|permission|consent)\b.{0,100}?\b(?:required|needed|necessary|mandatory)\b',sentence,re.I):
            waived=re.search(r'\bnot\s+(?:required|needed|necessary|mandatory)$',requirement.group(),re.I)
            waived=waived or re.search(r'\bno(?:\s+(?:prior|administrator|administrative|owner|moderator|additional))*\s+$',sentence[:requirement.start()],re.I)
            if not waived:
                raise MethodFactError('browser_method_permission_denied')
        if re.search(r'\b(?:must|need\s+to|have\s+to)\s+(?:obtain|receive|get|have|secure)\b.{0,100}\b(?:approval|authorization|permission|consent)\b',sentence,re.I):
            raise MethodFactError('browser_method_permission_denied')
        topic=re.search(r'\b(?:automat\w*|bots?|publish\w*|post(?:s|ing)?|repl\w*)\b|自动(?:化|工具)|机器人|发布|发帖|回复',sentence,re.I)
        denied=re.search(r"\b(?:no|not|never|prohibit\w*|forbidden|disallow\w*|disabled|banned|cannot|can't)\b|仅限管理员|禁止|不允许|不得|不能",sentence,re.I)
        privileged=re.search(r'\bonly\s+(?:administrators?|moderators?|owners?)\b',sentence,re.I)
        conditional=re.search(r'\b(?:if|unless|provided\s+that|subject\s+to|once|after)\b.{0,100}\b(?:approv\w*|authoriz\w*|permission|consent)\b',sentence,re.I)
        if topic and (denied or privileged or conditional):
            raise MethodFactError('browser_method_permission_denied')
    member=re.search(r'\b(?:ordinary|regular)\s+members?\b|普通(?:成员|会员)',normalized,re.I)
    action='(?:publish|post)' if operation=='publish' else '(?:reply|replies|respond)'
    allowed_action=re.search(r'\bmembers?\s+(?:may|can|are\s+allowed\s+to)\b.{0,220}\b'+action+r'\b',normalized,re.I)
    allowed_action=allowed_action or re.search(r'普通(?:成员|会员).{0,50}(?:可以|允许).{0,50}'+('(?:发布|发帖)' if operation=='publish' else '回复'),normalized)
    automated=re.search(r'\b(?:automated(?:\s+browser)?\s+tools?|browser\s+automation|automation|bots?)\s+(?:(?:are|is)\s+)?(?:permitted|allowed)\b',normalized,re.I)
    automated=automated or re.search(r'(?:允许|可以).{0,50}(?:自动工具|自动化)|(?:自动工具|自动化).{0,50}(?:允许|可以)',normalized)
    automated=automated or re.search(r'\bmembers?\s+(?:may|can)\s+use\s+authorized\s+browser\s+automation\b[^.!?。！？]{0,220}\b'+action+r'\b',normalized,re.I)
    automated=automated or re.search(r'\bmembers?\s+(?:may|can)\b[^.!?。！？]{0,220}\b'+action+r'\b[^.!?。！？]{0,220}\bincluding\s+through\s+authorized\s+browser\s+automation\b',normalized,re.I)
    if not member or not allowed_action or not automated:
        raise MethodFactError('browser_method_permission_unestablished')


_FORM = '''(body,submit)=>{
 const form=body.form;
 if(!form || submit.form!==form) return null;
 return {action:form.action,method:form.method,enctype:form.enctype,
  bodyName:body.name,bodyTag:body.tagName,
  submitTag:submit.tagName,submitType:submit.type,
  submitAction:submit.hasAttribute('formaction')?submit.formAction:form.action,
  submitMethod:submit.hasAttribute('formmethod')?submit.formMethod:form.method,
  submitEncoding:submit.hasAttribute('formenctype')?submit.formEnctype:form.enctype,
  submitLabel:submit.innerText||submit.value||'',formLabel:form.getAttribute('aria-label')||'',
  controls:Array.from(form.elements).map(node=>({tag:node.tagName,type:node.type,name:node.name,
   value:node.value,disabled:node.matches(':disabled'),body:node===body,submit:node===submit}))};
}'''


async def _form_facts(body, submit):
    handle=await submit.element_handle()
    try:
        return await body.evaluate(_FORM,handle)
    finally:
        if handle:
            await handle.dispose()


def _request(facts, *, page_url, expected_origin, operation, parent_url, external_account_id,parent_identifier=None):
    if (not facts or facts['bodyTag']!='TEXTAREA' or facts['method'].lower()!='post'
            or facts['enctype'].lower()!='application/x-www-form-urlencoded'
            or facts['submitTag'] not in ('BUTTON','INPUT') or facts['submitType']!='submit'
            or facts['submitAction']!=facts['action'] or facts['submitMethod'].lower()!='post'
            or facts['submitEncoding'].lower()!='application/x-www-form-urlencoded'):
        raise MethodFactError('browser_method_form_unsupported')
    url=_url(facts['action'],page_url)
    if origin(url)!=expected_origin:
        raise MethodFactError('browser_method_request_origin_changed')
    from .native_session_facts import reject_dangerous_endpoint
    reject_dangerous_endpoint(url)
    if re.search(r'\b(?:delete|destroy|remove|purchase|pay|payment|billing|checkout|charge|subscribe)\b|删除|销毁|购买|支付|扣费|充值',
                 facts['submitLabel']+' '+facts['formLabel'],re.I):
        raise MethodFactError('browser_method_dangerous_operation')
    content=facts['bodyName']
    if not isinstance(content,str) or not 1<=len(content)<=100:
        raise MethodFactError('browser_method_content_field_missing')
    fields={}
    for control in facts['controls']:
        name=control.get('name')
        if control['disabled'] or not name:
            continue
        # Non-selected buttons do not participate in this submitted form.
        if control['type'] in ('submit','button','reset') and not control['submit']:
            continue
        if not isinstance(name,str) or not 1<=len(name)<=100 or name in fields:
            raise MethodFactError('browser_method_form_fields_ambiguous')
        if control['body']:
            fields[name]=('body',None)
        elif control['submit']:
            fields[name]=('fixed',control['value'])
        elif control['tag']=='INPUT' and control['type']=='hidden':
            fields[name]=('hidden',control['value'])
        else:
            raise MethodFactError('browser_method_extra_editor_control')
    if fields.get(content,('',))[0]!='body':
        raise MethodFactError('browser_method_content_field_missing')
    parent=None
    parent_binding=None
    if operation=='reply':
        candidates=[name for name,(kind,value) in fields.items() if kind=='hidden'
            and (value==parent_url or parent_identifier is not None and value==parent_identifier)
            and safe_parent_field(name)]
        if len(candidates)!=1:
            raise MethodFactError('browser_method_parent_field_ambiguous')
        parent=candidates[0]
        if fields[parent][1]!=parent_url:
            parent_binding={'canonical_url':parent_url,'value':fields[parent][1]}
    fixed={}
    dynamic=[]
    for name,(kind,value) in fields.items():
        if kind=='body' or name==parent:
            continue
        if not isinstance(value,str) or len(value)>2000:
            raise MethodFactError('browser_method_fixed_field_unsupported')
        if kind=='hidden' and _DYNAMIC.fullmatch(name):
            dynamic.append(name)
            continue
        if (_CREDENTIAL.search(name) or _DANGEROUS.search(name)
                or name.casefold() in _CONTROL and value not in _ACTION_VALUES
                or name.casefold() in {'author','author_id','user_id','account_id'} and value!=external_account_id):
            raise MethodFactError('browser_method_sensitive_or_privileged_field')
        fixed[name]=value
    if len(fixed)>20 or len(dynamic)>12:
        raise MethodFactError('browser_method_form_fields_limit')
    result={'method':'POST','url':url,'body_kind':'form','content_field':content,
            'fixed_fields':fixed,'dynamic_fields':dynamic}
    if parent:
        result['parent_field']=parent
    if parent_binding:
        result['parent_binding']=parent_binding
    return result


_BOOKMARK = r'''proof=>{
 const node=proof.node,article=proof.article;
 if(proof.root!==document.documentElement || !node.isConnected || !article ||
    node.closest('article,[role=article]')!==article || node.closest('nav,[role=navigation],blockquote,q,pre,code') ||
    node.tagName!=='A' || !node.rel.toLowerCase().split(/\s+/).includes('bookmark') || node.href!==location.href) return null;
 const bookmarks=Array.from(document.querySelectorAll('a[rel]')).filter(link=>
    link.rel.toLowerCase().split(/\s+/).includes('bookmark')&&link.href===location.href);
 return bookmarks.length===1&&bookmarks[0]===node?{url:location.href,href:node.href}:null;
}'''


_PERMISSION_SOURCE = r'''node=>{
 const excluded='article,[role=article],blockquote,q,pre,code,textarea,input,select,[contenteditable]:not([contenteditable=false])';
 if(node.closest(excluded))return null;
 const read=root=>{
   if(root.closest(excluded))return null;
   const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT),parts=[];
   let item,count=0,length=0;
   while(item=walker.nextNode()){
     if(++count>2000)return null;
     const parent=item.parentElement;
     if(!parent||parent.closest(excluded+',script,style,template,[hidden],[inert]'))continue;
     const style=getComputedStyle(parent),range=document.createRange();range.selectNodeContents(item);
     if(style.display==='none'||style.visibility!=='visible'||style.opacity==='0'||!Array.from(range.getClientRects()).some(r=>r.width>0&&r.height>0))continue;
     length+=item.data.length;if(length>12000)return null;
     parts.push(item.data);
   }
   return parts.join(' ');
 };
 return {text:read(node),context:read(node.closest('section,[role=region],aside,fieldset')||node.parentElement||node)};
}'''


async def inspect_editor(page, proposal, *, account_id, binding_version, external_account_id,
                         connection_origin, operation, evidence_ref, permission_page=None):
    async with AsyncExitStack() as resources:
        return await _inspect_editor(page,proposal,account_id=account_id,binding_version=binding_version,
            external_account_id=external_account_id,connection_origin=connection_origin,operation=operation,
            evidence_ref=evidence_ref,permission_page=permission_page,resources=resources)


async def _inspect_editor(page, proposal, *, account_id, binding_version, external_account_id,
                         connection_origin, operation, evidence_ref, permission_page,resources):
    """Inspect one current member editor; the caller owns all page/DB authority."""
    if (operation not in ('publish','reply') or not isinstance(proposal,dict)
            or type(binding_version) is not int or binding_version<1
            or not isinstance(external_account_id,str) or not 1<=len(external_account_id)<=500
            or not isinstance(evidence_ref,str) or not evidence_ref.strip() or not 1<=len(evidence_ref)<=500):
        raise MethodFactError('browser_method_scope_missing')
    account_id=str(UUID(str(account_id)))
    expected_origin=origin(connection_origin)
    page_url=_url(page.url)
    target=_url(proposal.get('target_url'))
    permission_page=permission_page or page
    permission_url=_url(permission_page.url)
    if origin(page_url)!=expected_origin or origin(target)!=expected_origin or origin(permission_url)!=expected_origin:
        raise MethodFactError('browser_method_origin_changed')
    if permission_url!=target:
        raise MethodFactError('browser_method_permission_page_changed')
    selectors=proposal.get('selectors')
    needed=['self_subject','editor_target','editor_body','submit']+(['editor_parent'] if operation=='reply' else [])
    # Legacy identity hints are not evidence. The existing strong DOM observer
    # derives the actual selector and affixes below, including fresh rechecks.
    if isinstance(selectors,dict) and 'self_subject' not in selectors:
        selectors={'self_subject':'',**selectors}
    if not isinstance(selectors,dict) or set(selectors)!=set(needed):
        raise MethodFactError('browser_method_selector_contract_missing')
    nodes=await _nodes(page,selectors,[name for name in needed if name!='self_subject'])
    from .native_identity_facts import inspect_current_identity
    identity_root=await page.evaluate_handle('document.documentElement')
    resources.push_async_callback(identity_root.dispose)
    current=await inspect_current_identity(page,connection_origin=connection_origin,require_self_link=False)
    if current['acting_subject']!=external_account_id:
        raise MethodFactError('browser_method_identity_changed')
    prefix,suffix=current['self_subject_prefix'],current['self_subject_suffix']
    selectors={**selectors,'self_subject':current['acting_subject_selector']}
    nodes['self_subject']=await _node(page,selectors['self_subject'],field='self_subject')
    self_text=(await nodes['self_subject'].inner_text()).strip()
    if self_text!=(prefix+external_account_id+suffix).strip():
        raise MethodFactError('browser_method_identity_changed')
    target_href=await nodes['editor_target'].get_attribute('href')
    if (not await nodes['editor_target'].evaluate("node=>node.tagName==='A'")
            or not target_href or _url(target_href,page_url)!=target):
        raise MethodFactError('browser_method_target_changed')
    if not await nodes['editor_body'].is_editable() or not await nodes['submit'].is_enabled():
        raise MethodFactError('browser_method_editor_unavailable')
    parent=None
    parent_href=None
    parent_identifier=None
    bookmark=None
    bookmark_facts=None
    if operation=='reply':
        parent_href=await nodes['editor_parent'].get_attribute('href')
        parent=_url(parent_href,page_url)
        if not await nodes['editor_parent'].evaluate("node=>node.tagName==='A'"):
            raise MethodFactError('browser_method_parent_link_missing')
        if origin(parent)!=expected_origin:
            raise MethodFactError('browser_method_parent_origin_changed')
        canonicals=page.locator('a[rel~=canonical],link[rel~=canonical]')
        canonical_count=await canonicals.count()
        if canonical_count>1:
            raise MethodFactError('browser_method_parent_ambiguous')
        canonical_href=await canonicals.get_attribute('href') if canonical_count==1 else None
        canonical=canonical_href if canonical_count==1 else page_url
        if parent!=_url(canonical,page_url):
            raise MethodFactError('browser_method_parent_changed')
        if canonical_count==1:
            parent_identifier=canonical_numeric_id(parent)
        else:
            bookmark=await nodes['editor_parent'].evaluate_handle("node=>({node,root:document.documentElement,article:node.closest('article,[role=article]')})")
            resources.push_async_callback(bookmark.dispose)
            bookmark_facts=await bookmark.evaluate(_BOOKMARK)
            if bookmark_facts and bookmark_facts['url']==parent:
                parent_identifier=canonical_numeric_id(parent)
    facts=await _form_facts(nodes['editor_body'],nodes['submit'])
    request=_request(facts,page_url=page_url,expected_origin=expected_origin,operation=operation,
                     parent_url=parent,external_account_id=external_account_id,parent_identifier=parent_identifier)
    permission=await _node(permission_page,proposal.get('permission_selector'),field='permission_selector')
    quote=proposal.get('permission_quote')
    if not isinstance(quote,str) or not 20<=len(quote)<=3000:
        raise MethodFactError('browser_method_permission_quote_missing')
    permission_source=await permission.evaluate(_PERMISSION_SOURCE)
    if not permission_source or permission_source['text'] is None or permission_source['context'] is None:
        raise MethodFactError('browser_method_permission_source_invalid')
    actual=permission_source['text']
    if _text(quote) not in _text(actual):
        raise MethodFactError('browser_method_permission_quote_missing')
    context=permission_source['context']
    if len(context)>12000:
        raise MethodFactError('browser_method_permission_context_ambiguous')
    _permission(context,quote,operation)
    # Re-read the identity and request-bearing DOM before returning. A reload or
    # mutation during inspection must not produce a mixed-document contract.
    if (page.url!=page_url or permission_page.url!=permission_url
            or not await identity_root.evaluate('root=>root.isConnected&&root===document.documentElement')
            or current!=await inspect_current_identity(page,connection_origin=connection_origin,require_self_link=False)
            or (await nodes['self_subject'].inner_text()).strip()!=self_text
            or await _form_facts(nodes['editor_body'],nodes['submit'])!=facts
            or await nodes['editor_target'].get_attribute('href')!=target_href
            or operation=='reply' and await nodes['editor_parent'].get_attribute('href')!=parent_href
            or operation=='reply' and (await canonicals.count()!=canonical_count
                or canonical_count==1 and await canonicals.get_attribute('href')!=canonical_href)
            or bookmark_facts is not None and await bookmark.evaluate(_BOOKMARK)!=bookmark_facts
            or await permission.evaluate(_PERMISSION_SOURCE)!=permission_source):
        raise MethodFactError('browser_method_document_changed')
    return {'version':'native-browser-v1','account_id':account_id,'binding_version':binding_version,
            'target_url':target,'self_url':page_url,'operations':[operation],'role':'ordinary_member',
            'selectors':{name:selectors[name] for name in needed},
            'self_subject_prefix':prefix,'self_subject_suffix':suffix,'request':request,
            'receipt_state':'unobserved','evidence_refs':[evidence_ref],
            'platform_permission':{'state':'approved','evidence_refs':[evidence_ref],
                                   'quote':quote,'source_url':permission_url}}

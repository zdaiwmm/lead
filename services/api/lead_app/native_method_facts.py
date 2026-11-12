"""Read a bounded member editor contract from current DOM; never navigate or grant.

A proposal supplies selectors, not request facts, identity, permission or receipt
proof. The caller owns the TaskRef/page lease and replaces the candidate self URL
with its separately verified private account endpoint. Unrecognized forms/rules
remain local capability gaps.
"""
import re
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from .browser_execution import origin

_IDENTITY_PREFIX = re.compile(r'^(?:(?:signed|logged)\s+in\s+as|posting\s+as|replying\s+as|current\s+account|当前(?:登录)?(?:账号|账户|身份)|发布身份|回复身份)\s*[:：]?\s*$', re.I)
_DYNAMIC = re.compile(r'^(?:_?csrf(?:[_-]?token|middlewaretoken)?|_?nonce)$', re.I)
_CREDENTIAL = re.compile(r'(?:password|secret|credential|authorization|session|token|csrf|nonce|api[_-]?key|authenticity)', re.I)
_DANGEROUS = re.compile(r'(?:^|[_-])(?:admin|moderator|role|privilege|permission|grant|delete|remove|destroy|ban|invite|join|follow|like|share|scope)(?:$|[_-])', re.I)
_CONTROL = {'action', 'operation', 'mode', '_method', 'method', 'command', 'cmd', 'intent', 'action_type', 'operation_type'}
_ACTION_VALUES = {'publish', 'post', 'reply', 'comment', 'create_post', 'create_reply', 'create_comment', 'submit', 'POST'}


def _text(value):
    return ' '.join(str(value).split())


def _url(value, base=None):
    if not isinstance(value,str) or not value or len(value)>2000:
        raise ValueError('browser_method_url_missing')
    result=urljoin(base or '',value)
    origin(result)
    if urlsplit(result).fragment:
        raise ValueError('browser_method_url_fragment')
    return result


async def _node(page, selector):
    if not isinstance(selector,str) or not 1<=len(selector)<=300:
        raise ValueError('browser_method_selector_missing')
    node=page.locator(selector)
    if await node.count()!=1 or not await node.is_visible():
        raise ValueError('browser_method_element_ambiguous')
    return node


def _permission(context, quote, operation):
    """Conservative supported rule language, rather than model-asserted approval."""
    normalized=_text(context)
    if not quote or _text(quote) not in normalized:
        raise ValueError('browser_method_permission_quote_missing')
    for sentence in re.split(r'[.!?。！？\n]',context):
        topic=re.search(r'\b(?:automat\w*|bots?|publish\w*|post(?:s|ing)?|repl\w*)\b|自动(?:化|工具)|机器人|发布|发帖|回复',sentence,re.I)
        denied=re.search(r"\b(?:no|not|never|prohibit\w*|forbidden|disallow\w*|disabled|banned|cannot|can't)\b|仅限管理员|禁止|不允许|不得|不能",sentence,re.I)
        privileged=re.search(r'\bonly\s+(?:administrators?|moderators?|owners?)\b',sentence,re.I)
        if topic and (denied or privileged):
            raise ValueError('browser_method_permission_denied')
    member=re.search(r'\b(?:ordinary|regular)\s+member\b|普通(?:成员|会员)',normalized,re.I)
    action='(?:publish|post)' if operation=='publish' else '(?:reply|replies|respond)'
    allowed_action=re.search(r'\bmembers?\s+(?:may|can|are\s+allowed\s+to)\b.{0,220}\b'+action+r'\b',normalized,re.I)
    allowed_action=allowed_action or re.search(r'普通(?:成员|会员).{0,50}(?:可以|允许).{0,50}'+('(?:发布|发帖)' if operation=='publish' else '回复'),normalized)
    automated=re.search(r'\b(?:automated(?:\s+browser)?\s+tools?|browser\s+automation|automation|bots?)\s+(?:(?:are|is)\s+)?(?:permitted|allowed)\b',normalized,re.I)
    automated=automated or re.search(r'(?:允许|可以).{0,50}(?:自动工具|自动化)|(?:自动工具|自动化).{0,50}(?:允许|可以)',normalized)
    if not member or not allowed_action or not automated:
        raise ValueError('browser_method_permission_unestablished')


_FORM = '''(body,submit)=>{
 const form=body.form;
 if(!form || submit.form!==form) return null;
 return {action:form.action,method:form.method,enctype:form.enctype,
  bodyName:body.name,bodyTag:body.tagName,
  submitTag:submit.tagName,submitType:submit.type,
  submitAction:submit.hasAttribute('formaction')?submit.formAction:form.action,
  submitMethod:submit.hasAttribute('formmethod')?submit.formMethod:form.method,
  submitEncoding:submit.hasAttribute('formenctype')?submit.formEnctype:form.enctype,
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


def _request(facts, *, page_url, expected_origin, operation, parent_url, external_account_id):
    if (not facts or facts['bodyTag']!='TEXTAREA' or facts['method'].lower()!='post'
            or facts['enctype'].lower()!='application/x-www-form-urlencoded'
            or facts['submitTag'] not in ('BUTTON','INPUT') or facts['submitType']!='submit'
            or facts['submitAction']!=facts['action'] or facts['submitMethod'].lower()!='post'
            or facts['submitEncoding'].lower()!='application/x-www-form-urlencoded'):
        raise ValueError('browser_method_form_unsupported')
    url=_url(facts['action'],page_url)
    if origin(url)!=expected_origin:
        raise ValueError('browser_method_request_origin_changed')
    content=facts['bodyName']
    if not isinstance(content,str) or not 1<=len(content)<=100:
        raise ValueError('browser_method_content_field_missing')
    fields={}
    for control in facts['controls']:
        name=control.get('name')
        if control['disabled'] or not name:
            continue
        # Non-selected buttons do not participate in this submitted form.
        if control['type'] in ('submit','button','reset') and not control['submit']:
            continue
        if not isinstance(name,str) or not 1<=len(name)<=100 or name in fields:
            raise ValueError('browser_method_form_fields_ambiguous')
        if control['body']:
            fields[name]=('body',None)
        elif control['submit']:
            fields[name]=('fixed',control['value'])
        elif control['tag']=='INPUT' and control['type']=='hidden':
            fields[name]=('hidden',control['value'])
        else:
            raise ValueError('browser_method_extra_editor_control')
    if fields.get(content,('',))[0]!='body':
        raise ValueError('browser_method_content_field_missing')
    parent=None
    if operation=='reply':
        candidates=[name for name,(kind,value) in fields.items() if kind=='hidden' and value==parent_url]
        if len(candidates)!=1:
            raise ValueError('browser_method_parent_field_ambiguous')
        parent=candidates[0]
    fixed={}
    dynamic=[]
    for name,(kind,value) in fields.items():
        if kind=='body' or name==parent:
            continue
        if not isinstance(value,str) or len(value)>2000:
            raise ValueError('browser_method_fixed_field_unsupported')
        if kind=='hidden' and _DYNAMIC.fullmatch(name):
            dynamic.append(name)
            continue
        if (_CREDENTIAL.search(name) or _DANGEROUS.search(name)
                or name.casefold() in _CONTROL and value not in _ACTION_VALUES
                or name.casefold() in {'author','author_id','user_id','account_id'} and value!=external_account_id):
            raise ValueError('browser_method_sensitive_or_privileged_field')
        fixed[name]=value
    if len(fixed)>20 or len(dynamic)>12:
        raise ValueError('browser_method_form_fields_limit')
    result={'method':'POST','url':url,'body_kind':'form','content_field':content,
            'fixed_fields':fixed,'dynamic_fields':dynamic}
    if parent:
        result['parent_field']=parent
    return result


async def inspect_editor(page, proposal, *, account_id, binding_version, external_account_id,
                         connection_origin, operation, evidence_ref, permission_page=None):
    """Inspect one current member editor; the caller owns all page/DB authority."""
    if (operation not in ('publish','reply') or not isinstance(proposal,dict)
            or type(binding_version) is not int or binding_version<1
            or not isinstance(external_account_id,str) or not 1<=len(external_account_id)<=500
            or not isinstance(evidence_ref,str) or not evidence_ref.strip() or not 1<=len(evidence_ref)<=500):
        raise ValueError('browser_method_scope_missing')
    account_id=str(UUID(str(account_id)))
    expected_origin=origin(connection_origin)
    page_url=_url(page.url)
    target=_url(proposal.get('target_url'))
    permission_page=permission_page or page
    permission_url=_url(permission_page.url)
    if origin(page_url)!=expected_origin or origin(target)!=expected_origin or origin(permission_url)!=expected_origin:
        raise ValueError('browser_method_origin_changed')
    if permission_url!=target:
        raise ValueError('browser_method_permission_page_changed')
    selectors=proposal.get('selectors')
    needed=['self_subject','editor_target','editor_body','submit']+(['editor_parent'] if operation=='reply' else [])
    if not isinstance(selectors,dict) or set(selectors)!=set(needed):
        raise ValueError('browser_method_selector_contract_missing')
    nodes={name:await _node(page,selectors[name]) for name in needed}
    prefix=proposal.get('self_subject_prefix','')
    suffix=proposal.get('self_subject_suffix','')
    if (not isinstance(prefix,str) or not isinstance(suffix,str) or len(prefix)>150 or len(suffix)>150
            or not _IDENTITY_PREFIX.fullmatch(prefix) or not prefix
            or prefix[-1].isalnum() and external_account_id[0].isalnum()
            or suffix and suffix[0].isalnum() and external_account_id[-1].isalnum()):
        raise ValueError('browser_method_identity_semantics_missing')
    self_text=(await nodes['self_subject'].inner_text()).strip()
    quoted=await nodes['self_subject'].evaluate("node=>!!node.closest('article,[role=article],blockquote')")
    if quoted or self_text!=(prefix+external_account_id+suffix).strip():
        raise ValueError('browser_method_identity_changed')
    target_href=await nodes['editor_target'].get_attribute('href')
    if (not await nodes['editor_target'].evaluate("node=>node.tagName==='A'")
            or not target_href or _url(target_href,page_url)!=target):
        raise ValueError('browser_method_target_changed')
    if not await nodes['editor_body'].is_editable() or not await nodes['submit'].is_enabled():
        raise ValueError('browser_method_editor_unavailable')
    parent=None
    parent_href=None
    if operation=='reply':
        parent_href=await nodes['editor_parent'].get_attribute('href')
        parent=_url(parent_href,page_url)
        if not await nodes['editor_parent'].evaluate("node=>node.tagName==='A'"):
            raise ValueError('browser_method_parent_link_missing')
        if origin(parent)!=expected_origin:
            raise ValueError('browser_method_parent_origin_changed')
        canonicals=page.locator('a[rel~=canonical],link[rel~=canonical]')
        if await canonicals.count()>1:
            raise ValueError('browser_method_parent_ambiguous')
        canonical=await canonicals.get_attribute('href') if await canonicals.count()==1 else page_url
        if parent!=_url(canonical,page_url):
            raise ValueError('browser_method_parent_changed')
    facts=await _form_facts(nodes['editor_body'],nodes['submit'])
    request=_request(facts,page_url=page_url,expected_origin=expected_origin,operation=operation,
                     parent_url=parent,external_account_id=external_account_id)
    permission=await _node(permission_page,proposal.get('permission_selector'))
    quote=proposal.get('permission_quote')
    if not isinstance(quote,str) or not 20<=len(quote)<=3000:
        raise ValueError('browser_method_permission_quote_missing')
    actual=await permission.inner_text()
    if _text(quote) not in _text(actual):
        raise ValueError('browser_method_permission_quote_missing')
    context=await permission.evaluate("node=>(node.closest('section,[role=region],aside,fieldset')||node.parentElement||node).innerText")
    if len(context)>12000:
        raise ValueError('browser_method_permission_context_ambiguous')
    _permission(context,quote,operation)
    # Re-read the identity and request-bearing DOM before returning. A reload or
    # mutation during inspection must not produce a mixed-document contract.
    if (page.url!=page_url or permission_page.url!=permission_url
            or (await nodes['self_subject'].inner_text()).strip()!=self_text
            or await _form_facts(nodes['editor_body'],nodes['submit'])!=facts
            or await nodes['editor_target'].get_attribute('href')!=target_href
            or operation=='reply' and await nodes['editor_parent'].get_attribute('href')!=parent_href
            or await permission.inner_text()!=actual
            or await permission.evaluate("node=>(node.closest('section,[role=region],aside,fieldset')||node.parentElement||node).innerText")!=context):
        raise ValueError('browser_method_document_changed')
    return {'version':'native-browser-v1','account_id':account_id,'binding_version':binding_version,
            'target_url':target,'self_url':page_url,'operations':[operation],'role':'ordinary_member',
            'selectors':{name:selectors[name] for name in needed},
            'self_subject_prefix':prefix,'self_subject_suffix':suffix,'request':request,
            'receipt_state':'unobserved','evidence_refs':[evidence_ref],
            'platform_permission':{'state':'approved','evidence_refs':[evidence_ref],
                                   'quote':quote,'source_url':permission_url}}

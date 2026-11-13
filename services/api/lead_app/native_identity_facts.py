"""Finite current-identity DOM facts; no navigation, database or authorization.

Selectors and literal affixes are proposals. Identity comparisons, private-page
provenance, document ownership and freshness remain the caller's responsibility.
Unknown layouts or multiple current fields are local capability gaps.
"""
import re
from urllib.parse import urlsplit

from .browser_execution import origin
from .native_identity_errors import IdentityFactError

_ACTING = r'(?:(?:signed|logged)\s+in\s+as|posting\s+as|replying\s+as|current\s+account|当前(?:登录)?(?:账号|账户|身份))\s*[:：]?\s*'
_FIELD = r'(?:account\s*(?:id|identifier)|user\s*name|handle|账号(?:\s*ID|标识)?|帐号(?:\s*ID|标识)?|用户名|用户标识)'
_ACTING_PREFIX = re.compile(_ACTING + r'$', re.I)
_FIELD_LABEL = re.compile(_FIELD + r'\s*[:：]?\s*$', re.I)
_FIELD_PREFIX = re.compile(r'^(\s*' + _FIELD + r'\s*[:：]\s*)', re.I)
_SUBJECT = re.compile(r'[@\w][\w.@:+-]{0,199}$', re.UNICODE)

_COMMON = r'''
 const excluded = node => !!node.closest('article,[role=article],blockquote,q,pre,code,template,[itemprop=author]') ||
   !args.allow_navigation && !!node.closest('nav,[role=navigation]');
 const visible = node => {
   const style=getComputedStyle(node), bounds=node.getBoundingClientRect();
   return node.isConnected && !node.closest('[hidden],[inert]') && bounds.width>0 && bounds.height>0 &&
     style.display!=='none' && !['hidden','collapse'].includes(style.visibility);
 };
 const text = node => (node?.innerText || '').trim();
 const label = new RegExp('^\\s*'+args.field+'\\s*[:：]?\\s*$','i');
 const fieldStart = new RegExp('^\\s*'+args.field+'\\s*[:：]','i');
 const acting = new RegExp('^'+args.acting,'i');
 const labels = node => [node.getAttribute('aria-label')||'',
   ...(node.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>{
     const item=document.getElementById(id);return item&&!excluded(item)?text(item):'';
   }),
   ...Array.from(node.labels||[]).filter(item=>!excluded(item)).map(item=>text(item))].filter(Boolean);
 const all = Array.from(document.body?.querySelectorAll('p,div,span,strong,li,dd,td,label,section,aside,input,[aria-label],[aria-labelledby]')||[]);
 if(all.length>2000 || !visible(node) || excluded(node)) return null;
'''

_ENTRY = r'''(node,args)=>{''' + _COMMON + r'''
 const link=args.link;
 if(!visible(link) || excluded(link)) return null;
 const candidates=Array.from(new Set([...all,...document.querySelectorAll('header,nav,[role=navigation]')]))
   .filter(item=>visible(item)&&!excluded(item)&&acting.test(text(item)));
 const current=candidates.filter(item=>!candidates.some(other=>other!==item&&item.contains(other)));
 const links=Array.from(document.querySelectorAll('a[rel]')).filter(item=>visible(item)&&!excluded(item)&&
   item.rel.toLowerCase().split(/\s+/).includes('me'));
 const block=node.closest('p,li,dd,td,label');
 const sameBlock=!!block && block===link.closest('p,li,dd,td,label');
 const adjacent=(first,second)=>{
   if(first.parentElement!==second.parentElement || first.nextElementSibling!==second) return false;
   for(let sibling=first.nextSibling;sibling!==second;sibling=sibling.nextSibling){
     if(!sibling || sibling.nodeType!==Node.TEXT_NODE || !/^[\s·|—–:-]*$/.test(sibling.textContent)) return false;
   }
   return true;
 };
 return {source_url:location.href, text:text(node), link_tag:link.tagName,
   href:link.getAttribute('href'), url:link.href, rel:link.rel,
   unique_link:links.length===1&&links[0]===link,
   unique_subject:current.length===1&&(node===current[0]||node.contains(current[0])),
   related:node.contains(link)||sameBlock||adjacent(node,link)||adjacent(link,node)};
}'''

_PRIVATE = r'''(node,args)=>{''' + _COMMON + r'''
 const displayTags=['P','SPAN','STRONG','B','DD','TD','LI','DIV','LABEL','OUTPUT'];
 const isInput=node.tagName==='INPUT';
 const safeInput=isInput&&['text','email'].includes(node.type)&&(node.readOnly||node.matches(':disabled'))&&
   !/password|one-time-code|webauthn/i.test(node.getAttribute('autocomplete')||'');
 if(!(displayTags.includes(node.tagName)||safeInput)) return null;
 const form=node.closest('form');
 if(form && (/\b(?:log\s*in|sign\s*in|password|credential)\b|登录|登陆|密码|验证码/i.test(text(form)) ||
   Array.from(form.querySelectorAll('input')).some(item=>item.type==='password'||/username|one-time-code/i.test(item.autocomplete)))) return null;
 const value=isInput?node.value.trim():text(node);
 if(value.length>500) return null;
 const block=node.closest('p,dd,label,td,li')||node;
 const context=text(block);
 const actualLabels=labels(node);
 const dd=node.closest('dd');
 if(dd?.previousElementSibling?.tagName==='DT'&&!excluded(dd.previousElementSibling)) actualLabels.push(text(dd.previousElementSibling));
 if(block!==node&&value&&context.indexOf(value)===context.lastIndexOf(value)){
   actualLabels.push(context.replace(value,''));
 }
 const fields=all.filter(item=>{
   if(!visible(item)||excluded(item)||item.tagName==='LABEL'&&item.control) return false;
   return fieldStart.test(text(item))&&!label.test(text(item)) || labels(item).some(value=>label.test(value)) ||
     item.tagName==='DD'&&item.previousElementSibling?.tagName==='DT'&&!excluded(item.previousElementSibling)&&label.test(text(item.previousElementSibling));
 });
 const current=fields.filter(item=>!fields.some(other=>other!==item&&item.contains(other)));
 const kind=value=>Object.entries(args.kinds).find(([name,pattern])=>
   new RegExp('^\\s*'+pattern+'\\s*[:：]?\\s*$','i').test(value))?.[0];
 const kinds=values=>Array.from(new Set(values.map(kind).filter(Boolean)));
 const selectedKinds=kinds([...actualLabels,...(value.match(fieldStart)||[])]);
 const sameFields=current.filter(item=>{
   const values=labels(item),dd=item.closest('dd');
   if(dd?.previousElementSibling?.tagName==='DT'&&!excluded(dd.previousElementSibling)) values.push(text(dd.previousElementSibling));
   values.push(...(text(item).match(fieldStart)||[]));
   const names=kinds(values);
   return names.length===1&&selectedKinds.length===1&&names[0]===selectedKinds[0];
 });
 const leaves=Array.from(block.querySelectorAll('*')).filter(item=>visible(item)&&!excluded(item)&&
   (item.tagName==='INPUT'||!item.children.length&&text(item)&&!label.test(text(item))));
 const marker=new RegExp(args.field+'\\s*[:：]','ig');
 return {source_url:location.href, value, labels:actualLabels.filter(value=>label.test(value)),
   field_markers:Array.from(context.matchAll(marker)).length, leaf_values:leaves.length,
   unique_field:sameFields.length===1&&(sameFields[0]===node||sameFields[0].contains(node)||node.contains(sameFields[0]))};
}'''


def _patterns():
    return {'field': _FIELD, 'acting': _ACTING,'kinds':{
        'account_id':r'(?:account\s*(?:id|identifier)|账号(?:\s*ID|标识)?|帐号(?:\s*ID|标识)?)',
        'username':r'(?:user\s*name|用户名|用户标识)','handle':r'handle'}}


def _selector(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 300:
        raise IdentityFactError('identity_selector_missing')


async def _node(page, selector):
    _selector(selector)
    node = page.locator(selector)
    if await node.count() != 1 or not await node.is_visible():
        raise IdentityFactError('identity_element_ambiguous')
    return node


def _subject(value):
    if not isinstance(value, str) or not _SUBJECT.fullmatch(value):
        raise IdentityFactError('identity_subject_invalid')
    return value


def _affixes(prefix, suffix):
    if (not isinstance(prefix, str) or not isinstance(suffix, str)
            or len(prefix) > 150 or len(suffix) > 150):
        raise IdentityFactError('identity_affix_invalid')


def _extract(text, prefix, suffix):
    if (not text.startswith(prefix) or suffix and not text.endswith(suffix)
            or len(prefix) + len(suffix) >= len(text)):
        raise IdentityFactError('identity_value_missing')
    raw = text[len(prefix):len(text)-len(suffix) if suffix else len(text)]
    # A proposed affix may describe surrounding UI, never remove part of an ID.
    identifier = re.compile(r'[\w.@:+-]')
    if (prefix and identifier.fullmatch(prefix[-1]) and identifier.fullmatch(raw[0])
            or suffix and identifier.fullmatch(suffix[0]) and identifier.fullmatch(raw[-1])):
        raise IdentityFactError('identity_affix_splits_subject')
    return _subject(raw.strip())


async def inspect_self_entry(page, *, link_selector, subject_selector, subject_prefix,
                             subject_suffix='', connection_origin):
    """Observe a unique local current-subject/rel=me association, without opening it."""
    _affixes(subject_prefix, subject_suffix)
    if not subject_prefix or not _ACTING_PREFIX.fullmatch(subject_prefix):
        raise IdentityFactError('identity_current_subject_semantics_missing')
    page_url = page.url
    if origin(page_url) != origin(connection_origin):
        raise IdentityFactError('identity_connection_origin_changed')
    node = await _node(page, subject_selector)
    link = await _node(page, link_selector)
    handle = await link.element_handle()
    try:
        args = {**_patterns(), 'link': handle, 'allow_navigation': True}
        facts = await node.evaluate(_ENTRY, args)
        if (not facts or facts['source_url'] != page_url or facts['link_tag'] != 'A'
                or not facts['href'] or not facts['unique_link']
                or not facts['unique_subject'] or not facts['related']):
            raise IdentityFactError('identity_current_self_unestablished')
        href = facts['url']
        if origin(href) != origin(connection_origin) or urlsplit(href).fragment:
            raise IdentityFactError('identity_self_origin_changed')
        subject = _extract(facts['text'], subject_prefix, subject_suffix)
        if page.url != page_url or await node.evaluate(_ENTRY, args) != facts:
            raise IdentityFactError('identity_document_changed')
        return {'self_url': href, 'self_link_selector': link_selector, 'acting_subject': subject}
    finally:
        if handle:
            await handle.dispose()


async def inspect_private_subject(page, *, subject_selector, value_prefix='', value_suffix=''):
    """Read one explicit account-ID/username/handle field and its literal affixes."""
    _affixes(value_prefix, value_suffix)
    if (value_prefix and not _FIELD_LABEL.fullmatch(value_prefix)
            or value_suffix and not value_suffix[0].isspace()):
        raise IdentityFactError('identity_private_affix_unsupported')
    page_url = page.url
    origin(page_url)
    node = await _node(page, subject_selector)
    args = _patterns()
    facts = await node.evaluate(_PRIVATE, args)
    if (not facts or facts['source_url'] != page_url or not facts['unique_field']
            or facts['field_markers'] > 1 or facts['leaf_values'] > 1):
        raise IdentityFactError('identity_private_field_ambiguous')
    prefix = value_prefix
    match = _FIELD_PREFIX.match(facts['value'])
    if not prefix and match:
        prefix = match.group(1)
    actual_label = (prefix if prefix else next(iter(facts['labels']), '')).strip()
    if not _FIELD_LABEL.fullmatch(actual_label):
        raise IdentityFactError('identity_private_field_semantics_missing')
    subject = _extract(facts['value'], prefix, value_suffix)
    if page.url != page_url or await node.evaluate(_PRIVATE, args) != facts:
        raise IdentityFactError('identity_document_changed')
    return {'subject': subject, 'self_selector': subject_selector,
            'value_prefix': prefix, 'value_suffix': value_suffix, 'evidence': {'label': actual_label}}

"""Observe a small POST search form contract; never navigate, submit or authorize.

These are current DOM facts, not permission or identity evidence. The consumer
must bind them to its live task, document, account and network gate. Unknown or
ambiguous forms remain capability gaps. Query and CSRF/nonce values are omitted.
"""
import re
from urllib.parse import urljoin, urlsplit

from .browser_execution import origin
from .native_method_facts import _CONTROL, _CREDENTIAL, _DANGEROUS, _DYNAMIC, _text

_SEARCH = re.compile(r'\b(?:search|searchbox|find|lookup|query)\b|搜索|查询|检索', re.I)
_WRITE = re.compile(
    r'(?:^|[\W_])(?:publish|post|reply|replies|comment|create|update|edit|delete|remove|destroy|'
    r'settings|profile|admin|moderator|login|log\s*in|sign\s*in|register|password|credential|'
    r'upload|checkout|pay|buy|subscribe|join|follow|vote|like|share|invite|ban|body|message)'
    r'(?:$|[\W_])|发布|发帖|回复|评论|删除|设置|登录|登陆|注册|上传|支付|订阅|关注|点赞', re.I)
_READ_CONTROL_VALUES = {'search', 'query', 'find', 'lookup'}

_SNAPSHOT = r'''patterns => {
 const dynamic = new RegExp(patterns.dynamic, 'i');
 const sensitive = new RegExp(patterns.sensitive, 'i');
 const visible = node => {
   const style = getComputedStyle(node);
   return !node.closest('[hidden],[inert]') && style.visibility !== 'hidden' &&
     style.visibility !== 'collapse' && !!node.getClientRects().length;
 };
 const labels = node => [
   ...Array.from(node.labels || [], label => label.innerText),
   node.getAttribute('aria-label') || '',
   ...(node.getAttribute('aria-labelledby') || '').split(/\s+/)
     .map(id => document.getElementById(id)?.innerText || '')
 ].filter(Boolean);
 const forms = Array.from(document.forms).filter(visible);
 if (forms.length > 50) return null;
 return {source_url: location.href, charset: document.characterSet,
   forms: forms.map(form => {
     const elements = Array.from(form.elements);
     if (elements.length > 60) return null;
     return {action: form.action, method: form.method, enctype: form.enctype,
       charset: form.acceptCharset, text: form.innerText,
       labels: [...labels(form), form.getAttribute('role') || ''],
       controls: elements.map(node => {
         const name = node.name || '';
         const type = node.type || '';
         const hasValue = node.tagName === 'INPUT' && type === 'hidden' ||
           (node.tagName === 'BUTTON' || node.tagName === 'INPUT') && type === 'submit';
         const exposeValue = hasValue && !sensitive.test(name) && !dynamic.test(name);
         return {tag: node.tagName, type, name, disabled: node.matches(':disabled'),
           visible: visible(node), readonly: !!node.readOnly,
           autocomplete: node.getAttribute('autocomplete') || '',
           dirname: node.hasAttribute('dirname'), maxlength: node.maxLength ?? -1,
           labels: [...labels(node), node.getAttribute('role') || ''],
           text: node.tagName === 'BUTTON' ? node.innerText :
             node.tagName === 'INPUT' && ['submit','button','reset'].includes(type) ? node.value : '',
           override: ['formaction','formmethod','formenctype','formtarget'].some(attr => node.hasAttribute(attr)),
           value: exposeValue ? node.value : null,
           value_length: hasValue ? node.value.length : 0};
       })};
   })};
}'''


def _url(value, base=None):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise ValueError('browser_read_url_missing')
    result = urljoin(base or '', value)
    origin(result)
    if urlsplit(result).fragment:
        raise ValueError('browser_read_url_fragment')
    return result


def _quotes(form):
    quotes = list(form['labels'])
    for control in form['controls']:
        if control['disabled']:
            continue
        if control['visible'] and control['tag'] == 'INPUT' and control['type'] in ('text', 'search'):
            quotes.extend(control['labels'])
        elif control['visible'] and control['type'] == 'submit':
            quotes.extend([*control['labels'], control['text']])
    return [_text(quote) for quote in quotes if quote and len(quote) <= 1000]


def _request(form, *, page_url, quote):
    if (form['method'].lower() != 'post'
            or form['enctype'].lower() != 'application/x-www-form-urlencoded'
            or form['charset'].lower() not in ('', 'utf-8')
            or len(form['text']) > 12000 or _WRITE.search(form['text'])
            or any(_WRITE.search(text) for text in _quotes(form))):
        raise ValueError('browser_read_form_unsupported')
    url = _url(form['action'], page_url)
    if origin(url) != origin(page_url):
        raise ValueError('browser_read_request_origin_changed')
    controls = [control for control in form['controls']
                if not control['disabled'] and control['tag'] not in ('FIELDSET', 'OUTPUT')]
    submits = [control for control in controls if control['type'] == 'submit']
    queries = [control for control in controls
               if control['tag'] == 'INPUT' and control['type'] in ('text', 'search')]
    if len(queries) != 1 or len(submits) != 1:
        raise ValueError('browser_read_controls_ambiguous')
    query, submit = queries[0], submits[0]
    if (not query['visible'] or query['readonly'] or query['dirname']
            or query['maxlength'] == 0 or not submit['visible']
            or submit['tag'] not in ('INPUT', 'BUTTON')):
        raise ValueError('browser_read_control_unavailable')
    fixed, dynamic, names = {}, [], set()
    for control in controls:
        name = control['name']
        if control['override']:
            raise ValueError('browser_read_submit_override')
        if (control['type'] in ('password', 'file', 'image')
                or re.search(r'password|username|one-time-code|webauthn|cc-', control['autocomplete'], re.I)):
            raise ValueError('browser_read_sensitive_control')
        if control is not query and control is not submit and not (
                control['tag'] == 'INPUT' and control['type'] == 'hidden'):
            raise ValueError('browser_read_extra_control')
        if not name and control is submit:
            continue
        if (not isinstance(name, str) or not 1 <= len(name) <= 100
                or re.search(r'[\x00-\x20\x7f]', name) or name in names):
            raise ValueError('browser_read_fields_ambiguous')
        names.add(name)
        if control is not query and control is not submit and _DYNAMIC.fullmatch(name):
            if control['value_length'] > 2000:
                raise ValueError('browser_read_dynamic_field_unsupported')
            dynamic.append(name)
            continue
        semantic_name = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', name).casefold()
        if (_CREDENTIAL.search(semantic_name) or _DANGEROUS.search(semantic_name) or _WRITE.search(semantic_name)
                or semantic_name in {'author', 'author_id', 'user_id', 'account_id', '_charset_', '_method', 'method'}):
            raise ValueError('browser_read_sensitive_or_writing_field')
        if control is query:
            if semantic_name in _CONTROL:
                raise ValueError('browser_read_query_field_unsupported')
            continue
        value = control['value']
        if (not isinstance(value, str) or len(value) > 2000 or _WRITE.search(value)
                or semantic_name in _CONTROL and value.casefold() not in _READ_CONTROL_VALUES):
            raise ValueError('browser_read_fixed_field_unsupported')
        fixed[name] = value
    if len(fixed) > 20 or len(dynamic) > 12:
        raise ValueError('browser_read_fields_limit')
    return {'source_url': page_url,
            'request': {'method': 'POST', 'url': url, 'body_kind': 'form',
                        'query_field': query['name'], 'fixed_fields': fixed, 'dynamic_fields': dynamic},
            'max_query_length': min(query['maxlength'], 2000) if query['maxlength'] > 0 else 2000,
            'evidence': {'search_quote': quote}}


async def inspect_search_form(page, *, request_url: str | None = None) -> dict:
    """Return one live, bounded search request description or a local ValueError."""
    page_url = _url(page.url)
    expected = _url(request_url) if request_url is not None else None
    patterns = {'dynamic': _DYNAMIC.pattern, 'sensitive': _CREDENTIAL.pattern}
    facts = await page.evaluate(_SNAPSHOT, patterns)
    if (not facts or facts['source_url'] != page_url or facts['charset'].lower() != 'utf-8'
            or any(form is None for form in facts['forms'])):
        raise ValueError('browser_read_document_unsupported')
    forms = facts['forms']
    if expected is not None:
        forms = [form for form in forms if form['action'] == expected]
        if len(forms) != 1:
            raise ValueError('browser_read_form_ambiguous')
    candidates = [(form, next((text for text in _quotes(form) if _SEARCH.search(text)), None))
                  for form in forms]
    candidates = [(form, quote) for form, quote in candidates if quote]
    if len(candidates) != 1:
        raise ValueError('browser_read_search_form_ambiguous')
    form, quote = candidates[0]
    result = _request(form, page_url=page_url, quote=quote)
    # Do not return a contract assembled across a reload or DOM mutation. Values
    # deliberately omitted above (query and nonce/CSRF) are not frozen facts.
    if page.url != page_url or await page.evaluate(_SNAPSHOT, patterns) != facts:
        raise ValueError('browser_read_document_changed')
    return result

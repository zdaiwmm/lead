"""Read-only receipt diagnostics; fixed feedback never supplies receipt proof."""
from urllib.parse import urljoin

SELECTOR_DESCRIPTIONS={
    'author':'CSS for only the visible author value in the actual receipt article, matching the verified acting subject namespace. Select the username/handle value, not a whole byline, account metadata or a different stable Account ID.',
    'body':'CSS for one visible value element whose complete inner text is the exact authorized body, within the same receipt article. Do not infer paragraph positions from flattened text or select a timestamp, metadata, editor or enclosing article.',
    'url':'CSS for the visible canonical/permanent receipt link in that same article; its resolved href must equal the current page URL. Follow the actual permanent link first when the current address is a redirect or query variant.',
    'parent':'CSS for the visible, semantically labeled original parent link of this reply, within or locally related to the same receipt article. It must identify the original parent, not a different post, navigation link or hidden ID.',
}
RECEIPT_ERROR_HINTS={
    **{f'receipt_{field}_selector_unavailable':f'The {field} selector must identify one visible actual value node. Reobserve and correct this field.' for field in ('body','author','url','parent')},
    'receipt_body_value_mismatch':'The selected body value does not equal the complete original authorized body. Select only that actual body value, not metadata or a guessed paragraph position. Never submit again.',
    'receipt_author_value_mismatch':'The selected author value does not match the original verified acting subject. Select the actual author value without byline metadata; never infer a namespace alias or change the account.',
    'receipt_url_value_mismatch':'The receipt link href differs from the current page URL. Read the actual canonical/permanent receipt address and reobserve; never submit again.',
    'receipt_parent_value_mismatch':'The selected parent link does not match the immutable original parent. Reobserve the actual labeled parent value; do not change the Action.',
    'receipt_document_changed':'The receipt document changed during observation. Reobserve the same original action before reporting again.',
    'receipt_relationship_unproved':'The original receipt identity, body, URL, parent or their document relationship is not proven. Reobserve actual value nodes in the same receipt article. This grants no success and never authorizes another submission.',
    'browser_receipt_candidate_unproved':'An independently required receipt candidate could not be read or safely excluded. The original Action remains unknown; only continue read-only verification, never resubmit or ignore that candidate.',
}


class ReceiptFactError(ValueError):
    def __init__(self,code):
        if not isinstance(code,str) or code not in RECEIPT_ERROR_HINTS:
            raise ValueError('unrecognized_receipt_error_code')
        self.code=code
        # Keep the prior ValueError message for legacy consumers and logs.
        super().__init__(code if code=='browser_receipt_candidate_unproved' else 'browser_receipt_unproved')


def receipt_rpc_feedback(operation,status,payload):
    if operation!='report_receipt' or status!=409 or not isinstance(payload,dict):
        return None
    code=payload.get('code')
    if not isinstance(code,str) or code not in RECEIPT_ERROR_HINTS:
        return None
    return code+': '+RECEIPT_ERROR_HINTS[code]


def receipt_protocol_instructions():
    """The same content-free field contract may be sent by standard owner steer."""
    return 'Receipt value selector protocol: '+ ' '.join(name+': '+description for name,description in SELECTOR_DESCRIPTIONS.items())+' Use structural value selectors that remain readable across candidate receipt pages. A selector tied only to one post ID, URL or expected text cannot safely exclude a different candidate. These are proposals only; the original server must verify identity, current document, uniqueness and independent receipt evidence. Never resubmit the original Action.'


async def diagnose_receipt_selectors(page,selectors,*,body,author,parent=None):
    """Called only after the original receipt reader failed; never accepts facts."""
    try:
        for field,expected in [('body',body),('author',author),('url',page.url)]+([('parent',parent)] if parent is not None else []):
            node=page.locator(selectors[field])
            if await node.count()!=1 or not await node.is_visible():
                return f'receipt_{field}_selector_unavailable'
            actual=(await node.inner_text()).strip() if field in ('body','author') else urljoin(page.url,await node.get_attribute('href') or '')
            if actual!=expected:
                return f'receipt_{field}_value_mismatch'
    except Exception:
        # Arbitrary selector/DOM/SDK details never become public tool feedback.
        pass
    return 'receipt_relationship_unproved'

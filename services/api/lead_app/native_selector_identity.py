"""Match a full Playwright selector to the exact scoped native DOM node.

A read-only structural path bridges the two public browser interfaces. Both
physical node references remain pinned and are rechecked; attributes, text,
coordinates and reduced selectors never substitute for node identity.
"""

_IDENTITY = r'''node=>{
    if(!node || node.nodeType!==1 || !node.isConnected || node.ownerDocument!==document || window!==window.top)return null;
    const path=[];let item=node;
    for(let depth=0;item!==document.documentElement;depth++){
        if(depth>=128)return null;
        const parent=item.parentNode;
        if(!parent)return null;
        const index=Array.prototype.indexOf.call(parent.children||[],item);
        if(index<0 || index>10000)return null;
        if(parent.nodeType===11 && parent.host && parent.mode==='open'){
            path.unshift({shadow:index});item=parent.host;
        }else if(parent.nodeType===1){
            path.unshift(index);item=parent;
        }else return null;
    }
    return {path,url:document.URL,tag:node.tagName,type:node.type||''};
}'''

_SAME_PATH = r'''(node, expected)=>{
    if(!node.isConnected || node.ownerDocument!==document || document.URL!==expected.url || window!==window.top)return false;
    let actual=document.documentElement;
    for(const step of expected.path){
        if(!actual)return false;
        actual=typeof step==='number'?actual.children[step]:actual.shadowRoot?.children[step.shadow];
    }
    return node===actual;
}'''


async def match_control(controller,session,identifier,selector):
    """Return facts only for one stable original selector and the same element."""
    from playwright.async_api import Error as PlaywrightError

    if not isinstance(selector,str) or not selector:
        return {'matches':False}
    gate=controller.actor.native_runtime.gate
    handle=None
    async def native_identity():
        result=await gate.send('Runtime.callFunctionOn',{'objectId':identifier,
            'functionDeclaration':'function(){const identity='+_IDENTITY+';return identity(this);}',
            'returnByValue':True},session)
        return result.get('result',{}).get('value')
    try:
        before=await controller.document()
        original=await native_identity()
        if not original or original['url']!=controller.page.url:
            return {'matches':False}
        locator=controller.page.locator(selector)
        if await locator.count()!=1 or not await locator.is_visible():
            return {'matches':False}
        # A pinned public ElementHandle is needed here specifically to detect
        # replacement between reads; the selector is still resolved in full.
        handle=await locator.element_handle()
        if not handle or not await handle.evaluate(_SAME_PATH,original):
            return {'matches':False}
        if await native_identity()!=original or await locator.count()!=1:
            return {'matches':False}
        if not await locator.evaluate('(node,pinned)=>node===pinned',handle):
            return {'matches':False}
        if not await handle.evaluate(_SAME_PATH,original) or before!=await controller.document():
            return {'matches':False}
        return {'matches':True,'tag':original['tag'],'type':original['type']}
    except (PlaywrightError,KeyError,ValueError):
        return {'matches':False}
    finally:
        if handle:
            await handle.dispose()

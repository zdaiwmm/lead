"""Visible person-step facts; no credential input, navigation or authorization."""
import hashlib
import json


async def person_challenge(page):
    facts = await page.evaluate(r"""() => {
        const visible=node=>{const r=node.getBoundingClientRect(),s=getComputedStyle(node);
            return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&
                !node.closest('[hidden],[aria-hidden=true],[inert]');};
        const outsideContent=node=>!node.closest('article,[role=article],blockquote,shreddit-post');
        const headings=[...document.querySelectorAll('h1,h2,[role=heading]')]
            .filter(node=>visible(node)&&outsideContent(node));
        const oneTime=[...document.querySelectorAll('input[autocomplete="one-time-code"]')].find(visible);
        let kind=oneTime?'otp':null, heading=null;
        const patterns=[
            ['qr',/^(?:(?:please\s+)?scan\s+(?:(?:the|this|a)\s+)?(?:qr\s*code|code)(?:\s+to\s+(?:sign\s*in|log\s*in|continue))?|(?:qr\s*code\s+)?(?:sign[ -]?in|log[ -]?in)\s+(?:with|using)\s+(?:a\s+)?qr\s*code|(?:请)?(?:使用手机)?扫码登录|(?:请)?扫描(?:二维码)?(?:以|来)?登录|二维码登录)[.!?。！]?$/i],
            ['passkey',/^(?:(?:please\s+)?(?:use|sign\s*in\s+with)\s+(?:(?:your|a)\s+)?(?:passkey|security\s+key)|(?:请)?使用(?:你的)?(?:通行密钥|安全密钥)(?:登录)?)[.!?。！]?$/i],
            ['device',/^(?:check\s+your\s+(?:phone|device)|approve\s+(?:this|the)\s+sign[ -]?in(?:\s+on\s+your\s+(?:phone|device))?|(?:请)?(?:在|查看)(?:你的)?(?:手机|设备)(?:上)?(?:确认|批准)(?:此次)?登录)[.!?。！]?$/i]
        ];
        if(!kind) {
            // An offered alternative beside an available password form isn't
            // evidence that a person-only step is currently required.
            const password=[...document.querySelectorAll('input[type=password]')].some(node=>visible(node)&&outsideContent(node)&&
                node.form?.method.toLowerCase()==='post'&&/sign\s*in|log\s*in|login|登录|登入/i.test(node.form.innerText));
            if(password) return null;
            for(const node of headings) {
                const text=(node.innerText||'').trim();
                const matched=patterns.find(([,pattern])=>pattern.test(text));
                if(matched){kind=matched[0];heading=node;break;}
            }
        }
        if(!kind) return null;
        const region=oneTime?.form||heading?.closest('form,[role=dialog],section,main')||document.body;
        const markers=[...region.querySelectorAll('input[type=hidden]')]
            .filter(node=>/^(?:nonce|csrf(?:_token)?|challenge(?:_id|_token)?|state|flow_token)$/i.test(node.name))
            .slice(0,16).map(node=>[node.name,node.value]);
        const assets=kind==='qr'?[...region.querySelectorAll('img,canvas,[role=img]')]
            .filter(node=>visible(node)&&outsideContent(node)&&
                !/logo|avatar|brand/i.test([node.getAttribute('alt'),node.getAttribute('aria-label')].join(' ')))
            .slice(0,8).map(node=>{
                let pixels=null;
                try {
                    if(node.tagName==='CANVAS') pixels=node.toDataURL('image/png');
                    else if(node.tagName==='IMG'&&node.complete&&node.naturalWidth&&node.naturalWidth<=1024&&node.naturalHeight<=1024){
                        const canvas=document.createElement('canvas');canvas.width=node.naturalWidth;canvas.height=node.naturalHeight;
                        canvas.getContext('2d').drawImage(node,0,0);pixels=canvas.toDataURL('image/png');
                    }
                }catch{} // Cross-origin pixels remain opaque; the actual source still binds.
                return {source:node.currentSrc||node.getAttribute('src'),pixels,
                    nonce:node.getAttribute('data-nonce'),challenge:node.getAttribute('data-challenge-id')};
            }):[];
        return {kind,url:location.href,markers,assets,
            field:oneTime?{name:oneTime.name,autocomplete:oneTime.autocomplete,action:oneTime.form?.action}:null};
    }""")
    if facts is None:
        from .adapters.core import HumanVerificationRequired, check_challenge
        try:
            await check_challenge(page)
            return None
        except HumanVerificationRequired:
            # Retain the established dedicated CAPTCHA proof, including its
            # original exclusion of articles and hidden preloaded widgets.
            frames = await page.locator('iframe').evaluate_all(
                "nodes=>nodes.filter(n=>n.getBoundingClientRect().width&&n.getBoundingClientRect().height).map(n=>n.src)"
            )
            facts = {'kind': 'captcha', 'url': page.url, 'frames': frames[:16]}
    fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
    reasons = {'otp': '请在原窗口完成并提交平台验证码。', 'qr': '请使用你的设备扫描原窗口中的登录二维码。',
               'passkey': '请在你的设备上完成通行密钥或安全密钥确认。',
               'device': '请在你的受信设备上确认此次登录。', 'captcha': '请在原窗口完成平台真人验证。'}
    return {'kind': facts['kind'], 'fingerprint': fingerprint, 'reason': reasons[facts['kind']]}

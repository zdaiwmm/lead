"""One native model fragment's request envelope and measured-usage boundary."""
import json

from .native_vision import IMAGE_MODEL, checked_image, request_projection


class ModelBillingUnknown(RuntimeError):
    pass


class ModelCallMeter:
    def __init__(self, *, max_requests, max_total_tokens, max_output_tokens, sensitive_data=None,image_model=None):
        self.max_requests = min(8, int(max_requests))
        self.max_total_tokens = int(max_total_tokens)
        self.max_output_tokens = int(max_output_tokens)
        self.provider_usage = []
        self.unmeasured_requests = []
        self.provider_requests = []
        self.provider_responses = []
        self.secret_seen = []
        self._secrets = tuple(value for value in (sensitive_data or {}).values() if isinstance(value, str) and value)
        self.diagnostics = lambda stage: None
        self.image_model=image_model
        self._approved_image=None
        self._verify_image=None
        self.vision_images_sent=0

    def approve_image(self,image,verify=None):
        self._approved_image=None
        self._verify_image=None
        if image is not None:
            self._approved_image=checked_image(image)[0]
            self._verify_image=verify

    @property
    def billing_unknown(self):
        return bool(self.unmeasured_requests or len(self.provider_usage) != len(self.provider_requests))

    def require_measured(self):
        if self.billing_unknown:
            raise ModelBillingUnknown('The prior model request has unmeasured usage; reconcile its original reservation.')

    async def measure_request(self, request):
        self.require_measured()
        units,image_count,scan=request_projection(request.content,image_model=self.image_model,approved_image=self._approved_image)
        for secret in self._secrets:
            if secret.encode() in scan or json.dumps(secret)[1:-1].encode() in scan:
                self.secret_seen.append(True)
                raise ValueError('Credential redaction boundary rejected the model request')
        if len(self.provider_requests) >= self.max_requests:
            raise ValueError('Model request limit reached')
        if image_count:
            from urllib.parse import urlsplit
            target=urlsplit(str(request.url))
            if (self.image_model!=IMAGE_MODEL or target.scheme!='https' or target.hostname!='api.deepseek.com'
                    or target.port not in (None,443) or target.username or target.password or target.query or target.fragment
                    or target.path not in ('/chat/completions','/v1/chat/completions')
                    or not self._verify_image or not await self._verify_image()):
                raise ValueError('native_image_not_current')
        if self.max_total_tokens:
            used = sum(item['total_tokens'] for item in self.provider_usage)
            if used + units + self.max_output_tokens > self.max_total_tokens:
                raise ValueError('Model token envelope reached')
        self.provider_requests.append(1)
        self.vision_images_sent+=image_count
        self.diagnostics('model_request')

    async def measure_response(self, response):
        self.provider_responses.append(1)
        self.diagnostics('model_response')
        if not response.is_success:
            self.unmeasured_requests.append(1)
            return
        try:
            await response.aread()
        except BaseException:
            self.unmeasured_requests.append(1)
            raise
        try:
            usage = response.json().get('usage')
            fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
            valid = (isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in fields)
                and usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'])
            if not valid:
                self.unmeasured_requests.append(1)
            else:
                self.provider_usage.append(usage)
        except (ValueError, TypeError, AttributeError):
            self.unmeasured_requests.append(1)


def known_usage(usage):
    """A complete zero-transport report is known; a missing report never is."""
    if (not isinstance(usage,dict) or type(usage.get('entry_count')) is not int
            or type(usage.get('request_count')) is not int or usage['entry_count']<0
            or usage['request_count']!=usage['entry_count'] or usage.get('billing_unknown') is not False
            or usage.get('unmeasured_requests')!=0):
        return False
    if usage['entry_count']>0:
        return True
    return (usage.get('measurement_complete') is True and all(type(usage.get(key)) is int and usage[key]==0
        for key in ('total_tokens','total_prompt_tokens','total_completion_tokens')))

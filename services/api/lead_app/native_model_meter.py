"""One native model fragment's request envelope and measured-usage boundary."""
import json


class ModelBillingUnknown(RuntimeError):
    pass


class ModelCallMeter:
    def __init__(self, *, max_requests, max_total_tokens, max_output_tokens, sensitive_data=None):
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

    @property
    def billing_unknown(self):
        return bool(self.unmeasured_requests or len(self.provider_usage) != len(self.provider_requests))

    def require_measured(self):
        if self.billing_unknown:
            raise ModelBillingUnknown('The prior model request has unmeasured usage; reconcile its original reservation.')

    async def measure_request(self, request):
        self.require_measured()
        for secret in self._secrets:
            if secret.encode() in request.content or json.dumps(secret)[1:-1].encode() in request.content:
                self.secret_seen.append(True)
                raise ValueError('Credential redaction boundary rejected the model request')
        if len(self.provider_requests) >= self.max_requests:
            raise ValueError('Model request limit reached')
        if self.max_total_tokens:
            used = sum(item['total_tokens'] for item in self.provider_usage)
            if used + len(request.content) + self.max_output_tokens > self.max_total_tokens:
                raise ValueError('Model token envelope reached')
        self.provider_requests.append(1)
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

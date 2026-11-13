"""Fixed, content-free method-discovery feedback; never grants permission."""
from .native_identity_errors import IDENTITY_ERROR_HINTS

METHOD_ERROR_HINTS = {
    'browser_method_content_field_missing': 'A single observed text-body field with a real field name is required.',
    'browser_method_dangerous_operation': 'The observed form is a dangerous operation. Do not treat it as publishing.',
    'browser_method_document_changed': 'The document changed during inspection. Obtain a fresh observation.',
    'browser_method_editor_unavailable': 'The observed text editor and submit control must be editable and enabled.',
    'browser_method_element_ambiguous': 'Each proposed CSS selector must identify one visible observed element. Reobserve and correct the selector.',
    'browser_method_extra_editor_control': 'The form has an unsupported additional editable control. Retain this local capability gap.',
    'browser_method_fixed_field_unsupported': 'The form has an unsupported fixed field. Retain this local capability gap.',
    'browser_method_form_fields_ambiguous': 'The form fields are ambiguous. Do not guess a request contract.',
    'browser_method_form_fields_limit': 'The form exceeds the supported field limit. Retain this local capability gap.',
    'browser_method_form_unsupported': 'The observed editor must belong to one supported POST form with its actual submit control.',
    'browser_method_identity_changed': 'The current acting subject does not match the verified private identity. Stop and recheck identity.',
    'browser_method_identity_semantics_missing': 'Use bounded current-account wording and literal affixes from the observed identity element.',
    'browser_method_origin_changed': 'The editor or permission origin differs from the original account origin. Do not extend scope.',
    'browser_method_parent_ambiguous': 'The observed canonical parent is ambiguous. Do not guess a parent.',
    'browser_method_parent_changed': 'The observed parent does not match the current canonical parent. Reobserve it.',
    'browser_method_parent_field_ambiguous': 'The form does not establish one unambiguous parent mapping. Do not supply a guessed identifier.',
    'browser_method_parent_link_missing': 'Select the actual observed parent link for this reply editor.',
    'browser_method_parent_origin_changed': 'The observed parent is outside the original account origin. Do not follow it.',
    'browser_method_permission_context_ambiguous': 'The local permission context is too broad or ambiguous. Use the actual local rules.',
    'browser_method_permission_denied': 'The observed rules prohibit or condition this automation. Do not bypass them.',
    'browser_method_permission_page_changed': 'The permission page differs from the proposed target. Reobserve the actual target rules.',
    'browser_method_permission_quote_missing': 'The quoted permission must occur in the selected actual rule text.',
    'browser_method_permission_source_invalid': 'The selected permission source is unsupported or user-authored. Do not treat it as platform rules.',
    'browser_method_permission_unestablished': 'Unconditional ordinary-member permission for this action and browser automation is not established.',
    'browser_method_request_origin_changed': 'The observed form request leaves the original account origin. Do not extend scope.',
    'browser_method_scope_missing': 'The original method-discovery scope is incomplete. Stop and retain the original task.',
    'browser_method_selector_contract_missing': 'For publish, selectors must contain exactly self_subject, editor_target, editor_body, submit. For reply, include exactly those four plus editor_parent. Each value is observed CSS, never an SDK [n] reference; do not add other keys.',
    'browser_method_selector_missing': 'Use a bounded CSS selector for an actually observed element; SDK numbered references are not CSS.',
    'browser_method_sensitive_or_privileged_field': 'The form contains a sensitive or privileged field. Do not treat it as a member publishing form.',
    'browser_method_target_changed': 'The selected actual target link does not match the proposed target. Reobserve it.',
    'browser_method_url_fragment': 'The proposed method URL contains an unsupported fragment. Retain this local capability gap.',
    'browser_method_url_missing': 'Use a bounded actual observed URL for the original method scope.',
    'method_discovery_task_required': 'Only the original account-discovery task may record this method.',
    'method_discovery_scope_changed': 'The proposed operation or target is outside the original discovery scope. Do not extend it.',
    'target_not_permitted': 'The original intent does not permit this target. Do not bypass that restriction.',
    'method_document_changed': 'The document changed during method inspection. Obtain a fresh observation.',
}


class MethodFactError(ValueError):
    """Keep the original message and expose only one fixed, allowlisted code."""
    def __init__(self,code):
        if not isinstance(code,str) or code not in METHOD_ERROR_HINTS:
            raise ValueError('unrecognized_method_error_code')
        self.code=code
        super().__init__(code)


def method_rpc_feedback(operation,status,payload):
    """Never promote arbitrary response text, detail, selectors or values."""
    if status!=409 or operation!='record_method' or not isinstance(payload,dict):
        return None
    code=payload.get('code')
    if not isinstance(code,str):
        return None
    hint=METHOD_ERROR_HINTS.get(code) or IDENTITY_ERROR_HINTS.get(code)
    return code+': '+hint if hint else None

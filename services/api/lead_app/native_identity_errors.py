"""Fixed, content-free identity feedback shared by manager and native SDK worker."""

IDENTITY_ERROR_HINTS = {
    'identity_selector_missing': 'Choose a valid selector for an actually observed identity element.',
    'identity_element_ambiguous': 'The selector must identify exactly one visible observed element.',
    'identity_subject_invalid': 'The selected value is not one account ID or handle. Recheck the observed field and its literal prefix and suffix.',
    'identity_affix_invalid': 'Provide bounded literal prefix and suffix strings from the observed element.',
    'identity_value_missing': 'The proposed literal prefix or suffix does not match the observed identity element. Reobserve it and correct the affixes.',
    'identity_affix_splits_subject': 'A prefix or suffix cannot remove any part of the account ID or handle.',
    'identity_current_subject_semantics_missing': 'Use actual current-account wording, rather than a public author or generic account-link label.',
    'identity_connection_origin_changed': 'The original connection origin changed. Stop and retain this connection for review.',
    'identity_current_self_unestablished': 'A unique current-subject and locally related first-party rel=me link have not been established.',
    'identity_self_origin_changed': 'The self link is outside the original connection origin or has an unsupported fragment. Do not follow it.',
    'identity_document_changed': 'The document changed during inspection. Obtain a fresh observation before trying again.',
    'identity_private_affix_unsupported': 'The private field affixes must describe an actual account-ID, username or handle label.',
    'identity_private_field_ambiguous': 'The selected private self field is ambiguous or unsupported. Do not use public authors or editable login values.',
    'identity_private_field_semantics_missing': 'The selected private field lacks an explicit account-ID, username or handle label.',
}


class IdentityFactError(ValueError):
    """Preserve the original ValueError message while exposing only a fixed code."""
    def __init__(self, code):
        if not isinstance(code,str) or code not in IDENTITY_ERROR_HINTS:
            raise ValueError('unrecognized_identity_error_code')
        self.code=code
        super().__init__(code)


def identity_rpc_feedback(operation, status, payload):
    """Never return arbitrary manager detail, response text, selectors or values."""
    if (status!=409 or operation not in ('inspect_self','report_connection')
            or not isinstance(payload,dict)):
        return None
    code=payload.get('code')
    if not isinstance(code,str) or code not in IDENTITY_ERROR_HINTS:
        return None
    return code+': '+IDENTITY_ERROR_HINTS[code]

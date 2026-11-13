"""Explicit member authorization wording; no model assertion can replace rules."""
import pytest
from lead_app.native_method_errors import MethodFactError
from lead_app.native_method_facts import _permission

MEMBER='A regular member may publish community notes and reply to existing topics using their own signed-in account. Both actions are available to ordinary members.'
OBSERVED='Members may use an authorized browser assistant or browser automation to sign in, read pages, publish notes, and reply on their behalf.'


@pytest.mark.parametrize('operation',['publish','reply'])
@pytest.mark.parametrize('grant',[
    OBSERVED,
    'Members may use an authorized browser assistant to publish notes and reply on their behalf.',
    'Members can use an authorized browser automation to publish notes and reply.',
    'Members may use authorized browser automation to publish notes and reply.',
    'Automated browser tools are permitted for member publishing and replies.',
    'Members may use an authorized browser assistant to publish notes and reply; members may also publish notes and reply manually.',
    'Members may use an authorized browser assistant to publish notes and reply, or may perform these actions manually.',
])
def test_explicit_authorized_member_assistance_covers_the_actual_operation(operation,grant):
    context=MEMBER+' '+grant
    _permission(context,grant,operation)


@pytest.mark.parametrize('operation',['publish','reply'])
@pytest.mark.parametrize('grant',[
    'Members may not use an authorized browser assistant or browser automation to publish notes or reply.',
    'Only administrators may use an authorized browser assistant or browser automation to publish and reply.',
    'Members may use an authorized browser assistant or browser automation to publish and reply if administrator approval is given.',
    OBSERVED+' Administrator approval is required before doing so.',
    OBSERVED+' You must obtain owner permission first.',
    OBSERVED+' Browser assistants are not allowed.',
    OBSERVED+' Only administrators may use browser assistants.',
    OBSERVED+' Browser assistants may operate only after administrator approval.',
    'Members may use an authorized browser assistant or browser automation to read pages. Members can publish and reply manually.',
    'Members may use an unauthorized browser assistant to publish notes and reply.',
    'Members may use a browser assistant to publish notes and reply.',
    'Members may use an authorized browser assistant to read pages about how to publish notes and reply.',
    'Members may use an authorized browser assistant only to read pages, publish notes and reply by hand.',
    'Members may use an authorized browser assistant to read pages but publish notes and reply manually.',
])
def test_assistance_wording_keeps_denial_conditions_and_read_only_limits(operation,grant):
    with pytest.raises(MethodFactError):
        _permission(MEMBER+' '+grant,grant,operation)


@pytest.mark.parametrize('operation',['publish','reply'])
def test_assistance_quote_must_occur_in_the_actual_rule_context(operation):
    with pytest.raises(MethodFactError,match='permission_quote_missing'):
        _permission(MEMBER,OBSERVED,operation)

"""Public links use server IDs; native DOM identity remains the Web K mid."""

import pytest
from lead_app.community_runtime import _growth_group, telegram_message_id


@pytest.mark.parametrize('value,expected', [
    ('1', '1'), ('56334', '56334'), ('2147483647', '2147483647'),
    ('4294967297', '1'), ('4295023630', '56334'), ('6442450943', '2147483647'),
])
def test_confirmed_legacy_and_channel_message_id(value, expected):
    assert telegram_message_id(value) == expected


@pytest.mark.parametrize('value', [
    None, 1, True, 4295023630, '', '0', '-1', '+1', '01', ' 1', '1 ',
    '１', '١', '1e3', '10.0', '10.0001', '4295023630.0001',
    '2147483648', '4294967295', '4294967296', '6442450944', '8589934592',
    '8589934593', '12884901889', '9'*100,
])
def test_temporary_ephemeral_and_invalid_message_ids_never_become_links(value):
    with pytest.raises(ValueError, match='^community_source_binding_invalid$'):
        telegram_message_id(value)


def test_inspect_preserves_raw_mid_but_only_exposes_confirmed_message_links():
    observed = {'target_id': '-778899', 'title': 'Fixture', 'kind': 'discussion',
        'description': '', 'joined': True, 'can_reply': True, 'history': [
            {'mid': '4295023630', 'body': 'Original question', 'own': False},
            {'mid': '4295023630.0001', 'body': 'Pending', 'own': False},
            {'mid': '8589934593', 'body': 'Ephemeral', 'own': False},
            {'mid': '0', 'body': 'Invalid', 'own': False},
            {'mid': '4295023633', 'body': 'Own message', 'own': True},
        ]}
    group = _growth_group(observed, {'canonical_url': 'https://t.me/message_fixture'})
    assert group['messages'][0]['parent_url'] == 'https://t.me/message_fixture/56334'
    assert len(group['messages']) == 1
    assert group['messages'][0]['mid'] == '4295023630'
    assert observed['history'][0]['mid'] == '4295023630'

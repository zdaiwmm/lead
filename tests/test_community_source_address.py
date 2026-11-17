import pytest
from lead_app.community_search import public_group_url


@pytest.mark.parametrize('subtitle,expected', [
    ('@observed_public, 3\u00a0039 members\nP', 'https://t.me/observed_public'),
    (' @Mixed_Name\n12 members ', 'https://t.me/Mixed_Name'),
    ('@abcd', 'https://t.me/abcd'),
    (None, None), ('', None), ('12 members', None),
    ('Contact @advertised_name', None),
    ('https://t.me/advertised_name', None),
    ('@first_name, contact @second_name', None),
    ('@public_name/another', None), ('@public_name.evil', None),
    ('@12345', None), ('@_private', None),
    ('@public_name中文', None), ('@' + 'a'*33 + ', 12 members', None),
])
def test_only_an_explicit_unambiguous_public_username_supplies_the_address(subtitle, expected):
    assert public_group_url(subtitle) == expected

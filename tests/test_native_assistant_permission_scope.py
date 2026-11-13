"""An assistant's reading grant never authorizes an explicitly manual write."""
import pytest
from lead_app.native_method_errors import MethodFactError
from lead_app.native_method_facts import _permission

MEMBER = (
    "A regular member may publish community notes and reply to existing topics "
    "using their own signed-in account. Both actions are available to ordinary members."
)


@pytest.mark.parametrize("grant,operation", [
    ("Members may use an authorized browser assistant only to read pages; publish notes and reply manually.", "publish"),
    ("Members may use an authorized browser assistant only to read pages; publish notes and reply manually.", "reply"),
    ("Members may use an authorized browser assistant to publish notes; replies must be written manually.", "reply"),
    ("Members may use an authorized browser assistant to publish notes but only manually.", "publish"),
    ("Members may use an authorized browser assistant to reply, but replies must be written by hand.", "reply"),
])
def test_assistant_permission_does_not_cover_actions_explicitly_reserved_for_manual_work(grant, operation):
    with pytest.raises(MethodFactError):
        _permission(MEMBER + " " + grant, grant, operation)


@pytest.mark.parametrize("operation", ["publish", "reply"])
@pytest.mark.parametrize("grant", [
    "Members may use an authorized browser assistant to publish notes and reply; members may also publish notes and reply manually.",
    "Members may use an authorized browser assistant to publish notes and reply, or may perform these actions manually.",
])
def test_explicit_automated_permission_keeps_optional_manual_alternatives(grant, operation):
    _permission(MEMBER + " " + grant, grant, operation)

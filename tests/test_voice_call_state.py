"""Test VoiceCall state machine without real audio or network."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture()
def mock_bridge():
    b = MagicMock()
    b.send_frame = MagicMock()
    return b


def _make_call(bridge):
    # Patch sounddevice so tests run headlessly
    with patch("voice_call.sd"):
        from voice_call import VoiceCall
        vc = VoiceCall.__new__(VoiceCall)
        VoiceCall.__init__(vc, bridge, "alice")
        return vc


def test_initial_state_is_idle(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    assert vc.state == CallState.IDLE


def test_start_call_transitions_to_calling(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    with patch.object(vc, "_start_audio"), patch.object(vc, "_start_timer"):
        vc.start_call("bob")
    assert vc.state == CallState.CALLING
    assert vc.peer == "bob"
    mock_bridge.send_frame.assert_called_once()
    args = mock_bridge.send_frame.call_args
    from protocol import T
    assert args[0][0] == T.CALL_OFFER


def test_on_call_offer_transitions_to_ringing(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("charlie")
    assert vc.state == CallState.RINGING
    assert vc.peer == "charlie"


def test_busy_rejects_second_call(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("bob")
    assert vc.state == CallState.RINGING
    vc.on_call_offer("carol")   # arrives while ringing
    # bob still ringing, carol was auto-rejected
    assert vc.state == CallState.RINGING
    assert vc.peer == "bob"
    from protocol import T
    reject_calls = [c for c in mock_bridge.send_frame.call_args_list
                    if c[0][0] == T.CALL_REJECT]
    assert len(reject_calls) == 1
    assert reject_calls[0][1]["to"] == "carol"


def test_hangup_from_idle_does_nothing(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.hangup()
    assert vc.state == CallState.IDLE
    mock_bridge.send_frame.assert_not_called()


def test_reject_sends_reject_frame(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("dave")
    vc.reject_call("dave")
    assert vc.state == CallState.IDLE
    from protocol import T
    mock_bridge.send_frame.assert_called_once_with(T.CALL_REJECT, to="dave", reason="rejected")


def test_hangup_active_call_sends_hangup(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("eve")
    # force to CONNECTED state directly
    vc._state = CallState.CONNECTED
    vc._start_ts = 0.0
    vc._stop_event.clear()
    vc.hangup()
    assert vc.state == CallState.IDLE
    from protocol import T
    hangup_calls = [c for c in mock_bridge.send_frame.call_args_list
                    if c[0][0] == T.CALL_HANGUP]
    assert len(hangup_calls) == 1


def test_toggle_mute(mock_bridge):
    vc = _make_call(mock_bridge)
    assert vc.is_muted is False
    result = vc.toggle_mute()
    assert result is True
    assert vc.is_muted is True
    result = vc.toggle_mute()
    assert result is False

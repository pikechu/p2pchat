"""Test VoiceCall state machine without real audio or network."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import types
import pytest
from unittest.mock import MagicMock, patch
import numpy as np

sounddevice_stub = types.ModuleType("sounddevice")
sounddevice_stub.InputStream = object
sounddevice_stub.OutputStream = object
sys.modules["sounddevice"] = sounddevice_stub


@pytest.fixture()
def mock_bridge():
    b = MagicMock()
    b.send_frame = MagicMock()
    return b


def _make_call(bridge, voice_key_provider=None, username="alice"):
    # Patch sounddevice so tests run headlessly
    with patch("voice_call.sd"):
        from voice_call import VoiceCall
        vc = VoiceCall.__new__(VoiceCall)
        VoiceCall.__init__(vc, bridge, username, voice_key_provider=voice_key_provider)
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


def test_relay_voice_chunk_sends_encrypted_payload_and_receiver_decrypts_to_jitter(mock_bridge):
    from protocol import T
    from voice_call import CallState

    key = b"v" * 32
    alice = _make_call(mock_bridge, voice_key_provider=lambda peer, call_id, room_id: key)
    alice._peer = "bob"
    alice._call_id = "call-relay"
    alice._state = CallState.CONNECTED

    class DummyStream:
        def __init__(self, *args, **kwargs):
            self.callback = kwargs["callback"]

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    with patch("voice_call.sd.InputStream", DummyStream), patch("voice_call.sd.OutputStream", DummyStream):
        alice._start_audio()
        pcm_float = np.ones((320, 1), dtype=np.float32) * 0.25
        alice._in_stream.callback(pcm_float, 320, None, None)

    sent = mock_bridge.send_frame.call_args
    assert sent[0][0] == T.VOICE_CHUNK
    payload = sent[1]["voice"]
    legacy_pcm_b64 = "ACAA" * 160
    assert sent[1].get("data") is None
    assert legacy_pcm_b64 not in str(payload)

    bob_bridge = MagicMock()
    bob = _make_call(bob_bridge, voice_key_provider=lambda peer, call_id, room_id: key, username="bob")
    bob._peer = "alice"
    bob._call_id = "call-relay"
    bob._state = CallState.CONNECTED
    bob.on_voice_chunk(payload)

    assert len(bob._jitter) == 1
    assert np.allclose(bob._jitter[0][:4], np.array([0.25] * 4, dtype=np.float32), atol=1 / 32768)


def test_voice_call_without_key_does_not_send_plaintext_audio(mock_bridge):
    from voice_call import CallState

    vc = _make_call(mock_bridge, voice_key_provider=lambda peer, call_id, room_id: None)
    vc._peer = "bob"
    vc._state = CallState.CONNECTED

    class DummyStream:
        def __init__(self, *args, **kwargs):
            self.callback = kwargs["callback"]

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    with patch("voice_call.sd.InputStream", DummyStream), patch("voice_call.sd.OutputStream", DummyStream):
        vc._start_audio()
        pcm_float = np.ones((320, 1), dtype=np.float32) * 0.5
        vc._in_stream.callback(pcm_float, 320, None, None)

    mock_bridge.send_frame.assert_not_called()


def test_room_call_uses_fresh_call_id_and_provider_receives_room_id(mock_bridge):
    from protocol import T

    seen = []
    vc = _make_call(mock_bridge, voice_key_provider=lambda peer, call_id, room_id: seen.append((call_id, room_id)) or b"v" * 32)
    vc.start_call("bob", room_id="ROOM01")
    first_call_id = mock_bridge.send_frame.call_args.kwargs["call_id"]
    assert mock_bridge.send_frame.call_args == ((T.CALL_OFFER,), {"to": "bob", "room_id": "ROOM01", "call_id": first_call_id})

    vc._voice_cipher(transmit=True)
    assert seen == [(first_call_id, "ROOM01")]

    vc._teardown("test")
    vc.start_call("bob", room_id="ROOM01")
    second_call_id = mock_bridge.send_frame.call_args.kwargs["call_id"]
    assert second_call_id != first_call_id

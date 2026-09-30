"""通过真实的本地 DataChannel 验证多分块加密文件传输。"""

import asyncio

import pytest

pytest.importorskip("aiortc")

from file_transfer import CHUNK_SIZE
from protocol import T
from webrtc_transfer import WebRTCTransfer


def test_local_datachannel_transfers_encrypted_file_without_external_ice_servers(tmp_path):
    async def run():
        contents = bytes(range(256)) * (CHUNK_SIZE // 256 * 3 + 1)
        source = tmp_path / "本地直连文件.bin"
        source.write_bytes(contents)
        signals = asyncio.Queue()
        received = asyncio.get_running_loop().create_future()
        opened = []
        progress = []
        peers = {}
        delivered_types = []

        def signal_sender(username):
            def send(msg_type, **payload):
                signals.put_nowait((username, msg_type, payload))
            return send

        def file_received(path, metadata):
            if not received.done():
                received.set_result((path, metadata))

        file_key = bytes(range(32))
        peers["alice"] = WebRTCTransfer(
            signal_sender("alice"), local_user="alice", ice_servers=[],
            downloads_dir=tmp_path / "alice-downloads",
            file_key_provider=lambda _peer, _session_id: file_key,
            on_channel_open=opened.append, on_file_progress=progress.append,
        )
        peers["bob"] = WebRTCTransfer(
            signal_sender("bob"), local_user="bob", ice_servers=[],
            downloads_dir=tmp_path / "bob-downloads",
            file_key_provider=lambda _peer, _session_id: file_key,
            on_file_received=file_received,
        )

        async def relay():
            while True:
                username, msg_type, payload = await signals.get()
                delivered = dict(payload, **{"from": username})
                recipient = peers[payload["to"]]
                delivered_types.append(msg_type)
                if msg_type == T.WEBRTC_OFFER:
                    await recipient.handle_offer(delivered)
                elif msg_type == T.WEBRTC_ANSWER:
                    await recipient.handle_answer(delivered)
                elif msg_type == T.WEBRTC_ICE:
                    await recipient.handle_ice(delivered)
                else:
                    raise AssertionError(f"意外的直连信令：{msg_type}")

        relay_task = asyncio.create_task(relay())
        try:
            session_id = await asyncio.wait_for(
                peers["alice"].start_offer("bob", source, session_id="local-live-transfer"),
                timeout=20,
            )
            done, _ = await asyncio.wait(
                {received, relay_task}, timeout=20, return_when=asyncio.FIRST_COMPLETED,
            )
            if relay_task in done:
                await relay_task
            assert received in done, "本地 DataChannel 未在限定时间内完成文件接收"
            saved, metadata = received.result()
            assert saved != source
            assert saved.read_bytes() == contents
            assert metadata["filename"] == source.name
            assert metadata["size"] == len(contents)
            assert metadata["transfer_id"] == session_id
            assert metadata["from_user"] == "alice"
            assert T.WEBRTC_OFFER in delivered_types
            assert T.WEBRTC_ANSWER in delivered_types
            assert opened[0]["session_id"] == session_id
            assert len([event for event in progress if event["progress"] > 0]) >= 3
            assert progress[-1]["progress"] == 100
        finally:
            await asyncio.gather(*(peer.close_all() for peer in peers.values()))
            relay_task.cancel()
            await asyncio.gather(relay_task, return_exceptions=True)
            if not received.done():
                received.cancel()
        assert all(not peer._sessions and not peer._incoming_receivers for peer in peers.values())

    asyncio.run(run())

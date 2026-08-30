from __future__ import annotations

from collections import deque

import pytest

from murmur_voice import dji_microphone as dji


def _vendor_interface(
    *,
    number=7,
    endpoint=0x87,
    subclass=0xF0,
    protocol=0,
    alternate=0,
):
    return dji._UsbInterface(
        number=number,
        alternate_setting=alternate,
        class_code=0xFF,
        subclass_code=subclass,
        protocol_code=protocol,
        endpoints=(dji._UsbEndpoint(endpoint, 0x02),),
    )


def _v2_frame(online: bool, *, length: int = 54) -> bytes:
    frame = bytearray(length)
    frame[0] = 0x55
    frame[1] = length
    frame[2] = 0x04
    frame[8:11] = b"\x00\x5b\x03"
    frame[11] = 0x03
    frame[12] = {54: 0x26, 86: 0x46, 118: 0x66}[length]
    frame[44] = 0x01 if online else 0x00
    return bytes(frame)


def _v1_frame(first_online: bool, second_online: bool) -> bytes:
    frame = bytearray(64)
    frame[0] = 0x55
    frame[1] = len(frame)
    frame[2] = 0x04
    frame[8:11] = b"\x00\x5b\x03"
    frame[11] = 0x00
    offset = 14
    for online in (first_online, second_online):
        frame[offset + 1] = 0x20 if online else 0x40
        offset += 23 if online else 9
    return bytes(frame)


class FakeLibUsb:
    def __init__(
        self,
        *,
        transfers=(),
        interfaces=None,
        devices=("receiver",),
        ids=None,
        open_result=0,
        claim_result=0,
        alternate_result=0,
    ):
        self.transfers = deque(transfers)
        self._interfaces = (
            (_vendor_interface(),) if interfaces is None else tuple(interfaces)
        )
        self._devices = tuple(devices)
        self._ids = ids or {device: (0x2CA3, 0x4011) for device in devices}
        self.open_result = open_result
        self.claim_result = claim_result
        self.alternate_result = alternate_result
        self.claimed = []
        self.alternates = []
        self.reads = []
        self.released = []
        self.closed_handles = []
        self.closed = False

    def devices(self):
        return self._devices

    def device_ids(self, device):
        return self._ids.get(device)

    def interfaces(self, device):
        del device
        return self._interfaces

    def open(self, device):
        if self.open_result:
            return self.open_result, None
        return 0, f"handle:{device}"

    def claim_interface(self, handle, interface):
        self.claimed.append((handle, interface))
        return self.claim_result

    def set_alternate_setting(self, handle, interface, alternate):
        self.alternates.append((handle, interface, alternate))
        return self.alternate_result

    def bulk_read(self, handle, endpoint, size, timeout_ms):
        self.reads.append((handle, endpoint, size, timeout_ms))
        if self.transfers:
            return self.transfers.popleft()
        return -7, b""

    def release_interface(self, handle, interface):
        self.released.append((handle, interface))

    def close_handle(self, handle):
        self.closed_handles.append(handle)

    def close(self):
        self.closed = True


@pytest.fixture
def install_backend(monkeypatch):
    created = []

    def install(backend):
        created.append(backend)
        monkeypatch.setattr(dji, "_backend_factory", lambda: backend)
        return backend

    yield install
    assert all(backend.closed for backend in created)


@pytest.mark.parametrize(
    "name",
    [
        "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx_X-01.analog-stereo",
        "alsa_input.usb-wireless_microphone_rx-00.mono-fallback",
        "alsa_input.usb-WIRELESS-MICROPHONE-RX-00.analog-stereo",
    ],
)
def test_is_dji_source_accepts_known_pulse_names(name):
    assert dji.is_dji_source(name)


@pytest.mark.parametrize(
    "name",
    [
        "alsa_output.usb-DJI_Wireless_Mic_Rx-01.analog-stereo",
        "alsa_input.pci-0000_00_1f.3.analog-stereo",
        "alsa_output.usb-DJI.monitor",
        "alsa_input.usb-DJI_Wireless_Mic_Rx.monitor",
        "",
        None,
    ],
)
def test_is_dji_source_rejects_outputs_monitors_and_other_sources(name):
    assert not dji.is_dji_source(name)


def test_dynamic_vendor_interface_and_bulk_input_are_used(install_backend):
    backend = install_backend(
        FakeLibUsb(
            transfers=[(0, _v2_frame(True))],
            interfaces=[_vendor_interface(number=9, endpoint=0x89, alternate=2)],
        )
    )

    assert dji.probe_dji_link_state() is True
    assert backend.claimed == [("handle:receiver", 9)]
    assert backend.alternates == [("handle:receiver", 9, 2)]
    assert backend.reads[0][1:3] == (0x89, 512)
    assert backend.released == [("handle:receiver", 9)]
    assert backend.closed_handles == ["handle:receiver"]


def test_known_interface_and_endpoint_are_a_compatibility_fallback(install_backend):
    backend = install_backend(
        FakeLibUsb(transfers=[(0, _v2_frame(False))], interfaces=[])
    )

    assert dji.probe_dji_link_state() is False
    assert backend.claimed == [("handle:receiver", 6)]
    assert backend.reads[0][1] == 0x86


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (_v2_frame(True), True),
        (_v2_frame(False), False),
        (_v2_frame(True, length=86), True),
        (_v1_frame(True, False), True),
        (_v1_frame(False, False), False),
    ],
)
def test_valid_v1_and_v2_status_frames_return_online_or_offline(
    install_backend, frame, expected
):
    install_backend(FakeLibUsb(transfers=[(0, frame)]))
    assert dji.probe_dji_link_state() is expected


def test_fragmented_frame_with_leading_garbage_is_reassembled(install_backend):
    frame = _v2_frame(True)
    install_backend(
        FakeLibUsb(
            transfers=[
                (0, b"garbage\x55"),
                (0, frame[1:13]),
                (0, frame[13:]),
            ]
        )
    )

    assert dji.probe_dji_link_state() is True


def test_interleaved_non_status_telemetry_does_not_hide_later_link_frame(
    install_backend,
):
    telemetry = bytes([0x55, 14, 0x04]) + b"\x00" * 11
    backend = install_backend(
        FakeLibUsb(
            transfers=[(0, telemetry)] * 8 + [(0, _v2_frame(True))],
        )
    )

    assert dji.probe_dji_link_state() is True
    assert len(backend.reads) == 9


def test_stale_timeouts_are_bounded_and_return_unknown(install_backend):
    backend = install_backend(FakeLibUsb(transfers=[(-7, b"")] * 100))

    assert dji.probe_dji_link_state() is None
    assert len(backend.reads) == dji._MAX_TRANSFERS
    assert all(1 <= read[3] <= dji._TRANSFER_TIMEOUT_MS for read in backend.reads)


def test_busy_interface_returns_unknown_without_reading_or_releasing(
    install_backend,
):
    backend = install_backend(FakeLibUsb(claim_result=-6))

    assert dji.probe_dji_link_state() is None
    assert backend.reads == []
    assert backend.released == []
    assert backend.closed_handles == ["handle:receiver"]


def test_no_receiver_returns_unknown_without_opening(install_backend):
    backend = install_backend(FakeLibUsb(devices=()))

    assert dji.probe_dji_link_state() is None
    assert backend.claimed == []
    assert backend.reads == []


@pytest.mark.parametrize(
    "malformed",
    [
        b"not a framed response",
        bytes([0x55, 3, 0x04]),
        _v2_frame(True)[:20],
        _v2_frame(True)[:12] + b"\x00" + _v2_frame(True)[13:],
        bytes(bytearray(_v1_frame(False, False))[:15] + b"\x00" + b"\x00" * 48),
    ],
)
def test_malformed_or_incomplete_frames_return_unknown(install_backend, malformed):
    install_backend(FakeLibUsb(transfers=[(0, malformed)]))
    assert dji.probe_dji_link_state() is None


def test_non_dji_usb_devices_are_ignored(install_backend):
    backend = install_backend(
        FakeLibUsb(
            devices=("other",),
            ids={"other": (0x1234, 0x5678)},
            transfers=[(0, _v2_frame(True))],
        )
    )

    assert dji.probe_dji_link_state() is None
    assert backend.claimed == []


def test_definitive_online_wins_over_an_offline_receiver(install_backend):
    backend = FakeLibUsb(
        devices=("offline", "online"),
        transfers=[(0, _v2_frame(False)), (0, _v2_frame(True))],
    )
    install_backend(backend)

    assert dji.probe_dji_link_state() is True
    assert backend.closed_handles == ["handle:offline", "handle:online"]

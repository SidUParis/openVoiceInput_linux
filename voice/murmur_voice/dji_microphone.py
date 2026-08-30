# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, side-effect-free DJI wireless microphone link probing.

The DJI receiver keeps its USB audio source registered while its transmitters
are powered off.  Source enumeration alone therefore cannot distinguish a
usable wireless microphone from a silent receiver.  This module reads only the
receiver's vendor status endpoint when :func:`probe_dji_link_state` is called.
It never opens an audio stream, invokes ``pactl``, or changes an audio route.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

DJI_VENDOR_ID = 0x2CA3
DJI_PRODUCT_ID = 0x4011

_LIBUSB_PATH = "libusb-1.0.so.0"
_LIBUSB_SUCCESS = 0
_LIBUSB_ERROR_TIMEOUT = -7

_USB_CLASS_VENDOR_SPECIFIC = 0xFF
_USB_TRANSFER_TYPE_MASK = 0x03
_USB_TRANSFER_TYPE_BULK = 0x02
_USB_ENDPOINT_IN = 0x80

_KNOWN_INTERFACE = 6
_KNOWN_ENDPOINT = 0x86
_KNOWN_ROUTE: _UsbRoute

_TRANSFER_BYTES = 512
_TRANSFER_TIMEOUT_MS = 250
_PROBE_TIMEOUT_SECONDS = 1.5
# The receiver interleaves several telemetry frame types.  A live Mic Mini 2
# can emit eight non-status frames before its next link-status frame, so the
# monotonic deadline is the primary bound and this count only prevents a
# pathological zero-latency backend from spinning forever.
_MAX_TRANSFERS = 64
_MAX_ENUMERATED_DEVICES = 256
_MAX_MATCHING_DEVICES = 4
_MAX_FRAME_BYTES = 255
_MAX_BUFFER_BYTES = 2048


@dataclass(frozen=True, slots=True)
class _UsbEndpoint:
    address: int
    attributes: int


@dataclass(frozen=True, slots=True)
class _UsbInterface:
    number: int
    alternate_setting: int
    class_code: int
    subclass_code: int
    protocol_code: int
    endpoints: tuple[_UsbEndpoint, ...]


@dataclass(frozen=True, slots=True)
class _UsbRoute:
    interface: int
    endpoint: int
    alternate_setting: int = 0


_KNOWN_ROUTE = _UsbRoute(_KNOWN_INTERFACE, _KNOWN_ENDPOINT)


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


class _EndpointDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bEndpointAddress", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8),
        ("wMaxPacketSize", ctypes.c_uint16),
        ("bInterval", ctypes.c_uint8),
        ("bRefresh", ctypes.c_uint8),
        ("bSynchAddress", ctypes.c_uint8),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class _InterfaceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bInterfaceNumber", ctypes.c_uint8),
        ("bAlternateSetting", ctypes.c_uint8),
        ("bNumEndpoints", ctypes.c_uint8),
        ("bInterfaceClass", ctypes.c_uint8),
        ("bInterfaceSubClass", ctypes.c_uint8),
        ("bInterfaceProtocol", ctypes.c_uint8),
        ("iInterface", ctypes.c_uint8),
        ("endpoint", ctypes.POINTER(_EndpointDescriptor)),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class _Interface(ctypes.Structure):
    _fields_ = [
        ("altsetting", ctypes.POINTER(_InterfaceDescriptor)),
        ("num_altsetting", ctypes.c_int),
    ]


class _ConfigDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("wTotalLength", ctypes.c_uint16),
        ("bNumInterfaces", ctypes.c_uint8),
        ("bConfigurationValue", ctypes.c_uint8),
        ("iConfiguration", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8),
        ("MaxPower", ctypes.c_uint8),
        ("interface", ctypes.POINTER(_Interface)),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class _CtypesLibUsb:
    """Minimal libusb wrapper whose public surface is easy to fake in tests."""

    def __init__(self) -> None:
        self._library = ctypes.CDLL(_LIBUSB_PATH)
        self._declare_functions()
        self._context = ctypes.c_void_p()
        self._device_list = ctypes.POINTER(ctypes.c_void_p)()
        self._devices: tuple[ctypes.c_void_p, ...] | None = None
        self._device_list_acquired = False
        self._closed = False
        result = self._library.libusb_init(ctypes.byref(self._context))
        if result != _LIBUSB_SUCCESS:
            raise OSError("libusb initialization failed")

    def _declare_functions(self) -> None:
        library = self._library
        library.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        library.libusb_init.restype = ctypes.c_int
        library.libusb_exit.argtypes = [ctypes.c_void_p]
        library.libusb_exit.restype = None

        library.libusb_get_device_list.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ]
        library.libusb_get_device_list.restype = ctypes.c_ssize_t
        library.libusb_free_device_list.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int,
        ]
        library.libusb_free_device_list.restype = None
        library.libusb_get_device_descriptor.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_DeviceDescriptor),
        ]
        library.libusb_get_device_descriptor.restype = ctypes.c_int

        library.libusb_get_active_config_descriptor.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(_ConfigDescriptor)),
        ]
        library.libusb_get_active_config_descriptor.restype = ctypes.c_int
        library.libusb_free_config_descriptor.argtypes = [
            ctypes.POINTER(_ConfigDescriptor)
        ]
        library.libusb_free_config_descriptor.restype = None

        library.libusb_open.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.libusb_open.restype = ctypes.c_int
        library.libusb_close.argtypes = [ctypes.c_void_p]
        library.libusb_close.restype = None
        library.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.libusb_claim_interface.restype = ctypes.c_int
        library.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.libusb_release_interface.restype = ctypes.c_int
        library.libusb_set_interface_alt_setting.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        library.libusb_set_interface_alt_setting.restype = ctypes.c_int
        library.libusb_bulk_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
        ]
        library.libusb_bulk_transfer.restype = ctypes.c_int

    def devices(self) -> tuple[ctypes.c_void_p, ...]:
        if self._devices is not None:
            return self._devices
        count = self._library.libusb_get_device_list(
            self._context, ctypes.byref(self._device_list)
        )
        if count < 0:
            self._devices = ()
            return self._devices
        self._device_list_acquired = True
        self._devices = tuple(
            ctypes.c_void_p(self._device_list[index]) for index in range(count)
        )
        return self._devices

    def device_ids(self, device: ctypes.c_void_p) -> tuple[int, int] | None:
        descriptor = _DeviceDescriptor()
        result = self._library.libusb_get_device_descriptor(
            device, ctypes.byref(descriptor)
        )
        if result != _LIBUSB_SUCCESS:
            return None
        return int(descriptor.idVendor), int(descriptor.idProduct)

    def interfaces(self, device: ctypes.c_void_p) -> tuple[_UsbInterface, ...]:
        pointer = ctypes.POINTER(_ConfigDescriptor)()
        result = self._library.libusb_get_active_config_descriptor(
            device, ctypes.byref(pointer)
        )
        if result != _LIBUSB_SUCCESS or not pointer:
            return ()
        try:
            config = pointer.contents
            discovered: list[_UsbInterface] = []
            for interface_index in range(int(config.bNumInterfaces)):
                interface = config.interface[interface_index]
                if interface.num_altsetting < 1 or not interface.altsetting:
                    continue
                for alternate_index in range(interface.num_altsetting):
                    descriptor = interface.altsetting[alternate_index]
                    endpoints = tuple(
                        _UsbEndpoint(
                            int(descriptor.endpoint[endpoint_index].bEndpointAddress),
                            int(descriptor.endpoint[endpoint_index].bmAttributes),
                        )
                        for endpoint_index in range(int(descriptor.bNumEndpoints))
                    )
                    discovered.append(
                        _UsbInterface(
                            number=int(descriptor.bInterfaceNumber),
                            alternate_setting=int(descriptor.bAlternateSetting),
                            class_code=int(descriptor.bInterfaceClass),
                            subclass_code=int(descriptor.bInterfaceSubClass),
                            protocol_code=int(descriptor.bInterfaceProtocol),
                            endpoints=endpoints,
                        )
                    )
            return tuple(discovered)
        except (IndexError, TypeError, ValueError):
            return ()
        finally:
            self._library.libusb_free_config_descriptor(pointer)

    def open(self, device: ctypes.c_void_p) -> tuple[int, ctypes.c_void_p | None]:
        handle = ctypes.c_void_p()
        result = self._library.libusb_open(device, ctypes.byref(handle))
        return int(result), handle if handle.value is not None else None

    def claim_interface(self, handle: ctypes.c_void_p, interface: int) -> int:
        return int(self._library.libusb_claim_interface(handle, interface))

    def set_alternate_setting(
        self, handle: ctypes.c_void_p, interface: int, alternate_setting: int
    ) -> int:
        return int(
            self._library.libusb_set_interface_alt_setting(
                handle, interface, alternate_setting
            )
        )

    def bulk_read(
        self,
        handle: ctypes.c_void_p,
        endpoint: int,
        size: int,
        timeout_ms: int,
    ) -> tuple[int, bytes]:
        buffer = (ctypes.c_ubyte * size)()
        transferred = ctypes.c_int()
        result = self._library.libusb_bulk_transfer(
            handle,
            endpoint,
            buffer,
            size,
            ctypes.byref(transferred),
            timeout_ms,
        )
        count = max(0, min(size, int(transferred.value)))
        return int(result), bytes(buffer[:count])

    def release_interface(self, handle: ctypes.c_void_p, interface: int) -> None:
        self._library.libusb_release_interface(handle, interface)

    def close_handle(self, handle: ctypes.c_void_p) -> None:
        self._library.libusb_close(handle)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._device_list_acquired:
            self._library.libusb_free_device_list(self._device_list, 1)
        self._library.libusb_exit(self._context)


_backend_factory: Callable[[], Any] = _CtypesLibUsb


class _FrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        self._buffer.extend(chunk[:_MAX_BUFFER_BYTES])
        if len(self._buffer) > _MAX_BUFFER_BYTES:
            del self._buffer[: len(self._buffer) - _MAX_BUFFER_BYTES]

        frames: list[bytes] = []
        while True:
            start = self._find_start()
            if start is None:
                if len(self._buffer) > 2:
                    del self._buffer[:-2]
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 2:
                break
            length = int(self._buffer[1])
            if length < 4 or length > _MAX_FRAME_BYTES:
                del self._buffer[0]
                continue
            if len(self._buffer) < length:
                break
            frames.append(bytes(self._buffer[:length]))
            del self._buffer[:length]
        return tuple(frames)

    def _find_start(self) -> int | None:
        for index in range(max(0, len(self._buffer) - 2)):
            if self._buffer[index] == 0x55 and self._buffer[index + 2] == 0x04:
                return index
        return None


def is_dji_source(name: str) -> bool:
    """Return whether a Pulse source name identifies a DJI wireless receiver."""

    if not isinstance(name, str):
        return False
    folded = name.casefold()
    return (
        not folded.endswith(".monitor")
        and folded.startswith("alsa_input.usb-")
        and (
            "dji" in folded
            or "wireless_mic_rx" in folded
            or "wireless_microphone_rx" in folded
            or "wireless-microphone-rx" in folded
        )
    )


def probe_dji_link_state() -> bool | None:
    """Return transmitter link state, or ``None`` when it cannot be proven.

    The call is bounded by native libusb transfer timeouts plus a hard transfer
    count.  A receiver that is absent, busy, inaccessible, stale, or produces
    only malformed/non-status frames returns ``None``.  No device identifier or
    frame content is logged or retained.
    """

    backend: Any | None = None
    try:
        backend = _backend_factory()
        return _probe_backend(backend)
    except Exception:
        return None
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass


def _probe_backend(
    backend: Any,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool | None:
    saw_offline = False
    matching_devices = 0
    deadline = monotonic() + _PROBE_TIMEOUT_SECONDS
    for device in backend.devices()[:_MAX_ENUMERATED_DEVICES]:
        if backend.device_ids(device) != (DJI_VENDOR_ID, DJI_PRODUCT_ID):
            continue
        matching_devices += 1
        if matching_devices > _MAX_MATCHING_DEVICES or monotonic() >= deadline:
            break
        route = _select_route(backend.interfaces(device)) or _KNOWN_ROUTE
        result, handle = backend.open(device)
        if result != _LIBUSB_SUCCESS or handle is None:
            continue
        claimed = False
        try:
            if backend.claim_interface(handle, route.interface) != _LIBUSB_SUCCESS:
                continue
            claimed = True
            if route.alternate_setting and (
                backend.set_alternate_setting(
                    handle, route.interface, route.alternate_setting
                )
                != _LIBUSB_SUCCESS
            ):
                continue
            state = _read_link_state(
                backend,
                handle,
                route,
                deadline=deadline,
                monotonic=monotonic,
            )
            if state is True:
                return True
            if state is False:
                saw_offline = True
        finally:
            if claimed:
                try:
                    backend.release_interface(handle, route.interface)
                except Exception:
                    pass
            try:
                backend.close_handle(handle)
            except Exception:
                pass
    return False if saw_offline else None


def _select_route(interfaces: Sequence[_UsbInterface]) -> _UsbRoute | None:
    routes: list[tuple[_UsbRoute, _UsbInterface]] = []
    for interface in interfaces:
        if interface.class_code != _USB_CLASS_VENDOR_SPECIFIC:
            continue
        for endpoint in interface.endpoints:
            if (
                endpoint.address & _USB_ENDPOINT_IN
                and endpoint.attributes & _USB_TRANSFER_TYPE_MASK
                == _USB_TRANSFER_TYPE_BULK
            ):
                routes.append(
                    (
                        _UsbRoute(
                            interface.number,
                            endpoint.address,
                            interface.alternate_setting,
                        ),
                        interface,
                    )
                )

    dji_vendor_routes = [
        route
        for route, interface in routes
        if interface.subclass_code == 0xF0 and interface.protocol_code == 0
    ]
    if len(dji_vendor_routes) == 1:
        return dji_vendor_routes[0]
    if len(routes) == 1:
        return routes[0][0]
    known = [route for route, _interface in routes if route == _KNOWN_ROUTE]
    if len(known) == 1:
        return known[0]
    return None


def _read_link_state(
    backend: Any,
    handle: Any,
    route: _UsbRoute,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> bool | None:
    decoder = _FrameDecoder()
    for _attempt in range(_MAX_TRANSFERS):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        timeout_ms = max(
            1,
            min(_TRANSFER_TIMEOUT_MS, int(remaining * 1000)),
        )
        result, chunk = backend.bulk_read(
            handle,
            route.endpoint,
            _TRANSFER_BYTES,
            timeout_ms,
        )
        if result == _LIBUSB_ERROR_TIMEOUT:
            continue
        if result != _LIBUSB_SUCCESS:
            return None
        for frame in decoder.feed(chunk):
            state = _transmitter_state(frame)
            if state is not None:
                return state
    return None


def _transmitter_state(frame: bytes) -> bool | None:
    if len(frame) < 14 or frame[8:11] != b"\x00\x5b\x03":
        return None

    dialect = frame[11]
    if dialect == 0x03:
        expected_capability = {54: 0x26, 86: 0x46, 118: 0x66}
        if len(frame) < 45 or expected_capability.get(len(frame)) != frame[12]:
            return None
        return bool(frame[44] & 0x03)

    if dialect == 0x00:
        offset = 14
        any_present = False
        for _slot in range(2):
            if offset + 1 >= len(frame):
                return None
            flags = frame[offset + 1]
            present = bool(flags & 0x20)
            absent = bool(flags & 0x40)
            if present == absent:
                return None
            any_present = any_present or present
            offset += 23 if present else 9
        return any_present

    return None

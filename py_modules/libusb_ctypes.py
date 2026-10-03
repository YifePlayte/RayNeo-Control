"""Minimal libusb-1.0 binding via ctypes.

The Decky plugin backend ships without third-party wheels, so we talk to the
system ``libusb-1.0.so`` directly. Only the handful of functions we need are
declared; everything else is accessed through the opaque ``void*`` handles.

The RayNeo glasses expose a single HID interface with two *interrupt* endpoints
(see PROTOCOL.md), which is exactly what libusb gives us without any kernel
driver.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
from typing import Optional

#: ``bmAttributes & 0x03`` transfer types.
TRANSFER_CONTROL = 0
TRANSFER_ISOCHRONOUS = 1
TRANSFER_BULK = 2
TRANSFER_INTERRUPT = 3

#: Transfer types we are willing to use for the control channel.
TRANSFER_TYPES_OK = (TRANSFER_BULK, TRANSFER_INTERRUPT)


def transfer_type_name(tt: int) -> str:
    return {
        TRANSFER_CONTROL: "CONTROL",
        TRANSFER_ISOCHRONOUS: "ISOCHRONOUS",
        TRANSFER_BULK: "BULK",
        TRANSFER_INTERRUPT: "INTERRUPT",
    }.get(tt, f"TYPE{tt}")


# --- libusb constants -------------------------------------------------------

LIBUSB_SUCCESS = 0
#: A transfer that timed out leaves its endpoint halted, exactly like PIPE.
#: Both need libusb_clear_halt() or every later transfer fails instantly.
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_NOT_FOUND = -5
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_NO_MEM = -11

LIBUSB_ENDPOINT_OUT = 0x00
LIBUSB_ENDPOINT_IN = 0x80

LIBUSB_REQUEST_TYPE_VENDOR = 0x40
LIBUSB_RECIPIENT_DEVICE = 0x00

LIBUSB_LOG_LEVEL_NONE = 0

# --- library loading --------------------------------------------------------

_LIBUSB_NAMES = ("libusb-1.0.so.0", "libusb-1.0.so", "libusb.so.0", "libusb.so")


def _load_libusb() -> ctypes.CDLL:
    last: Optional[Exception] = None
    candidates = list(_LIBUSB_NAMES)
    found = ctypes.util.find_library("usb-1.0")
    if found:
        candidates.insert(0, found)
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError as exc:  # pragma: no cover - depends on host
            last = exc
    raise RuntimeError(
        "libusb-1.0 shared library not found. Install it (e.g. `sudo pacman -S "
        "libusb` on SteamOS / Arch)."
    ) from last


class _LibUsb:
    """Thin, thread-safe wrapper around the libusb entry points we use."""

    def __init__(self) -> None:
        self.lib = _load_libusb()
        self._lock = threading.Lock()
        self.ctx: ctypes.c_void_p = ctypes.c_void_p()
        self.has_set_option = False
        self._declare()

    def _declare(self) -> None:
        lib = self.lib
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_init.restype = ctypes.c_int
        lib.libusb_exit.argtypes = [ctypes.c_void_p]
        lib.libusb_exit.restype = None

        lib.libusb_get_device_list.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ]
        lib.libusb_get_device_list.restype = ctypes.c_int
        lib.libusb_free_device_list.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int,
        ]
        lib.libusb_free_device_list.restype = None

        lib.libusb_get_device_descriptor.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.libusb_get_device_descriptor.restype = ctypes.c_int

        lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_open.restype = ctypes.c_int
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_close.restype = None
        lib.libusb_ref_device.argtypes = [ctypes.c_void_p]
        lib.libusb_ref_device.restype = ctypes.c_void_p
        lib.libusb_unref_device.argtypes = [ctypes.c_void_p]
        lib.libusb_unref_device.restype = None


        lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_claim_interface.restype = ctypes.c_int
        lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_release_interface.restype = ctypes.c_int
        lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int

        lib.libusb_bulk_transfer.argtypes = [
            ctypes.c_void_p,       # dev_handle
            ctypes.c_ubyte,        # endpoint
            ctypes.c_void_p,       # data
            ctypes.c_int,          # length
            ctypes.POINTER(ctypes.c_int),  # transferred
            ctypes.c_uint,         # timeout (ms)
        ]
        lib.libusb_bulk_transfer.restype = ctypes.c_int
        # The glasses expose INTERRUPT endpoints (bmAttributes == 3), not bulk.
        # libusb_hardcodes USB_ENDPOINT_XFER_BULK inside libusb_bulk_transfer(),
        # so submitting it to an interrupt endpoint fails with EINVAL. The
        # synchronous transfer helpers are otherwise identical.
        lib.libusb_interrupt_transfer.argtypes = list(lib.libusb_bulk_transfer.argtypes)
        lib.libusb_interrupt_transfer.restype = ctypes.c_int
        lib.libusb_clear_halt.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]
        lib.libusb_clear_halt.restype = ctypes.c_int

        # libusb_set_option only exists in newer libusb (>= 1.0.26). Resolve it
        # lazily and degrade gracefully, because SteamOS and various distros
        # ship a range of versions.
        self.has_set_option = hasattr(lib, "libusb_set_option")
        if self.has_set_option:
            lib.libusb_set_option.argtypes = [
                ctypes.c_void_p,   # libusb_context *
                ctypes.c_int,      # option
                ctypes.c_void_p,   # value
            ]
            lib.libusb_set_option.restype = ctypes.c_int

    # -- lifecycle ---------------------------------------------------------

    def init(self) -> None:
        with self._lock:
            if self.ctx:
                return
            ctx = ctypes.c_void_p()
            if self.lib.libusb_init(ctypes.byref(ctx)) != LIBUSB_SUCCESS:
                raise RuntimeError("libusb_init failed")
            self.ctx = ctx
            # Keep libusb quiet unless the plugin log level is raised.
            if self.has_set_option:
                level = ctypes.c_int(LIBUSB_LOG_LEVEL_NONE)
                self.lib.libusb_set_option(ctx, 0, ctypes.byref(level))

    def exit(self) -> None:
        with self._lock:
            if self.ctx:
                self.lib.libusb_exit(self.ctx)
                self.ctx = ctypes.c_void_p()

    def ensure(self) -> ctypes.c_void_p:
        if not self.ctx:
            self.init()
        return self.ctx


_libusb: Optional[_LibUsb] = None
_libusb_init_lock = threading.Lock()


def libusb() -> _LibUsb:
    """Process-wide singleton for the libusb wrapper."""
    global _libusb
    if _libusb is None:
        with _libusb_init_lock:
            if _libusb is None:
                _libusb = _LibUsb()
    return _libusb
# comtypes builds COM interfaces dynamically: the base class and _iid_ are untyped by nature.
# pyright: reportUntypedBaseClass=false, reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportAssignmentType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false
"""System volume through Windows Core Audio (IAudioEndpointVolume).

Ported from V1's `tools/play_music.py`. Why a tool at all: without one the model improvises
PowerShell, and on 2026-09-05 it burned six confirmations looping on a broken `Add-Type`
p/invoke before the supervisor stopped it. The media keys can only *step* the volume, so
"set it to 33%" was a guess; this sets an absolute level and reads it back, so the answer is
what the volume actually IS, not what was asked for.

COM interface pointers belong to the apartment of the thread that created them, so the
endpoint is built per call and every call runs on the host's single COM thread.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)


class AudioError(RuntimeError):
    pass


def _endpoint() -> Any:
    if sys.platform != "win32":
        raise AudioError("volume control is Windows-only on this host")
    from ctypes import HRESULT, POINTER, c_float, c_uint32, c_void_p
    from ctypes.wintypes import BOOL, DWORD, LPCWSTR

    from comtypes import CLSCTX_ALL, COMMETHOD, GUID, CoCreateInstance, IUnknown

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = (
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint32), "pnChannelCount")),
            COMMETHOD(
                [],
                HRESULT,
                "SetMasterVolumeLevel",
                (["in"], c_float, "fLevelDB"),
                (["in"], POINTER(GUID), "pguidEventContext"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "SetMasterVolumeLevelScalar",
                (["in"], c_float, "fLevel"),
                (["in"], POINTER(GUID), "pguidEventContext"),
            ),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD(
                [],
                HRESULT,
                "SetChannelVolumeLevel",
                (["in"], c_uint32, "nChannel"),
                (["in"], c_float, "fLevelDB"),
                (["in"], POINTER(GUID), "pguidEventContext"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "SetChannelVolumeLevelScalar",
                (["in"], c_uint32, "nChannel"),
                (["in"], c_float, "fLevel"),
                (["in"], POINTER(GUID), "pguidEventContext"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetChannelVolumeLevel",
                (["in"], c_uint32, "nChannel"),
                (["out"], POINTER(c_float), "pfLevelDB"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetChannelVolumeLevelScalar",
                (["in"], c_uint32, "nChannel"),
                (["out"], POINTER(c_float), "pfLevel"),
            ),
            COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL, "bMute"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "pbMute")),
        )

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = (
            COMMETHOD(
                [],
                HRESULT,
                "Activate",
                (["in"], POINTER(GUID), "iid"),
                (["in"], DWORD, "dwClsCtx"),
                (["in"], c_void_p, "pActivationParams"),
                (["out"], POINTER(POINTER(IAudioEndpointVolume)), "ppInterface"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "OpenPropertyStore",
                (["in"], DWORD, "stgmAccess"),
                (["out"], POINTER(c_void_p), "ppProperties"),
            ),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(LPCWSTR), "ppstrId")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "pdwState")),
        )

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = (
            COMMETHOD(
                [],
                HRESULT,
                "EnumAudioEndpoints",
                (["in"], DWORD, "dataFlow"),
                (["in"], DWORD, "dwStateMask"),
                (["out"], POINTER(c_void_p), "ppDevices"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetDefaultAudioEndpoint",
                (["in"], DWORD, "dataFlow"),
                (["in"], DWORD, "role"),
                (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
            ),
        )

    try:
        enumerator = CoCreateInstance(
            GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),  # MMDeviceEnumerator
            IMMDeviceEnumerator,
            CLSCTX_ALL,
        )
        device = enumerator.GetDefaultAudioEndpoint(0, 1)  # eRender, eMultimedia
        return device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    except Exception as exc:
        raise AudioError(f"Core Audio endpoint unavailable: {exc}") from exc


class Audio:
    """Volume read/write. Every method must run on the COM worker thread."""

    def get(self) -> dict[str, Any]:
        endpoint = _endpoint()
        return {
            "volume": round(endpoint.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(endpoint.GetMute()),
        }

    def set(self, volume: int | None = None, muted: bool | None = None) -> dict[str, Any]:
        endpoint = _endpoint()
        if volume is not None:
            level = max(0, min(100, volume))
            endpoint.SetMasterVolumeLevelScalar(level / 100.0, None)
            if level > 0 and muted is None:
                # A level above zero is meaningless while muted; they asked to hear something.
                endpoint.SetMute(False, None)
        if muted is not None:
            endpoint.SetMute(muted, None)
        # Read back: report what the volume IS, never what was requested.
        return {
            "volume": round(endpoint.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(endpoint.GetMute()),
        }

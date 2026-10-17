"""List camera indexes with device names without opening any camera.

OpenCV opens cameras by index but cannot name them. The index order is the backend's own device
order, so each backend is asked separately: DirectShow and Media Foundation can list different
devices (virtual cameras are often DirectShow-only)."""
from __future__ import annotations
import ctypes
from pathlib import Path
import re
import sys
from .imaging import resolve_backend


def cameras(backend="auto"):
    """[(index, name)] in the backend's index order; [] when names cannot be listed here."""
    backend = resolve_backend(backend)
    try:
        if sys.platform == "win32" and backend == "dshow": return _directshow()
        if sys.platform == "win32" and backend == "msmf": return _media_foundation()
        if sys.platform.startswith("linux") and backend in ("auto", "v4l2"): return _video4linux()
    except OSError:
        pass
    return []


def _video4linux():
    found = []
    for folder in Path("/sys/class/video4linux").glob("video*"):
        match = re.fullmatch(r"video(\d+)", folder.name)
        name = (folder/"name").read_text(errors="replace").strip() if (folder/"name").exists() else ""
        if match: found.append((int(match.group(1)), name or folder.name))
    return sorted(found)


class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_ulong), ("data2", ctypes.c_ushort), ("data3", ctypes.c_ushort), ("data4", ctypes.c_ubyte*8)]


def _guid(text):
    value = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value))
    return value


def _call(pointer, index, *args):
    """Call COM vtable slot `index` of `pointer`; failing HRESULTs raise OSError."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *[type(arg) for arg in args])(table[index])(pointer, *args)


def _release(pointer):
    if pointer: _call(pointer, 2)


class _COM:
    def __enter__(self):
        self.started = ctypes.windll.ole32.CoInitializeEx(None, 2) >= 0
    def __exit__(self, *_):
        if self.started: ctypes.windll.ole32.CoUninitialize()


def _directshow():
    class VARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort*3), ("value", ctypes.c_void_p), ("extra", ctypes.c_void_p)]
    found = []
    with _COM():
        devices, listing = ctypes.c_void_p(), ctypes.c_void_p()
        ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(_guid("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")), None, 1,
                                             ctypes.byref(_guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")), ctypes.byref(devices))
        try:
            # ICreateDevEnum::CreateClassEnumerator(video input category); S_FALSE and no enumerator = no cameras.
            _call(devices, 3, ctypes.pointer(_guid("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")), ctypes.pointer(listing), ctypes.c_ulong(0))
            while listing:
                moniker, fetched = ctypes.c_void_p(), ctypes.c_ulong()
                if _call(listing, 3, ctypes.c_ulong(1), ctypes.pointer(moniker), ctypes.pointer(fetched)) != 0 or not fetched.value: break
                bag, name = ctypes.c_void_p(), VARIANT()
                try:
                    # IMoniker::BindToStorage -> IPropertyBag::Read("FriendlyName")
                    _call(moniker, 9, ctypes.c_void_p(), ctypes.c_void_p(),
                          ctypes.pointer(_guid("{55272A00-42CB-11CE-8135-00AA004BB851}")), ctypes.pointer(bag))
                    _call(bag, 3, ctypes.c_wchar_p("FriendlyName"), ctypes.pointer(name), ctypes.c_void_p())
                    found.append((len(found), ctypes.wstring_at(name.value) if name.vt == 8 and name.value else f"Camera {len(found)}"))
                except OSError:
                    found.append((len(found), f"Camera {len(found)}"))
                finally:
                    ctypes.windll.oleaut32.VariantClear(ctypes.pointer(name))
                    _release(bag)
                    _release(moniker)
        finally:
            _release(listing)
            _release(devices)
    return found


def _media_foundation():
    found = []
    mfplat, mf = ctypes.oledll.mfplat, ctypes.oledll.mf
    with _COM():
        mfplat.MFStartup(ctypes.c_ulong(0x20070), ctypes.c_ulong(0))
        attributes, devices, count = ctypes.c_void_p(), ctypes.POINTER(ctypes.c_void_p)(), ctypes.c_uint32()
        try:
            mfplat.MFCreateAttributes(ctypes.byref(attributes), ctypes.c_uint32(1))
            # IMFAttributes::SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, ..._VIDCAP_GUID)
            _call(attributes, 24, ctypes.pointer(_guid("{C60AC5FE-252A-478F-A0EF-BC8FA5F7CAD3}")),
                  ctypes.pointer(_guid("{8AC3587A-4AE7-42D8-99E0-0A6013EEF90F}")))
            mf.MFEnumDeviceSources(attributes, ctypes.byref(devices), ctypes.byref(count))
            for index in range(count.value):
                text, length = ctypes.c_wchar_p(), ctypes.c_uint32()
                try:
                    # IMFAttributes::GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME)
                    _call(devices[index], 13, ctypes.pointer(_guid("{60D0E559-52F8-4FA2-BBCE-ACDB34A8EC01}")),
                          ctypes.pointer(text), ctypes.pointer(length))
                    found.append((index, text.value or f"Camera {index}"))
                except OSError:
                    found.append((index, f"Camera {index}"))
                finally:
                    if text: ctypes.windll.ole32.CoTaskMemFree(text)
                    _release(devices[index])
        finally:
            if devices: ctypes.windll.ole32.CoTaskMemFree(devices)
            _release(attributes)
            mfplat.MFShutdown()
    return found

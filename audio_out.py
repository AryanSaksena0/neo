"""
audio_out.py — send Neo's voice wherever the user is actually listening.

THE BUG THIS EXISTS FOR
Neo's speech always came out of the MacBook speakers, even with AirPods on.
Nothing in the code ever chose that: every playback path just uses PortAudio's
default output device. The problem is WHEN that default is decided.

PortAudio enumerates the audio devices once, inside Pa_Initialize, which
sounddevice runs at import. Neo starts at login from a LaunchAgent — long
before any Bluetooth headset has connected — so its device list, and the
default-output index resolved from it, are frozen as the built-in speakers for
the entire life of the process. Connecting AirPods an hour later changes
nothing PortAudio can see.

live.py already had the cure (reset_portaudio re-runs the enumeration) but only
ever called it when opening a stream FAILED. Opening the built-in speakers
never fails. So the recovery could not fire on the one case that mattered.

WHAT THIS ADDS
A second opinion that does not go through PortAudio at all: ask CoreAudio
directly which device is the system default output, right now. If that name and
PortAudio's cached name disagree, the list is stale — re-enumerate before
opening the stream.

The query is a couple of ctypes calls against the framework already loaded in
every macOS process. No new dependency, no subprocess, roughly free.

INPUT IS DELIBERATELY NOT TOUCHED
their rule, and it is the right one: the microphone stays on the Mac.
pick_input_device already avoids Bluetooth headsets on purpose — routing the
mic through a headset flips it into hands-free mode, which wrecks the audio
quality on both ends. This module only ever concerns itself with OUTPUT.
"""

import ctypes
import ctypes.util
import threading

_LOCK = threading.Lock()
_libs = {"loaded": False, "ca": None, "cf": None}

# CoreAudio property selectors are four-character codes packed into a uint32.
_kAudioObjectSystemObject = 1
_kCFStringEncodingUTF8 = 0x08000100


def _fourcc(code):
    return int.from_bytes(code.encode("ascii"), "big")


class _Address(ctypes.Structure):
    """AudioObjectPropertyAddress — selector, scope, element."""
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


def _frameworks():
    """CoreAudio + CoreFoundation, loaded once. (None, None) if unavailable —
    on anything that isn't macOS this module simply does nothing."""
    with _LOCK:
        if not _libs["loaded"]:
            _libs["loaded"] = True
            try:
                _libs["ca"] = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
                _libs["cf"] = ctypes.CDLL(
                    ctypes.util.find_library("CoreFoundation"))
            except Exception:
                _libs["ca"] = _libs["cf"] = None
        return _libs["ca"], _libs["cf"]


def _cfstring_to_str(cf, ref):
    """A CFStringRef as a Python string. The fast path returns an interior
    pointer and is allowed to fail for any string, so the copy is not optional."""
    try:
        cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        direct = cf.CFStringGetCStringPtr(ref, _kCFStringEncodingUTF8)
        if direct:
            return direct.decode("utf-8", "replace")
        buf = ctypes.create_string_buffer(512)
        if cf.CFStringGetCString(ref, buf, 512, _kCFStringEncodingUTF8):
            return buf.value.decode("utf-8", "replace")
    except Exception:
        pass
    return None


def system_default_output():
    """The name of the device macOS is sending audio to RIGHT NOW, or None.

    Straight from CoreAudio, so it is true even when PortAudio's cached list is
    hours out of date — which is the entire point.
    """
    ca, cf = _frameworks()
    if ca is None or cf is None:
        return None
    try:
        addr = _Address(_fourcc("dOut"), _fourcc("glob"), 0)
        dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(dev))
        if ca.AudioObjectGetPropertyData(
                ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
                0, None, ctypes.byref(size), ctypes.byref(dev)) != 0:
            return None
        if not dev.value:
            return None
        name_addr = _Address(_fourcc("lnam"), _fourcc("glob"), 0)
        ref = ctypes.c_void_p()
        nsize = ctypes.c_uint32(ctypes.sizeof(ref))
        if ca.AudioObjectGetPropertyData(
                dev, ctypes.byref(name_addr), 0, None,
                ctypes.byref(nsize), ctypes.byref(ref)) != 0:
            return None
        return _cfstring_to_str(cf, ref)
    except Exception:
        return None


def portaudio_default_output(sd):
    """The name PortAudio believes is the default output. Frozen at import."""
    try:
        info = sd.query_devices(kind="output")
        return (info or {}).get("name") or None
    except Exception:
        return None


def output_is_stale(system_name, portaudio_name):
    """Pure, so the judgement is tested rather than trusted.

    Only a genuine disagreement counts. If either side won't answer we say NO:
    re-enumerating PortAudio tears down and rebuilds the whole audio system, and
    doing that on a guess is far worse than playing out of the wrong speaker.
    """
    if not system_name or not portaudio_name:
        return False
    return system_name.strip().casefold() != portaudio_name.strip().casefold()


def ensure_current_output(sd, log=print, reset=None):
    """Call this immediately BEFORE opening an output stream.

    Returns True when the device list was re-read. Safe to call often: the
    comparison is two cheap reads, and reset_portaudio has its own cooldown.

    Must only be called with no output stream of ours open — _terminate()
    invalidates every existing stream, which is why every caller does this as
    the first thing inside the same lock that then opens the stream.
    """
    try:
        real = system_default_output()
        cached = portaudio_default_output(sd)
        if not output_is_stale(real, cached):
            return False
        if reset is None:
            import live as _live
            reset = _live.reset_portaudio
        did = reset(sd, log)
        if did:
            log(f"[audio] output moved to {real!r} — Neo follows it "
                f"(was still using {cached!r})")
        return bool(did)
    except Exception as e:
        try:
            log(f"[audio] couldn't check the output device ({e}); "
                "playing on the current one")
        except Exception:
            pass
        return False


def describe():
    """One line for the doctor."""
    real = system_default_output()
    return real or "unknown (CoreAudio didn't answer)"

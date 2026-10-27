# SPDX-License-Identifier: AGPL-3.0-only
"""Send only standard Quit to an exact subprocess PID owned by a test."""
import ctypes


def send_quit(pid, *, register_timeout=15.0):
    """Send Quit; wait (bounded) only while macOS has not yet registered the new process.

    AESendMessage returns procNotFound (-600) until a freshly launched app has registered with the Apple Event
    Manager (measured on the packaged app: -600 right after ``launcher_started``, 0 three seconds later). Only that
    code is retried; every other result fails immediately, and the caller's cleanup assertions are unchanged.
    """
    import time
    assert isinstance(pid, int) and pid > 0
    carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
    class AEDesc(ctypes.Structure):
        _fields_ = [('descriptorType', ctypes.c_uint32), ('dataHandle', ctypes.c_void_p)]
    ptr = ctypes.POINTER(AEDesc)
    carbon.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ptr]
    carbon.AECreateDesc.restype = ctypes.c_int32
    carbon.AECreateAppleEvent.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ptr,
                                        ctypes.c_int16, ctypes.c_int32, ptr]
    carbon.AECreateAppleEvent.restype = ctypes.c_int32
    carbon.AESendMessage.argtypes = [ptr, ptr, ctypes.c_int32, ctypes.c_long]
    carbon.AESendMessage.restype = ctypes.c_int32
    carbon.AEDisposeDesc.argtypes = [ptr]
    carbon.AEDisposeDesc.restype = ctypes.c_int32
    fourcc = lambda value: int.from_bytes(value, 'big')
    address, event = AEDesc(), AEDesc()
    target = ctypes.c_int32(pid)
    try:
        assert carbon.AECreateDesc(fourcc(b'kpid'), ctypes.byref(target), ctypes.sizeof(target),
                                   ctypes.byref(address)) == 0
        assert carbon.AECreateAppleEvent(fourcc(b'aevt'), fourcc(b'quit'), ctypes.byref(address),
                                         -1, 0, ctypes.byref(event)) == 0
        # kAENoReply | kAENeverInteract: no GUI automation prompt or reply wait.
        deadline = time.monotonic() + register_timeout
        while True:
            result = carbon.AESendMessage(ctypes.byref(event), None, 0x11, 60)
            if result != -600 or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        assert result == 0, 'appleevent_send_failed:%d' % result
    finally:
        carbon.AEDisposeDesc(ctypes.byref(event))
        carbon.AEDisposeDesc(ctypes.byref(address))

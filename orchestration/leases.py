"""Lease expiry is a warning; takeover additionally requires terminal-owner evidence.

Modal Dict has atomic put-if-absent, but no CAS. The immutable owner record
and an atomic per-owner recovery claim serialize proven-dead-owner removal.
"""
from contextlib import contextmanager
import os
import socket
import threading
import time
from uuid import uuid4


class LeaseBusy(RuntimeError):
    pass


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = ctypes.c_ulong()
            return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def owner_stopped(owner, heartbeat, call_terminal=None):
    return bool(heartbeat.get("worker_finished") or
                (owner.get("call_id") and call_terminal and call_terminal(owner["call_id"])) or
                (owner.get("host") == socket.gethostname() and process_alive(owner.get("pid")) is False))


def terminal_proof(owner, heartbeat, call_terminal=None):
    operation = heartbeat.get("operation")
    if not owner_stopped(owner, heartbeat, call_terminal):
        return False
    if operation:
        # A dead local CLI does not prove that a remote deployment RPC stopped.
        return bool(operation.get("call_id") and call_terminal and call_terminal(operation["call_id"]))
    return True


class Lease:
    def __init__(self, registry, name, *, ttl=90, key=None, call_id=None, proof=None, clock=time.time):
        self.registry, self.name, self.ttl, self.clock = registry, name, ttl, clock
        self.owner = {"owner": uuid4().hex, "key": key, "acquired_at": clock(), "host": socket.gethostname(),
                      "pid": os.getpid(), "call_id": call_id}
        self.proof = proof or terminal_proof
        self.stop = threading.Event()
        self.thread = None
        self.operation = None
        self.lost = None

    @staticmethod
    def heartbeat_key(name, owner):
        return name + ":heartbeat:" + owner["owner"]

    def assert_owned(self):
        if self.lost or self.registry.get(self.name) != self.owner:
            raise LeaseBusy("Lease ownership lost: " + self.name)

    def renew(self):
        self.assert_owned()
        self.registry[self.heartbeat_key(self.name, self.owner)] = {
            "renewed_at": self.clock(), "expires_at": self.clock() + self.ttl, "operation": self.operation}

    def acquire(self):
        if not self.registry.put(self.name, self.owner, skip_if_exists=True):
            holder = self.registry.get(self.name)
            if not isinstance(holder, dict) or not holder.get("owner"):
                raise LeaseBusy("Legacy claim has no ownership proof; inspect manually: " + self.name)
            beat = self.registry.get(self.heartbeat_key(self.name, holder), {})
            expiry = beat.get("expires_at", holder.get("acquired_at", self.clock()) + self.ttl)
            if (self.clock() <= expiry and not beat.get("worker_finished")) or not self.proof(holder, beat):
                raise LeaseBusy("Lease active or expired without terminal-owner evidence: " + self.name)
            recovery = self.name + ":recover:" + holder["owner"]
            if not self.registry.put(recovery, self.owner["owner"], skip_if_exists=True):
                raise LeaseBusy("Another recovery is already checking this owner: " + self.name)
            # Proof must be rechecked while this recovery is exclusively reserved.
            if self.registry.get(self.name) != holder or not self.proof(holder, self.registry.get(self.heartbeat_key(self.name, holder), {})):
                raise LeaseBusy("Owner changed while checking recovery: " + self.name)
            self.registry.pop(self.name)
            if not self.registry.put(self.name, self.owner, skip_if_exists=True):
                raise LeaseBusy("Another owner acquired the recovered lease: " + self.name)
        self.renew()
        return self

    def start_heartbeat(self):
        def heartbeat():
            while not self.stop.wait(max(0.05, self.ttl / 3)):
                try:
                    self.renew()
                except Exception as exc:
                    self.lost = exc
                    return
        self.thread = threading.Thread(target=heartbeat, daemon=True, name="lease-heartbeat")
        self.thread.start()
        return self

    @contextmanager
    def external(self, kind, call_id=None, **metadata):
        self.assert_owned()
        self.operation = {"kind": kind, "call_id": call_id, "status": "uncertain", **metadata}
        self.renew()  # Persist intent before a side effect.
        yield self
        self.assert_owned()
        self.operation = None
        self.renew()

    def attach_call(self, call_id):
        self.assert_owned()
        self.operation = {**(self.operation or {}), "call_id": call_id}
        self.renew()

    def release(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.operation is None and self.registry.get(self.name) == self.owner:
            self.registry.pop(self.name)
            # A successfully finished lease needs no owner/heartbeat evidence.
            # Do not leak a heartbeat entry on every model preparation.
            self.registry.pop(self.heartbeat_key(self.name, self.owner), None)
        elif self.registry.get(self.name) == self.owner:
            self.registry[self.heartbeat_key(self.name, self.owner)] = {
                "renewed_at": self.clock(), "expires_at": self.clock(), "operation": self.operation, "worker_finished": True}
        # Unknown side effects deliberately retain the immutable owner claim.

    def __enter__(self):
        return self.acquire().start_heartbeat()

    def __exit__(self, *_):
        self.release()

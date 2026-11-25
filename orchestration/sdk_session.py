"""Bounded, process-local SDK reuse. Never cache mutable execution evidence."""
from collections import OrderedDict
from threading import Lock, RLock
import time


class SDKSession:
    def __init__(self):
        self.lock = RLock()
        self.stripes = tuple(Lock() for _ in range(32))
        self.handles = OrderedDict()
        self.inputs = OrderedDict()
        self.observed = OrderedDict()
        self.accepted_calls = OrderedDict()

    def keyed_lock(self, key):
        return self.stripes[hash(key) % len(self.stripes)]

    @staticmethod
    def _remember(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    def handle(self, key, factory):
        with self.lock:
            if key in self.handles:
                self.handles.move_to_end(key)
                return self.handles[key]
            value = factory()
            self._remember(self.handles, key, value, 256)
            return value

    def forget_handle(self, key):
        with self.lock:
            self.handles.pop(key, None)

    def input_verified(self, key):
        with self.lock:
            if key in self.inputs:
                self.inputs.move_to_end(key)
                return True
            return False

    def remember_input(self, key):
        with self.lock:
            self._remember(self.inputs, key, True, 512)

    def accepted_call(self, key):
        with self.lock:
            value = self.accepted_calls.get(key)
            if value is not None:
                self.accepted_calls.move_to_end(key)
            return value

    def remember_accepted_call(self, key, call_id):
        with self.lock:
            self._remember(self.accepted_calls, key, call_id, 512)

    def forget_accepted_call(self, key):
        with self.lock:
            self.accepted_calls.pop(key, None)

    def ledger_due(self, key, interval):
        with self.lock:
            when = self.observed.get(key)
            return when is None or time.monotonic() - when >= interval

    def note_ledger_read(self, key):
        with self.lock:
            self._remember(self.observed, key, time.monotonic(), 512)

    def forget_ledger(self, key):
        with self.lock:
            self.observed.pop(key, None)

"""Observe real Modal registration through public decorators, without deploying."""
from contextlib import contextmanager
from unittest.mock import patch

import modal


@contextmanager
def registrations():
    classes, functions = {}, {}
    original_cls, original_function = modal.App.cls, modal.App.function
    original_concurrent = modal.concurrent
    names = {}

    def concurrent(*args, **kwargs):
        decorator = original_concurrent(*args, **kwargs)
        def apply(target):
            wrapped = decorator(target)
            names[id(wrapped)] = target.__name__
            return wrapped
        return apply

    def observe(original, records):
        def register(app, *args, **kwargs):
            decorator = original(app, *args, **kwargs)
            def apply(target):
                handle = decorator(target)
                name = kwargs.get("name") or names.get(id(target)) or target.__name__
                records[(app, name)] = handle
                return handle
            return apply
        return register

    with patch.object(modal.App, "cls", observe(original_cls, classes)), \
         patch.object(modal.App, "function", observe(original_function, functions)), \
         patch.object(modal, "concurrent", concurrent):
        yield classes, functions

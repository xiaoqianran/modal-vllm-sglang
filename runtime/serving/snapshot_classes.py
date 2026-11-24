"""Source-importable GPU snapshot variants with stable deployed class names."""
from runtime.serving.model_runtime import SnapshotModelRuntime


class Model(SnapshotModelRuntime):
    pass


class Moss(SnapshotModelRuntime):
    pass


class Pika(SnapshotModelRuntime):
    pass


class H3(SnapshotModelRuntime):
    pass


class Qwen38Pi(SnapshotModelRuntime):
    pass


RUNTIME_CLASSES = {cls.__name__: cls for cls in (Model, Moss, Pika, H3, Qwen38Pi)}

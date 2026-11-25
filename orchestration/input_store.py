"""Immutable, verified workflow input staging in the shared Volume namespace."""
import hashlib
import io

from orchestration import state
from runtime.config.storage import volume_key
from runtime.tasks.artifacts import image_reference
from runtime.utils.media import image_format

# Only new dependency-managed workflows stage to this namespace.
# Old CAS and direct submissions remain outside its lifecycle.
LIFECYCLE = "managed-inputs-v1"
MANAGED_PREFIX = "workflow-inputs/managed-v1/"


def _verify(chunks, ref):
    size = 0
    digest = hashlib.sha256()
    for chunk in chunks:
        size += len(chunk)
        if size > ref["size"]:
            raise ValueError("Stored workflow input exceeds declared size")
        digest.update(chunk)
    if size != ref["size"] or digest.hexdigest() != ref["sha256"]:
        raise ValueError("Stored workflow input content mismatch")


def stage_input(volume, session, directory, artifact, *, managed=False):
    prefix = MANAGED_PREFIX if managed else "workflow-inputs/"
    ref = image_reference({"path": prefix + artifact["sha256"],
                           "sha256": artifact["sha256"], "size": artifact["size"]})
    # Snapshot and validate the very bytes that will be uploaded; avoid a
    # validate-file then reopen race. Even a cache hit checks the local binding.
    path = state.local_path(directory, artifact["path"])
    with path.open("rb") as stream:
        data = stream.read(ref["size"] + 1)
    if len(data) != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise ValueError("Frozen input image changed")
    image_format(data)
    key = volume_key("/" + ref["path"])
    identity = (key, ref["sha256"], ref["size"])
    with session.keyed_lock(identity):
        if session.input_verified(identity):
            return ref
        try:
            _verify(volume.read_file(key), ref)
        except FileNotFoundError:
            try:
                with volume.batch_upload(force=False) as batch:
                    batch.put_file(io.BytesIO(data), key)
            except Exception as exc:
                from modal.exception import InvalidError
                # Volume V2 SDK 1.6.4 may report a write conflict as
                # InvalidError rather than FileExistsError. Only accept the
                # exact destination's observed conflict message.
                if not (isinstance(exc, FileExistsError) or
                        isinstance(exc, InvalidError) and str(exc) == f'path "{key}" already exists'):
                    raise
                _verify(volume.read_file(key), ref)
        session.remember_input(identity)
    return ref

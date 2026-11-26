"""Recovery decisions shared by SDK tasks and HTTP workflows.

Call after checking locally published artifacts. A missing result is never
evidence that a previously submitted generation did not run.
"""
TERMINAL = frozenset({"completed", "failed", "cancelled"})


class SubmissionRejected(RuntimeError):
    """The shared gateway explicitly confirms it did not forward this POST."""


def recovery_action(record):
    if record.get("status") in {"failed", "cancelled"}:
        return "stop"
    if record.get("job_id"):
        return "resume"
    if not record or record.get("status") in {"pending", "blocked"}:
        return "submit"
    return "uncertain"


CALL_TERMINAL = frozenset({"SUCCESS", "FAILURE", "INIT_FAILURE", "TERMINATED", "TIMEOUT"})


def call_input_status(modal_api, call_id, *, input_id=None):
    """Require exactly one matching platform input; SDK errors prove nothing."""
    from modal.types import InputStatus
    if not isinstance(call_id, str) or not call_id.startswith("fc-"):
        return None
    try:
        graph = modal_api.FunctionCall.from_id(call_id).get_call_graph()
    except Exception:
        return None
    matches = [node for node in graph if node.function_call_id == call_id
               and (input_id is None or node.input_id == input_id)]
    return matches[0].status.name if len(matches) == 1 and isinstance(matches[0].status, InputStatus) else None


def terminal_call_status(modal_api, call_id, *, input_id=None):
    status = call_input_status(modal_api, call_id, input_id=input_id)
    return status if status in CALL_TERMINAL else None

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

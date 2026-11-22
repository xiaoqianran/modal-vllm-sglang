"""Compatibility imports for the pre-split task helper module.

Prefer task_identity, result_integrity and result_summary in new code.
"""

from runtime.tasks.task_identity import intent_digest, safe_id
from runtime.tasks.result_integrity import completed_output
from runtime.tasks.result_summary import counts

__all__ = ("intent_digest", "safe_id", "completed_output", "counts")

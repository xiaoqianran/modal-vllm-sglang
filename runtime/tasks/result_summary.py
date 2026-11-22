"""Summarize a batch's observed result states without modifying task records."""


def counts(results, total, *, completed="completed", skipped="skipped", failed="failed"):
    return {
        "completed": sum(entry.get("status") == completed for entry in results),
        "skipped": sum(entry.get("status") == skipped for entry in results),
        "failed": sum(entry.get("status") == failed for entry in results),
        "unattempted": total - len(results),
    }

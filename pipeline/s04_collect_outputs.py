"""Stage 04: schedule deliveries, collect results, publish reports."""
from orchestration.delivery import (
    schedule_deliveries, collect_finished_deliveries, publish_report,
)


def collect_outputs(directory, backend, downloads, pool, *, item=None):
    from orchestration.sdk import snapshot
    plan, current = snapshot(directory)
    schedule_deliveries(directory, plan, current, backend, downloads, pool, item=item)
    collect_finished_deliveries(directory, downloads)
    return publish_report(directory)

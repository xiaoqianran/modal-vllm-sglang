"""Build metadata-only archive requests from committed generation records."""
from orchestration import model_capabilities


def archive_request(job, record, plan):
    kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
    request = {"artifact": record["artifacts"][kind], "delivery": plan["delivery"],
               "project": plan["project"]}
    from runtime.archive.works import work_job
    work = work_job(plan, job)
    if work is not None:
        request["work"] = work
    return request

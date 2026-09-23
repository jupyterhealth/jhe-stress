"""
Stress test JHE


"""

import concurrent.futures as cf
import csv
import logging
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial

import click
from jupyterhealth_client import JupyterHealthClient
from tqdm import tqdm

log = logging.getLogger(__name__)


@dataclass
class Timer:
    start: float
    stop: float | None = None

    @property
    def duration(self) -> float:
        stop = self.stop or time.perf_counter()
        return stop - self.start


class Sample:
    start: float
    stop: float
    duration: float
    records: float


@contextmanager
def timed():
    start = time.perf_counter()
    timer = Timer(start=start)
    yield timer
    timer.stop = time.perf_counter()


def one(patient_id: int, per_page: int, limit: int):
    jhe = JupyterHealthClient()
    if per_page:
        jhe._default_page_limit = per_page
    df = jhe.list_observations_df(patient_id=patient_id, limit=limit)
    return len(df)


def time_one(**kwargs):
    with timed() as timer:
        try:
            result = one(**kwargs)
        except Exception as e:
            result = (type(e), str(e))
    return timer, result


@click.command()
@click.option("--concurrency", default=10, help="number of concurrent requests")
@click.option("--count", default=100, help="Total number of requests")
@click.option("--per-page", default=1000, help="Records per page")
@click.option("--limit", default=2000, help="Records per page")
@click.option(
    "--time-limit",
    default=300,
    help="Time limit (in seconds) after which to stop collection.",
)
@click.option("-x", "--stop-on-error", is_flag=True, help="Stop on first error")
@click.argument("patient_id")
def bench(
    *, concurrency, count, per_page, limit, time_limit, stop_on_error, patient_id
):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s %(levelname)s] %(message)s"
    )

    # validate credentials
    jhe = JupyterHealthClient()
    log.info("Running as %s", jhe.get_user())
    log.info("Testing with patient %s", jhe.get_patient(patient_id))
    deadline = time.perf_counter() + time_limit
    csvwriter = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            "concurrency",
            "count",
            "per_page",
            "start",
            "stop",
            "duration",
            "n_records",
            "error",
        ],
    )
    csvwriter.writeheader()
    common_record = {
        "concurrency": concurrency,
        "count": count,
        "per_page": per_page,
    }
    with cf.ProcessPoolExecutor(concurrency) as pool:
        pending = [
            pool.submit(
                partial(time_one, patient_id=patient_id, per_page=per_page, limit=limit)
            )
            for _ in range(count)
        ]
        with tqdm("requests", total=count) as progress:
            remaining = 1
            while pending and time.perf_counter() < deadline:
                remaining = max(deadline - time.perf_counter(), 0)
                done, pending = cf.wait(
                    pending, timeout=remaining, return_when=cf.FIRST_COMPLETED
                )
                progress.update(len(done))
                for f in done:
                    record = common_record.copy()
                    timer, result = f.result()
                    record["start"] = timer.start
                    record["stop"] = timer.stop
                    record["duration"] = timer.duration

                    if isinstance(result, tuple):
                        exc_type, strerror = result
                        record["error"] = strerror
                        log.error("Error: %s", strerror)
                        if stop_on_error:
                            [f.cancel() for f in pending]
                            raise RuntimeError("Error: %s", strerror)
                    else:
                        record["n_records"] = result
                    csvwriter.writerow(record)
            if pending:
                log.error("Reached timeout with %i/%i remaining", len(pending), count)
                [f.cancel() for f in pending]


if __name__ == "__main__":
    bench()

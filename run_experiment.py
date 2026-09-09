"""CLI: run the batch experiment and print the comparison report.

Usage:
  python run_experiment.py                      # quick demo subset (see models.yaml)
  python run_experiment.py --full               # every test query
  python run_experiment.py --limit 9            # an explicit stratified subset
  python run_experiment.py --compression both   # Headroom A/B: each job on + off

The compression A/B needs headroom-ai (pip install -r requirements-compression.txt).
It is local-only - the dependency tree is over the serverless size limit - so
set DATABASE_URL in .env to have the results land in the deployed dashboard.

Any subset is stratified across SIMPLE/MEDIUM/HARD, so a short run still
exercises the whole routing range.
"""
import argparse
import asyncio

from backend import compression, experiment, storage, telemetry
from backend.config.loader import load_settings
from backend.llm.client import ClientPool


async def main(args):
    settings = load_settings()
    limit = None if args.full else (args.limit or settings.demo_query_count)
    modes = experiment.COMPRESSION_CHOICES[args.compression]
    if "headroom" in modes and not compression.available():
        raise SystemExit("headroom-ai is not installed: pip install -r requirements-compression.txt")
    pool = ClientPool.from_settings(settings)
    await storage.init()
    n = len(telemetry.select_queries(limit))
    print(f"Running {n} test queries x 3 strategies x compression {list(modes)} "
          f"(concurrency {settings.experiment_concurrency}, store: {storage.backend()})...")

    def progress(s):
        print(f"\r  {s['completed']}/{s['total']} done, {s['failed']} failed",
              end="", flush=True)

    # A short step budget only affects how often progress is reported.
    final = await experiment.run(settings, pool, limit=limit, budget_s=5,
                                 on_progress=progress, compressions=modes)
    print("\n")
    print(experiment.format_report(final["result"]))
    # Individual call failures are retried and failed over inside the pipeline,
    # so anything reaching here exhausted every tier. Worth showing in a CLI.
    if final["errors"]:
        print(f"\n{final['failed']} request(s) could not be completed:")
        for e in final["errors"]:
            print(" -", e)
    await pool.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="run a stratified subset of N test queries")
    p.add_argument("--full", action="store_true",
                   help="run every test query instead of the demo subset")
    p.add_argument("--compression", choices=sorted(experiment.COMPRESSION_CHOICES),
                   default="off", help="context compression: off, headroom, or both (A/B)")
    asyncio.run(main(p.parse_args()))

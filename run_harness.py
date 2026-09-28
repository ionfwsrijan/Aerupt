"""Run the canonical scenario suite against AERUPT and score it.

Usage:
    py run_harness.py                       # full suite at 1x
    py run_harness.py s02                   # single scenario
    py run_harness.py --scale 0.4           # sprinted suite
    py run_harness.py --detail s03          # include per-check breakdown
    py run_harness.py --sweep 4 s03         # N runs over several scales
    py run_harness.py --sweep 3             # whole suite, robustness gate
    py run_harness.py --profile             # per-scenario real-time timeline
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import sys

sys.path.insert(0, ".")

from aerupt.config import load_env

load_env()


async def _score_chosen(factory, chosen, scale: float, save: bool):
    from harness.harness import run_suite
    from harness.scorer import Scorer

    results = await run_suite(factory, chosen, scale=scale, save=save)
    rows = []
    for res in results:
        sc = Scorer(res["trace"], res["gold"], res["env"], res["events"])
        score, detail = sc.score()
        rows.append((res["scenario"], score, detail, res))
    return rows


def _print_rows(rows, with_detail: bool) -> None:
    print("\n" + "-" * 60)
    total = 0.0
    for name, score, detail, _ in rows:
        total += score
        mm = "  ◈1.5×" if detail["multimodal"] else ""
        print(f"  {name:<34} {score:>6.2f}{mm}")
        if with_detail and score < 100.0:
            for c in detail.get("checks_report", []):
                mark = "ok " if c["passed"] else "FAIL"
                print(f"      [{mark}] {c['check']:<34} {c['outcome']}")
    print("-" * 60)
    print(f"  {'AVERAGE':<34} {total/max(len(rows),1):>6.2f}")


def _print_one(res) -> None:
    from harness.scorer import Scorer

    sc = Scorer(res["trace"], res["gold"], res["env"], res["events"])
    score, detail = sc.score()
    print(f"\n=== {res['scenario']} → {score}")
    weights = {"task_completion": ".40×", "interruption_recovery": ".35×",
               "response_latency": ".15×", "safety_protocol": ".10×"}
    for k, v in weights.items():
        print(f"    {v}{k:<22} {detail[k]}")
    print(f"    quality multiplier      {detail['quality_multiplier']}")
    for c in detail.get("checks_report", []):
        mark = "ok " if c["passed"] else "FAIL"
        print(f"        [{mark}] {c['check']:<34} {c['outcome']}")


async def _profile(chosen, scale: float) -> None:
    from aerupt.agent import InterruptibleAgent
    from harness.harness import run_suite
    from harness.scorer import Scorer

    def factory():
        return InterruptibleAgent(config={"time_scale": 1.0})

    results = await run_suite(factory, chosen, scale=scale, save=True)
    print("\nreal-time profile (virtual seconds, scale=%.2f)" % scale)
    hdr = (f"  {'scenario':<32} {'dur':>6} {'acts':>5} {'t2s':>6} "
           f"{'spok':>5} {'tools':>5} {'cxl':>4}   per-tool avg latency")
    print(hdr)
    for res in results:
        trace = res["trace"]
        events = res["events"]
        dur = trace[-1]["ts"] - (trace[0]["ts"] if trace else 0.0)
        actions = len(trace)
        sc = Scorer(trace, res["gold"], res["env"], events)
        t2s = sc._latency()
        spoken = sum(1 for a in trace if a["type"] in
                     ("narration", "filler", "clarify", "response"))
        recs = res["env"].get("call_records", [])
        by_tool: dict = {}
        for c in recs:
            lat = (c.get("done_ts") or c.get("ts")) - c.get("ts", 0.0)
            by_tool.setdefault(c["tool"], []).append(max(lat, 0.0))
        lat_s = "  ".join(
            f"{t}={sum(v)/len(v):.2f}s" for t, v in sorted(by_tool.items()))
        cancels = sum(1 for a in trace if a["type"] == "cancel")
        print(f"  {res['scenario']:<32} {dur:>6.2f} {actions:>5} "
              f"{t2s:>6.1f} {spoken:>5} {len(recs):>5} {cancels:>4}   {lat_s}")


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="?", default=None, help="scenario name/prefix")
    ap.add_argument("--scale", type=float, default=1.0, help="time scale")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--detail", action="store_true",
                    help="print per-check breakdown for imperfect scores")
    ap.add_argument("--sweep", type=int, default=0, metavar="N",
                    help="run each scenario N times over several scales "
                         "(robustness gate); N<=0 disables")
    ap.add_argument("--profile", action="store_true",
                    help="print a per-scenario real-time timeline")
    args = ap.parse_args()

    from aerupt.agent import InterruptibleAgent
    from harness.scenarios import SCENARIOS

    def factory():
        return InterruptibleAgent(config={"time_scale": 1.0})

    chosen = SCENARIOS
    if args.only:
        chosen = [s for s in SCENARIOS if args.only in s["name"]]
        if not chosen:
            print(f"No scenario matching {args.only!r}")
            sys.exit(1)

    if args.profile:
        await _profile(chosen, args.scale)
        return

    if args.sweep > 0:
        scales = (0.2, 0.4, 1.0)
        agg = collections.defaultdict(list)
        bad: dict = collections.defaultdict(list)
        print(f"robustness sweep: {args.sweep} run(s) × scales {scales} "
              f"over {len(chosen)} scenario(s)")
        for scale in scales:
            for _ in range(args.sweep):
                rows = await _score_chosen(factory, chosen, scale,
                                           save=not args.no_save)
                for name, score, detail, _ in rows:
                    agg[name].append(score)
                    if score < 100.0:
                        failed = [c["check"] for c in detail["checks_report"]
                                  if not c["passed"]]
                        bad[name].append((scale, score, failed))
        print("\n" + "-" * 68)
        print(f"  {'scenario':<34} {'worst':>6} {'min':>6} {'max':>6} {'runs':>5}  robust")
        all_ok = True
        for name, scores in agg.items():
            fails = bad.get(name, [])
            robust = not fails and len(scores) == args.sweep * len(scales)
            all_ok = all_ok and robust
            print(f"  {name:<34} {min(scores):>6.2f} {min(scores):>6.2f} "
                  f"{max(scores):>6.2f} {len(scores):>5}  "
                  f"{'YES' if robust else 'NO'}")
            for scale, score, checks in fails[:6]:
                print(f"      scale={scale} score={score} -> "
                      f"{', '.join(checks[:4])}")
        print("-" * 68)
        print(f"  {'ROBUST' if all_ok else 'REGRESSIONS FOUND'}")
        return

    rows = await _score_chosen(factory, chosen, args.scale,
                               save=not args.no_save)
    if len(rows) == 1 or args.detail:
        for _, _, _, res in rows:
            _print_one(res)
    _print_rows(rows, with_detail=True)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\ninterrupted")
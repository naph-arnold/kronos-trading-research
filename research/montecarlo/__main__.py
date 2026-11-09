"""Kronos Monte Carlo validation CLI (research only; never touches runners, signals or risk controls).

  python -m research.montecarlo run   [--config research/configs/montecarlo_v1.json] [--only xauusd:orb_newyork]
  python -m research.montecarlo check  # resources and dataset availability, no simulation

Cancel a running job by creating <output_dir>/CANCEL; it stops after the current batch and keeps its checkpoint,
so a later `run` resumes with identical results.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.montecarlo import ALGORITHM_VERSION
from research.montecarlo.engine import DEFAULTS, available_memory_mb, run, write_report
from research.montecarlo.records import RecordError, from_phase2_report, from_shadow_store
from research.montecarlo.regimes import attach_report_regimes

EXAMINED = ("Phase 2 holdout: evaluated once after walk-forward selection, then examined when choosing the Phase 2C "
            "pre-registered candidates. Historical evidence only; not untouched forward evidence.")


def load_dataset(spec: dict):
    if spec["source"] == "phase2_report":
        ds = from_phase2_report(Path(spec["report"]), spec["market"], spec["strategy"], examined_note=EXAMINED,
                                initial_equity=spec.get("initial_equity"), commission=spec.get("commission"),
                                config_source=spec.get("book_parameters_from"))
        if spec.get("note"):
            ds.provenance["note"] = spec["note"]
        if spec.get("regime_csv"):
            rep = json.loads(Path(spec["report"]).read_text(encoding="utf-8"))
            attach_report_regimes(ds, Path(spec["regime_csv"]), rep["strategies"][spec["strategy"]].get("holdout_by_regime") or {})
        return ds, None
    if spec["source"] == "shadow_store":
        if not Path(spec["db"]).exists():
            return None, "runner not launched: no forward-test store exists"
        return from_shadow_store(Path(spec["db"]), spec["market"], spec["strategy"], spec["book"],
                                 forward_start=spec["forward_start"]), None
    return None, spec.get("reason", "no verified trade records")


def stub(spec: dict, reason: str) -> dict:
    return {"schema_version": 1, "algorithm": ALGORITHM_VERSION, "kind": "monte_carlo_research",
            "market": spec["market"], "strategy": spec["strategy"], "partition": spec.get("partition", "historical_holdout"),
            "status": "no_trade_records", "reason": reason, "variants": [],
            "evidence": {"closed_trades": 0}, "dataset": {"provenance": {"source": spec["source"]}}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "check"])
    ap.add_argument("--config", default="research/configs/montecarlo_v1.json")
    ap.add_argument("--only", action="append", help="market:strategy[:partition]")
    args = ap.parse_args(argv)
    conf = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out = Path(conf["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    cancel = out / "CANCEL"
    index = []
    for spec in conf["datasets"]:
        part = spec.get("partition", "historical_holdout")
        tag = f"{spec['market']}:{spec['strategy']}:{part}"
        if args.only and not any(tag.startswith(o) for o in args.only):
            continue
        try:
            ds, reason = load_dataset(spec)
        except (RecordError, OSError, KeyError, ValueError) as exc:
            ds, reason = None, f"dataset refused: {type(exc).__name__}: {exc}"
        if args.command == "check":
            print(f"{tag:45s} {'ok, ' + str(len(ds.trades)) + ' trades' if ds else reason}")
            continue
        if ds is None:
            report = stub(spec, reason)
        else:
            cfg = {**DEFAULTS, **conf["run"], "cost_scenarios": conf["cost_scenarios"][spec["market"]]}
            report = run(ds, cfg, checkpoint_dir=out / "checkpoints", cancel_file=cancel,
                         progress=lambda i, n, k, t=tag: print(f"  {t} variant {i}/{n} {k}", flush=True))
        path = write_report(report, out / "reports")
        print(f"{tag}: {report['status']} -> {path.name}", flush=True)
        index.append({"id": path.stem, "market": report["market"], "strategy": report["strategy"],
                      "partition": report["partition"], "status": report["status"],
                      "closed_trades": report["evidence"].get("closed_trades"), "file": path.name,
                      "result_sha256": report.get("result_sha256")})
        if report["status"] == "cancelled":
            break
    if args.command == "check":
        print(f"available memory: {available_memory_mb():.0f} MB")
        return 0
    idx_path = out / "index.json"
    old = json.loads(idx_path.read_text(encoding="utf-8"))["reports"] if idx_path.exists() else []
    merged = {r["id"]: r for r in old}
    merged.update({r["id"]: r for r in index})
    idx_path.write_text(json.dumps({"algorithm": ALGORITHM_VERSION, "updated_at": datetime.now(timezone.utc).isoformat(),
                                    "reports": sorted(merged.values(), key=lambda r: r["id"])}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

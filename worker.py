"""
worker.py — Background worker process entry point.

Runs ONLY the background loops, no HTTP server. For production split where
the API container handles requests and a separate worker container handles
all the periodic jobs (price polling, explainer, alert engine, macro snap).

Usage:
    python worker.py                  # run all loops
    python worker.py --only=alerts    # run only one loop (debug)

The same loops also run inline inside the API process by default — set the
env var WORKER_DISABLE_INLINE=true to suppress them in API and let the
dedicated worker handle them.
"""
import os
import sys
import time
import asyncio
import signal as _sigmod

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
except ImportError:
    _env = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(_env):
        for line in open(_env):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from production import log, heartbeat


# ─── Loop definitions (mirror dashboard_api.py background tasks) ────────────

async def loop_continuous_refresh():
    """Keep news + indices + macro caches warm."""
    log("INFO", "worker", "loop_continuous_refresh started")
    await asyncio.sleep(15)
    while True:
        try:
            from news import get_all_news
            await asyncio.to_thread(get_all_news)
            heartbeat("continuous_refresh")
        except Exception as e:
            log("ERROR", "worker", "continuous_refresh", err=type(e).__name__, msg=str(e)[:120])
        await asyncio.sleep(20)


async def loop_macro_desk_snapshot():
    """Persist macro regime snapshot every 15 min."""
    log("INFO", "worker", "loop_macro_desk_snapshot started")
    await asyncio.sleep(120)
    while True:
        try:
            from macro_desk import get_macro_regime_view, store_snapshot
            view = await asyncio.to_thread(get_macro_regime_view)
            await asyncio.to_thread(store_snapshot, view)
            heartbeat("macro_desk_snap")
            log("INFO", "worker", "macro_desk snapshot stored")
        except Exception as e:
            log("ERROR", "worker", "macro_desk_snap", err=type(e).__name__, msg=str(e)[:120])
        await asyncio.sleep(900)


async def loop_explainer_scan():
    """Scan tracked assets for strong moves every 7 min."""
    log("INFO", "worker", "loop_explainer_scan started")
    await asyncio.sleep(180)
    while True:
        try:
            from explainer import scan_and_explain
            summary = await asyncio.to_thread(scan_and_explain, 3)
            heartbeat("explainer_scan")
            if summary.get("generated"):
                log("INFO", "worker", "explainer generated", assets=summary['generated'])
        except Exception as e:
            log("ERROR", "worker", "explainer_scan", err=type(e).__name__, msg=str(e)[:120])
        await asyncio.sleep(420)


async def loop_alert_engine():
    """Run alert triggers every 3 min."""
    log("INFO", "worker", "loop_alert_engine started")
    await asyncio.sleep(240)
    while True:
        try:
            from alert_engine import run_all_checks
            summary = await asyncio.to_thread(run_all_checks, True)
            heartbeat("alert_engine")
            if summary.get("sent"):
                log("INFO", "worker", "alerts sent",
                    sent=summary['sent'], cooldown=summary.get('in_cooldown',0))
        except Exception as e:
            log("ERROR", "worker", "alert_engine", err=type(e).__name__, msg=str(e)[:120])
        await asyncio.sleep(180)


async def loop_signal_verify():
    """Hourly signal outcome verification."""
    log("INFO", "worker", "loop_signal_verify started")
    await asyncio.sleep(3600)
    while True:
        try:
            import signal_memory as _sm
            await asyncio.to_thread(_sm.run_verification_pass)
            heartbeat("signal_verify")
        except Exception as e:
            log("ERROR", "worker", "signal_verify", err=type(e).__name__, msg=str(e)[:120])
        await asyncio.sleep(3600)


LOOPS = {
    "refresh":         loop_continuous_refresh,
    "macro_snap":      loop_macro_desk_snapshot,
    "explainer":       loop_explainer_scan,
    "alerts":          loop_alert_engine,
    "signal_verify":   loop_signal_verify,
}


# ─── Main ────────────────────────────────────────────────────────────────────

async def main(only: str | None = None):
    log("INFO", "worker", "starting", pid=os.getpid(),
        loops=",".join([only] if only else list(LOOPS.keys())))
    tasks = []
    selected = [LOOPS[only]] if only else LOOPS.values()
    for loop in selected:
        tasks.append(asyncio.create_task(loop()))
    # Graceful shutdown on SIGTERM
    stop = asyncio.Event()
    def _shutdown(*_):
        log("INFO", "worker", "shutdown signal received")
        stop.set()
    for sig in (_sigmod.SIGINT, _sigmod.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            pass
    await stop.wait()
    for t in tasks:
        t.cancel()
    log("INFO", "worker", "shutdown complete")


if __name__ == "__main__":
    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1]
            if only not in LOOPS:
                print(f"Unknown loop: {only}. Available: {list(LOOPS.keys())}")
                sys.exit(1)
    asyncio.run(main(only=only))

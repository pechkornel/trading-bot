"""
FastAPI dashboard.

Read-only views over logs/bot.sqlite (predictions, orders, equity, events)
plus controls to start/stop the runner and trigger training.

Templates: Jinja2 server-rendered + HTMX polling. No build step.
Styling: Tailwind via CDN. Charts: Chart.js via CDN.

Run:
    uvicorn dashboard.app:app --host 127.0.0.1 --port 8765 --reload   # from the repo root
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import (
    load_app_config,
    load_portfolio,
    load_sentiment_config,
    load_sleeves,
)
from dashboard import process as procmgr
from live.storage import Storage
from models.registry import list_models

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Trading Bot Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _storage() -> Storage:
    """Fresh Storage per request — SQLite connections aren't thread-safe across requests."""
    return Storage()


# ---------- pages ----------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = load_app_config()
    s = _storage()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "cfg": cfg,
            "predictions": s.recent_predictions(limit=20),
            "orders": s.recent_orders(limit=20),
            "events": s.recent_events(limit=20),
            "processes": procmgr.list_processes(),
        },
    )


@app.get("/models", response_class=HTMLResponse)
def models_page(request: Request):
    return templates.TemplateResponse(request, "models.html", {"models": list_models()})


@app.get("/control", response_class=HTMLResponse)
def control_page(request: Request):
    return templates.TemplateResponse(
        request,
        "control.html",
        {
            "cfg": load_app_config(),
            "portfolio": load_portfolio(),
            "models": list_models(),
            "processes": procmgr.list_processes(),
        },
    )


@app.get("/targets", response_class=HTMLResponse)
def targets_page(request: Request, limit: int = 10, sleeve: str | None = None):
    cycles = _storage().recent_target_cycles(limit=limit)
    if sleeve:
        cycles = [
            {
                "cycle_id": c["cycle_id"],
                "ts": c["ts"],
                "targets": [t for t in c["targets"] if t["sleeve"] == sleeve],
            }
            for c in cycles
        ]
        cycles = [c for c in cycles if c["targets"]]
    return templates.TemplateResponse(
        request,
        "targets.html",
        {"cycles": cycles, "sleeve_filter": sleeve},
    )


@app.get("/sleeves", response_class=HTMLResponse)
def sleeves_page(request: Request):
    s = _storage()
    lt_cfg, tc_cfg = load_sleeves()
    pc = load_portfolio()
    total = pc.starting_equity
    lt_eq = total * lt_cfg.allocation_pct
    tc_eq = total * tc_cfg.allocation_pct
    return templates.TemplateResponse(
        request,
        "sleeves.html",
        {
            "total_equity": total,
            "lt_cfg": lt_cfg,
            "tc_cfg": tc_cfg,
            "lt_equity": lt_eq,
            "tc_equity": tc_eq,
            "lt_holdings": s.get_holdings("long_term"),
            "tc_holdings": s.get_holdings("tactical"),
            "last_rebalance": s.last_rebalance("long_term"),
            "sentiment_cfg": load_sentiment_config(),
        },
    )


@app.get("/logs/{name}", response_class=HTMLResponse)
def logs_page(request: Request, name: str, lines: int = 200):
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"name": name, "content": procmgr.tail_log(name, lines=lines), "lines": lines},
    )


# ---------- HTMX partials (auto-refresh every few seconds) ----------


@app.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/status.html",
        {"cfg": load_app_config(), "processes": procmgr.list_processes()},
    )


@app.get("/partials/predictions", response_class=HTMLResponse)
def partial_predictions(request: Request, symbol: str | None = None, limit: int = 20):
    return templates.TemplateResponse(
        request,
        "partials/predictions.html",
        {"predictions": _storage().recent_predictions(limit, symbol)},
    )


@app.get("/partials/orders", response_class=HTMLResponse)
def partial_orders(request: Request, limit: int = 20):
    return templates.TemplateResponse(
        request,
        "partials/orders.html",
        {"orders": _storage().recent_orders(limit)},
    )


@app.get("/partials/events", response_class=HTMLResponse)
def partial_events(request: Request, limit: int = 30):
    return templates.TemplateResponse(
        request,
        "partials/events.html",
        {"events": _storage().recent_events(limit)},
    )


# ---------- JSON APIs (for Chart.js and external tooling) ----------


@app.get("/api/predictions")
def api_predictions(symbol: str | None = None, limit: int = 500):
    rows = _storage().recent_predictions(limit, symbol)
    # Chart.js wants oldest → newest
    rows.reverse()
    return JSONResponse(rows)


@app.get("/api/orders")
def api_orders(limit: int = 500):
    rows = _storage().recent_orders(limit)
    rows.reverse()
    return JSONResponse(rows)


@app.get("/api/events")
def api_events(limit: int = 200):
    return JSONResponse(_storage().recent_events(limit))


@app.get("/api/equity")
def api_equity(limit: int = 1000):
    rows = _storage().equity_history(limit)
    rows.reverse()
    return JSONResponse(rows)


@app.get("/api/processes")
def api_processes():
    return JSONResponse(
        [
            {
                "name": p.name,
                "pid": p.pid,
                "cmd": p.cmd,
                "started_at": p.started_at,
                "log_path": p.log_path,
            }
            for p in procmgr.list_processes()
        ]
    )


@app.get("/api/health")
def api_health():
    cfg = load_app_config()
    return {
        "ok": True,
        "paper": cfg.paper,
        "dry_run": cfg.dry_run,
        "ib_port": cfg.ib.port,
        "universe": cfg.universe,
        "active_processes": len(procmgr.list_processes()),
    }


# ---------- control actions (POST → redirect) ----------


@app.post("/actions/runner/start")
def action_start_runner(
    symbol: str = Form(...),
    model: str = Form(...),
    quantity: float = Form(1.0),
    mode: str = Form("dry-run"),
):
    try:
        procmgr.start_runner(
            symbol=symbol,
            model=model,
            dry_run=(mode != "live"),
            quantity=quantity,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return RedirectResponse("/control", status_code=303)


@app.post("/actions/runner/stop")
def action_stop_runner(symbol: str = Form(...)):
    procmgr.stop_process(f"runner:{symbol}")
    return RedirectResponse("/control", status_code=303)


@app.post("/actions/portfolio/start")
def action_start_portfolio(mode: str = Form("dry-run")):
    try:
        procmgr.start_portfolio(dry_run=(mode != "live"))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return RedirectResponse("/control", status_code=303)


@app.post("/actions/portfolio/stop")
def action_stop_portfolio():
    procmgr.stop_process("portfolio")
    return RedirectResponse("/control", status_code=303)


@app.post("/actions/sentiment/start")
def action_start_sentiment():
    try:
        procmgr.start_sentiment_worker()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return RedirectResponse("/control", status_code=303)


@app.post("/actions/sentiment/stop")
def action_stop_sentiment():
    procmgr.stop_process("sentiment")
    return RedirectResponse("/control", status_code=303)


@app.get("/api/targets")
def api_targets(limit: int = 10):
    return JSONResponse(_storage().recent_target_cycles(limit=limit))


@app.get("/api/sentiment")
def api_sentiment():
    """Latest sentiment per configured symbol."""
    cfg = load_sentiment_config()
    s = _storage()
    out = []
    for sym in cfg.symbols:
        row = s.latest_sentiment(sym, max_age_sec=86400)
        if row:
            out.append(
                {
                    "symbol": sym,
                    **{k: row[k] for k in ("ts", "score", "confidence", "article_count", "source")},
                }
            )
        else:
            out.append(
                {
                    "symbol": sym,
                    "ts": None,
                    "score": None,
                    "confidence": None,
                    "article_count": 0,
                    "source": cfg.provider,
                }
            )
    return JSONResponse(out)


@app.post("/actions/train/start")
def action_start_training(
    symbol: str = Form(...),
    kind: str = Form("xgboost"),
    classifier: str = Form("multiclass"),
):
    try:
        procmgr.start_training(symbol=symbol, kind=kind, classifier=classifier)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return RedirectResponse("/control", status_code=303)


@app.post("/actions/train/stop")
def action_stop_training(symbol: str = Form(...), kind: str = Form("xgboost")):
    procmgr.stop_process(f"train:{symbol}:{kind}")
    return RedirectResponse("/control", status_code=303)

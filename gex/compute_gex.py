#!/usr/bin/env python3
"""
Calcula métricas de posicionamento de dealers no mercado de opções:

  * Gamma Exposure (GEX) por strike, por vencimento e total
  * Call Wall  -> strike >= spot com maior GEX de calls
  * Put Wall   -> strike <= spot com maior GEX (absoluto) de puts
  * Volatility Trigger / Gamma Flip -> nível do subjacente onde o GEX total
    muda de sinal (calculado recalculando o gamma Black-Scholes de todas as
    opções para uma grelha de preços hipotéticos)

Tudo é calculado para vários horizontes de vencimento (todos, <=90, <=30 e
<=7 dias), porque os níveis de curto prazo costumam ser mais relevantes para
intraday / swing de poucos dias.

Fonte: cotações atrasadas (15 min) da CBOE, gratuitas e sem API key.

Convenção (padrão de mercado): os dealers estão LONG calls e SHORT puts,
logo GEX das calls é positivo e das puts é negativo.

    GEX ($ por 1% de movimento) = gamma * OI * 100 * S^2 * 0.01

Uso:
    python gex/compute_gex.py --ticker SPX
    python gex/compute_gex.py --ticker SPX --input raw.json   # ficheiro local
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
INDEX_TICKERS = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"}
AM_SETTLED_ROOTS = {"SPX", "NDX", "RUT"}
CONTRACT_SIZE = 100
NY = ZoneInfo("America/New_York")

RISK_FREE = 0.04        # taxa de juro anual (aprox.)
DIV_YIELD = 0.013       # dividend yield anual do índice (aprox.)
MIN_T = 1.0 / (365 * 24)  # 1 hora: evita gamma infinito em 0DTE perto do fecho
PROFILE_RANGE = 0.15    # perfil de gamma de -15% a +15% do spot
PROFILE_POINTS = 301
STRIKE_RANGE = 0.20     # strikes exportados: +/- 20% do spot
HISTORY_MAX = 750       # nº máximo de snapshots diários guardados

# Horizontes de vencimento: chave -> nº máximo de dias de calendário até ao vencimento
HORIZONS: dict[str, int | None] = {"all": None, "90": 90, "30": 30, "7": 7}

SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")


# --------------------------------------------------------------------------- #
# Dados
# --------------------------------------------------------------------------- #
def fetch_cboe(ticker: str) -> dict:
    symbol = f"_{ticker}" if ticker.upper() in INDEX_TICKERS else ticker.upper()
    req = urllib.request.Request(
        CBOE_URL.format(symbol=symbol),
        headers={"User-Agent": "Mozilla/5.0 (gex-dashboard)", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def parse_chain(raw: dict, now: datetime) -> tuple[float, dict]:
    """Devolve (spot, dict de arrays numpy) apenas com opções úteis."""
    data = raw["data"]
    spot = float(data.get("current_price") or data.get("close"))
    today = now.date()

    rows = []
    for opt in data["options"]:
        m = SYMBOL_RE.match(opt.get("option", ""))
        if not m:
            continue
        oi = float(opt.get("open_interest") or 0)
        if oi <= 0:
            continue
        exp_date = datetime.strptime(m["exp"], "%y%m%d").date()
        # Mensais de índice (roots SPX, NDX, RUT) liquidam na abertura (AM);
        # semanais (SPXW, NDXP, RUTW) e restantes ao fecho (PM)
        settle = time(9, 30) if m["root"] in AM_SETTLED_ROOTS else time(16, 0)
        exp_dt = datetime.combine(exp_date, settle, tzinfo=NY)
        t_years = (exp_dt - now).total_seconds() / (365 * 86400)
        if t_years <= 0:
            continue  # já expirou
        rows.append((
            float(m["strike"]) / 1000.0,
            1.0 if m["type"] == "C" else -1.0,
            oi,
            float(opt.get("gamma") or 0),
            float(opt.get("iv") or 0),
            max(t_years, MIN_T),
            exp_date.isoformat(),
            (exp_date - today).days,
        ))

    if not rows:
        raise RuntimeError("Nenhuma opção válida encontrada nos dados da CBOE")

    strike, sign, oi, gamma, iv, t, exp, dte = zip(*rows)
    chain = {
        "strike": np.array(strike),
        "sign": np.array(sign),
        "oi": np.array(oi),
        "gamma": np.array(gamma),
        "iv": np.array(iv),
        "t": np.array(t),
        "exp": np.array(exp),
        "dte": np.array(dte),
    }
    return spot, chain


def subset(chain: dict, mask: np.ndarray) -> dict:
    return {k: v[mask] for k, v in chain.items()}


# --------------------------------------------------------------------------- #
# Cálculos
# --------------------------------------------------------------------------- #
def bs_gamma(S, K: np.ndarray, iv: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Gamma Black-Scholes (com dividend yield). S pode ter shape (m,1) para broadcast."""
    sqrt_t = np.sqrt(t)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(S / K) + (RISK_FREE - DIV_YIELD + 0.5 * iv**2) * t) / (iv * sqrt_t)
        g = np.exp(-DIV_YIELD * t) * np.exp(-0.5 * d1**2) / (np.sqrt(2 * np.pi) * S * iv * sqrt_t)
    return np.nan_to_num(g, nan=0.0, posinf=0.0, neginf=0.0)


def gex_dollars(gamma: np.ndarray, oi: np.ndarray, sign: np.ndarray, spot) -> np.ndarray:
    """GEX em $ por 1% de movimento do subjacente."""
    return sign * gamma * oi * CONTRACT_SIZE * spot**2 * 0.01


def find_zero_cross(levels: np.ndarray, values: np.ndarray, spot: float) -> float | None:
    """Nível onde o perfil cruza zero; se houver vários, o mais próximo do spot."""
    s = np.sign(values)
    idx = np.where(s[:-1] * s[1:] < 0)[0]
    if idx.size == 0:
        return None
    crosses = np.array([
        levels[i] - values[i] * (levels[i + 1] - levels[i]) / (values[i + 1] - values[i]) for i in idx
    ])
    return float(crosses[np.argmin(np.abs(crosses - spot))])


def profile_levels(spot: float) -> np.ndarray:
    return np.linspace(spot * (1 - PROFILE_RANGE), spot * (1 + PROFILE_RANGE), PROFILE_POINTS)


def gamma_profile(c: dict, levels: np.ndarray) -> np.ndarray:
    ok = c["iv"] > 0
    if not ok.any():
        return np.zeros_like(levels)
    K, iv, t, oi, sign = (c[k][ok] for k in ("strike", "iv", "t", "oi", "sign"))
    g = bs_gamma(levels[:, None], K[None, :], iv[None, :], t[None, :])
    return (sign[None, :] * g * oi[None, :]).sum(axis=1) * CONTRACT_SIZE * levels**2 * 0.01


def rnd(x: float | None, nd: int = 2):
    return None if x is None else round(float(x), nd)


def compute_view(ch: dict, spot: float) -> dict:
    """Todas as métricas para um subconjunto de opções (um horizonte de vencimentos)."""
    # A CBOE arredonda o gamma a 4 casas decimais (ex.: 0.0006), o que gera erros
    # relevantes. Recalculamos com Black-Scholes a partir da IV; se não houver IV,
    # usamos o gamma da CBOE.
    gamma = np.where(ch["iv"] > 0, bs_gamma(spot, ch["strike"], ch["iv"], ch["t"]), ch["gamma"])
    gex = gex_dollars(gamma, ch["oi"], ch["sign"], spot)
    is_call = ch["sign"] > 0

    # --- Agregação por strike ---------------------------------------------- #
    strikes = np.unique(ch["strike"])
    idx = np.searchsorted(strikes, ch["strike"])
    agg = lambda w: np.bincount(idx, weights=w, minlength=strikes.size)
    call_gex, put_gex = agg(np.where(is_call, gex, 0)), agg(np.where(~is_call, gex, 0))
    call_oi, put_oi = agg(np.where(is_call, ch["oi"], 0)), agg(np.where(~is_call, ch["oi"], 0))
    net_gex = call_gex + put_gex

    # Call Wall procurada em strikes >= spot (resistência) e Put Wall em strikes
    # <= spot (suporte). Sem esta restrição, puts ITM com muito OI (ex.: 8000 com
    # spot a 7550) apareceriam como "Put Wall" acima do preço.
    above, below = strikes >= spot, strikes <= spot
    call_wall = strikes[above][np.argmax(call_gex[above])] if above.any() else strikes[np.argmax(call_gex)]
    put_wall = strikes[below][np.argmin(put_gex[below])] if below.any() else strikes[np.argmin(put_gex)]

    # --- Perfil de gamma e Volatility Trigger (gamma flip) ----------------- #
    levels = profile_levels(spot)
    profile = gamma_profile(ch, levels)
    flip = find_zero_cross(levels, profile, spot)

    expiries = np.unique(ch["exp"])
    next_exp = expiries[0]
    if expiries.size > 1:
        profile_ex_next = gamma_profile(subset(ch, ch["exp"] != next_exp), levels)
        flip_ex_next = find_zero_cross(levels, profile_ex_next, spot)
    else:
        profile_ex_next, flip_ex_next = np.zeros_like(levels), None

    # --- GEX por vencimento (20 com mais gamma bruto, por ordem de data) ---- #
    by_exp = []
    for e in expiries:
        m = ch["exp"] == e
        by_exp.append({
            "expiry": str(e),
            "dte": int(ch["dte"][m][0]),
            "call_gex": round(float(gex[m & is_call].sum()), 0),
            "put_gex": round(float(gex[m & ~is_call].sum()), 0),
            "net_gex": round(float(gex[m].sum()), 0),
        })
    by_exp = sorted(
        sorted(by_exp, key=lambda x: x["call_gex"] - x["put_gex"], reverse=True)[:20],
        key=lambda x: x["expiry"],
    )

    in_range = (strikes >= spot * (1 - STRIKE_RANGE)) & (strikes <= spot * (1 + STRIKE_RANGE))
    by_strike = [
        {
            "strike": float(strikes[i]),
            "call_gex": round(float(call_gex[i]), 0),
            "put_gex": round(float(put_gex[i]), 0),
            "net_gex": round(float(net_gex[i]), 0),
            "call_oi": int(call_oi[i]),
            "put_oi": int(put_oi[i]),
        }
        for i in np.where(in_range)[0]
        if call_oi[i] + put_oi[i] > 0
    ]

    total_gex = float(gex.sum())
    tot_call_oi, tot_put_oi = float(ch["oi"][is_call].sum()), float(ch["oi"][~is_call].sum())
    positive = (spot > flip) if flip is not None else (total_gex > 0)

    return {
        "levels": {
            "call_wall": float(call_wall),
            "put_wall": float(put_wall),
            "vol_trigger": rnd(flip),
            "vol_trigger_ex_next_expiry": rnd(flip_ex_next),
            "max_abs_gex_strike": float(strikes[np.argmax(np.abs(net_gex))]),
            "call_wall_any_strike": float(strikes[np.argmax(call_gex)]),
            "put_wall_any_strike": float(strikes[np.argmin(put_gex)]),
        },
        "totals": {
            "net_gex": round(total_gex, 0),
            "call_gex": round(float(gex[is_call].sum()), 0),
            "put_gex": round(float(gex[~is_call].sum()), 0),
            "call_oi": int(tot_call_oi),
            "put_oi": int(tot_put_oi),
            "put_call_oi_ratio": round(tot_put_oi / tot_call_oi, 3) if tot_call_oi else None,
            "regime": "positivo" if positive else "negativo",
            "n_options": int(ch["oi"].size),
            "n_expiries": int(expiries.size),
            "first_expiry": str(expiries[0]),
            "last_expiry": str(expiries[-1]),
            "next_expiry": str(next_exp),
        },
        "profile": {
            "all": [round(float(x), 0) for x in profile],
            "ex_next_expiry": [round(float(x), 0) for x in profile_ex_next],
        },
        "by_strike": by_strike,
        "by_expiry": by_exp,
    }


def compute(raw: dict, ticker: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(NY)
    spot, ch = parse_chain(raw, now)

    views = {}
    for key, max_dte in HORIZONS.items():
        mask = np.ones(ch["dte"].size, bool) if max_dte is None else ch["dte"] <= max_dte
        if mask.any():
            views[key] = compute_view(subset(ch, mask), spot)

    main = views["all"]
    return {
        "ticker": ticker.upper(),
        "source": "CBOE delayed quotes",
        "cboe_timestamp": raw.get("timestamp"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spot": round(spot, 2),
        "prev_close": raw["data"].get("prev_day_close"),
        "profile_levels": [round(float(x), 2) for x in profile_levels(spot)],
        "horizons": {k: ("Todos" if v is None else f"≤ {v} dias") for k, v in HORIZONS.items() if k in views},
        # atalhos para o horizonte "todos" (compatibilidade)
        "levels": main["levels"],
        "totals": main["totals"],
        "views": views,
        "params": {"risk_free": RISK_FREE, "div_yield": DIV_YIELD, "units": "USD por 1% de movimento"},
    }


def update_history(path: Path, result: dict) -> None:
    history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    day = datetime.now(NY).date().isoformat()
    entry = {
        "date": day,
        "generated_at": result["generated_at"],
        "spot": result["spot"],
        "views": {
            k: {**v["levels"], "net_gex": v["totals"]["net_gex"]} for k, v in result["views"].items()
        },
    }
    history = [h for h in history if h.get("date") != day] + [entry]  # 1 snapshot por dia (o último)
    path.write_text(json.dumps(history[-HISTORY_MAX:], indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="SPX")
    ap.add_argument("--input", help="JSON da CBOE já descarregado (opcional)")
    ap.add_argument("--out-dir", default="data")
    args = ap.parse_args()

    raw = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else fetch_cboe(args.ticker)
    result = compute(raw, args.ticker)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t = args.ticker.lower()
    (out / f"{t}_latest.json").write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    update_history(out / f"{t}_history.json", result)

    print(f"{result['ticker']} spot={result['spot']}")
    for key, v in result["views"].items():
        lv, tt = v["levels"], v["totals"]
        print(f"  [{result['horizons'][key]:>10}] GEX={tt['net_gex'] / 1e9:+7.2f}B  regime={tt['regime']:8s} "
              f"CallWall={lv['call_wall']:<8} PutWall={lv['put_wall']:<8} VolTrigger={lv['vol_trigger']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

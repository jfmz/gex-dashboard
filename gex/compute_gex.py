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
<=7 dias).

Instrumentos agregados (ex.: SPX + SPY + XSP): as opções dos ETFs são
convertidas para a escala do índice base pela razão de preços r = S_base/S_etf
(strike' = strike*r, multiplicador' = 100/r). O GEX em dólares por 1% é
invariante a esta conversão, por isso as parcelas podem ser somadas.

Fonte: cotações atrasadas (15 min) da CBOE, gratuitas e sem API key.

Convenção (padrão de mercado): os dealers estão LONG calls e SHORT puts,
logo GEX das calls é positivo e das puts é negativo.

    GEX ($ por 1% de movimento) = gamma * OI * multiplicador * S^2 * 0.01

Uso:
    python gex/compute_gex.py --all                     # todos os de gex/instruments.json
    python gex/compute_gex.py --ticker SPX              # um ticker avulso
    python gex/compute_gex.py --ticker SPX --input raw.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time as _time
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
CONFIG_PATH = Path(__file__).with_name("instruments.json")

RISK_FREE = 0.04          # taxa de juro anual (aprox.)
DEFAULT_DIV = 0.012       # dividend yield por defeito (sobreposto por instruments.json)
MIN_T = 1.0 / (365 * 24)  # 1 hora: evita gamma infinito em 0DTE perto do fecho
PROFILE_RANGE = 0.15      # perfil de gamma de -15% a +15% do spot
PROFILE_POINTS = 301
PROFILE_CHUNK = 25        # nº de níveis por bloco no perfil (limita memória)
STRIKE_RANGE = 0.25       # strikes exportados: +/- 25% do spot
HISTORY_MAX = 750         # nº máximo de snapshots diários guardados

# Horizontes de vencimento: chave -> nº máximo de dias de calendário até ao vencimento
HORIZONS: dict[str, int | None] = {"all": None, "90": 90, "30": 30, "7": 7}

SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")


# --------------------------------------------------------------------------- #
# Dados
# --------------------------------------------------------------------------- #
def fetch_cboe(ticker: str, retries: int = 3) -> dict:
    symbol = f"_{ticker}" if ticker.upper() in INDEX_TICKERS else ticker.upper()
    req = urllib.request.Request(
        CBOE_URL.format(symbol=symbol),
        headers={"User-Agent": "Mozilla/5.0 (gex-dashboard)", "Accept": "application/json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except Exception:
            if attempt == retries - 1:
                raise
            _time.sleep(3 * (attempt + 1))


def spot_of(raw: dict) -> float:
    d = raw["data"]
    return float(d.get("current_price") or d.get("close"))


def ratio_price(raw: dict) -> float:
    """Preço usado na razão índice/ETF dos agregados. Os ETFs negoceiam after-hours
    (current_price muda) mas os índices não; 'close' é o último preço da sessão
    regular em ambos, o que mantém a razão sincronizada fora de horas."""
    d = raw["data"]
    return float(d.get("close") or d.get("current_price"))


def parse_chain(raw: dict, now: datetime, div_yield: float, ratio: float = 1.0, source: str = "") -> dict:
    """Arrays numpy com as opções úteis, já na escala da base (strike*ratio, mult=100/ratio)."""
    today = now.date()
    rows = []
    for opt in raw["data"]["options"]:
        m = SYMBOL_RE.match(opt.get("option", ""))
        if not m:
            continue
        oi = float(opt.get("open_interest") or 0)
        if oi <= 0:
            continue
        exp_date = datetime.strptime(m["exp"], "%y%m%d").date()
        # Mensais de índice (roots SPX, NDX, RUT) liquidam na abertura (AM);
        # semanais (SPXW, NDXP, RUTW), ETFs e ações ao fecho (PM)
        settle = time(9, 30) if m["root"] in AM_SETTLED_ROOTS else time(16, 0)
        t_years = (datetime.combine(exp_date, settle, tzinfo=NY) - now).total_seconds() / (365 * 86400)
        if t_years <= 0:
            continue  # já expirou
        rows.append((
            float(m["strike"]) / 1000.0 * ratio,
            1.0 if m["type"] == "C" else -1.0,
            oi,
            float(opt.get("gamma") or 0) / ratio,
            float(opt.get("iv") or 0),
            max(t_years, MIN_T),
            exp_date.isoformat(),
            (exp_date - today).days,
        ))
    if not rows:
        raise RuntimeError(f"{source}: nenhuma opção válida nos dados da CBOE")

    strike, sign, oi, gamma, iv, t, exp, dte = zip(*rows)
    n = len(rows)
    return {
        "strike": np.array(strike), "sign": np.array(sign), "oi": np.array(oi),
        "gamma": np.array(gamma), "iv": np.array(iv), "t": np.array(t),
        "exp": np.array(exp), "dte": np.array(dte),
        "mult": np.full(n, CONTRACT_SIZE / ratio), "q": np.full(n, div_yield),
        "src": np.full(n, source),
    }


def concat(chains: list[dict]) -> dict:
    return {k: np.concatenate([c[k] for c in chains]) for k in chains[0]}


def subset(chain: dict, mask: np.ndarray) -> dict:
    return {k: v[mask] for k, v in chain.items()}


# --------------------------------------------------------------------------- #
# Cálculos
# --------------------------------------------------------------------------- #
def bs_gamma(S, K, iv, t, q):
    """Gamma Black-Scholes com dividend yield. S pode ter shape (m,1) para broadcast."""
    sqrt_t = np.sqrt(t)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        d1 = (np.log(S / K) + (RISK_FREE - q + 0.5 * iv**2) * t) / (iv * sqrt_t)
        g = np.exp(-q * t) * np.exp(-0.5 * d1**2) / (np.sqrt(2 * np.pi) * S * iv * sqrt_t)
    return np.nan_to_num(g, nan=0.0, posinf=0.0, neginf=0.0)


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
    out = np.zeros_like(levels)
    if not ok.any():
        return out
    K, iv, t, q = c["strike"][ok], c["iv"][ok], c["t"][ok], c["q"][ok]
    w = (c["sign"] * c["oi"] * c["mult"])[ok]
    for i in range(0, levels.size, PROFILE_CHUNK):
        L = levels[i:i + PROFILE_CHUNK]
        g = bs_gamma(L[:, None], K, iv, t, q)
        out[i:i + PROFILE_CHUNK] = (g * w).sum(axis=1) * L**2 * 0.01
    return out


def rnd(x, nd: int = 2):
    return None if x is None else round(float(x), nd)


def compute_view(ch: dict, spot: float, bucket: float | None) -> dict:
    """Todas as métricas para um subconjunto de opções (um horizonte de vencimentos)."""
    # A CBOE arredonda o gamma a 4 casas decimais (ex.: 0.0006), o que gera erros
    # relevantes. Recalculamos com Black-Scholes a partir da IV; sem IV usamos o da CBOE.
    gamma = np.where(ch["iv"] > 0, bs_gamma(spot, ch["strike"], ch["iv"], ch["t"], ch["q"]), ch["gamma"])
    gex = ch["sign"] * gamma * ch["oi"] * ch["mult"] * spot**2 * 0.01
    is_call = ch["sign"] > 0
    # contratos equivalentes na escala da base (ex.: 10 SPY ≈ 1 SPX)
    oi_eq = ch["oi"] * ch["mult"] / CONTRACT_SIZE

    # --- Agregação por strike (com agrupamento opcional para agregados) ---- #
    key = np.round(ch["strike"] / bucket) * bucket if bucket else ch["strike"]
    strikes = np.unique(key)
    idx = np.searchsorted(strikes, key)
    agg = lambda w: np.bincount(idx, weights=w, minlength=strikes.size)
    call_gex, put_gex = agg(np.where(is_call, gex, 0)), agg(np.where(~is_call, gex, 0))
    call_oi, put_oi = agg(np.where(is_call, oi_eq, 0)), agg(np.where(~is_call, oi_eq, 0))
    net_gex = call_gex + put_gex

    # Call Wall em strikes >= spot (resistência) e Put Wall em strikes <= spot (suporte)
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
            "strike": round(float(strikes[i]), 2),
            "call_gex": round(float(call_gex[i]), 0),
            "put_gex": round(float(put_gex[i]), 0),
            "net_gex": round(float(net_gex[i]), 0),
            "call_oi": int(round(call_oi[i])),
            "put_oi": int(round(put_oi[i])),
        }
        for i in np.where(in_range)[0]
        if call_oi[i] + put_oi[i] > 0
    ]

    # --- Contribuição de cada componente (agregados) ----------------------- #
    gross = np.abs(gex).sum()
    components = {
        str(s): {
            "net_gex": round(float(gex[ch["src"] == s].sum()), 0),
            "gross_share": round(float(np.abs(gex[ch["src"] == s]).sum() / gross), 4) if gross else None,
        }
        for s in np.unique(ch["src"])
    }

    total_gex = float(gex.sum())
    tot_call_oi, tot_put_oi = float(oi_eq[is_call].sum()), float(oi_eq[~is_call].sum())
    positive = (spot > flip) if flip is not None else (total_gex > 0)

    return {
        "levels": {
            "call_wall": rnd(call_wall),
            "put_wall": rnd(put_wall),
            "vol_trigger": rnd(flip),
            "vol_trigger_ex_next_expiry": rnd(flip_ex_next),
            "max_abs_gex_strike": rnd(strikes[np.argmax(np.abs(net_gex))]),
            "call_wall_any_strike": rnd(strikes[np.argmax(call_gex)]),
            "put_wall_any_strike": rnd(strikes[np.argmin(put_gex)]),
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
        "components": components,
        "profile": {
            "all": [round(float(x), 0) for x in profile],
            "ex_next_expiry": [round(float(x), 0) for x in profile_ex_next],
        },
        "by_strike": by_strike,
        "by_expiry": by_exp,
    }


def compute_instrument(inst: dict, raws: dict[str, dict], divs: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(NY)
    comps = inst["components"]
    base = comps[0]
    base_spot = spot_of(raws[base])

    chains, comp_info = [], []
    base_ref = ratio_price(raws[base])
    for c in comps:
        s = spot_of(raws[c])
        ratio = 1.0 if c == base else base_ref / ratio_price(raws[c])
        chains.append(parse_chain(raws[c], now, divs.get(c, DEFAULT_DIV), ratio, c))
        comp_info.append({"symbol": c, "spot": round(s, 2), "ratio_price": round(ratio_price(raws[c]), 4),
                          "ratio": round(ratio, 4), "cboe_timestamp": raws[c].get("timestamp")})
    ch = concat(chains)

    views = {}
    for key, max_dte in HORIZONS.items():
        mask = np.ones(ch["dte"].size, bool) if max_dte is None else ch["dte"] <= max_dte
        if mask.any():
            views[key] = compute_view(subset(ch, mask), base_spot, inst.get("bucket"))

    main = views["all"]
    return {
        "id": inst["id"],
        "ticker": inst.get("label", base),
        "group": inst.get("group"),
        "base": base,
        "components": comp_info,
        "note": inst.get("note"),
        "default_range": inst.get("range", 8),
        "source": "CBOE delayed quotes",
        "cboe_timestamp": raws[base].get("timestamp"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spot": round(base_spot, 2),
        "prev_close": raws[base]["data"].get("prev_day_close"),
        "profile_levels": [round(float(x), 2) for x in profile_levels(base_spot)],
        "horizons": {k: ("Todos" if v is None else f"≤ {v} dias") for k, v in HORIZONS.items() if k in views},
        "levels": main["levels"],   # atalhos para o horizonte "todos"
        "totals": main["totals"],
        "views": views,
        "params": {"risk_free": RISK_FREE, "div_yield": {c: divs.get(c, DEFAULT_DIV) for c in comps},
                   "units": "USD por 1% de movimento", "bucket": inst.get("bucket")},
    }


# --------------------------------------------------------------------------- #
# Saída
# --------------------------------------------------------------------------- #
def update_history(path: Path, result: dict) -> None:
    history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    day = datetime.now(NY).date().isoformat()
    entry = {
        "date": day,
        "generated_at": result["generated_at"],
        "spot": result["spot"],
        "views": {k: {**v["levels"], "net_gex": v["totals"]["net_gex"]} for k, v in result["views"].items()},
    }
    history = [h for h in history if h.get("date") != day] + [entry]  # 1 snapshot por dia (o último)
    path.write_text(json.dumps(history[-HISTORY_MAX:], indent=1), encoding="utf-8")


def write_result(out: Path, result: dict) -> None:
    i = result["id"]
    (out / f"{i}_latest.json").write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    update_history(out / f"{i}_history.json", result)


def write_manifest(out: Path, config: dict) -> None:
    """Lista de instrumentos disponíveis, lida pelo dashboard para construir o seletor."""
    items = []
    for inst in config["instruments"]:
        f = out / f"{inst['id']}_latest.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        items.append({"id": inst["id"], "label": inst.get("label"), "group": inst.get("group"),
                      "components": inst["components"], "generated_at": d.get("generated_at")})
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "instruments": items, "notes": config.get("notes", {})}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")


def summary(result: dict) -> str:
    lines = [f"{result['ticker']} ({'+'.join(c['symbol'] for c in result['components'])}) spot={result['spot']}"]
    for key, v in result["views"].items():
        lv, tt = v["levels"], v["totals"]
        lines.append(f"  [{result['horizons'][key]:>10}] GEX={tt['net_gex'] / 1e9:+8.3f}B  regime={tt['regime']:8s} "
                     f"CallWall={lv['call_wall']:<9} PutWall={lv['put_wall']:<9} VolTrigger={lv['vol_trigger']}")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # consola Windows (cp1252) não tem "≤"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="calcular todos os instrumentos de instruments.json")
    ap.add_argument("--ticker", help="calcular um único ticker (ex.: SPX)")
    ap.add_argument("--input", help="JSON da CBOE já descarregado (só com --ticker)")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--out-dir", default="data")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    divs = config.get("dividend_yield", {})
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if args.ticker:
        t = args.ticker.upper()
        inst = next((i for i in config["instruments"] if i["components"] == [t]),
                    {"id": t.lower(), "label": t, "components": [t]})
        raw = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else fetch_cboe(t)
        result = compute_instrument(inst, {t: raw}, divs)
        write_result(out, result)
        print(summary(result))
        return 0

    if not args.all:
        ap.error("use --all ou --ticker")

    cache: dict[str, dict] = {}
    failed = []
    for inst in config["instruments"]:
        try:
            for c in inst["components"]:
                if c not in cache:
                    cache[c] = fetch_cboe(c)
            result = compute_instrument(inst, {c: cache[c] for c in inst["components"]}, divs)
            write_result(out, result)
            print(summary(result))
        except Exception as e:  # um instrumento falhado não impede os outros
            failed.append(inst["id"])
            print(f"::warning::Falhou {inst['id']}: {type(e).__name__}: {e}")
    write_manifest(out, config)
    if failed:
        print(f"Falharam: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

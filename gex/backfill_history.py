#!/usr/bin/env python3
"""
Preenche histórico inicial no data/{id}_history.json para o gráfico
Gamma Throttle vs. 10-Day Realized Volatility.

Modos disponíveis:
  1. warmup (defeito): adiciona apenas os 12 fechos anteriores do subjacente
     antes da primeira data gravada, desbloqueando a 10D rVol para TODAS as
     sessões reais da CBOE já existentes no ficheiro (zero GEX estimado).
  2. proxy: preenche os últimos N dias úteis (ex.: 130 sessões = ~6 meses)
     com uma estimativa calibrada a partir do SPX e VIX (marcada com
     "estimated": true), preservando intactos todos os dias reais da CBOE.

Uso:
    python gex/backfill_history.py --mode warmup
    python gex/backfill_history.py --mode proxy --days 130
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import yfinance as yf


def fetch_closes(ticker: str, days_back: int) -> dict[str, float]:
    start = (datetime.now(timezone.utc) - timedelta(days=int(days_back * 1.7) + 25)).strftime("%Y-%m-%d")
    df = yf.Ticker(ticker).history(start=start, interval="1d")
    if df.empty:
        raise RuntimeError(f"Sem dados históricos no Yahoo Finance para {ticker}")
    return {idx.strftime("%Y-%m-%d"): round(float(val), 2) for idx, val in df["Close"].dropna().items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", default="data/spx_history.json", help="Caminho para o ficheiro *_history.json")
    ap.add_argument("--spot-ticker", default="^GSPC", help="Ticker Yahoo Finance do subjacente (ex.: ^GSPC, ^NDX)")
    ap.add_argument("--mode", choices=["warmup", "proxy"], default="warmup", help="Modo de preenchimento")
    ap.add_argument("--days", type=int, default=130, help="Nº de sessões a preencher no modo proxy (~130 = 6 meses)")
    args = ap.parse_args()

    path = Path(args.history)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    # Mantém apenas os registos reais (não estimados) como âncora
    real_by_date = {row["date"]: row for row in existing if not row.get("estimated") and row.get("views")}
    real_dates = sorted(real_by_date.keys())

    if not real_dates:
        raise RuntimeError(f"{path} não tem sessões reais. Corre primeiro o compute_gex.py.")

    first_real_date = real_dates[0]
    lookback = 25 if args.mode == "warmup" else args.days + 20
    spot_map = fetch_closes(args.spot_ticker, lookback)
    all_dates = sorted(d for d in spot_map.keys() if d < first_real_date)

    if args.mode == "warmup":
        # Apenas os 12 dias úteis anteriores ao primeiro snapshot real
        target_dates = all_dates[-12:]
        new_entries = [
            {
                "date": d,
                "generated_at": f"{d}T21:00:00+00:00",
                "spot": spot_map[d],
                "estimated": True,
                "views": {},  # Sem net_gex: serve apenas para calcular a janela de 10D rVol
            }
            for d in target_dates
        ]
    else:
        # Modo proxy: estima Net GEX a partir do VIX e do desvio à média móvel de 20 dias
        vix_map = fetch_closes("^VIX", lookback)
        target_dates = all_dates[-args.days :]

        # Calibra a escala com base na mediana real do ficheiro
        real_throttles = [
            abs(float(r["views"]["all"]["net_gex"])) / (float(r["spot"]) * 1e6)
            for r in real_by_date.values()
            if "all" in r.get("views", {})
        ]
        scale_ref = float(np.median(real_throttles)) if real_throttles else 6.0

        rng = np.random.default_rng(42)
        new_entries = []
        spots_list = [spot_map[d] for d in all_dates]

        for d in target_dates:
            idx = all_dates.index(d)
            s = spot_map[d]
            vix = vix_map.get(d, 18.0)
            ma20 = float(np.mean(spots_list[max(0, idx - 19) : idx + 1]))
            trend_pct = (s / ma20 - 1.0) * 100.0

            # Regime estrutural: VIX < 18.5 tende a long gamma (+); VIX > 18.5 tende a short gamma (-)
            raw_throttle = (18.5 - vix) * (scale_ref / 8.0) + trend_pct * 0.8 + float(rng.normal(0, 1.1))
            est_net_gex = round(raw_throttle * s * 1e6, 0)

            new_entries.append(
                {
                    "date": d,
                    "generated_at": f"{d}T21:00:00+00:00",
                    "spot": s,
                    "estimated": True,
                    "views": {
                        "all": {"net_gex": est_net_gex},
                        "90": {"net_gex": round(est_net_gex * 0.85, 0)},
                        "30": {"net_gex": round(est_net_gex * 0.65, 0)},
                        "7": {"net_gex": round(est_net_gex * 0.40, 0)},
                    },
                }
            )

    merged = sorted(new_entries + list(real_by_date.values()), key=lambda x: x["date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=1), encoding="utf-8")
    print(f"Atualizado {path}: {len(new_entries)} sessões históricas + {len(real_by_date)} sessões reais = {len(merged)} total.")


if __name__ == "__main__":
    main()

# Gamma Exposure Dashboard (SPX, NDX)

Pipeline em Python que corre no **GitHub Actions**, calcula métricas de posicionamento
de dealers no mercado de opções do SPX e grava o resultado em JSON. Um dashboard
HTML estático (servido pelo **GitHub Pages**) lê esses ficheiros e mostra tudo em gráficos.

```
CBOE (cotações atrasadas 15 min)
        │
        ▼
gex/compute_gex.py  ──►  data/spx_latest.json   (snapshot actual)
 (GitHub Actions,         data/spx_history.json  (1 snapshot por dia)
  de 30 em 30 min)
        │
        ▼
index.html  (GitHub Pages, Chart.js)
```

## Métricas

| Métrica | Como é calculada |
|---|---|
| **GEX por strike** | `gamma × OI × 100 × S² × 1%` em $ por 1% de movimento. Calls `+`, puts `−` (convenção: dealers long calls, short puts). Gamma Black-Scholes a partir da IV da CBOE (o gamma da CBOE vem arredondado a 4 casas). |
| **Net GEX** | Soma de todos os strikes e vencimentos. Positivo → dealers amortecem a volatilidade; negativo → amplificam. |
| **Call Wall** | Strike ≥ spot com maior GEX de calls (resistência). |
| **Put Wall** | Strike ≤ spot com maior GEX de puts (suporte). O JSON também traz `*_any_strike`, sem esta restrição. |
| **Volatility Trigger** | *Gamma flip*: o GEX total é recalculado para 301 preços hipotéticos (−15% a +15%) e o trigger é onde a curva cruza zero. Acima: regime de gamma positivo; abaixo: negativo. Também é calculado sem o vencimento mais próximo (`vol_trigger_ex_next_expiry`). |

### Horizontes de vencimento

Todas as métricas são calculadas para quatro horizontes, escolhidos no dashboard em **Vencimentos**:

| Horizonte | Opções incluídas | Para quê |
|---|---|---|
| Todos | Todos os vencimentos com OI (0DTE até LEAPS) | Visão estrutural |
| ≤ 90 dias | Vencimento a 90 dias de calendário ou menos | Inclui os mensais/trimestrais principais |
| ≤ 30 dias | Até 30 dias | Swing de algumas semanas |
| ≤ 7 dias | Até 7 dias | Intraday / próximos dias |

No JSON ficam em `views.<all|90|30|7>`; `levels` e `totals` no topo correspondem a "Todos".
Os horizontes mudam-se em `HORIZONS` no topo do script.
O OI do 0DTE é o do início do dia, por isso os contratos abertos e fechados na própria sessão não entram.

> O "Volatility Trigger" de fornecedores comerciais é proprietário. Aqui é a aproximação
> habitual pelo gamma flip. Ferramenta informativa, não é recomendação de investimento.

## Publicar no GitHub

1. Crie um repositório novo no GitHub (pode ser público ou privado; o Pages gratuito requer público).
2. Envie estes ficheiros:
   ```bash
   git init
   git add .
   git commit -m "GEX dashboard"
   git branch -M main
   git remote add origin https://github.com/<utilizador>/<repo>.git
   git push -u origin main
   ```
3. **Settings → Actions → General → Workflow permissions** → *Read and write permissions*.
4. **Settings → Pages** → *Deploy from a branch* → `main` / `(root)`.
5. **Actions → Atualizar GEX → Run workflow** para o primeiro cálculo.
   Depois corre sozinho de 30 em 30 min nos dias úteis (13:00–21:30 UTC).
6. O dashboard fica em `https://<utilizador>.github.io/<repo>/`.

## Correr localmente

```bash
pip install -r requirements.txt
python gex/compute_gex.py --ticker SPX
python -m http.server 8000
```
Abra `http://localhost:8000`. (Abrir o `index.html` com duplo-clique não funciona: o browser
bloqueia `fetch` de ficheiros locais.)

## Adicionar outros tickers

SPX e NDX já estão incluídos. O script aceita outros (`--ticker RUT`, `--ticker SPY`, …).
Para os incluir: acrescente o ticker ao ciclo `for t in SPX NDX` no workflow e uma
`<option value="rut">RUT</option>` no `<select id="tickerSel">` do `index.html`.

## Parâmetros

No topo de `gex/compute_gex.py`: taxa de juro (`RISK_FREE`), dividend yield (`DIV_YIELD`),
intervalo do perfil (`PROFILE_RANGE`), intervalo de strikes exportados (`STRIKE_RANGE`).

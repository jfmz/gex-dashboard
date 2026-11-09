# Gamma Exposure Dashboard

Pipeline em Python que corre no **GitHub Actions**, calcula métricas de posicionamento
de dealers no mercado de opções e grava o resultado em JSON. Um dashboard HTML estático
(servido pelo **GitHub Pages**) lê esses ficheiros e mostra tudo em gráficos.

Dashboard: https://jfmz.github.io/gex-dashboard/

```
CBOE (cotações atrasadas 15 min)
        │
        ▼
gex/compute_gex.py --all  ──►  data/<id>_latest.json   (snapshot actual)
 (GitHub Actions,               data/<id>_history.json  (1 snapshot por dia)
  de 30 em 30 min)              data/manifest.json      (lista lida pelo dashboard)
        │
        ▼
index.html  (GitHub Pages, Chart.js)
```

## Instrumentos

Definidos em [`gex/instruments.json`](gex/instruments.json):

| Grupo | Instrumentos |
|---|---|
| Índices | SPX, NDX, RUT |
| Agregados | **S&P agregado** (SPX + SPY + XSP), **Nasdaq agregado** (NDX + QQQ) |
| ETFs | IWM, GLD |
| Megacaps | NVDA, TSLA, AAPL, MSFT, AMZN, META |

### Como funcionam os agregados

Os ETFs são convertidos para a escala do índice base pela razão de preços `r = S_índice / S_ETF`
(calculada em cada execução, ex.: SPX/SPY ≈ 10,03; NDX/QQQ ≈ 41,07):

- strike convertido = strike do ETF × r
- multiplicador = 100 / r (1 contrato SPY ≈ 0,1 contrato SPX)

O GEX em $ por 1% não muda com esta conversão, por isso as parcelas somam-se diretamente
(ex.: S&P agregado = SPX + SPY + XSP). Os strikes convertidos são agrupados em intervalos
(`bucket`: 5 pontos no S&P, 25 no Nasdaq) para as walls e os gráficos. O dashboard mostra o
peso de cada componente no gamma bruto.

### Ações individuais

Os dados são bons, mas a convenção "dealers long calls / short puts" é menos fiável do que nos
índices (muito retalho compra calls). O dashboard mostra este aviso. As walls e a concentração
de OI são mais fiáveis do que o sinal do GEX.

### Adicionar ou remover instrumentos

Edite `gex/instruments.json`; o workflow e o seletor do dashboard adaptam-se sozinhos.

```json
{"id": "smh", "label": "SMH", "group": "ETFs", "components": ["SMH"], "range": 12}
```

Para um agregado, liste vários `components` (o primeiro é a base) e defina `bucket`.
Acrescente o dividend yield em `dividend_yield` (valores aproximados).

## Métricas

| Métrica | Como é calculada |
|---|---|
| **GEX por strike** | `gamma × OI × multiplicador × S² × 1%` em $ por 1% de movimento. Calls `+`, puts `−` (convenção: dealers long calls, short puts). Gamma Black-Scholes a partir da IV da CBOE (o gamma da CBOE vem arredondado a 4 casas). |
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

1. Crie um repositório novo no GitHub (o Pages gratuito requer repositório público).
2. Envie estes ficheiros (`git init`, `git add .`, `git commit`, `git push`).
3. **Settings → Actions → General → Workflow permissions** → *Read and write permissions*.
4. **Settings → Pages** → *Deploy from a branch* → `main` / `(root)`.
5. **Actions → Atualizar GEX → Run workflow** para o primeiro cálculo.
   Depois corre sozinho de 30 em 30 min nos dias úteis (13:00–21:30 UTC).

## Correr localmente

```bash
pip install -r requirements.txt
python gex/compute_gex.py --all
python -m http.server 8000
```
Abra `http://localhost:8000`. (Abrir o `index.html` com duplo-clique não funciona: o browser
bloqueia `fetch` de ficheiros locais.) Para um ticker avulso: `python gex/compute_gex.py --ticker SMH`.

## Parâmetros

No topo de `gex/compute_gex.py`: taxa de juro (`RISK_FREE`), horizontes (`HORIZONS`),
intervalo do perfil (`PROFILE_RANGE`), intervalo de strikes exportados (`STRIKE_RANGE`).
Dividend yields por ticker em `gex/instruments.json`.

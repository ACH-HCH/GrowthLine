# Paper trading con Claude

Simulatore di trading azionario **con soldi finti ma condizioni realistiche**, pensato per rispondere a una sola domanda:

> Un portafoglio gestito da Claude batte, al netto di tutti i costi, il semplice acquisto e mantenimento di un ETF?

Se dopo mesi di simulazione la risposta è no, non ha senso metterci soldi veri.

## Cosa rende la simulazione realistica

| Aspetto | Come è simulato |
|---|---|
| Prezzi | Reali, da Yahoo Finance (gratuiti, ritardati di circa 15 minuti) |
| Esecuzione | Solo a mercato aperto. Gli ordini dati a mercato chiuso vanno in coda e vengono eseguiti al prezzo della prossima apertura, non a quello vecchio |
| Commissioni | 0,005 $ per azione, minimo 1 $, tetto 1% (simile a un broker online economico) |
| Slippage | Ogni esecuzione è peggiorata dello 0,05% (si compra un po' più caro, si vende un po' più basso) |
| Azioni intere | Niente frazioni, salvo che tu le abiliti |
| Tasse | Stima del 26% sulle plusvalenze nel report |
| Costo di Claude | Ogni chiamata API viene contabilizzata e sottratta dal rendimento |
| Benchmark | Il giorno di partenza un conto "gemello" compra l'ETF (SPY) con lo stesso capitale e gli stessi costi, poi non fa più nulla |

## Le regole di sicurezza sono nel codice, non nel prompt

Claude può solo **proporre** ordini. È il codice (`papertrader/engine.py`) a decidere se eseguirli:

- si comprano solo i titoli dell'universo configurato;
- solo posizioni lunghe, niente leva né vendite allo scoperto;
- massimo 10% del conto per ordine, 20% per titolo e 5% di liquidità minima;
- al massimo 5 ordini per esecuzione;
- stop-loss automatico al -15% dal prezzo di carico;
- se il conto perde il 20% dal massimo, il trading viene sospeso finché non lo riattivi tu (`resume`);
- c'è un tetto di spesa API per ogni esecuzione.

Se un ordine viola un limite viene rifiutato e Claude riceve il motivo e la quantità massima ammessa.

## Installazione

Serve Python 3.11 o successivo e una chiave API Anthropic (da https://console.anthropic.com).

```bash
cd paper-trading-bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."   # Windows: set ANTHROPIC_API_KEY=...
cp config.example.toml config.toml      # poi modifica capitale, universo, strategia, limiti
python -m papertrader init
```

## Uso

```bash
python -m papertrader run            # un ciclo completo: prezzi, ordini in coda, stop-loss, decisione di Claude
python -m papertrader run --no-agent # aggiorna prezzi e ordini senza chiamare Claude (gratis)
python -m papertrader status         # portafoglio attuale
python -m papertrader report         # confronto con il benchmark
python -m papertrader resume         # riattiva il trading dopo una sospensione
python -m papertrader init --force   # ricomincia da zero (cancella i dati)
```

Ad ogni `run`, Claude riceve il portafoglio, gli eventi automatici e le sue ultime 5 decisioni (per evitare che cambi idea ogni giorno). Poi usa gli strumenti a disposizione (quotazioni, storico con indicatori tecnici, fondamentali, notizie, invio ordini) e chiude con un breve resoconto in italiano.

### Esecuzione automatica una volta al giorno

Il bot va eseguito **a mercato aperto**. Per la borsa USA, le 17:30 ora italiana dei giorni feriali sono sempre in orario, anche nelle settimane in cui Europa e USA cambiano l'ora legale in date diverse.

Su Linux o macOS, con `crontab -e`:

```cron
30 17 * * 1-5 cd /percorso/paper-trading-bot && ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python -m papertrader run >> data/cron.log 2>&1
```

Su Windows si usa l'Utilità di pianificazione con lo stesso comando. Il computer deve essere acceso a quell'ora; in alternativa va bene un piccolo server sempre attivo.

## Dove finiscono i dati

Tutto è salvato nella cartella `data/`:

- `state.json`: cassa, posizioni, ordini in coda, costi accumulati;
- `trades.jsonl`: ogni operazione eseguita, con prezzo, commissione e motivazione;
- `journal.jsonl`: ogni esecuzione di Claude, con resoconto, ordini, token usati e costo;
- `equity.jsonl`: il valore del conto e del benchmark a ogni esecuzione.

## Come valutare se andare avanti

1. **Durata.** Lascialo girare almeno 3 mesi, meglio 6-12. Il report avvisa sotto i 60 giorni di borsa: prima di allora il risultato è quasi solo fortuna.
2. **Confronto giusto.** Conta la riga "Netto stimato se liquidassi oggi", che include costi API e tasse. Se il bot non batte il benchmark, conviene comprare l'ETF.
3. **Rischio.** Un rendimento più alto con un drawdown molto peggiore non è per forza un risultato migliore.
4. **Lettura delle operazioni.** Apri `trades.jsonl` e `journal.jsonl`: se il guadagno viene da una o due operazioni fortunate, non è una strategia.
5. **Costi API.** Con le impostazioni predefinite (Claude Opus 5.5, effort `medium`) un'esecuzione costa indicativamente qualche decina di centesimi di dollaro. Il costo reale è nel report. Su un conto da 10.000 $, 0,30 $ al giorno sono circa 75 $ l'anno, cioè lo 0,75%: il bot deve guadagnare almeno questo solo per pareggiare.

## Limiti noti della simulazione

- Gli ordini a mercato vengono eseguiti per intero all'ultimo prezzo (ritardato) più lo slippage. Non ci sono esecuzioni parziali né ordini limite.
- I dividendi non vengono accreditati, né al bot né al benchmark. Il confronto resta equo, ma i rendimenti assoluti sono leggermente sottostimati.
- Tutti i titoli devono essere nella stessa valuta del conto. Non sono simulati il cambio EUR/USD per un investitore italiano né i suoi costi.
- La stima fiscale è semplificata (aliquota unica sul guadagno complessivo, nessuna distinzione tra redditi diversi e di capitale). Per il caso reale serve un commercialista.
- Le festività di borsa vengono riconosciute solo in modo indiretto (prezzo non aggiornato = mercato chiuso).
- Yahoo Finance non è una fonte ufficiale e a volte restituisce dati mancanti o errati.

## Passare ai soldi veri (solo se i risultati lo giustificano)

Il passo successivo non sono subito i soldi veri, ma il **conto demo di un broker vero** (per esempio Interactive Brokers o Alpaca), sostituendo il broker simulato in `engine.py` con le loro API. Solo dopo ha senso un capitale piccolo, che puoi permetterti di perdere, tenendo gli stessi limiti di rischio.

## Test

```bash
pip install pytest
python -m pytest -q
```

I test usano prezzi finti e un client Claude simulato: non servono rete né chiave API.

---

*Questo progetto è uno strumento di studio e simulazione, non una consulenza finanziaria.*

# 🧭 Concentrazione Portafoglio

Script che individua le **concentrazioni nascoste** in un portafoglio di azioni ed ETF, usando la correlazione dei rendimenti storici (dati Yahoo Finance).

Risponde a domande come:
- Quanti titoli "indipendenti" ho davvero, rispetto a quanti ne possiedo?
- Ci sono due ETF o azioni che di fatto fanno la stessa cosa (duplicati)?
- Quali posizioni generano la maggior parte del rischio, a prescindere dai soldi investiti?

Il portafoglio viene letto da **Firebase (Firestore)**, nella collection `portfolio`. Il peso di ogni titolo è calcolato in automatico come **quantità × ultimo prezzo**, convertito in EUR.

---

## ⚙️ Configurazione

### 1. Installa le dipendenze
```bash
pip install yfinance pandas numpy scipy matplotlib firebase-admin python-dotenv
```

### 2. Crea la chiave del service account Firebase
1. Nella [console Firebase](https://console.firebase.google.com) apri il progetto → **Impostazioni progetto** → **Account di servizio**
2. Clicca **Genera nuova chiave privata** e scarica il file JSON
3. **Non committarlo mai nel repository** (aggiungilo al `.gitignore`): chi lo possiede ha accesso al database

### 3. Crea il file `.env`
```env
FIREBASE_CREDENTIALS=percorso/al/serviceAccount.json
```
In alternativa, per ambienti senza file (CI), puoi usare `FIREBASE_CREDENTIALS_JSON` con l'intero contenuto del JSON. Se presente, ha la precedenza.

### 4. Inserisci le posizioni su Firestore
Collection `portfolio`, **un documento per posizione**:

| Campo | Tipo | Descrizione |
|---|---|---|
| `ticker` | string | Ticker Yahoo Finance, es. `MWRD.MI` |
| `quantita` | number | Numero di quote/azioni possedute (deve essere > 0) |
| `currency` | string, opzionale | Valuta di quotazione su Yahoo. Se manca vale `EUR` |

Esempio:
```
ticker:   "AAPL"
quantita: 3
currency: "USD"
```

Note:
- Se lo stesso ticker compare in più documenti, le **quantità vengono sommate**.
- I documenti senza `ticker`, o con `quantita` mancante o ≤ 0, vengono ignorati con un avviso.
- Verifica sempre i ticker su Yahoo (suffisso di borsa e quotazione giusta): un ticker sbagliato falsa l'analisi.

---

## ▶️ Uso

```bash
python concentrazione_portafoglio.py                      # legge il portafoglio da Firestore
python concentrazione_portafoglio.py --anni 5 --soglia 0.9
python concentrazione_portafoglio.py --coppia SWDA.MI MWRD.MI   # diagnostica di una coppia
```

| Opzione | Default | Descrizione |
|---|---|---|
| `--anni` | `3` | Anni di storico da scaricare |
| `--soglia` | `0.9` | Correlazione oltre cui due titoli sono segnalati come probabili duplicati |
| `--out` | `concentrazione.png` | File del grafico generato |
| `--outlier` | `0.10` | Scarto dalla mediana vicina oltre cui un prezzo viene considerato errato e rimosso (`0` = disattivo) |
| `--coppia T1 T2` | – | Diagnostica di due ticker (vedi sotto) |

---

## 💱 Valute

Per i titoli non quotati in EUR, i prezzi vengono convertiti in euro con il cambio storico di Yahoo (`USDEUR=X`, `CHFEUR=X`, ...). La conversione riguarda **l'intera serie storica**, quindi pesi, rendimenti e correlazioni sono tutti in EUR: per chi investe in euro, un titolo in dollari si muove anche col cambio, e questo conta per il rischio.

- **Pence di Londra:** Yahoo quota molti titoli `.L` in `GBp` (pence). Se scrivi `GBp` (o `GBX`) lo script divide per 100 e poi converte da GBP. Scrivere `GBP` quando il prezzo è in pence darebbe un peso 100 volte troppo alto.
- **Controllo di coerenza:** lo script confronta la valuta nel database con quella che Yahoo associa al ticker e avvisa se non coincidono.
- **Cambio non disponibile:** lo script si ferma con un errore chiaro invece di proseguire con numeri sbagliati.

---

## 📊 Cosa produce

**1. Tabella delle posizioni** con quantità, prezzo in EUR, valore in EUR e peso.

**2. Indicatori di concentrazione**

| Indicatore | Significato |
|---|---|
| **Numero effettivo** (1/HHI) | Quante posizioni "pesano" davvero. 13 titoli con pesi molto sbilanciati possono valere come 5 |
| **Correlazione media pesata** | Quanto si muovono insieme, in media, le coppie di titoli |
| **Diversification ratio** | Media pesata delle volatilità / volatilità del portafoglio. `1` = nessuna diversificazione, più è alto meglio è |
| **Quota del 1° fattore comune** | Quanta parte dei movimenti è spiegata da un unico fattore comune (tipicamente il mercato) |
| **Volatilità annualizzata** | Volatilità storica del portafoglio |

**3. Coppie più correlate**, con l'avviso `[!]` per quelle oltre `--soglia`: probabili duplicati o sovrapposizioni (es. due ETF sul mondo).

**4. Peso vs contributo al rischio**, per ciascun titolo:
- **peso** = dove sono i tuoi soldi;
- **contributo al rischio** = quanta parte dell'oscillazione del portafoglio viene da quel titolo. Dipende da peso, volatilità e correlazione con il resto; la somma fa 100%;
- **delta** (contributo − peso): positivo = il titolo concentra rischio, negativo = diversifica.

**5. Grafico** (`concentrazione.png`): a sinistra la matrice di correlazione riordinata per cluster, a destra peso e contributo al rischio a confronto.

---

## 🔍 Diagnostica di una coppia

```bash
python concentrazione_portafoglio.py --coppia SWDA.MI MWRD.MI
```

Utile quando due titoli che dovrebbero essere quasi identici (es. due ETF sullo stesso indice) risultano poco correlati. Mostra valuta e borsa di ciascun ticker, la correlazione a frequenza giornaliera, settimanale e mensile (prima e dopo la rimozione degli outlier) e i giorni con la maggiore differenza di rendimento, che spesso rivelano dati errati su Yahoo.

---

## ⚠️ Note e limiti
- Tutto è calcolato sui **rendimenti storici** (settimanali, ultimi 3 anni di default): descrive il passato e non prevede il futuro.
- Il peso usa l'**ultima chiusura disponibile**, non il prezzo in tempo reale.
- I ticker con meno del 70% di osservazioni vengono scartati. Con pochi dati, o con dati Yahoo sporchi su qualche titolo, i numeri possono muoversi parecchio.
- Il filtro outlier toglie i prezzi isolati che si discostano dalla mediana dei giorni vicini: un crollo reale di un solo giorno, seguito da pieno recupero, verrebbe rimosso. Controlla la lista stampata dallo script.
- L'analisi è puramente informativa e non costituisce consulenza finanziaria.

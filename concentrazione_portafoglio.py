"""
concentrazione_portafoglio.py
Individua concentrazioni in un portafoglio (azioni + ETF) usando la correlazione
dei rendimenti storici.

Il portafoglio viene letto dalla collection Firestore "portfolio", esempio:
    ticker:   "MWRD.MI"   (string, obbligatorio)
    quantita: 12.5        (number, obbligatorio: numero di quote/azioni possedute)
    currency: "EUR"       (string, opzionale, default EUR: valuta di quotazione su Yahoo)
Il peso di ogni titolo = quantita x ultimo prezzo di chiusura in EUR, normalizzato a somma 1.
I prezzi in altra valuta (es. USD, GBP, CHF) vengono convertiti in EUR con il cambio storico
di Yahoo ("USDEUR=X", ...), quindi anche i rendimenti e le correlazioni sono in EUR.
"GBp" (pence, tipico di Londra) viene diviso per 100 e trattato come GBP.
Se lo stesso ticker compare in piu' documenti, le quantita' vengono sommate.

Dipendenze:
    pip install yfinance pandas numpy scipy matplotlib firebase-admin python-dotenv

Variabili d'ambiente (.env):
    FIREBASE_CREDENTIALS=serviceAccount.json
    FIREBASE_CREDENTIALS_JSON={...contenuto del JSON...}   (alternativa, per CI)

Uso:
    python concentrazione_portafoglio.py                      # legge da Firestore
    python concentrazione_portafoglio.py --coppia SWDA.MI MWRD.MI   # diagnostica di una coppia
"""
import argparse
import json
import os

import firebase_admin
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from firebase_admin import credentials, firestore
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform

load_dotenv()

# --- CONFIGURAZIONE ---------------------------------------------------------
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS")
FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON")
COLLECTION = "portfolio"
CAMPO_TICKER = "ticker"
CAMPO_QUANTITA = "qt"
CAMPO_VALUTA = "currency"
VALUTA_BASE = "EUR"

FREQ = "W"          # rendimenti settimanali: meno rumore da fusi orari/orari di chiusura
MIN_COPERTURA = 0.7  # scarta ticker con meno del 70% di osservazioni


# --- FIRESTORE --------------------------------------------------------------
def init_firestore():
    if FIREBASE_CREDENTIALS_JSON:
        cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS_JSON))
    elif FIREBASE_CREDENTIALS:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS)
    else:
        raise RuntimeError(
            "Imposta FIREBASE_CREDENTIALS (percorso) o FIREBASE_CREDENTIALS_JSON (contenuto)."
        )
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def normalizza_valuta(v):
    """'eur' -> 'EUR'; i pence di Londra ('GBp', 'GBX') restano distinti da 'GBP'."""
    v = str(v or VALUTA_BASE).strip()
    return "GBp" if v in ("GBp", "GBX", "GBx") else v.upper()


def carica_portafoglio_firestore():
    """Legge la collection "portfolio": DataFrame indicizzato per ticker con quantita e valuta."""
    db = init_firestore()
    righe = []
    for doc in db.collection(COLLECTION).stream():
        d = doc.to_dict()
        t = str(d.get(CAMPO_TICKER, "")).strip().upper()
        if not t:
            print(f"[!] Documento {doc.id} senza campo '{CAMPO_TICKER}': ignorato")
            continue
        q = d.get(CAMPO_QUANTITA)
        if q is None or q <= 0:
            print(f"[!] {t} (documento {doc.id}): '{CAMPO_QUANTITA}' mancante o <= 0, ignorato")
            continue
        righe.append({"ticker": t, "quantita": float(q),
                      "valuta": normalizza_valuta(d.get(CAMPO_VALUTA))})

    if not righe:
        raise RuntimeError(
            f"Collection '{COLLECTION}' vuota o senza campi '{CAMPO_TICKER}'/'{CAMPO_QUANTITA}'."
        )
    df = pd.DataFrame(righe)
    diverse = df.groupby("ticker")["valuta"].nunique()
    for t in diverse[diverse > 1].index:
        print(f"[!] {t}: valute diverse nei documenti, uso la prima")
    # ticker ripetuti in piu' documenti: somma delle quantita'
    return df.groupby("ticker").agg(quantita=("quantita", "sum"), valuta=("valuta", "first"))


def controlla_valute(portafoglio):
    """Avvisa se la valuta indicata a DB non coincide con quella che Yahoo associa al ticker."""
    for t, valuta_db in portafoglio["valuta"].items():
        try:
            valuta_yf = normalizza_valuta(yf.Ticker(t).fast_info.currency)
        except Exception:
            continue
        if valuta_yf != valuta_db:
            print(f"[!] {t}: a DB '{valuta_db}' ma Yahoo indica '{valuta_yf}'. Controlla il campo "
                  f"'{CAMPO_VALUTA}': un errore falsa sia i pesi sia i rendimenti.")


def scarica_cambio(valuta, anni):
    """Serie storica: quanti EUR vale 1 unita' di `valuta` (ticker Yahoo es. USDEUR=X)."""
    simbolo = f"{valuta}{VALUTA_BASE}=X"
    fx = yf.download(simbolo, period=f"{anni}y", auto_adjust=True, progress=False)["Close"]
    fx = fx.squeeze().dropna()
    if fx.empty:
        raise RuntimeError(f"Cambio {simbolo} non disponibile su Yahoo.")
    return fx


def converti_in_eur(prezzi, valute, anni):
    """Converte ogni colonna di prezzi in EUR, cosi' pesi e rendimenti sono omogenei."""
    prezzi = prezzi.copy()
    cambi = {}
    for t in prezzi.columns:
        v = valute.get(t, VALUTA_BASE)
        if v == "GBp":
            prezzi[t] = prezzi[t] / 100.0
            v = "GBP"
        if v == VALUTA_BASE:
            continue
        if v not in cambi:
            cambi[v] = scarica_cambio(v, anni)
            print(f"[i] Convertiti in EUR i titoli in {v} (ultimo cambio {cambi[v].iloc[-1]:.4f})")
        fx = cambi[v]
        fx = fx.reindex(prezzi.index.union(fx.index)).ffill().reindex(prezzi.index)
        prezzi[t] = prezzi[t] * fx
    return prezzi


def pesi_da_quantita(portafoglio, prezzi_eur):
    """Valore (in EUR) di ogni posizione = quantita x ultimo prezzo disponibile."""
    quantita = portafoglio["quantita"]
    ultimo = prezzi_eur.ffill().iloc[-1].reindex(quantita.index)
    mancanti = ultimo[ultimo.isna()].index.tolist()
    if mancanti:
        print(f"[!] Nessun prezzo per {mancanti}: esclusi dall'analisi")
    valore = (quantita * ultimo).dropna()

    tab = pd.DataFrame({
        "valuta": portafoglio["valuta"].reindex(valore.index),
        "quantita": quantita.reindex(valore.index),
        "prezzo_eur": ultimo.reindex(valore.index),
        "valore_eur": valore,
        "peso": valore / valore.sum(),
    }).sort_values("valore_eur", ascending=False)
    print("\n=== POSIZIONI (quantita x ultimo prezzo, in EUR) ===")
    print(tab.to_string(formatters={
        "quantita": "{:,.2f}".format, "prezzo_eur": "{:,.2f}".format,
        "valore_eur": "{:,.2f}".format, "peso": "{:.1%}".format,
    }))
    return valore


# --- DATI -------------------------------------------------------------------
def rimuovi_outlier(prezzi, soglia=0.10, finestra=5, verbose=True):
    """Toglie i prezzi isolati che si discostano più di `soglia` dalla mediana dei giorni
    vicini (tipico errore dati: +17% un giorno, -13% il giorno dopo). I salti reali, che
    persistono nei giorni successivi, restano. Un crollo reale di un solo giorno seguito
    da pieno recupero verrebbe tolto: per i titoli singoli controlla la lista stampata."""
    if not soglia:
        return prezzi
    med = prezzi.rolling(finestra, center=True, min_periods=3).median()
    cattivi = (prezzi / med - 1).abs() > soglia
    if verbose:
        for t in prezzi.columns:
            for d in prezzi.index[cattivi[t]]:
                print(f"[!] Outlier rimosso: {t} {d.date()} prezzo {prezzi.at[d, t]:.2f} "
                      f"(mediana vicina {med.at[d, t]:.2f})")
    return prezzi.mask(cattivi)


def scarica_prezzi(tickers, anni=3):
    prezzi = yf.download(tickers, period=f"{anni}y", auto_adjust=True, progress=False)["Close"]
    if isinstance(prezzi, pd.Series):
        prezzi = prezzi.to_frame(tickers[0])
    return prezzi


def prezzi_a_rendimenti(prezzi, freq=FREQ, soglia_outlier=0.10):
    prezzi = rimuovi_outlier(prezzi, soglia_outlier)
    # ffill PRIMA del resample: .last() salta i NaN colonna per colonna, quindi un ETF
    # che non ha scambiato venerdì darebbe il prezzo di giovedì mentre l'altro quello di
    # venerdì -> rendimenti sfasati e correlazione artificialmente bassa.
    prezzi = prezzi.ffill(limit=5).resample(freq).last()
    rend = prezzi.pct_change(fill_method=None)
    return rend.iloc[1:]


def diagnostica_prezzi(prezzi, a, b):
    """Spiega perché due titoli che dovrebbero essere quasi identici non lo sono."""
    p = prezzi[[a, b]]
    print("\n=== DIAGNOSTICA COPPIA ===")
    for t in (a, b):
        s = p[t].dropna()
        giorni_fermi = (s.pct_change() == 0).mean()
        print(f"{t}: {len(s)} osservazioni, dal {s.index[0].date()} al {s.index[-1].date()}, "
              f"giorni a prezzo invariato: {giorni_fermi:.0%}")

    pf = p.ffill(limit=5)
    for nome, f in (("giornaliera", "D"), ("settimanale", "W"), ("mensile", "ME")):
        r = pf.resample(f).last().pct_change(fill_method=None).dropna()
        print(f"Correlazione {nome}: {r[a].corr(r[b]):.3f}  (n={len(r)})")

    pc = rimuovi_outlier(p).ffill(limit=5)
    print("\nDopo la rimozione degli outlier:")
    for nome, f in (("giornaliera", "D"), ("settimanale", "W"), ("mensile", "ME")):
        r = pc.resample(f).last().pct_change(fill_method=None).dropna()
        print(f"Correlazione {nome}: {r[a].corr(r[b]):.3f}  (n={len(r)})")

    rd = pf.pct_change(fill_method=None).dropna()
    diff = (rd[a] - rd[b]).abs().sort_values(ascending=False).head(5)
    print("\nGiorni con la maggiore differenza di rendimento (spesso dati errati su Yahoo):")
    print(pd.DataFrame({a: rd[a].loc[diff.index], b: rd[b].loc[diff.index]})
            .to_string(float_format=lambda x: f"{x:.2%}"))


def diagnostica_coppia(a, b, anni=3):
    for t in (a, b):
        info = yf.Ticker(t).info
        print(f"{t}: {info.get('longName')} | valuta {info.get('currency')} | "
              f"borsa {info.get('exchange')}")
    prezzi = yf.download([a, b], period=f"{anni}y", auto_adjust=True, progress=False)["Close"]
    diagnostica_prezzi(prezzi, a, b)


def pulisci(rend, pesi):
    """Scarta ticker con troppi buchi, allinea le date, rinormalizza i pesi."""
    copertura = rend.notna().mean()
    scartati = copertura[copertura < MIN_COPERTURA].index.tolist()
    if scartati:
        print(f"[!] Scartati per dati insufficienti: {scartati}")
    rend = rend.drop(columns=scartati).dropna()
    w = pesi.reindex(rend.columns)
    w = w / w.sum()
    return rend, w


# --- METRICHE ---------------------------------------------------------------
def metriche(rend, w):
    corr = rend.corr()
    cov = rend.cov()
    wv = w.values
    n = len(wv)

    # 1) Numero effettivo di posizioni (1/HHI): 6 titoli ma pesi sbilanciati -> ~3
    n_eff = 1.0 / np.sum(wv**2)

    # 2) Correlazione media pesata tra coppie distinte
    W = np.outer(wv, wv)
    np.fill_diagonal(W, 0)
    corr_media = float((W * corr.values).sum() / W.sum()) if n > 1 else np.nan

    # 3) Contributo al rischio di ciascun titolo (somma = 100%)
    var_ptf = float(wv @ cov.values @ wv)
    contributo = pd.Series(wv * (cov.values @ wv) / var_ptf, index=rend.columns)

    # 4) Diversification ratio: media pesata delle vol / vol di portafoglio (>1 = diversifica)
    vol = np.sqrt(np.diag(cov.values))
    div_ratio = float((wv @ vol) / np.sqrt(var_ptf))

    # 5) Quota di varianza spiegata dal primo fattore comune (sulla correlazione)
    autoval = np.sort(np.linalg.eigvalsh(corr.values))[::-1]
    quota_pc1 = float(autoval[0] / autoval.sum())

    return {
        "corr": corr,
        "n_eff": n_eff,
        "corr_media": corr_media,
        "contributo": contributo,
        "div_ratio": div_ratio,
        "quota_pc1": quota_pc1,
        "vol_ptf_ann": float(np.sqrt(var_ptf * {"W": 52, "D": 252, "M": 12}.get(FREQ, 52))),
    }


def coppie_correlate(corr, soglia, top=8):
    iu = np.triu_indices_from(corr.values, k=1)
    coppie = pd.DataFrame({
        "a": corr.index[iu[0]],
        "b": corr.columns[iu[1]],
        "corr": corr.values[iu],
    }).sort_values("corr", ascending=False)
    return coppie.head(top), coppie[coppie["corr"] >= soglia]


def ordine_cluster(corr):
    """Riordina i titoli per cluster gerarchico (distanza = sqrt(0.5*(1-corr)))."""
    if len(corr) < 3:
        return list(corr.index)
    dist = np.sqrt(0.5 * (1 - corr.clip(-1, 1)))
    d = squareform(dist.values, checks=False)
    idx = leaves_list(linkage(d, method="average"))
    return [corr.index[i] for i in idx]


# --- OUTPUT -----------------------------------------------------------------
def report(m, w, soglia):
    print("\n=== INDICATORI DI CONCENTRAZIONE ===")
    print(f"Posizioni: {len(w)}  |  numero effettivo (1/HHI): {m['n_eff']:.1f}")
    print(f"Correlazione media pesata tra le coppie: {m['corr_media']:.2f}")
    print(f"Diversification ratio: {m['div_ratio']:.2f}  (1 = nessuna diversificazione)")
    print(f"Quota di varianza del 1° fattore comune: {m['quota_pc1']:.0%}")
    print(f"Volatilità annualizzata del portafoglio (storica): {m['vol_ptf_ann']:.1%}")

    top, sopra = coppie_correlate(m["corr"], soglia)
    print("\n=== COPPIE PIÙ CORRELATE ===")
    print(top.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    if len(sopra):
        print(f"\n[!] {len(sopra)} coppie con correlazione >= {soglia}: probabili duplicati/sovrapposizioni")

    conf = pd.DataFrame({"peso": w, "contributo_rischio": m["contributo"]})
    conf["delta"] = conf["contributo_rischio"] - conf["peso"]
    print("\n=== PESO vs CONTRIBUTO AL RISCHIO ===")
    print(conf.sort_values("contributo_rischio", ascending=False)
              .to_string(float_format=lambda x: f"{x:.1%}"))


def grafico(m, w, path_png):
    corr = m["corr"]
    ordine = ordine_cluster(corr)
    c = corr.loc[ordine, ordine]

    fig, ax = plt.subplots(1, 2, figsize=(15, 6))

    im = ax[0].imshow(c.values, cmap="RdYlGn_r", vmin=-1, vmax=1)
    ax[0].set_xticks(range(len(ordine)), ordine, rotation=90)
    ax[0].set_yticks(range(len(ordine)), ordine)
    for i in range(len(ordine)):
        for j in range(len(ordine)):
            ax[0].text(j, i, f"{c.values[i, j]:.2f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax[0], fraction=0.046)
    ax[0].set_title("Correlazione rendimenti (riordinata per cluster)")

    idx = np.arange(len(ordine))
    h = 0.4
    ax[1].barh(idx - h / 2, w.loc[ordine] * 100, h, label="Peso %")
    ax[1].barh(idx + h / 2, m["contributo"].loc[ordine] * 100, h, label="Contributo al rischio %")
    ax[1].set_yticks(idx, ordine)
    ax[1].invert_yaxis()
    ax[1].legend()
    ax[1].set_title("Peso vs contributo al rischio")
    ax[1].set_xlabel("%")

    plt.tight_layout()
    plt.savefig(path_png, dpi=150)
    print(f"\nGrafico salvato in {path_png}")
    plt.show()


# --- MAIN -------------------------------------------------------------------
def analizza(rend, pesi, soglia=0.9, path_png="concentrazione.png"):
    rend, w = pulisci(rend, pesi)
    m = metriche(rend, w)
    report(m, w, soglia)
    grafico(m, w, path_png)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anni", type=int, default=3)
    ap.add_argument("--soglia", type=float, default=0.9)
    ap.add_argument("--out", default="concentrazione.png")
    ap.add_argument("--outlier", type=float, default=0.10,
                    help="soglia di scarto dalla mediana vicina per togliere prezzi errati (0 = disattivo)")
    ap.add_argument("--coppia", nargs=2, metavar=("T1", "T2"),
                    help="diagnostica di due ticker (es. --coppia SWDA.MI MWRD.MI)")
    a = ap.parse_args()

    if a.coppia:
        diagnostica_coppia(*a.coppia, anni=a.anni)
        return

    portafoglio = carica_portafoglio_firestore()
    controlla_valute(portafoglio)

    tickers = list((portafoglio if portafoglio is not None else pesi).index)
    print(f"Portafoglio: {len(tickers)} titoli")
    prezzi = scarica_prezzi(tickers, anni=a.anni)
    if portafoglio is not None:
        prezzi = converti_in_eur(prezzi, portafoglio["valuta"], a.anni)
        pesi = pesi_da_quantita(portafoglio, prezzi)
    rend = prezzi_a_rendimenti(prezzi, soglia_outlier=a.outlier)
    analizza(rend, pesi, soglia=a.soglia, path_png=a.out)


if __name__ == "__main__":
    main()
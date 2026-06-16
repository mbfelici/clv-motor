#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════╗
# ║  capturar_fechamento.py — captura a linha da Pinnacle perto   ║
# ║  do apito (endpoint /odds GRÁTIS) e grava a odd JUSTA em      ║
# ║  fechamentos.csv. Feito p/ rodar no GitHub Actions (cron).    ║
# ╚══════════════════════════════════════════════════════════════╝
import os, csv, re, math, time, unicodedata, requests
from datetime import datetime, timezone, timedelta

API_KEY   = os.environ.get("ODDS_API_KEY", "")     # vem do secret do GitHub
REGION    = "eu"
BOOKMAKER = "pinnacle"
BASE_URL  = "https://api.the-odds-api.com/v4"

LEAD_MIN  = 30     # captura quem começa nos próximos LEAD_MIN minutos
GRACE_MIN = 10    # tolerância p/ atraso do cron (cobre lag tipico do Actions)

WATCHLIST = "watchlist.csv"
SAIDA     = "fechamentos.csv"

# ── matemática (cópia fiel do motor: de-vig + escada Poisson) ──────────
def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower().strip())

def prob_justa(odds, market, label, point=None):
    saidas = [o for o in odds if o["market"] == market
              and (market != "totals" or o["point"] == point)]
    invs = [(o["label"], 1.0/o["price"]) for o in saidas if o["price"] and o["price"] > 1]
    if len(invs) < 2: return None
    s = sum(v for _, v in invs)
    if s <= 0: return None
    al = _norm(label)
    for lab, v in invs:
        if _norm(lab) == al: return round(v/s, 4)
    return None

def _pk(k, l): return math.exp(-l) * l**k / math.factorial(k)
def _le(k, l): return sum(_pk(i, l) for i in range(0, k+1)) if k >= 0 else 0.0
def fair_under_prob(lam, linha):
    n = int(math.floor(linha + 1e-9)); frac = round(linha - n, 2)
    if   frac < 0.375: m, h = n-1, n
    elif frac < 0.625: m, h = n, None
    else:              m, h = n, n+1
    p = _le(m, lam)
    if h is not None: p += 0.5 * _pk(h, lam)
    return min(1.0, max(0.0, p))
def _solve_lambda(linha, pu, lo=0.05, hi=9.0, it=80):
    for _ in range(it):
        mid = 0.5*(lo+hi)
        if fair_under_prob(mid, linha) > pu: lo = mid
        else: hi = mid
    return 0.5*(lo+hi)
def lambda_do_jogo(odds):
    pts = {}
    for o in odds:
        if o["market"] != "totals" or o["point"] is None: continue
        pts.setdefault(o["point"], {})[_norm(o["label"])] = o["price"]
    lams = []
    for pt, l in pts.items():
        u, ov = l.get("under"), l.get("over")
        if not u or not ov or u <= 1 or ov <= 1: continue
        pu = (1.0/u)/(1.0/u + 1.0/ov); lams.append(_solve_lambda(pt, pu))
    return sum(lams)/len(lams) if lams else None

def parse_aposta(mercado, aposta):
    if mercado == "totals":
        mo = re.search(r"(Over|Under)\s+([\d.]+)", str(aposta), re.I)
        if mo: return mo.group(1).capitalize(), float(mo.group(2))
    return str(aposta), None

def fair_fechamento(odds, mercado, label, point):
    if mercado == "h2h":
        pm = prob_justa(odds, "h2h", label); return round(1/pm, 4) if pm else None
    if mercado == "totals":
        pm = prob_justa(odds, "totals", label, point)
        if pm: return round(1/pm, 4)
        lam = lambda_do_jogo(odds)
        if lam is None: return None
        pu = fair_under_prob(lam, point); p = pu if _norm(label) == "under" else 1-pu
        return round(1/p, 4) if p > 1e-9 else None
    return None

# ── API ────────────────────────────────────────────────────────────────
_SPORTS = None
def sport_key_de(liga):
    global _SPORTS
    if _SPORTS is None:
        r = requests.get(f"{BASE_URL}/sports", params={"apiKey": API_KEY}, timeout=15)
        r.raise_for_status()
        _SPORTS = {_norm(s["title"]): s["key"] for s in r.json() if "soccer" in s["key"]}
    return _SPORTS.get(_norm(liga))

def odds_atuais(sport_key, market):
    """Endpoint /odds ATUAL (grátis). Retorna lista de eventos. Cache por (sport,market)."""
    url = f"{BASE_URL}/sports/{sport_key}/odds"
    p = {"apiKey": API_KEY, "regions": REGION, "markets": market,
         "oddsFormat": "decimal", "dateFormat": "iso"}
    r = requests.get(url, params=p, timeout=20)
    if r.status_code != 200:
        print(f"   ⚠️ /odds {sport_key} status {r.status_code}: {r.text[:70]}")
        return None, r.headers.get("x-requests-remaining", "?")
    return r.json(), r.headers.get("x-requests-remaining", "?")

def achar_evento(eventos, home, away):
    hn, an = _norm(home), _norm(away)
    for ev in eventos:
        if _norm(ev.get("home_team","")) == hn and _norm(ev.get("away_team","")) == an:
            return ev
    for ev in eventos:
        eh, ea = _norm(ev.get("home_team","")), _norm(ev.get("away_team",""))
        if (hn in eh or eh in hn) and (an in ea or ea in an):
            return ev
    return None

def odds_pinnacle(ev):
    out = []
    for bk in ev.get("bookmakers", []):
        if bk["key"] != BOOKMAKER: continue
        for mkt in bk.get("markets", []):
            for o in mkt.get("outcomes", []):
                out.append({"market": mkt["key"], "label": o["name"],
                            "price": o["price"], "point": o.get("point")})
    return out

# ── loop principal ──────────────────────────────────────────────────────
def carregar_csv(path):
    if not os.path.exists(path): return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def chave(row): return f"{row['partida']}|{row['mercado']}|{row['aposta']}"

def main():
    if not API_KEY:
        print("❌ ODDS_API_KEY ausente (defina o secret no GitHub)."); return
    watch = carregar_csv(WATCHLIST)
    if not watch:
        print("ℹ️ watchlist.csv vazio/ausente — nada a capturar."); return
    feitos = carregar_csv(SAIDA)
    ja = {chave(r) for r in feitos}
    now = datetime.now(timezone.utc)

    # quais picks estão na janela de captura e ainda não foram pegos
    alvos = []
    for w in watch:
        if chave(w) in ja: continue
        try:
            ko = datetime.fromisoformat(w["kickoff_iso"].replace("Z", "+00:00"))
        except Exception:
            continue
        if (ko - timedelta(minutes=LEAD_MIN)) <= now <= (ko + timedelta(minutes=GRACE_MIN)):
            alvos.append(w)
    if not alvos:
        print(f"⏳ {now.isoformat()} — nenhum pick na janela de captura agora."); return

    cache = {}; novos = []; rem = "?"
    for w in alvos:
        sk = sport_key_de(w["liga"])
        if not sk:
            print(f"   ⏭️ {w['partida']} — liga não mapeada ({w['liga']})"); continue
        mercado = w["mercado"]
        api_mkt = "totals" if mercado == "totals" else ("h2h" if mercado == "h2h" else mercado)
        ck = (sk, api_mkt)
        if ck not in cache:
            cache[ck], rem = odds_atuais(sk, api_mkt); time.sleep(0.3)
        eventos = cache[ck]
        if not eventos:
            print(f"   ⏭️ {w['partida']} — sem dados no /odds"); continue
        if " vs " not in w["partida"]:
            print(f"   ⏭️ {w['partida']} — formato inesperado"); continue
        home, away = [x.strip() for x in w["partida"].split(" vs ", 1)]
        ev = achar_evento(eventos, home, away)
        if not ev:
            print(f"   ⏭️ {w['partida']} — evento não encontrado (ainda sem linha?)"); continue
        odds = odds_pinnacle(ev)
        label, point = parse_aposta(mercado, w["aposta"])
        fair = fair_fechamento(odds, mercado, label, point)
        if not fair:
            print(f"   ⏭️ {w['partida']} {w['aposta']} — não deu p/ derivar"); continue
        novos.append({"partida": w["partida"], "mercado": mercado, "aposta": w["aposta"],
                      "odd_fech": fair, "captured_iso": now.isoformat(),
                      "kickoff_iso": w["kickoff_iso"]})
        print(f"   ✅ {w['partida']} {w['aposta']} → fech(justa)={fair:.3f}")

    if not novos:
        print("ℹ️ Nada novo capturado nesta passada."); return
    existe = os.path.exists(SAIDA)
    with open(SAIDA, "a", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=["partida","mercado","aposta","odd_fech",
                                            "captured_iso","kickoff_iso"])
        if not existe: wtr.writeheader()
        for n in novos: wtr.writerow(n)
    print(f"\n✅ {len(novos)} fechamento(s) gravado(s) em {SAIDA} | quota restante: {rem}")

if __name__ == "__main__":
    main()

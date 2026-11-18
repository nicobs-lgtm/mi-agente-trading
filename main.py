"""
Agente de trading v2 (fase 1)
-----------------------------
- Datos de mercado: Twelve Data (velas diarias + cotización).
- TODOS los cálculos (indicadores, niveles, R:B, tamaño, % ajustado) se hacen aquí, en código.
  No se usa la IA para calcular nada: así no hay errores de cuentas ni niveles inventados.
- Comprueba la fecha de resultados antes de dar nada por bueno.
- Tarjeta corta por Telegram. Solo se "aprueba" lo que llega al umbral (60 %).
- Escaneo de tu lista de tickers: /scan, o automático llamando a /cron/scan?token=...
"""
import os
import re
import time
import threading
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request

app = Flask(__name__)

# ----------------------------------------------------------------------------
# Configuración (variables de entorno en Render)
# ----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

CRON_TOKEN = os.environ.get("CRON_TOKEN", "")            # secreto para /cron/scan
CAPITAL = float(os.environ.get("CAPITAL", "5000"))        # por operación (se trata como $, igual que el Excel)
UMBRAL = float(os.environ.get("UMBRAL", "60"))            # % mínimo para aprobar
PAUSA_SCAN = float(os.environ.get("PAUSA_SCAN", "25"))    # segundos entre tickers (límite de Twelve Data)
PAIS = os.environ.get("PAIS", "United States")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")      # opcional: calendario de resultados gratuito (finnhub.io)

WATCHLIST = [t.strip().upper() for t in os.environ.get(
    "WATCHLIST",
    "AAPL,MSFT,GOOGL,AMZN,META,NVDA,AVGO,TSLA,AMD,NFLX,JPM,V,COST,LLY,WMT"
).split(",") if t.strip()]

# Fechas de resultados puestas a mano (por si la API no las da): "NVDA=2026-11-17,BABA=2026-11-19"
def _parse_overrides(texto):
    out = {}
    for par in (texto or "").split(","):
        if "=" in par:
            k, v = par.split("=", 1)
            try:
                out[k.strip().upper()] = date.fromisoformat(v.strip())
            except ValueError:
                pass
    return out

EARNINGS_OVERRIDES = _parse_overrides(os.environ.get("EARNINGS_OVERRIDES", ""))

TZ_ET = ZoneInfo("America/New_York")
TZ_ES = ZoneInfo("Europe/Madrid")

# Festivos de la bolsa de EE. UU. (completar cuando haga falta)
FESTIVOS_US = {
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 4, 2),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}

_scan_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        limite = 3900
        for i in range(0, len(mensaje), limite):
            datos = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje[i:i + limite]}
            requests.post(url, data=datos, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")


# ----------------------------------------------------------------------------
# Twelve Data
# ----------------------------------------------------------------------------
class ErrorDatos(Exception):
    pass


def td_get(endpoint, params, reintento=True):
    params = dict(params)
    params["apikey"] = TWELVEDATA_API_KEY
    resp = requests.get(f"https://api.twelvedata.com/{endpoint}", params=params, timeout=20)
    data = resp.json()
    if isinstance(data, dict) and data.get("status") == "error":
        msg = str(data.get("message", "Error desconocido de Twelve Data"))
        if reintento and ("credits" in msg.lower() or data.get("code") == 429):
            time.sleep(61)
            return td_get(endpoint, params, reintento=False)
        raise ErrorDatos(msg)
    return data


# ----------------------------------------------------------------------------
# Calendario
# ----------------------------------------------------------------------------
def es_laborable(d):
    return d.weekday() < 5 and d not in FESTIVOS_US


def siguiente_laborable(d):
    d = d + timedelta(days=1)
    while not es_laborable(d):
        d += timedelta(days=1)
    return d


def anterior_laborable(d):
    d = d - timedelta(days=1)
    while not es_laborable(d):
        d -= timedelta(days=1)
    return d


def sumar_laborables(d, n):
    while n > 0:
        d = siguiente_laborable(d)
        n -= 1
    return d


def dia_de_entrada():
    """Día laborable en el que se entraría si se opera ahora."""
    ahora = datetime.now(TZ_ET)
    d = ahora.date()
    if (not es_laborable(d)) or ahora.hour >= 16:
        d = siguiente_laborable(d)
    return d


def fmt_fecha(d):
    return d.strftime("%d/%m")


# ----------------------------------------------------------------------------
# Indicadores
# ----------------------------------------------------------------------------
def sma(valores, n):
    return sum(valores[-n:]) / n if len(valores) >= n else None


def rsi_wilder(cierres, n=14):
    if len(cierres) < n + 1:
        return None
    ganancias, perdidas = [], []
    for i in range(1, len(cierres)):
        cambio = cierres[i] - cierres[i - 1]
        ganancias.append(max(cambio, 0.0))
        perdidas.append(max(-cambio, 0.0))
    media_g = sum(ganancias[:n]) / n
    media_p = sum(perdidas[:n]) / n
    for i in range(n, len(ganancias)):
        media_g = (media_g * (n - 1) + ganancias[i]) / n
        media_p = (media_p * (n - 1) + perdidas[i]) / n
    if media_p == 0:
        return 100.0
    rs = media_g / media_p
    return 100.0 - 100.0 / (1.0 + rs)


def atr_simple(altos, bajos, cierres, n=14):
    if len(cierres) < n + 1:
        return None
    rangos = []
    for i in range(1, len(cierres)):
        rangos.append(max(altos[i] - bajos[i],
                          abs(altos[i] - cierres[i - 1]),
                          abs(bajos[i] - cierres[i - 1])))
    return sum(rangos[-n:]) / n


def percentil(valores, p):
    if not valores:
        return 0.0
    v = sorted(valores)
    k = (len(v) - 1) * p
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def swings(altos, bajos, k=3, ventana=60):
    """Máximos y mínimos locales (swing highs / swing lows) de las últimas `ventana` velas."""
    n = len(altos)
    inicio = max(k, n - ventana)
    sh, sl = [], []
    for i in range(inicio, n - k):
        if altos[i] == max(altos[i - k:i + k + 1]):
            sh.append(altos[i])
        if bajos[i] == min(bajos[i - k:i + k + 1]):
            sl.append(bajos[i])
    return sh, sl


# ----------------------------------------------------------------------------
# Resultados de la empresa
# ----------------------------------------------------------------------------
def _fechas_finnhub(ticker, hoy):
    if not FINNHUB_KEY:
        return []
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings", params={
            "symbol": ticker, "from": (hoy - timedelta(days=100)).isoformat(),
            "to": (hoy + timedelta(days=200)).isoformat(), "token": FINNHUB_KEY}, timeout=15).json()
        return [date.fromisoformat(str(x["date"])[:10]) for x in (r.get("earningsCalendar") or []) if x.get("date")]
    except Exception:
        return []


def _fechas_twelvedata(ticker):
    try:
        data = td_get("earnings", {"symbol": ticker, "country": PAIS, "outputsize": 12})
    except Exception:
        return []
    lista = data.get("earnings") or data.get("values") or [] if isinstance(data, dict) else []
    out = []
    for item in lista:
        try:
            out.append(date.fromisoformat(str(item.get("date"))[:10]))
        except Exception:
            continue
    return out


def buscar_resultados(ticker, hoy):
    """Devuelve (fecha | None, estado) con estado: manual / confirmada / estimada / desconocida.
    Orden de fuentes: tu variable EARNINGS_OVERRIDES > Finnhub (si hay clave) > Twelve Data."""
    if ticker in EARNINGS_OVERRIDES:
        return EARNINGS_OVERRIDES[ticker], "manual"
    fechas = _fechas_finnhub(ticker, hoy) or _fechas_twelvedata(ticker)
    corte = anterior_laborable(hoy)     # incluye ayer: si presentaron tras el cierre, la reacción es hoy
    futuras = [f for f in fechas if f >= corte]
    pasadas = [f for f in fechas if f < corte]
    if futuras:
        return min(futuras), "confirmada"
    if pasadas:
        return max(pasadas) + timedelta(days=91), "estimada"
    return None, "desconocida"


# ----------------------------------------------------------------------------
# Plan de operación a partir de las velas
# ----------------------------------------------------------------------------
def construir_plan(direccion, precio, atr, sh, sl, ma50):
    es_long = direccion == "LONG"
    if es_long:
        # stop bajo el mínimo local más cercano (si está a una distancia razonable)
        niveles = [x for x in sl if 0.8 * atr <= precio - x <= 2.5 * atr]
        if niveles:
            stop = max(niveles) - 0.15 * atr
        else:
            stop = precio - 1.5 * atr
        riesgo = precio - stop
        res = [x for x in sh if x > precio + 0.5 * atr]
        if ma50 and ma50 > precio + 0.5 * atr:
            res.append(ma50)
        res = sorted(res)
        lejos = [x for x in res if x - precio >= riesgo]   # TP1 con R:B >= 1
        saltado = bool(res) and (not lejos or lejos[0] != res[0])
        tp1 = lejos[0] if lejos else precio + max(2.0 * atr, riesgo)
        sig = [x for x in res if x >= tp1 + 0.75 * atr]
        tp2 = sig[0] if sig else max(tp1 + 1.5 * atr, precio + 3.5 * atr)
        fuente = "resistencias de las velas" if res else "sin resistencia visible (múltiplos de ATR)"
        if saltado:
            fuente += " (TP1 más lejos: la resistencia cercana daba R:B < 1)"
    else:
        niveles = [x for x in sh if 0.8 * atr <= x - precio <= 2.5 * atr]
        if niveles:
            stop = min(niveles) + 0.15 * atr
        else:
            stop = precio + 1.5 * atr
        riesgo = stop - precio
        sup = [x for x in sl if x < precio - 0.5 * atr]
        if ma50 and ma50 < precio - 0.5 * atr:
            sup.append(ma50)
        sup = sorted(sup, reverse=True)
        lejos = [x for x in sup if precio - x >= riesgo]   # TP1 con R:B >= 1
        saltado = bool(sup) and (not lejos or lejos[0] != sup[0])
        tp1 = lejos[0] if lejos else precio - max(2.0 * atr, riesgo)
        sig = [x for x in sup if x <= tp1 - 0.75 * atr]
        tp2 = sig[0] if sig else min(tp1 - 1.5 * atr, precio - 3.5 * atr)
        fuente = "soportes de las velas" if sup else "sin soporte visible (múltiplos de ATR)"
        if saltado:
            fuente += " (TP1 más lejos: el soporte cercano daba R:B < 1)"
    r1 = abs(tp1 - precio) / riesgo if riesgo > 0 else 0.0
    r2 = abs(tp2 - precio) / riesgo if riesgo > 0 else 0.0
    return {
        "entrada": precio, "stop": stop, "riesgo": riesgo,
        "tp1": tp1, "tp2": tp2, "rb1": r1, "rb2": r2, "fuente": fuente, "saltado": saltado,
    }


# ----------------------------------------------------------------------------
# Puntuación (el "% ajustado")
# ----------------------------------------------------------------------------
def puntuar(direccion, ctx, plan, hoy_entrada, resultados):
    """Devuelve (porcentaje, lista de factores, veto | None, aviso | None)."""
    es_long = direccion == "LONG"
    precio, ma50, rsi, vol = ctx["precio"], ctx["ma50"], ctx["rsi"], ctx["vol_ratio"]
    puntos = 45.0
    factores = [(45, "base: acierto real sept-nov (8/18)")]
    # (los puntos de cada factor se suman a la base; resultados al final)

    def anotar(pts, texto):
        nonlocal puntos
        puntos += pts
        factores.append((round(pts, 1), texto))

    # Tendencia (continua: cuanto más separada de la MM50, más puntos; demasiado estirada resta)
    a_favor = (precio > ma50) if es_long else (precio < ma50)
    dist = abs(precio / ma50 - 1) * 100
    lado = ("sobre" if es_long else "bajo") if a_favor else ("bajo" if es_long else "sobre")
    if a_favor:
        pts = min(8.0, 1.5 + dist * 1.2)
        if dist > 12:
            pts -= 4
        anotar(pts, f"a favor de la tendencia ({dist:.1f}% {lado} la MM50)")
    else:
        anotar(-8 - min(4.0, dist * 0.5), f"contra la tendencia ({dist:.1f}% {lado} la MM50)")

    # Pendiente de la MM50 (últimas 10 sesiones)
    slope = ctx.get("slope")
    if slope is not None:
        anotar(max(-3.0, min(3.0, (slope if es_long else -slope))), f"pendiente MM50 {slope:+.1f}% en 10 sesiones")

    # Fuerza relativa frente al S&P 500 (20 sesiones)
    rs = ctx.get("rs")
    if rs is not None:
        anotar(max(-3.0, min(3.0, (rs if es_long else -rs) * 0.3)), f"fuerza relativa vs S&P 500 {rs:+.1f} pts (20 ses.)")

    # RSI (continuo: centro 55 en largos, 45 en cortos; extremos restan)
    d = abs(rsi - (55 if es_long else 45))
    if d <= 10:
        pr = 4 - d * 0.2
    elif d <= 20:
        pr = 2 - (d - 10) * 0.4
    else:
        pr = -4
    anotar(pr, f"RSI {rsi:.1f}")

    # Stop: ni pegado (se lo come el ruido) ni lejísimos (pérdida enorme en $)
    pct_stop = plan["riesgo"] / precio * 100
    if pct_stop < 1.0:
        anotar(-5, f"stop muy ajustado ({pct_stop:.1f}%)")
    elif pct_stop <= 5.0:
        anotar(2, f"stop a {pct_stop:.1f}% de la entrada")
    elif pct_stop <= 8.0:
        anotar(0, f"stop a {pct_stop:.1f}% de la entrada")
    else:
        anotar(-min(5.0, (pct_stop - 8.0) * 0.8), f"stop muy lejano ({pct_stop:.1f}%): pérdida grande en $")

    # R:B ponderado 50/50 (continuo) y TP1 por debajo de 1
    rbp = (plan["rb1"] + plan["rb2"]) / 2
    anotar(max(-6.0, min(6.0, (rbp - 1.2) * 4)), f"R:B ponderado 1:{rbp:.2f} (TP1 1:{plan['rb1']:.2f} · TP2 1:{plan['rb2']:.2f})")
    if plan["rb1"] < 1.0:
        anotar(-5, f"TP1 con R:B 1:{plan['rb1']:.2f} (por debajo de 1)")
    if plan.get("saltado"):
        anotar(-2, "TP1 desplazado: hay un nivel cercano que puede frenar el precio antes")

    # Volumen (continuo; un volumen anómalo > 2,5x no se premia: puede ser noticia o evento)
    if vol > 2.5:
        anotar(0, f"volumen {vol:.2f}x la media (anómalo: mira si hay noticia)")
    else:
        anotar(max(-2.0, min(2.0, (vol - 1.0) * 3)), f"volumen {vol:.2f}x la media")

    # Gap típico
    gap_usd = ctx["gap_p90"] * precio
    if gap_usd > 0.6 * plan["riesgo"]:
        anotar(-3, f"el stop no cubre un gap típico (p90 {ctx['gap_p90'] * 100:.1f}%)")

    # Resultados
    fecha_res, estado = resultados
    limite_plan = sumar_laborables(hoy_entrada, 5)
    limite_15 = sumar_laborables(hoy_entrada, 15)
    veto, aviso = None, None
    if fecha_res is not None:
        etiqueta = {"manual": "puestos a mano", "confirmada": "confirmados", "estimada": "estimados (≈ último + 91 días)"}[estado]
        if estado == "estimada":
            en_ventana = (hoy_entrada - timedelta(days=5)) <= fecha_res <= (limite_plan + timedelta(days=7))
        else:
            # incluye el día laborable anterior: si presentan tras el cierre, la reacción cae en el día de entrada
            en_ventana = anterior_laborable(hoy_entrada) <= fecha_res <= limite_plan
        if en_ventana:
            puntos -= 10
            veto = f"resultados {etiqueta} el {fecha_res.strftime('%d/%m/%Y')}, dentro del plazo"
            factores.append((-10, veto))
        elif fecha_res <= limite_15:
            factores.append((0, f"resultados {etiqueta} el {fecha_res.strftime('%d/%m/%Y')} (fuera del plazo)"))
        else:
            puntos += 3
            factores.append((3, f"sin resultados antes del {fecha_res.strftime('%d/%m/%Y')} ({etiqueta})"))
        if estado == "estimada":
            aviso = "fecha de resultados ESTIMADA, no confirmada: compruébala"
    else:
        aviso = "fecha de resultados NO verificada: compruébala (máximo 59 %)"
        puntos -= 2
        factores.append((-2, "resultados no verificados"))

    puntos = min(puntos, 70.0)
    if fecha_res is None:
        puntos = min(puntos, 59.0)
    return round(puntos), factores, veto, aviso


# ----------------------------------------------------------------------------
# Análisis completo de un ticker
# ----------------------------------------------------------------------------
_spy_cache = {"t": 0.0, "datos": None}


def spy_cierres():
    """Cierres diarios del S&P 500 (SPY) por fecha; se cachea 6 horas para no gastar llamadas."""
    if _spy_cache["datos"] and time.time() - _spy_cache["t"] < 6 * 3600:
        return _spy_cache["datos"]
    try:
        ts = td_get("time_series", {"symbol": "SPY", "country": PAIS, "interval": "1day", "outputsize": 40})
        datos = {v["datetime"][:10]: float(v["close"]) for v in ts.get("values", [])}
        if datos:
            _spy_cache.update(t=time.time(), datos=datos)
        return datos or None
    except Exception:
        return _spy_cache["datos"]


def analizar(ticker):
    cot = td_get("quote", {"symbol": ticker, "country": PAIS})
    if "close" not in cot:
        raise ErrorDatos(f"No se han encontrado datos para '{ticker}'.")
    precio = float(cot["close"])
    abierto = bool(cot.get("is_market_open"))

    ts = td_get("time_series", {"symbol": ticker, "country": PAIS, "interval": "1day", "outputsize": 130})
    velas = ts.get("values", [])
    if len(velas) < 60:
        raise ErrorDatos(f"Pocas velas diarias para '{ticker}' ({len(velas)}).")
    velas = list(reversed(velas))              # de más antigua a más reciente
    cerradas = velas[:-1] if abierto else velas

    op = [float(v["open"]) for v in cerradas]
    al = [float(v["high"]) for v in cerradas]
    ba = [float(v["low"]) for v in cerradas]
    ci = [float(v["close"]) for v in cerradas]
    vo = [float(v["volume"]) for v in cerradas]

    ma50 = sma(ci, 50)
    rsi = rsi_wilder(ci, 14)
    atr = atr_simple(al, ba, ci, 14)
    if None in (ma50, rsi, atr) or atr <= 0:
        raise ErrorDatos(f"No hay datos suficientes para calcular indicadores de '{ticker}'.")
    previos = vo[-11:-1]
    vol_ratio = vo[-1] / (sum(previos) / len(previos)) if previos and sum(previos) > 0 else 1.0
    gaps = [abs(op[i] / ci[i - 1] - 1) for i in range(max(1, len(ci) - 60), len(ci))]
    gap_p90 = percentil(gaps, 0.90)
    sh, sl = swings(al, ba)

    hoy_entrada = dia_de_entrada()
    resultados = buscar_resultados(ticker, hoy_entrada)

    ma50_ant = sma(ci[:-10], 50)
    slope = (ma50 / ma50_ant - 1) * 100 if ma50_ant else None
    rs = None
    if len(cerradas) > 21:
        spy = spy_cierres()
        f_fin, f_ini = cerradas[-1]["datetime"][:10], cerradas[-21]["datetime"][:10]
        if spy and f_fin in spy and f_ini in spy:
            rs = ((ci[-1] / ci[-21] - 1) - (spy[f_fin] / spy[f_ini] - 1)) * 100

    ctx = {"precio": precio, "ma50": ma50, "rsi": rsi, "vol_ratio": vol_ratio, "gap_p90": gap_p90,
           "atr": atr, "slope": slope, "rs": rs}
    opciones = []
    for direccion in ("LONG", "SHORT"):
        plan = construir_plan(direccion, precio, atr, sh, sl, ma50)
        pct, factores, veto, aviso = puntuar(direccion, ctx, plan, hoy_entrada, resultados)
        opciones.append({"dir": direccion, "plan": plan, "pct": pct, "factores": factores, "veto": veto, "aviso": aviso})
    mejor = max(opciones, key=lambda o: (o["pct"], o["dir"] == "LONG"))

    if mejor["veto"]:
        estado = "VETADA"
    elif mejor["pct"] >= UMBRAL and resultados[0] is not None:
        estado = "APROBADA"
    elif mejor["pct"] >= 50:
        estado = "VIGILAR"
    else:
        estado = "DESCARTADA"

    return {
        "ticker": ticker, "estado": estado, "mejor": mejor, "ctx": ctx,
        "abierto": abierto, "fecha_dato": cerradas[-1]["datetime"][:10],
        "hoy_entrada": hoy_entrada, "resultados": resultados,
    }


# ----------------------------------------------------------------------------
# Tarjeta de Telegram
# ----------------------------------------------------------------------------
ICONO = {"APROBADA": "✅", "VIGILAR": "🟡", "DESCARTADA": "❌", "VETADA": "⛔"}


def tarjeta(r, detalle=True):
    m, p, c = r["mejor"], r["mejor"]["plan"], r["ctx"]
    es_long = m["dir"] == "LONG"
    lado = "COMPRA (LONG)" if es_long else "VENTA (SHORT)"
    unidades = CAPITAL / p["entrada"]
    mitad = unidades / 2
    perdida = unidades * p["riesgo"]
    g1 = mitad * abs(p["tp1"] - p["entrada"])
    g2 = mitad * abs(p["tp2"] - p["entrada"])
    rb_pond = (p["rb1"] + p["rb2"]) / 2
    gap_usd = unidades * max(p["riesgo"], c["gap_p90"] * p["entrada"])
    fecha_res, estado_res = r["resultados"]
    limite = sumar_laborables(r["hoy_entrada"], 5)

    lineas = [
        f"{ICONO[r['estado']]} {r['ticker']} — {lado} · {m['pct']} % · {r['estado']}",
        f"Precio {p['entrada']:.2f} · MM50 {c['ma50']:.2f} · RSI {c['rsi']:.1f} · Vol {c['vol_ratio']:.2f}x",
        f"Entrada {p['entrada']:.2f} · SL {p['stop']:.2f} ({p['riesgo'] / p['entrada'] * 100:.1f}%)",
        f"TP1 {p['tp1']:.2f} (R:B 1:{p['rb1']:.2f}) · TP2 {p['tp2']:.2f} (R:B 1:{p['rb2']:.2f})",
        f"Reparto 50/50 → R:B ponderado 1:{rb_pond:.2f}",
        f"{CAPITAL:,.0f} € = {unidades:.1f} uds → pérdida máx -{perdida:.0f} $ · TP1 +{g1:.0f} $ · TP2 +{g2:.0f} $",
        f"Gap típico (p90) {c['gap_p90'] * 100:.1f}% → peor caso ≈ -{gap_usd:.0f} $",
    ]
    if fecha_res:
        lineas.append(f"Resultados: {fecha_res.strftime('%d/%m/%Y')} ({estado_res})")
    else:
        lineas.append("Resultados: ⚠️ sin fecha")
    if m["aviso"]:
        lineas.append(f"⚠️ {m['aviso']}")
    if m["veto"]:
        lineas.append(f"⛔ VETO: {m['veto']}")
    elif r["estado"] == "APROBADA":
        lineas.append(f"Entrar 16:15-16:45 (hora España) si el precio sigue a ±0,5% de {p['entrada']:.2f}")
        lineas.append(f"Límite de 5 días laborables: {fmt_fecha(limite)}")
    sesion = "sesión abierta" if r["abierto"] else "sesión cerrada"
    lineas.append(f"Niveles: {p['fuente']} · última vela cerrada {r['fecha_dato']} ({sesion})")
    if detalle:
        lineas.append("Por qué: " + " · ".join(f"{pts:+.1f} {txt}" for pts, txt in m["factores"]))
    return "\n".join(lineas)


# ----------------------------------------------------------------------------
# Escaneo de la lista
# ----------------------------------------------------------------------------
def escanear(lista):
    if not _scan_lock.acquire(blocking=False):
        enviar_telegram("⏳ Ya hay un escaneo en marcha.")
        return
    try:
        enviar_telegram(f"🔎 Escaneando {len(lista)} tickers (tardará ~{int(len(lista) * (PAUSA_SCAN + 4) / 60) + 1} min)…")
        resultados, errores = [], []
        for i, t in enumerate(lista):
            try:
                resultados.append(analizar(t))
            except Exception as e:
                errores.append(f"{t}: {e}")
            if i < len(lista) - 1:
                time.sleep(PAUSA_SCAN)
        aprobadas = [r for r in resultados if r["estado"] == "APROBADA"]
        for r in aprobadas:
            enviar_telegram(tarjeta(r, detalle=True))
        vigilar = [f"{r['ticker']} {r['mejor']['pct']}%" for r in resultados if r["estado"] == "VIGILAR"]
        vetadas = [r["ticker"] for r in resultados if r["estado"] == "VETADA"]
        resumen = [f"📋 Escaneo: {len(resultados)} analizados · {len(aprobadas)} aprobados (≥ {UMBRAL:.0f}%)"]
        if vigilar:
            resumen.append("🟡 En vigilancia (50-59%): " + ", ".join(vigilar))
        if vetadas:
            resumen.append("⛔ Vetados por resultados: " + ", ".join(vetadas))
        ranking = sorted([r for r in resultados if r["estado"] != "VETADA"], key=lambda r: -r["mejor"]["pct"])[:5]
        if ranking:
            resumen.append("🏆 Ranking: " + " · ".join(f"{r['ticker']} {r['mejor']['dir']} {r['mejor']['pct']}%" for r in ranking))
        if errores:
            resumen.append("⚠️ Con error: " + " | ".join(errores))
        enviar_telegram("\n".join(resumen))
    finally:
        _scan_lock.release()


def analizar_y_enviar(ticker):
    try:
        enviar_telegram(f"🔍 Analizando {ticker}…")
        r = analizar(ticker)
        enviar_telegram(tarjeta(r, detalle=True))
    except Exception as e:
        enviar_telegram(f"❌ {ticker}: {e}")


# ----------------------------------------------------------------------------
# Texto del usuario
# ----------------------------------------------------------------------------
PALABRAS_IGNORADAS = {"MIRA", "ANALIZA", "ANALISIS", "HOY", "QUE", "POR", "ESTA", "ESTÁ", "DAME", "DIME", "EL", "LA", "DE", "UN", "UNA"}


def extraer_ticker(texto):
    # 1) tokens ya escritos en mayúsculas (AAPL, BRK.B)
    for palabra in texto.split():
        limpia = palabra.strip(",.!?¿¡:;()")
        if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", limpia) and limpia not in PALABRAS_IGNORADAS:
            return limpia
    # 2) mensaje de una sola palabra en minúsculas
    palabras = texto.split()
    if len(palabras) == 1:
        limpia = palabras[0].strip(",.!?¿¡:;()").upper()
        if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", limpia):
            return limpia
    # 3) "analiza nvda", "mira nvda"
    m = re.search(r"(?:analiza|analisis|análisis|mira|dame|ticker)\s+([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b", texto, re.I)
    if m and m.group(1).upper() not in PALABRAS_IGNORADAS:
        return m.group(1).upper()
    return None


AYUDA = (
    "Comandos:\n"
    "• AAPL (o 'analiza AAPL') → tarjeta del ticker\n"
    "• /scan → escanea tu lista ahora\n"
    "• /lista → ver la lista\n"
    "• /add TICKER · /quitar TICKER → editar la lista (se reinicia si Render se reinicia; para fijarla usa la variable WATCHLIST)\n"
    f"Se aprueba a partir de {UMBRAL:.0f} %. Resultados dentro de 5 días laborables = veto."
)


def manejar_comando(texto):
    partes = texto.strip().split()
    cmd = partes[0].split("@")[0].lower()
    arg = partes[1].upper() if len(partes) > 1 else ""
    if cmd == "/scan":
        threading.Thread(target=escanear, args=(list(WATCHLIST),), daemon=True).start()
    elif cmd == "/lista":
        enviar_telegram("📃 Lista (" + str(len(WATCHLIST)) + "): " + ", ".join(WATCHLIST))
    elif cmd == "/add" and arg:
        if arg not in WATCHLIST:
            WATCHLIST.append(arg)
        enviar_telegram(f"➕ {arg} añadido. Lista: {', '.join(WATCHLIST)}")
    elif cmd == "/quitar" and arg:
        if arg in WATCHLIST:
            WATCHLIST.remove(arg)
        enviar_telegram(f"➖ {arg} quitado. Lista: {', '.join(WATCHLIST)}")
    else:
        enviar_telegram(AYUDA)


@app.route('/telegram', methods=['POST'])
def recibir_mensaje_telegram():
    datos = request.json or {}
    if "message" in datos and "text" in datos["message"]:
        chat_id_remitente = str(datos["message"]["chat"]["id"])
        texto_usuario = datos["message"]["text"]
        if chat_id_remitente == str(TELEGRAM_CHAT_ID):
            if texto_usuario.strip().startswith("/"):
                manejar_comando(texto_usuario)
            else:
                ticker = extraer_ticker(texto_usuario)
                if ticker:
                    threading.Thread(target=analizar_y_enviar, args=(ticker,), daemon=True).start()
                else:
                    enviar_telegram("⚠️ No he detectado ningún ticker válido. Escribe, por ejemplo: AAPL\n\n" + AYUDA)
    return "OK", 200


@app.route('/cron/scan', methods=['GET', 'POST'])
def cron_scan():
    """Lo llama un cron externo (p. ej. cron-job.org) a las 22:15 hora de España, de lunes a viernes."""
    if not CRON_TOKEN or request.args.get("token") != CRON_TOKEN:
        return "forbidden", 403
    hoy = datetime.now(TZ_ET).date()
    if not es_laborable(hoy):
        return "mercado cerrado hoy", 200
    threading.Thread(target=escanear, args=(list(WATCHLIST),), daemon=True).start()
    return "scan iniciado", 200


@app.route('/')
def inicio():
    return "Agente de trading v2 encendido."


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True)

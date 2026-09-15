import os
import requests
import threading
from flask import Flask, request

app = Flask(__name__)

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")


def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        
        # Si el mensaje es muy largo (más de 6000 caracteres), Telegram lo rechaza. 
        # Lo partimos en trozos seguros de 6000 caracteres.
        limite = 8500
        trozos = [mensaje[i:i+limite] for i in range(0, len(mensaje), limite)]
        
        for parte in trozos:
            datos = {"chat_id": TELEGRAM_CHAT_ID, "text": parte, "parse_mode": "Markdown"}
            resp = requests.post(url, data=datos, timeout=10)
            
            # Si falla el Markdown por caracteres especiales, lo enviamos como texto plano (sin parse_mode)
            if resp.status_code != 200:
                datos_plano = {"chat_id": TELEGRAM_CHAT_ID, "text": parte}
                requests.post(url, data=datos_plano, timeout=10)
                
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")


def extraer_ticker_del_texto(texto):
    palabras = texto.split()
    for palabra in palabras:
        palabra_limpia = palabra.strip(",.!?").upper()
        if 1 <= len(palabra_limpia) <= 5 and palabra_limpia.isalpha() and palabra_limpia not in ["MIRA", "ANALIZA", "HOY", "QUE", "POR", "ESTÁ", "ESTA"]:
            return palabra_limpia

    if palabras:
        ultima_palabra = palabras[-1].strip(",.!?").upper()
        if len(ultima_palabra) <= 5 and ultima_palabra.isalpha():
            return ultima_palabra

    return None


def _pedir_twelvedata(endpoint, params):
    params = dict(params)
    params["apikey"] = TWELVEDATA_API_KEY
    resp = requests.get(f"https://api.twelvedata.com/{endpoint}", params=params, timeout=15)
    data = resp.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise Exception(data.get("message", "Error desconocido de Twelve Data"))
    return data


def obtener_datos_mercado(ticker_symbol):
    try:
        print(f"[{ticker_symbol}] -> pidiendo /quote")
        cotizacion = _pedir_twelvedata("quote", {"symbol": ticker_symbol, "exchange": "NASDAQ"})
        print(f"[{ticker_symbol}] <- /quote OK")
        if "close" not in cotizacion:
            return None, f"No se han encontrado datos para el ticker '{ticker_symbol}'."

        precio_actual = float(cotizacion["close"])

        print(f"[{ticker_symbol}] -> pidiendo /time_series (volumen)")
        series_resp = _pedir_twelvedata("time_series", {
            "symbol": ticker_symbol, "exchange": "NASDAQ",
            "interval": "1day", "outputsize": 11
        })
        print(f"[{ticker_symbol}] <- /time_series OK")

        velas = series_resp.get("values", [])
        if not velas:
            return None, f"No se han encontrado velas diarias para '{ticker_symbol}'."

        mercado_abierto = bool(cotizacion.get("is_market_open"))
        indice_ultimo_dia_cerrado = 1 if (mercado_abierto and len(velas) > 1) else 0

        volumen_actual = float(velas[indice_ultimo_dia_cerrado]["volume"])
        fecha_volumen = velas[indice_ultimo_dia_cerrado]["datetime"]
        etiqueta_volumen = f"Último día cerrado ({fecha_volumen})" if indice_ultimo_dia_cerrado == 1 else f"Hoy en curso ({fecha_volumen})"

        inicio_previos = indice_ultimo_dia_cerrado + 1
        volumenes_previos = [float(v["volume"]) for v in velas[inicio_previos:inicio_previos + 10]]
        volumen_medio_10d = sum(volumenes_previos) / len(volumenes_previos) if volumenes_previos else 0.0

        print(f"[{ticker_symbol}] -> pidiendo /sma")
        sma_resp = _pedir_twelvedata("sma", {"symbol": ticker_symbol, "exchange": "NASDAQ", "interval": "1day", "time_period": 50, "outputsize": 1})
        print(f"[{ticker_symbol}] <- /sma OK")
        ma_50 = float(sma_resp["values"][0]["sma"]) if sma_resp.get("values") else None

        print(f"[{ticker_symbol}] -> pidiendo /rsi")
        rsi_resp = _pedir_twelvedata("rsi", {"symbol": ticker_symbol, "exchange": "NASDAQ", "interval": "1day", "time_period": 14, "outputsize": 1})
        print(f"[{ticker_symbol}] <- /rsi OK")
        rsi_actual = float(rsi_resp["values"][0]["rsi"]) if rsi_resp.get("values") else None

        texto_ma_50 = f"${ma_50:.2f}" if ma_50 is not None else "N/D"
        texto_rsi = f"{rsi_actual:.1f}" if rsi_actual is not None else "N/D"
        texto_mercado = "ABIERTO ahora mismo (sesión en curso)" if mercado_abierto else "CERRADO en este momento"

        info_resumida = (
            f"Activo: {ticker_symbol.upper()}\n"
            f"Estado del mercado: {texto_mercado}\n"
            f"Precio actual: ${precio_actual:.2f}\n"
            f"Media Móvil (50): {texto_ma_50}\n"
            f"RSI (14): {texto_rsi}\n"
            f"Volumen [{etiqueta_volumen}] vs Medio (10d cerrados): {volumen_actual:,.0f} vs {volumen_medio_10d:,.0f}\n"
        )

        return info_resumida, None
    except Exception as e:
        return None, str(e)


def consultar_claude(datos_mercado):
    try:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1500,  # Reducimos ligeramente para que no supere los límites de Telegram
            "system": (
                "Eres un analista cuantitativo senior y trader institucional. "
                "Aplica rigurosamente el protocolo de los 7 filtros a los datos reales que te proporcione el sistema. "
                "OBLIGATORIAMENTE, basándote en la estructura técnica, debes terminar el análisis arrojando un plan de operativa "
                "claro y cerrado que incluya: Dirección (Compra/Venta), Precio de Entrada, Stop Loss (SL) técnico, objetivos "
                "de Take Profit (TP1 y TP2) con su respectivo ratio Riesgo/Beneficio, la probabilidad estimada de éxito de la operación (en porcentaje), "
                "y el periodo de tiempo planificado estimado que durará la operación (ej. Intradiario, 24-48 horas, de 3 a 5 días, etc.)."
            ),
            "messages": [
                {"role": "user", "content": f"Analiza esta situación de mercado con datos reales obtenidos: {datos_mercado}"}
            ]
        }

        response = requests.post(url, headers=headers, json=payload, timeout=25)
        resultado = response.json()

        if response.status_code == 200:
            return resultado["content"][0]["text"]
        else:
            return f"❌ Error de Anthropic ({response.status_code}): {resultado.get('error', {}).get('message', 'Desconocido')}"

    except requests.exceptions.Timeout:
        return "❌ Error: La IA tardó demasiado en responder y la conexión expiró."
    except Exception as e:
        return f"❌ Error interno crítico: {str(e)}"


def procesar_analisis_en_segundo_plan(ticker):
    try:
        print(f"[{ticker}] Iniciando análisis...")
        enviar_telegram(f"🔍 *Buscando datos de mercado en tiempo real para {ticker}...*")

        datos_tecnicos, error = obtener_datos_mercado(ticker)

        if error:
            enviar_telegram(f"❌ {error}")
        else:
            enviar_telegram("⏳ *Aplicando protocolo de 7 filtros y calculando setup...*")
            analisis = consultar_claude(datos_tecnicos)
            enviar_telegram(analisis)
            print(f"[{ticker}] Análisis enviado a Telegram con éxito.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        enviar_telegram(f"❌ Error inesperado procesando el análisis: {str(e)}")


@app.route('/telegram', methods=['POST'])
def recibir_mensaje_telegram():
    datos = request.json

    if "message" in datos and "text" in datos["message"]:
        chat_id_remitente = str(datos["message"]["chat"]["id"])
        texto_usuario = datos["message"]["text"]

        if chat_id_remitente == TELEGRAM_CHAT_ID:
            ticker = extraer_ticker_del_texto(texto_usuario)

            if ticker:
                hilo = threading.Thread(target=procesar_analisis_en_segundo_plan, args=(ticker,))
                hilo.start()
            else:
                enviar_telegram("⚠️ No he detectado ningún ticker válido. Escribe el símbolo en mayúsculas, por ejemplo: *'TSLA'* o *'AAPL'*.")

    return "OK", 200


@app.route('/')
def inicio():
    return "¡El bot asíncrono con partición de mensajes está encendido!"


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True)

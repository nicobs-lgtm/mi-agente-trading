import os
import requests
import yfinance as yf
import pandas as pd
from flask import Flask, request

app = Flask(__name__)

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    datos = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    requests.post(url, data=datos)

def extraer_ticker_del_texto(texto):
    """Mapeo rápido de nombres comunes a Tickers de bolsa"""
    texto = texto.lower()
    if "tesla" in texto or "tsla" in texto:
        return "TSLA"
    elif "gilead" in texto or "gild" in texto:
        return "GILD"
    elif "apple" in texto or "aapl" in texto:
        return "AAPL"
    elif "nvidia" in texto or "nvda" in texto:
        return "NVDA"
    elif "microsoft" in texto or "msft" in texto:
        return "MSFT"
    # Si escriben directamente el ticker en mayúsculas (ej: "GOOG")
    palabras = texto.split()
    for palabra in palabras:
        if len(palabra) <= 5 and palabra.isupper():
            return palabra
    return None

def obtener_datos_mercado(ticker_symbol):
    try:
        stock = yf.Ticker(ticker_symbol)
        # Descargar histórico de los últimos 6 meses para calcular indicadores
        df = stock.history(period="6mo")
        
        if df.empty:
            return None, "No se encontraron datos para ese activo."
            
        precio_actual = df['Close'].iloc[-1]
        volumen_actual = df['Volume'].iloc[-1]
        volumen_medio = df['Volume'].rolling(window=20).mean().iloc[-1]
        
        # Calcular Media Móvil de 200 sesiones (o la máxima disponible)
        ma_200 = df['Close'].rolling(window=200).mean().iloc[-1] if len(df) >= 200 else df['Close'].mean()
        
        # Calcular RSI básico de 14 periodos
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_actual = rsi.iloc[-1]
        
        info_resumida = (
            f"Activo: {ticker_symbol.upper()}\n"
            f"Precio actual: ${precio_actual:.2f}\n"
            f"Media Móvil 200 (aprox): ${ma_200:.2f}\n"
            f"RSI (14): {rsi_actual:.1f}\n"
            f"Volumen actual vs Medio: {volumen_actual:,.0f} (Media 20d: {volumen_medio:,.0f})"
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
            "max_tokens": 2000,
            "system": (
                "Eres un analista cuantitativo senior y trader institucional. "
                "Aplica rigurosamente el protocolo de los 7 filtros a los datos reales que te proporcione el sistema. "
                "OBLIGATORIAMENTE, basándote en la estructura técnica, debes terminar el análisis arrojando un plan de operativa "
                "claro y cerrado que incluya: Dirección (Compra/Venta), Precio de Entrada, Stop Loss (SL) técnico y objetivos "
                "de Take Profit (TP1 y TP2) con su respectivo ratio Riesgo/Beneficio."
            ),
            "messages": [
                {"role": "user", "content": f"Analiza esta situación de mercado con datos reales obtenidos: {datos_mercado}"}
            ]
        }
        
        response = requests.post(url, headers=headers, json=payload)
        resultado = response.json()
        
        if response.status_code == 200:
            return resultado["content"][0]["text"]
        else:
            return f"❌ Error de Anthropic ({response.status_code}): {resultado.get('error', {}).get('message', 'Desconocido')}"
            
    except Exception as e:
        return f"❌ Error interno crítico: {str(e)}"

@app.route('/telegram', methods=['POST'])
def recibir_mensaje_telegram():
    datos = request.json
    
    if "message" in datos and "text" in datos["message"]:
        chat_id_remitente = str(datos["message"]["chat"]["id"])
        texto_usuario = datos["message"]["text"]
        
        if chat_id_remitente == TELEGRAM_CHAT_ID:
            ticker = extraer_ticker_del_texto(texto_usuario)
            
            if ticker:
                enviar_telegram(f"🔍 *Buscando datos de mercado en tiempo real para {ticker}...*")
                datos_tecnicos, error = obtener_datos_mercado(ticker)
                
                if error:
                    enviar_telegram(f"❌ No pude obtener datos de {ticker}: {error}")
                else:
                    enviar_telegram("⏳ *Aplicando protocolo de 7 filtros y calculando setup...*")
                    analisis = consultar_claude(datos_tecnicos)
                    enviar_telegram(analisis)
            else:
                enviar_telegram("⚠️ No he reconocido el activo. Prueba a escribir algo como: *'Analiza Gilead'* o *'Mira Tesla'*.")
            
    return "OK", 200

@app.route('/')
def inicio():
    return "¡El bot con auto-fetch y trading cuantitativo está encendido!"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

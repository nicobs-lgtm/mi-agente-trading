import os
import requests
import anthropic
from flask import Flask, request

# Creamos el mini servidor web
app = Flask(__name__)

# Cargamos las claves de las variables de entorno
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def consultar_claude(datos_mercado):
    cliente = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    # Aquí pegas tu prompt largo de los 7 filtros
    prompt_sistema = "Eres un analista cuantitativo. Aplica el protocolo de los 7 filtros..." 
    mensaje_usuario = f"Analiza estos datos recibidos de TradingView: {datos_mercado}"

    respuesta = cliente.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=1000,
        system=prompt_sistema,
        messages=[{"role": "user", "content": mensaje_usuario}]
    )
    return respuesta.content[0].text

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    datos = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    requests.post(url, data=datos)

# --- LA PUERTA QUE ESCUCHA A TRADINGVIEW ---
@app.route('/webhook', methods=['POST'])
def recibir_alerta():
    # 1. Atrapamos el texto/JSON que manda TradingView
    datos_tradingview = request.data.decode('utf-8')
    
    # 2. Le preguntamos a Claude
    analisis = consultar_claude(datos_tradingview)
    
    # 3. Te lo enviamos al móvil
    enviar_telegram(analisis)
    
    return "Alerta procesada y enviada", 200

# Una página de inicio para comprobar que está encendido
@app.route('/')
def inicio():
    return "¡El bot de Trading está encendido y esperando alertas!"

if __name__ == '__main__':
    # Arrancamos el servidor
    app.run(host='0.0.0.0', port=8080)

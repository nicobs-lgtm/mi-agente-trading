import os
import requests
import anthropic
from flask import Flask, request

app = Flask(__name__)

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    datos = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    requests.post(url, data=datos)

def consultar_claude(datos_mercado):
    try:
        cliente = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        prompt_sistema = "Eres un analista cuantitativo. Aplica el protocolo de los 7 filtros. Sé riguroso." 
        mensaje_usuario = f"Analiza esta situación de mercado que te indico: {datos_mercado}"

        respuesta = cliente.messages.create(
            model="claude-3-sonnet-20240229",
            max_tokens=1000,
            system=prompt_sistema,
            messages=[{"role": "user", "content": mensaje_usuario}]
        )
        return respuesta.content[0].text
    except Exception as e:
        return f"❌ Error interno al conectar con Claude: {str(e)}"

@app.route('/telegram', methods=['POST'])
def recibir_mensaje_telegram():
    datos = request.json
    
    if "message" in datos and "text" in datos["message"]:
        chat_id_remitente = str(datos["message"]["chat"]["id"])
        texto_usuario = datos["message"]["text"]
        
        if chat_id_remitente == TELEGRAM_CHAT_ID:
            enviar_telegram("⏳ *Analizando la estructura con Claude...*")
            analisis = consultar_claude(texto_usuario)
            enviar_telegram(analisis)
            
    return "OK", 200

@app.route('/')
def inicio():
    return "¡El bot interactivo está encendido!"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

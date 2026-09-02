import os
import requests
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
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 2000,
            "system": "Eres un analista cuantitativo. Aplica rigurosamente el protocolo de los 7 filtros a los datos que te proporcione el usuario.",
            "messages": [
                {"role": "user", "content": f"Analiza esta situación de mercado que te indico: {datos_mercado}"}
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
            enviar_telegram("⏳ *Analizando la estructura con Claude...*")
            analisis = consultar_claude(texto_usuario)
            enviar_telegram(analisis)

    return "OK", 200


@app.route('/')
def inicio():
    return "¡El bot interactivo está encendido!"


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

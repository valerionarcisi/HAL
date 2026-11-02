"""Messaggi Telegram con il bot di HAL. Se mancano le chiavi, scrive solo nel log."""
import json
import logging
import os
import urllib.request

log = logging.getLogger("radar")


def telegram(msg):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    log.info("notify: %s", msg)
    if not token or not chat:
        return
    try:
        body = json.dumps({"chat_id": chat, "text": "📡 Radar\n" + msg, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log.warning("telegram fallito: %s", e)

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///

import os
import sys
import requests
import argparse

def send_document(chat_id, file_path, caption=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not set.")
        sys.exit(1)
    
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    
    with open(file_path, 'rb') as f:
        data = {'chat_id': chat_id}
        if caption:
            data['caption'] = caption
            data['parse_mode'] = 'HTML'
        
        files = {'document': f}
        response = requests.post(url, data=data, files=files)
        response.raise_for_status()
        return response.json()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a document via Telegram Bot API")
    parser.add_argument("file_path", help="Path to the file to send")
    parser.add_argument("--chat-id", required=True, help="Telegram Chat ID")
    parser.add_argument("--caption", help="Caption for the document (HTML supported)")
    
    args = parser.parse_args()
    
    try:
        result = send_document(args.chat_id, args.file_path, args.caption)
        print("Success!")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

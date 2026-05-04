# Bill Splitter Bot — Setup Guide

## Requirements
```
python-telegram-bot==21.6
google-generativeai==0.7.2
Pillow==10.4.0
```

Save as `requirements.txt`

---

## Setup Steps

### 1. Create your Telegram Bot
- Message @BotFather on Telegram
- Send `/newbot`, follow prompts
- Copy your **bot token**

### 2. Get Gemini API Key
- Go to https://aistudio.google.com/app/apikey
- Create a free API key
- Free tier: 15 req/min, 1500 req/day — plenty for occasional use

### 3. Set environment variables
```bash
export TELEGRAM_TOKEN="your-telegram-bot-token"
export GEMINI_API_KEY="your-gemini-api-key"
```

### 4. Install dependencies
```bash
pip install -r requirements.txt
```

### 5. Run locally
```bash
python bill_splitter_bot.py
```

---

## Deploy free on Render

1. Push code to a GitHub repo
2. Go to https://render.com → New → Web Service
3. Connect your repo
4. Set environment variables in Render dashboard
5. Start command: `python bill_splitter_bot.py`

> Note: Free Render instances sleep after 15min inactivity.  
> The bot will wake up when someone uses it — takes ~30sec first message.  
> For occasional use this is totally fine.

---

## How to use

1. Add the bot to your group chat
2. When you have a bill photo, send it with `/split` attached  
   OR send the photo first, then reply to it with `/split`
3. Bot reads the bill with Gemini vision
4. Confirm item list — water is auto-marked as shared (🌊)
5. Enter number of people
6. Enter each person's name
7. For each person, tap the items they took → Done
8. Bot posts the final breakdown: who owes you what, with GST+SC applied per person

---

## How GST + SC is calculated

For each person:
- Their items subtotal + their share of water/shared items
- + 10% SC on that subtotal
- + 8% GST on (subtotal + SC)

This matches how Maldivian restaurant bills stack the taxes.

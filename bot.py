# bot.py
import os
import re
import json
import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)

from dotenv import load_dotenv
import gspread

# ============== Config ==============
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_TAB = os.getenv("SHEET_TAB", "Custos")
SHEET_EVENTS_TAB = os.getenv("SHEET_EVENTS_TAB", "Eventos")
SHEET_CHECKINS_TAB = os.getenv("SHEET_CHECKINS_TAB", "Checkins")
TZ = os.getenv("TZ", "America/Sao_Paulo")
LOGO_FILE_ID = os.getenv("LOGO_FILE_ID")

# Suporte a credencial via variável (para cloud) ou arquivo local (para dev)
GOOGLE_CREDENTIALS_JSON_CONTENT = os.getenv("GOOGLE_CREDENTIALS_JSON_CONTENT")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not BOT_TOKEN or not SHEET_ID:
    raise RuntimeError("Faltam variáveis obrigatórias no .env: BOT_TOKEN e/ou SHEET_ID")

if not (GOOGLE_CREDENTIALS_JSON_CONTENT or GOOGLE_CREDENTIALS_JSON):
    raise RuntimeError(
        "Informe as credenciais do Google via GOOGLE_CREDENTIALS_JSON_CONTENT (JSON completo) "
        "ou GOOGLE_CREDENTIALS_JSON (caminho local do arquivo)."
    )

# ============== Google Sheets ==============
if GOOGLE_CREDENTIALS_JSON_CONTENT:
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON_CONTENT)
    gc = gspread.service_account_from_dict(creds_dict)
else:
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_JSON)

sh = gc.open_by_key(SHEET_ID)

def get_or_create_worksheet(sheet, title, headers):
    try:
        _ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        _ws = sheet.add_worksheet(title=title, rows=2000, cols=len(headers))
        _ws.update(f"A1:{chr(64 + len(headers))}1", [headers])
    return _ws

ws_custos = get_or_create_worksheet(sh, SHEET_TAB, ["Data", "Cidade", "Custo", "Tipo"])
ws_eventos = get_or_create_worksheet(sh, SHEET_EVENTS_TAB, ["Data", "Hora", "Título", "Local", "Observações"])
ws_checkins = get_or_create_worksheet(
    sh, SHEET_CHECKINS_TAB,
    ["Data","Hora","TZ","Lat","Lon","Local","País","Categoria","Descrição","Odômetro (km)","Foto (file_id)","Fonte"]
)

print("🔗 Testando conexão com Google Sheets...")
print("Primeiras linhas (Custos):", ws_custos.get_all_values()[:3])

# ============== Utilidades ==============
START_ROW_CUSTOS = 3
TELEGRAM_CHUNK = 3500

def now_tz():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ))
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=-3)

def today_str_br():
    return now_tz().strftime("%d/%m/%Y")

def first_empty_row_ws(_ws, col=1, start_row=2):
    vals = _ws.col_values(col)
    return max(len(vals) + 1, start_row)

def normalize_money(value: str) -> float:
    v = re.sub(r"[^0-9,\.]", "", value.strip()).replace(",", ".")
    return float(v)

# ============== Bot / Teclado ==============
dp = Dispatcher(storage=MemoryStorage())

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Roteiro"), KeyboardButton(text="💸 Adicionar custo")],
            [KeyboardButton(text="➕ Cadastrar evento"), KeyboardButton(text="🗓️ Agenda de hoje")],
            [KeyboardButton(text="✅ Check-in")],
        ],
        resize_keyboard=True
    )

# ============== START ==============
@dp.message(CommandStart())
async def start(m: Message):
    print(f"📩 {m.from_user.full_name} — ID: {m.from_user.id}")
    caption = (
        "🏍️ **Bem-vinda ao bot da viagem da Iris Motovicio!**\n\n"
        "Use o menu para registrar **custos**, **eventos**, fazer **check-in** e ver o **roteiro**. 💚"
    )

    if LOGO_FILE_ID:
        await m.answer_photo(photo=LOGO_FILE_ID, caption=caption, parse_mode="Markdown", reply_markup=main_keyboard())
        return

    logo_path = os.path.join(os.path.dirname(__file__), "logo_iris.png")
    if os.path.exists(logo_path):
        await m.answer_photo(photo=FSInputFile(logo_path), caption=caption, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        await m.answer(caption, parse_mode="Markdown", reply_markup=main_keyboard())

# (restante igual ao que você enviou — custos, eventos, checkins, agenda e roteiro)

# ============== Run ==============
async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

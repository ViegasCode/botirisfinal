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

# ================= Config =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_TAB = os.getenv("SHEET_TAB", "Custos")
SHEET_EVENTS_TAB = os.getenv("SHEET_EVENTS_TAB", "Eventos")
SHEET_CHECKINS_TAB = os.getenv("SHEET_CHECKINS_TAB", "Checkins")
TZ = os.getenv("TZ", "America/Sao_Paulo")
LOGO_FILE_ID = os.getenv("LOGO_FILE_ID")
DEBUG = os.getenv("DEBUG", "0") == "1"

# Credenciais via ENV (cloud) ou arquivo (dev local)
GOOGLE_CREDENTIALS_JSON_CONTENT = os.getenv("GOOGLE_CREDENTIALS_JSON_CONTENT")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not BOT_TOKEN or not SHEET_ID:
    raise RuntimeError("Faltam variáveis obrigatórias: BOT_TOKEN e/ou SHEET_ID")

if not (GOOGLE_CREDENTIALS_JSON_CONTENT or GOOGLE_CREDENTIALS_JSON):
    raise RuntimeError(
        "Informe as credenciais do Google via GOOGLE_CREDENTIALS_JSON_CONTENT (JSON completo) "
        "ou GOOGLE_CREDENTIALS_JSON (caminho local do arquivo, para uso em dev)."
    )

# ================= Google Sheets =================
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
try:
    print("Primeiras linhas (Custos):", ws_custos.get_all_values()[:3])
except Exception as e:
    print("Falha ao ler planilha:", e)

# ================= Utilidades =================
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

def find_first_empty_row_custos():
    colA = ws_custos.col_values(1)
    row = max(len(colA) + 1, START_ROW_CUSTOS)
    if row < START_ROW_CUSTOS:
        row = START_ROW_CUSTOS
    return row

def normalize_money(value: str) -> float:
    v = re.sub(r"[^0-9,\.]", "", value.strip()).replace(",", ".")
    return float(v)

def _parse_br_date(txt: str):
    try:
        return datetime.strptime(txt.strip(), "%d/%m/%Y").date()
    except Exception:
        return None

def _month_range_today():
    today = now_tz().date()
    start = today.replace(day=1)
    return start, today

def _last_7_days_range():
    today = now_tz().date()
    start = today - timedelta(days=6)
    return start, today

# === Envio seguro (evita crash por Markdown quebrado) ===
from aiogram.exceptions import TelegramBadRequest

async def send_md_safe(message_or_cbmsg, text: str, *, disable_preview: bool = False):
    """
    Tenta enviar com Markdown; se der TelegramBadRequest (parse),
    reenvia sem parse_mode.
    """
    kwargs = {"disable_web_page_preview": disable_preview}
    try:
        await message_or_cbmsg.answer(text, parse_mode="Markdown", **kwargs)
    except TelegramBadRequest:
        await message_or_cbmsg.answer(text, parse_mode=None, **kwargs)

# ================= Bot / Teclado =================
dp = Dispatcher(storage=MemoryStorage())

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Roteiro"), KeyboardButton(text="💸 Adicionar custo")],
            [KeyboardButton(text="➕ Cadastrar evento"), KeyboardButton(text="🗓️ Agenda de hoje")],
            [KeyboardButton(text="✅ Check-in"), KeyboardButton(text="📊 Resumo de custos")],
        ],
        resize_keyboard=True
    )

def _custos_inline_keyboard():
    rows = [
        [
            InlineKeyboardButton(text="Hoje", callback_data="custos:today"),
            InlineKeyboardButton(text="Últimos 7 dias", callback_data="custos:7d"),
        ],
        [
            InlineKeyboardButton(text="Este mês", callback_data="custos:month"),
            InlineKeyboardButton(text="Tudo", callback_data="custos:all"),
        ],
        [
            InlineKeyboardButton(text="Período personalizado", callback_data="custos:period"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _build_days_inline_keyboard():
    rows = [
        [
            InlineKeyboardButton(text="1 dia", callback_data="roteiro:1"),
            InlineKeyboardButton(text="3 dias", callback_data="roteiro:3"),
        ],
        [
            InlineKeyboardButton(text="5 dias", callback_data="roteiro:5"),
            InlineKeyboardButton(text="7 dias", callback_data="roteiro:7"),
        ],
        [
            InlineKeyboardButton(text="📄 Roteiro completo", callback_data="roteiro:all"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ================= START =================
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
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

# ================= CUSTOS =================
class CustoForm(StatesGroup):
    waiting_city = State()
    waiting_value = State()
    waiting_type = State()

def append_cost_row(cidade: str, custo_str: str, tipo: str):
    dt = now_tz().strftime("%d/%m/%Y")
    custo = normalize_money(custo_str)
    row = find_first_empty_row_custos()
    ws_custos.update(f"A{row}:D{row}", [[dt, cidade, custo, tipo]])
    return row

@dp.message(F.text.contains("Adicionar custo"))
@dp.message(Command("custo"))
async def add_cost(m: Message, state: FSMContext):
    await state.clear()  # evita ficar preso em outro fluxo
    await state.set_state(CustoForm.waiting_city)
    await m.answer("Qual **cidade**? (ex.: *Uyuni*)", parse_mode="Markdown")

@dp.message(CustoForm.waiting_city)
async def step_city(m: Message, state: FSMContext):
    await state.update_data(cidade=m.text.strip())
    await state.set_state(CustoForm.waiting_value)
    await m.answer("Qual **valor**? (ex.: *150*, *150,90* ou *150.90*)", parse_mode="Markdown")

@dp.message(CustoForm.waiting_value)
async def step_value(m: Message, state: FSMContext):
    valor_raw = m.text.strip()
    try:
        _ = normalize_money(valor_raw)
    except Exception:
        await m.answer("Valor inválido. Tente no formato *150* ou *150,90*.", parse_mode="Markdown")
        return
    await state.update_data(valor=valor_raw)
    await state.set_state(CustoForm.waiting_type)
    await m.answer("Qual **tipo**? (ex.: *combustível*, *hospedagem*, *alimentação*, *passeio*...)", parse_mode="Markdown")

@dp.message(CustoForm.waiting_type)
async def step_type(m: Message, state: FSMContext):
    tipo = m.text.strip()
    data = await state.get_data()
    cidade = data["cidade"]
    valor = data["valor"]
    try:
        row = append_cost_row(cidade=cidade, custo_str=valor, tipo=tipo)
    except Exception as e:
        await m.answer(f"Não consegui salvar no Google Sheets. 😕")
        await state.clear()
        return

    await m.answer(
        f"**Custo registrado com sucesso!** ✅\n\n"
        f"**Data:** hoje\n**Cidade:** {cidade}\n**Valor:** {valor}\n**Tipo:** {tipo}\n"
        f"(gravado na linha {row} da aba *{SHEET_TAB}*)",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )
    await state.clear()

# ================= EVENTOS =================
class EventoForm(StatesGroup):
    waiting_title = State()
    waiting_place = State()
    waiting_date = State()
    waiting_time = State()
    waiting_notes = State()

def _parse_date_ddmmyyyy(txt: str):
    txt = txt.strip()
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", txt):
        return None
    try:
        return datetime.strptime(txt, "%d/%m/%Y").date()
    except:
        return None

def _parse_time_hhmm(txt: str):
    try:
        return datetime.strptime(txt.strip(), "%H:%M").time()
    except:
        return None

@dp.message(F.text.regexp(r"(?i)evento"))
@dp.message(Command("evento"))
async def evento_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(EventoForm.waiting_title)
    await m.answer("Qual o **título** do evento/lugar? (ex.: *Ruínas Jesuíticas*)", parse_mode="Markdown")

@dp.message(EventoForm.waiting_title)
async def evento_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(EventoForm.waiting_place)
    await m.answer("Qual o **local**? (ex.: *Encarnación*)", parse_mode="Markdown")

@dp.message(EventoForm.waiting_place)
async def evento_place(m: Message, state: FSMContext):
    await state.update_data(place=m.text.strip())
    await state.set_state(EventoForm.waiting_date)
    await m.answer("Qual a **data**? (formato *dd/mm/aaaa*)", parse_mode="Markdown")

@dp.message(EventoForm.waiting_date)
async def evento_date(m: Message, state: FSMContext):
    d = _parse_date_ddmmyyyy(m.text)
    if not d:
        await m.answer("Data inválida. Use *dd/mm/aaaa* (ex.: 26/10/2025).", parse_mode="Markdown")
        return
    await state.update_data(date=m.text.strip())
    await state.set_state(EventoForm.waiting_time)
    await m.answer("Qual a **hora**? (formato *HH:MM* 24h, ex.: 16:30)", parse_mode="Markdown")

@dp.message(EventoForm.waiting_time)
async def evento_time(m: Message, state: FSMContext):
    t = _parse_time_hhmm(m.text)
    if not t:
        await m.answer("Hora inválida. Use *HH:MM* (ex.: 08:00).", parse_mode="Markdown")
        return
    await state.update_data(time=m.text.strip())
    await state.set_state(EventoForm.waiting_notes)
    await m.answer("Alguma **observação**? (ou envie `-` para deixar em branco)", parse_mode="Markdown")

@dp.message(EventoForm.waiting_notes)
async def evento_notes(m: Message, state: FSMContext):
    notes = "" if m.text.strip() == "-" else m.text.strip()
    data = await state.get_data()
    title = data["title"]
    place = data["place"]
    date_ = data["date"]
    time_ = data["time"]

    row = first_empty_row_ws(ws_eventos, col=1, start_row=2)
    ws_eventos.update(f"A{row}:E{row}", [[date_, time_, title, place, notes]])

    await state.clear()
    await m.answer(
        "✅ **Evento cadastrado!**\n\n"
        f"📅 {date_} — ⏰ {time_}\n"
        f"📝 *{title}* em *{place}*\n"
        f"{'Obs: ' + notes if notes else ''}",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ================= CHECKINS =================
class CheckinForm(StatesGroup):
    waiting_category = State()
    waiting_desc = State()
    waiting_location = State()
    waiting_place = State()
    waiting_country = State()
    waiting_odo = State()
    waiting_photo = State()

CHECKIN_CATEGORIES = ["em rota", "parada", "fronteira", "passeio", "pernoite", "abastecimento"]

@dp.message(F.text.contains("Check-in"))
@dp.message(Command("checkin"))
async def checkin_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(CheckinForm.waiting_category)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=cat)] for cat in CHECKIN_CATEGORIES],
        resize_keyboard=True
    )
    await m.answer("Escolha a **categoria** do check-in:", parse_mode="Markdown", reply_markup=kb)

@dp.message(CheckinForm.waiting_category)
async def checkin_category(m: Message, state: FSMContext):
    cat = m.text.strip().lower()
    if cat not in CHECKIN_CATEGORIES:
        await m.answer("Categoria inválida. Escolha uma das opções do teclado.")
        return
    await state.update_data(categoria=cat)
    await state.set_state(CheckinForm.waiting_desc)
    await m.answer("Escreva uma **descrição curta** (ex.: 'Marco das 3 Fronteiras')", parse_mode="Markdown", reply_markup=main_keyboard())

@dp.message(CheckinForm.waiting_desc)
async def checkin_desc(m: Message, state: FSMContext):
    await state.update_data(descricao=m.text.strip())
    await state.set_state(CheckinForm.waiting_location)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Enviar localização atual 📍", request_location=True)],
                  [KeyboardButton(text="Pular localização")]],
        resize_keyboard=True
    )
    await m.answer("Compartilhe sua **localização** ou toque em *Pular localização*.", parse_mode="Markdown", reply_markup=kb)

@dp.message(CheckinForm.waiting_location, F.location)
async def checkin_location(m: Message, state: FSMContext):
    loc = m.location
    await state.update_data(lat=loc.latitude, lon=loc.longitude, fonte="localização")
    await state.set_state(CheckinForm.waiting_place)
    await m.answer("Qual **local/cidade**?", parse_mode="Markdown", reply_markup=main_keyboard())

@dp.message(CheckinForm.waiting_location, F.text.casefold() == "pular localização")
async def checkin_skip_location(m: Message, state: FSMContext):
    await state.update_data(fonte="manual")
    await state.set_state(CheckinForm.waiting_place)
    await m.answer("Qual **local/cidade**?", parse_mode="Markdown", reply_markup=main_keyboard())

@dp.message(CheckinForm.waiting_place)
async def checkin_place(m: Message, state: FSMContext):
    await state.update_data(local=m.text.strip())
    await state.set_state(CheckinForm.waiting_country)
    await m.answer("Qual **país**?", parse_mode="Markdown")

@dp.message(CheckinForm.waiting_country)
async def checkin_country(m: Message, state: FSMContext):
    await state.update_data(pais=m.text.strip())
    await state.set_state(CheckinForm.waiting_odo)
    await m.answer("Informe o **odômetro (km)** ou envie `-` para pular.", parse_mode="Markdown")

@dp.message(CheckinForm.waiting_odo)
async def checkin_odo(m: Message, state: FSMContext):
    odo = None
    txt = m.text.strip()
    if txt != "-":
        try:
            odo = float(re.sub(r"[^0-9,\.]", "", txt).replace(",", "."))
        except:
            await m.answer("Odômetro inválido. Envie um número (ex.: 25432) ou `-` para pular.")
            return
    await state.update_data(odo=odo)
    await state.set_state(CheckinForm.waiting_photo)
    await m.answer("Envie uma **foto** deste check-in (opcional) ou mande `-` para finalizar.", parse_mode="Markdown")

@dp.message(CheckinForm.waiting_photo, F.photo)
async def checkin_photo(m: Message, state: FSMContext):
    file_id = m.photo[-1].file_id
    await state.update_data(photo_file_id=file_id)
    await _finalize_checkin(m, state)

@dp.message(CheckinForm.waiting_photo, F.text)
async def checkin_photo_skip(m: Message, state: FSMContext):
    if m.text.strip() != "-":
        await m.answer("Envie uma **foto**, ou digite `-` para pular.")
        return
    await _finalize_checkin(m, state)

async def _finalize_checkin(m: Message, state: FSMContext):
    data = await state.get_data()
    agora = now_tz()
    row = first_empty_row_ws(ws_checkins, col=1, start_row=2)

    values = [
        agora.strftime("%d/%m/%Y"),
        agora.strftime("%H:%M"),
        TZ,
        data.get("lat", ""),
        data.get("lon", ""),
        data.get("local", ""),
        data.get("pais", ""),
        data.get("categoria", ""),
        data.get("descricao", ""),
        data.get("odo", ""),
        data.get("photo_file_id", ""),
        data.get("fonte", "manual"),
    ]
    ws_checkins.update(f"A{row}:L{row}", [values])

    legenda_linhas = [
        f"📍 *{values[5]}*, {values[6]}",
        f"🕒 {values[1]} ({values[0]})",
        f"🏷️ {values[7]}",
    ]
    if values[8]:
        legenda_linhas.append(f"📝 {values[8]}")
    if values[9] != "":
        legenda_linhas.append(f"🧭 Odômetro: {values[9]} km")
    caption = "\n".join(legenda_linhas)

    reply_markup = None
    try:
        lat = float(values[3]) if values[3] != "" else None
        lon = float(values[4]) if values[4] != "" else None
    except:
        lat = lon = None
    if lat is not None and lon is not None:
        maps_url = f"https://maps.google.com/?q={lat},{lon}"
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Ver no Maps", url=maps_url)]
        ])

    await state.clear()

    photo_file_id = values[10]
    if photo_file_id:
        await m.answer_photo(
            photo=photo_file_id,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup or None
        )
    else:
        if lat is not None and lon is not None:
            await m.answer_location(latitude=lat, longitude=lon)
        await m.answer(
            "✅ **Check-in registrado!**\n" + caption,
            parse_mode="Markdown",
            reply_markup=reply_markup or None
        )

# ================= Agenda / Roteiro HOJE =================
@dp.message(F.text.contains("Agenda"))
@dp.message(Command("hoje"))
async def agenda_hoje(m: Message, state: FSMContext):
    await state.clear()
    hoje = today_str_br()

    trecho = None
    try:
        sheet_roteiro = sh.worksheet("Roteiro da Viagem")
        linhas = sheet_roteiro.get_all_values()
        for linha in linhas:
            if len(linha) >= 6 and linha[1].strip() == hoje:
                trecho = {
                    "percurso": linha[2],
                    "km": linha[4],
                    "horas": linha[5],
                    "obs": linha[6] if len(linha) > 6 else ""
                }
                break
    except Exception:
        trecho = None

    eventos = ws_eventos.get_all_values()[1:]
    eventos_hoje = [r for r in eventos if len(r) >= 5 and r[0].strip() == hoje]

    def _key(row):
        try:
            return datetime.strptime(row[1], "%H:%M")
        except:
            return datetime.max
    eventos_hoje.sort(key=_key)

    partes = [f"📌 *Agenda de hoje* ({hoje})\n"]
    if trecho:
        partes.append(
            "🛣️ *Roteiro*\n"
            f"• {trecho['percurso']}\n"
            f"• {trecho['km']} km — {trecho['horas']}\n"
            + (f"• Obs: {trecho['obs']}\n" if trecho['obs'] else "")
        )
    else:
        partes.append("🛣️ *Roteiro*\n• (não encontrado para hoje)\n")

    if eventos_hoje:
        partes.append("🗓️ *Eventos*\n" + "\n".join(
            [f"• {r[1]} — {r[2]} (*{r[3]}*){(' — ' + r[4]) if r[4] else ''}" for r in eventos_hoje]
        ))
    else:
        partes.append("🗓️ *Eventos*\n• (nenhum evento cadastrado hoje)")

    await send_md_safe(m, "\n".join(partes))

# ================= ROTEIRO (N dias / completo) =================
def _today_date():
    return now_tz().date()

@dp.message(F.text.contains("Roteiro"))
@dp.message(Command("roteiro"))
async def roteiro_perguntar_dias(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "De quantos dias pra frente você quer ver o roteiro (contando hoje)?",
        reply_markup=_build_days_inline_keyboard()
    )

@dp.callback_query(F.data.regexp(r"^roteiro:"))
async def roteiro_listar_intervalo(cb: CallbackQuery):
    try:
        await cb.answer("Carregando…")
    except:
        pass
    if DEBUG:
        print("[DBG-CB ROTEIRO]", cb.data)

    try:
        ws_roteiro = sh.worksheet("Roteiro da Viagem")
        linhas = ws_roteiro.get_all_values()
    except Exception as e:
        await cb.message.answer(f"Não consegui abrir a aba *Roteiro da Viagem*.", parse_mode="Markdown")
        return

    try:
        arg = (cb.data or "").split(":", 1)[1]
    except Exception:
        await send_md_safe(cb.message, "Callback inválido. Tente novamente com /roteiro.")
        return

    itens = []
    for row in linhas:
        if len(row) < 6:
            continue
        dia = row[0].strip()
        data_str = row[1].strip()
        if not data_str or data_str.lower() == "data":
            continue
        percurso = row[2]
        maps = row[3]
        km = row[4]
        horas = row[5]
        obs = row[6] if len(row) > 6 else ""
        itens.append({
            "dia": dia,
            "data": data_str,
            "percurso": percurso,
            "maps": maps,
            "km": km,
            "horas": horas,
            "obs": obs
        })

    def _to_date(s):
        try:
            return datetime.strptime(s, "%d/%m/%Y").date()
        except:
            return None
    itens.sort(key=lambda x: _to_date(x["data"]) or datetime.max.date())

    if arg != "all":
        try:
            n_days = int(arg)
        except:
            await send_md_safe(cb.message, "Valor inválido. Tente novamente.")
            return
        base = _today_date()
        alvo = {(base + timedelta(days=i)).strftime("%d/%m/%Y") for i in range(n_days)}
        itens = [it for it in itens if it["data"] in alvo]
        titulo = f"🧭 *Roteiro — próximos {n_days} dia(s)* (inclui hoje)\n"
    else:
        titulo = "🧭 *Roteiro completo*\n"

    if not itens:
        await send_md_safe(cb.message, "Não encontrei trechos para o período selecionado.")
        return

    bloco = titulo

    async def _send(text):
        await send_md_safe(cb.message, f"Não consegui abrir a aba *Roteiro da Viagem*. Contate o Desenvolvedor.")

    for it in itens:
        linha = (
            f"\n📅 *{it['data']}* — Dia {it['dia']}\n"
            f"🛣️ {it['percurso']}\n"
            f"⏱️ {it['km']} km — {it['horas']}\n"
        )
        if it["obs"]:
            linha += f"📝 {it['obs']}\n"
        if it["maps"] and it["maps"].startswith("http"):
            linha += f"🔗 {it['maps']}\n"

        if len(bloco) + len(linha) > TELEGRAM_CHUNK:
            await _send(bloco)
            bloco = ""
        bloco += linha

    if bloco.strip():
        await _send(bloco)

# ================= Resumo de Custos =================
class CustosPeriodoForm(StatesGroup):
    waiting_start = State()
    waiting_end = State()

def _sumarizar_custos_por_periodo(start_date=None, end_date=None):
    linhas = ws_custos.get_all_values()
    dados = linhas[2:] if len(linhas) > 2 else []

    total = 0.0
    por_tipo = {}
    count = 0

    for r in dados:
        if len(r) < 4:
            continue
        data_s, _cidade, custo_s, tipo = r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip()
        d = _parse_br_date(data_s)
        if not d:
            continue

        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue

        try:
            valor = normalize_money(custo_s)
        except Exception:
            try:
                valor = float(custo_s)
            except Exception:
                continue

        total += valor
        por_tipo[tipo.lower()] = por_tipo.get(tipo.lower(), 0.0) + valor
        count += 1

    if start_date and end_date:
        periodo_str = f"{start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')}"
    elif start_date:
        periodo_str = f"desde {start_date.strftime('%d/%m/%Y')}"
    elif end_date:
        periodo_str = f"até {end_date.strftime('%d/%m/%Y')}"
    else:
        periodo_str = "todas as datas"

    return total, por_tipo, count, periodo_str

def _formatar_resumo(total, por_tipo, count, periodo_str):
    linhas = [f"📊 *Resumo de custos* — _{periodo_str}_"]
    if count == 0:
        linhas.append("Nenhum lançamento encontrado nesse período.")
        return "\n".join(linhas)

    linhas.append(f"• Lançamentos: *{count}*")
    linhas.append("• Por tipo:")
    for tipo, val in sorted(por_tipo.items(), key=lambda x: x[1], reverse=True):
        linhas.append(f"    — *{tipo.capitalize()}*: R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    linhas.append(f"\n💰 **Total**: *R$ {total:,.2f}*".replace(",", "X").replace(".", ",").replace("X", "."))
    return "\n".join(linhas)

@dp.message(F.text.contains("Resumo de custos"))
@dp.message(Command("custos"))
async def custos_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Escolha o período para resumir os custos:", reply_markup=_custos_inline_keyboard())

@dp.callback_query(F.data.regexp(r"^custos:"))
async def custos_callback(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.answer()
    except:
        pass
    if DEBUG:
        print("[DBG-CB CUSTOS]", cb.data)

    arg = (cb.data or "").split(":", 1)[1]

    if arg == "today":
        d = now_tz().date()
        total, por_tipo, count, periodo = _sumarizar_custos_por_periodo(d, d)
        await cb.message.answer(_formatar_resumo(total, por_tipo, count, periodo), parse_mode="Markdown")
        return

    if arg == "7d":
        start, end = _last_7_days_range()
        total, por_tipo, count, periodo = _sumarizar_custos_por_periodo(start, end)
        await cb.message.answer(_formatar_resumo(total, por_tipo, count, periodo), parse_mode="Markdown")
        return

    if arg == "month":
        start, end = _month_range_today()
        total, por_tipo, count, periodo = _sumarizar_custos_por_periodo(start, end)
        await cb.message.answer(_formatar_resumo(total, por_tipo, count, periodo), parse_mode="Markdown")
        return

    if arg == "all":
        total, por_tipo, count, periodo = _sumarizar_custos_por_periodo(None, None)
        await cb.message.answer(_formatar_resumo(total, por_tipo, count, periodo), parse_mode="Markdown")
        return

    if arg == "period":
        await state.set_state(CustosPeriodoForm.waiting_start)
        await cb.message.answer("Envie a **data inicial** no formato *dd/mm/aaaa* (ex.: 18/10/2025).", parse_mode="Markdown")
        return

@dp.message(CustosPeriodoForm.waiting_start)
async def custos_periodo_inicio(m: Message, state: FSMContext):
    d = _parse_br_date(m.text)
    if not d:
        await m.answer("Data inválida. Envie no formato *dd/mm/aaaa*.", parse_mode="Markdown")
        return
    await state.update_data(start=d)
    await state.set_state(CustosPeriodoForm.waiting_end)
    await m.answer("Agora envie a **data final** no formato *dd/mm/aaaa*.", parse_mode="Markdown")

@dp.message(CustosPeriodoForm.waiting_end)
async def custos_periodo_fim(m: Message, state: FSMContext):
    end = _parse_br_date(m.text)
    if not end:
        await m.answer("Data inválida. Envie no formato *dd/mm/aaaa*.", parse_mode="Markdown")
        return

    data = await state.get_data()
    start = data.get("start")
    if end < start:
        start, end = end, start

    total, por_tipo, count, periodo = _sumarizar_custos_por_periodo(start, end)
    await state.clear()
    await m.answer(_formatar_resumo(total, por_tipo, count, periodo), parse_mode="Markdown", reply_markup=main_keyboard())

# ================= Catch-all (debug) =================
@dp.message()
async def _catch_all_log(m: Message):
    try:
        print(f"[DBG] text={repr(m.text)} chat={m.chat.type} from={m.from_user.id}")
    except Exception:
        pass
    if DEBUG and m.text:
        await m.answer(f"(debug) Recebi: `{m.text}`", parse_mode="Markdown")

# ================= Run =================
async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

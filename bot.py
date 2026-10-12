import os
import sys
import logging
import asyncio
import sqlite3
from datetime import date
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
import yt_dlp
from groq import Groq
import stripe

# Carrega variáveis de ambiente
load_dotenv()

# Ajuste de Loop para Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
STRIPE_PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK")
stripe.api_key = STRIPE_API_KEY

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

CREDITOS_FREE_DIARIOS = 3

groq_client = Groq(api_key=GROQ_API_KEY)

# Dicionário em memória para guardar o estado temporário do utilizador
user_states = {}

# Mapeamento de idiomas (10 opções)
IDIOMAS = {
    "pt": "🇧🇷 Português",
    "en": "🇺🇸 Inglês",
    "es": "🇪🇸 Espanhol",
    "fr": "🇫🇷 Francês",
    "de": "🇩🇪 Alemão",
    "it": "🇮🇹 Italiano",
    "ru": "🇷🇺 Russo",
    "ja": "🇯🇵 Japonês",
    "zh": "🇨🇳 Chinês",
    "ar": "🇸🇦 Árabe"
}

# === SETUP DO BANCO DE DADOS ===
def inicializar_banco():
    conn = sqlite3.connect('tradubot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id INTEGER PRIMARY KEY,
            creditos INTEGER,
            data_ultimo_uso TEXT,
            is_premium BOOLEAN DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

inicializar_banco()

# === LÓGICA DE LIMITES E ACESSO ===
def verificar_acesso(user_id):
    if user_id == ADMIN_ID:
        return True

    hoje = date.today().isoformat()
    conn = sqlite3.connect('tradubot.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT creditos, data_ultimo_uso, is_premium FROM usuarios WHERE user_id = ?', (user_id,))
    resultado = cursor.fetchone()
    
    if resultado is None:
        cursor.execute('INSERT INTO usuarios (user_id, creditos, data_ultimo_uso) VALUES (?, ?, ?)', 
                       (user_id, CREDITOS_FREE_DIARIOS, hoje))
        conn.commit()
        conn.close()
        return True
        
    creditos, data_ultimo_uso, is_premium = resultado
    
    if is_premium:
        conn.close()
        return True
        
    if data_ultimo_uso != hoje:
        cursor.execute('UPDATE usuarios SET creditos = ?, data_ultimo_uso = ? WHERE user_id = ?', 
                       (CREDITOS_FREE_DIARIOS, hoje, user_id))
        conn.commit()
        conn.close()
        return True
        
    if creditos > 0:
        conn.close()
        return True
    
    conn.close()
    return False

def descontar_credito(user_id):
    if user_id == ADMIN_ID:
        return
    conn = sqlite3.connect('tradubot.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE usuarios SET creditos = creditos - 1 WHERE user_id = ? AND is_premium = 0', (user_id,))
    conn.commit()
    conn.close()

def devolver_credito(user_id):
    if user_id == ADMIN_ID:
        return
    conn = sqlite3.connect('tradubot.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE usuarios SET creditos = creditos + 1 WHERE user_id = ? AND is_premium = 0', (user_id,))
    conn.commit()
    conn.close()

# === FUNÇÕES DE ÁUDIO E IA ===
def baixar_audio(url):
    ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'audio_temp_%(id)s.%(ext)s',
            'ffmpeg_location': FFMPEG_PATH,
            'quiet': True,
            'overwrites': True,
            'noplaylist': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            },
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '128',
            }]
        }
    
    if "instagram.com" in url:
        ydl_opts['cookiefile'] = 'cookies_inst.txt'
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filename)
        return base + ".m4a"

def processar_ia(caminho_arquivo, modo, idioma_alvo=None):
    logging.info("A enviar áudio para transcrição (Whisper)...")
    
    with open(caminho_arquivo, "rb") as audio_file:
        transcricao = groq_client.audio.transcriptions.create(
            file=(caminho_arquivo, audio_file.read()),
            model="whisper-large-v3",
            response_format="text" 
        )
    
    if not transcricao or len(transcricao.strip()) < 10:
        raise Exception("O áudio estava vazio ou a transcrição falhou.")

    logging.info("Áudio transcrito. A gerar processamento com LLM...")

    # Escolha do prompt com base no modo
    if modo == "1":
        system_prompt = (
            "És um formatador de texto estritamente literal. A tua ÚNICA tarefa é pegar na transcrição bruta de áudio "
            "e adicionar quebras de parágrafo (linhas em branco) para facilitar a leitura. "
            "REGRA ABSOLUTA: É expressamente proibido alterar, adicionar, remover ou substituir qualquer palavra. "
            "Deves manter o vocabulário, as gírias, os erros gramaticais e as repetições originais intactos. "
            "Devolve o texto 100% fiel ao original, apenas organizado visualmente em parágrafos."
        )
    elif modo == "2":
        nome_idioma = IDIOMAS.get(idioma_alvo, "Português")
        system_prompt = (
            f"És um professor de idiomas. Aqui está uma transcrição de um vídeo. "
            f"A tua tarefa é quebrar o texto em parágrafos lógicos ou frases curtas. Para CADA bloco, "
            f"escreve primeiro o texto no idioma original em negrito e logo abaixo a tradução para o {nome_idioma}. "
            f"Usa o formato rigoroso:\n\n**Original:** [texto na língua falada]\n**Tradução:** [texto em {nome_idioma}]\n\n"
            f"Faz isso para todo o texto, bloco por bloco."
        )
    else: # modo 3
        nome_idioma = IDIOMAS.get(idioma_alvo, "Português")
        system_prompt = (
            f"És um tradutor profissional nativo. Traduz a transcrição a seguir inteiramente para o {nome_idioma}. "
            f"Fornece APENAS o texto traduzido final, bem formatado e pontuado em parágrafos, sem incluir partes do texto original."
        )

    resposta_llm = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Aqui está a transcrição bruta do vídeo:\n\n{transcricao}"}
        ],
        model="openai/gpt-oss-20b",
        temperature=0.3,
        max_tokens=3000
    )

    return resposta_llm.choices[0].message.content

# === HANDLERS DO TELEGRAM ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text(
        "👋 Olá! Envia um link do **Instagram** ou **TikTok** e eu farei a transcrição/tradução.\n\n"
        f"🎁 Tens um limite de traduções gratuitas diárias.",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    user_id = update.message.from_user.id

    if not url or ("instagram.com" not in url and "tiktok.com" not in url):
        await update.message.reply_text("❌ Por favor, envia apenas links válidos do **Instagram** ou **TikTok**.")
        return

    if not verificar_acesso(user_id):
        link_pagamento = f"{STRIPE_PAYMENT_LINK}?client_reference_id={user_id}"
        keyboard = [
            [InlineKeyboardButton("🪙 Comprar Créditos Avulsos", url=link_pagamento)],
            [InlineKeyboardButton("💎 Assinar Plano Ilimitado", url=link_pagamento)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚠️ **Limite Gratuito Atingido!**\n\nAdquire créditos avulsos (pagamento único) ou assina o plano premium ilimitado para continuar a traduzir vídeos.",
            parse_mode="Markdown", reply_markup=reply_markup
        )
        return

    # Salva o estado do utilizador temporariamente
    user_states[user_id] = {'url': url}

    # Opções amigáveis
    keyboard = [
        [InlineKeyboardButton("📝 1. Texto Original (Apenas Formatar)", callback_data="modo_1")],
        [InlineKeyboardButton("📚 2. Texto Original + Tradução (Para estudo)", callback_data="modo_2")],
        [InlineKeyboardButton("🌐 3. Apenas Tradução Direta", callback_data="modo_3")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Escolhe o que desejas fazer com este vídeo:", reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_states or 'url' not in user_states[user_id]:
        await query.edit_message_text("❌ Sessão expirada. Envia o link novamente.")
        return

    if data.startswith("modo_"):
        modo = data.split("_")[1]
        user_states[user_id]['modo'] = modo

        if modo == "1":
            await query.edit_message_text("⏳ A processar...")
            await iniciar_processamento(query.message, user_id)
        else:
            keyboard = [
                [InlineKeyboardButton(IDIOMAS["pt"], callback_data="lang_pt"), InlineKeyboardButton(IDIOMAS["en"], callback_data="lang_en")],
                [InlineKeyboardButton(IDIOMAS["es"], callback_data="lang_es"), InlineKeyboardButton(IDIOMAS["fr"], callback_data="lang_fr")],
                [InlineKeyboardButton(IDIOMAS["de"], callback_data="lang_de"), InlineKeyboardButton(IDIOMAS["it"], callback_data="lang_it")],
                [InlineKeyboardButton(IDIOMAS["ru"], callback_data="lang_ru"), InlineKeyboardButton(IDIOMAS["ja"], callback_data="lang_ja")],
                [InlineKeyboardButton(IDIOMAS["zh"], callback_data="lang_zh"), InlineKeyboardButton(IDIOMAS["ar"], callback_data="lang_ar")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Para qual idioma desejas traduzir?", reply_markup=reply_markup)

    elif data.startswith("lang_"):
        idioma = data.split("_")[1]
        user_states[user_id]['idioma'] = idioma
        await query.edit_message_text(f"⏳ A processar no idioma alvo: {IDIOMAS[idioma]}...")
        await iniciar_processamento(query.message, user_id)

async def iniciar_processamento(message, user_id):
    url = user_states[user_id]['url']
    modo = user_states[user_id]['modo']
    idioma = user_states[user_id].get('idioma', 'pt')
    
    descontar_credito(user_id)

    audio_file = None
    try:
        audio_file = await asyncio.to_thread(baixar_audio, url)
        resultado_ia = await asyncio.to_thread(processar_ia, audio_file, modo, idioma)

        # === LÓGICA DE DIVISÃO DE MENSAGENS ===
        MAX_LENGTH = 4000
        partes = []
        texto_restante = resultado_ia
        
        while len(texto_restante) > MAX_LENGTH:
            ponto_corte = texto_restante.rfind('\n', 0, MAX_LENGTH)
            
            if ponto_corte == -1:
                ponto_corte = MAX_LENGTH
            
            partes.append(texto_restante[:ponto_corte])
            texto_restante = texto_restante[ponto_corte:].lstrip()
            
        partes.append(texto_restante)

        for i, parte in enumerate(partes):
            texto_envio = parte
            
            if len(partes) > 1:
                texto_envio = f"*(Parte {i+1}/{len(partes)})*\n\n{parte}"
            
            try:
                await message.reply_text(texto_envio, parse_mode="Markdown")
            except Exception as telegram_err:
                if "parse entities" in str(telegram_err).lower():
                    logging.warning("Falha na formatação Markdown. A enviar texto puro.")
                    await message.reply_text(texto_envio)
                else:
                    raise telegram_err
            
            await asyncio.sleep(0.5)

    except Exception as e:
        logging.error(f"Erro: {e}")
        devolver_credito(user_id)
        await message.reply_text(f"❌ Ocorreu um erro: {str(e)}\n\nO teu crédito foi devolvido.")
        
    finally:
        if user_id in user_states:
            del user_states[user_id]
            
        if audio_file and os.path.exists(audio_file):
            try: os.remove(audio_file)
            except: pass
            
        arquivos_na_pasta = os.listdir('.')
        for arquivo in arquivos_na_pasta:
            if arquivo.startswith('audio_temp_') and arquivo != audio_file:
                try: os.remove(arquivo)
                except: pass

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    print("🤖 Tradubot em execução (Modo 10 Idiomas Globais)...")
    app.run_polling()

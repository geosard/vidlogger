import os
import sys
import logging
import asyncio
import sqlite3
from datetime import date
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
import yt_dlp
from groq import Groq
import stripe

# Carrega variáveis de ambiente do ficheiro .env
load_dotenv()

# Ajuste de Loop para Windows (evita erros caso teste localmente antes de enviar para a VPS)
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Configuração de logs para monitorizar erros e acessos no terminal
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# === CARREGAMENTO DE VARIÁVEIS ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg") # Usa "ffmpeg" como padrão se não for definido

# Stripe
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
STRIPE_PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK")
stripe.api_key = STRIPE_API_KEY

# Tratamento do ADMIN_ID para evitar erros se estiver vazio no .env
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

CREDITOS_FREE_DIARIOS = 3

# Inicializa o cliente da API da Groq
groq_client = Groq(api_key=GROQ_API_KEY)

# === SETUP DO BANCO DE DADOS (SQLite) ===
def inicializar_banco():
    """Cria o ficheiro do banco de dados e a tabela se não existirem."""
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

# Executa a inicialização ao arrancar o bot
inicializar_banco()

# === LÓGICA DE LIMITES E ACESSO ===
def verificar_acesso(user_id):
    """
    Controla o acesso:
    - Retorna True se o utilizador tem permissão (Admin, Premium ou tem créditos gratuitos).
    - Retorna False se o limite diário gratuito esgotou.
    """
    if user_id == ADMIN_ID:
        return True

    hoje = date.today().isoformat()
    conn = sqlite3.connect('tradubot.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT creditos, data_ultimo_uso, is_premium FROM usuarios WHERE user_id = ?', (user_id,))
    resultado = cursor.fetchone()
    
    # 1. Utilizador novo
    if resultado is None:
        cursor.execute('INSERT INTO usuarios (user_id, creditos, data_ultimo_uso) VALUES (?, ?, ?)', 
                       (user_id, CREDITOS_FREE_DIARIOS - 1, hoje))
        conn.commit()
        conn.close()
        return True
        
    creditos, data_ultimo_uso, is_premium = resultado
    
    # 2. Utilizador Premium (Acesso ilimitado)
    if is_premium:
        conn.close()
        return True
        
    # 3. Mudança de dia: Reseta os créditos
    if data_ultimo_uso != hoje:
        cursor.execute('UPDATE usuarios SET creditos = ?, data_ultimo_uso = ? WHERE user_id = ?', 
                       (CREDITOS_FREE_DIARIOS - 1, hoje, user_id))
        conn.commit()
        conn.close()
        return True
        
    # 4. Ainda tem créditos hoje
    if creditos > 0:
        cursor.execute('UPDATE usuarios SET creditos = creditos - 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    
    # 5. Esgotou os créditos
    conn.close()
    return False

# === FUNÇÕES DE ÁUDIO E IA ===
def baixar_audio(url):
    """Baixa o áudio do Instagram ou TikTok usando o yt-dlp."""
    ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'audio_temp.%(ext)s',
            'ffmpeg_location': FFMPEG_PATH,
            'quiet': True,
            'overwrites': True,
            'noplaylist': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
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

def processar_ia(caminho_arquivo):
    """Transcreve com Whisper e resume com LLaMA 3 via Groq."""
    logging.info("Enviando áudio para transcrição (Whisper)...")
    
    with open(caminho_arquivo, "rb") as audio_file:
        transcricao = groq_client.audio.transcriptions.create(
            file=(caminho_arquivo, audio_file.read()),
            model="whisper-large-v3",
            prompt="A transcrição a seguir é em português.",
            response_format="text",
            language="pt"
        )
    
    if not transcricao or len(transcricao.strip()) < 10:
        raise Exception("O áudio estava vazio ou a transcrição falhou.")

    logging.info("Áudio transcrito. Gerando resumo com LLaMA...")

    resposta_llm = groq_client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": "Você é um assistente especialista em analisar vídeos. Analise a transcrição fornecida. Forneça primeiro um resumo executivo claro com os principais pontos discutidos e, em seguida, adicione os tópicos de destaque em bullet points. Responda em Português."
            },
            {
                "role": "user",
                "content": f"Aqui está a transcrição do vídeo:\n\n{transcricao}"
            }
        ],
        model="llama3-8b-8192", 
        temperature=0.5,
        max_tokens=2000
    )

    return resposta_llm.choices[0].message.content

# === HANDLERS DO TELEGRAM ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde ao comando /start e imprime o ID do utilizador no terminal."""
    user_id = update.message.from_user.id
    logging.info(f"O utilizador iniciou o bot. ID: {user_id}")
    await update.message.reply_text(
        "👋 Olá! Envie um link do **Instagram** ou **TikTok** e eu farei a transcrição e o resumo usando IA.\n\n"
        f"Você tem limite de {CREDITOS_FREE_DIARIOS} transcrições gratuitas por dia.",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa as mensagens de texto (links)."""
    url = update.message.text
    user_id = update.message.from_user.id

    if not url or ("instagram.com" not in url and "tiktok.com" not in url):
        await update.message.reply_text("❌ Por favor, envie apenas links válidos do **Instagram** ou **TikTok**.")
        return

    # 1. Verifica se o utilizador pode processar
    if not verificar_acesso(user_id):
        # Gera o botão da Stripe anexando o ID do utilizador para rastreamento
        link_pagamento = f"{STRIPE_PAYMENT_LINK}?client_reference_id={user_id}"
        keyboard = [[InlineKeyboardButton("💎 Assinar Premium (Cartão/Pix)", url=link_pagamento)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        texto_venda = (
            "⚠️ **Limite Gratuito Atingido!**\n\n"
            "Usou todas as suas traduções gratuitas de hoje. Para continuar a traduzir vídeos sem limites, faça o upgrade para o plano Premium.\n\n"
            "Aceitamos Cartão, Apple Pay, Google Pay e Pix!"
        )
        await update.message.reply_text(texto_venda, parse_mode="Markdown", reply_markup=reply_markup)
        return

    await update.message.reply_text("🔗 Extraindo o áudio do vídeo...")

    audio_file = None
    try:
        # 2. Faz o download do áudio
        audio_file = await asyncio.to_thread(baixar_audio, url)
        await update.message.reply_text("🎧 Áudio extraído! A transcrever e resumir com IA...")

        # 3. Processa na IA
        resultado_ia = await asyncio.to_thread(processar_ia, audio_file)

        # Corta a mensagem se ultrapassar o limite do Telegram (4096 caracteres)
        if len(resultado_ia) > 4000:
            resultado_ia = resultado_ia[:4000] + "\n\n[Resumo cortado por excesso de tamanho]"

        # 4. Envia o resultado
        try:
            await update.message.reply_text(f"✨ **Resumo:**\n\n{resultado_ia}", parse_mode="Markdown")
        except Exception as telegram_err:
            if "parse entities" in str(telegram_err).lower():
                logging.warning("Falha na formatação Markdown. A enviar texto puro.")
                await update.message.reply_text(f"✨ Resumo:\n\n{resultado_ia}")
            else:
                raise telegram_err

    except Exception as e:
        logging.error(f"Erro ao processar o vídeo de {user_id}: {e}")
        
        # Como ocorreu um erro, devolvemos o crédito ao utilizador
        conn = sqlite3.connect('tradubot.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE usuarios SET creditos = creditos + 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"❌ Ocorreu um erro: {str(e)}\nSeu crédito foi devolvido.")
        
    finally:
        # 5. Limpeza de ficheiros temporários
        if audio_file and os.path.exists(audio_file):
            os.remove(audio_file)
        
        arquivos_na_pasta = os.listdir('.')
        for arquivo in arquivos_na_pasta:
            if arquivo.startswith('audio_temp.') and arquivo != audio_file:
                os.remove(arquivo)

if __name__ == '__main__':
    # Cria a aplicação
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Regista os comandos e os processadores de texto
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("🤖 Tradubot em execução (Foco: Insta/TikTok | Banco: SQLite | Pagamento: Stripe)...")
    app.run_polling()

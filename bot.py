import os
import sys
import logging
import asyncio
import time
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
import yt_dlp
from google import genai
from google.genai import types

load_dotenv()

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

def baixar_audio(url):
    """Baixa o áudio selecionando o cookie correto conforme a plataforma"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audio_temp.%(ext)s',
        'ffmpeg_location': FFMPEG_PATH,
        'quiet': True,
        'overwrites': True,
        'noplaylist': True,
        # TRUQUE ATUALIZADO: Combina clientes para burlar o bloqueio web
        'extractor_args': {'youtube': ['player_client=android,web']}
    }
    
    # Usa cookies APENAS no Instagram. 
    # O YouTube baixará vídeos públicos nativamente disfarçado de Android.
    if "instagram.com" in url:
        ydl_opts['cookiefile'] = 'cookies_inst.txt'
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename

def processar_ia(caminho_arquivo, extensao):
    """Faz upload para o Google suportando vídeos longos (ex: aulas de 2h)"""
    max_tentativas = 3
    arquivo_gemini = None
    
    try:
        arquivo_gemini = ai_client.files.upload(file=caminho_arquivo)
        
        for tentativa in range(max_tentativas):
            try:
                response = ai_client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=[
                        arquivo_gemini,
                        (
                            "Por favor, analise este áudio de vídeo. "
                            "Forneça primeiro um resumo executivo claro com os principais pontos discutidos, "
                            "e em seguida adicione os tópicos de destaque. Responda em Português."
                        )
                    ]
                )
                return response.text
                
            except Exception as e:
                if "503" in str(e) and tentativa < max_tentativas - 1:
                    logging.warning(f"Servidor ocupado. Aguardando 10s...")
                    time.sleep(10)
                else:
                    raise e
    finally:
        if arquivo_gemini:
            try:
                ai_client.files.delete(name=arquivo_gemini.name)
            except Exception as e:
                logging.error(f"Erro ao deletar arquivo: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensagem_boas_vindas = (
        "👋 Olá! Eu sou o seu bot assistente de transcrição e resumo de vídeos.\n\n"
        "**O que eu faço?**\n"
        "Eu consigo extrair o áudio de links de vídeos (como YouTube, Instagram, Vimeo), transcrever o conteúdo e gerar um resumo estruturado.\n\n"
        "**Como usar:**\n"
        "Basta me enviar o link de um vídeo e aguardar. Eu farei todo o trabalho pesado!"
    )
    await update.message.reply_text(mensagem_boas_vindas, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text

    if not user_text or "http" not in user_text:
        await update.message.reply_text("Por favor, envie um link válido de vídeo.")
        return

    await update.message.reply_text("🔗 Baixando e extraindo o áudio bruto...")

    audio_file = None
    try:
        audio_file = await asyncio.to_thread(baixar_audio, user_text)
        
        await update.message.reply_text("🎧 Áudio extraído! Fazendo upload e analisando (isso pode levar um tempo para vídeos muito longos)...")

        extensao = os.path.splitext(audio_file)[1].lower()
        resultado_ia = await asyncio.to_thread(processar_ia, audio_file, extensao)

        if len(resultado_ia) > 4000:
            resultado_ia = resultado_ia[:4000] + "\n\n[Resumo cortado por excesso de tamanho]"

        await update.message.reply_text(f"✨ **Resumo do Vídeo:**\n\n{resultado_ia}", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Erro ao processar: {e}")
        await update.message.reply_text(f"❌ Ocorreu um erro ao processar o vídeo: {str(e)}")
        
    finally:
        if audio_file and os.path.exists(audio_file):
            os.remove(audio_file)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("🤖 Bot rodando com upload inteligente e suporte a Cookies (YouTube/Insta)...")
    app.run_polling()
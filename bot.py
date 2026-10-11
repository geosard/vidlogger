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
    """Baixa o áudio exclusivamente do Instagram e TikTok"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audio_temp.%(ext)s',
        'ffmpeg_location': FFMPEG_PATH,
        'quiet': True,
        'overwrites': True,
        'noplaylist': True
    }
    
    # Aplica cookies apenas para o Instagram. TikTok público costuma baixar direto.
    if "instagram.com" in url:
        ydl_opts['cookiefile'] = 'cookies_inst.txt'
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename

def processar_ia(caminho_arquivo, extensao):
    """Upload e análise focada nos motores atualizados do Gemini"""
    max_tentativas_por_modelo = 3
    modelos_disponiveis = [
        'gemini-2.5-flash',
        'gemini-2.0-flash',
        'gemini-2.5-pro'
    ]
    arquivo_gemini = None
    
    try:
        logging.info("Fazendo upload do arquivo para o Gemini...")
        arquivo_gemini = ai_client.files.upload(file=caminho_arquivo)
        
        for modelo in modelos_disponiveis:
            for tentativa in range(max_tentativas_por_modelo):
                try:
                    logging.info(f"Analisando com o motor: {modelo} (Tentativa {tentativa + 1})")
                    
                    response = ai_client.models.generate_content(
                        model=modelo,
                        contents=[
                            arquivo_gemini,
                            (
                                "Analise este áudio. Forneça um resumo executivo claro com os "
                                "principais pontos discutidos e os tópicos de destaque. Responda em Português."
                            )
                        ]
                    )
                    return response.text
                    
                except Exception as e:
                    if "503" in str(e) or "429" in str(e):
                        if tentativa < max_tentativas_por_modelo - 1:
                            time.sleep(10)
                        else:
                            break
                    else:
                        raise e
        
        raise Exception("Todos os motores de IA estão sobrecarregados. Tente novamente em alguns minutos.")
        
    finally:
        if arquivo_gemini:
            try:
                ai_client.files.delete(name=arquivo_gemini.name)
            except Exception as e:
                logging.error(f"Erro ao deletar arquivo: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Olá! Envie um link do **Instagram** ou **TikTok** e eu farei a transcrição e o resumo do vídeo para você.",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text

    if not url or ("instagram.com" not in url and "tiktok.com" not in url):
        await update.message.reply_text("❌ Por favor, envie apenas links válidos do **Instagram** ou **TikTok**.")
        return

    await update.message.reply_text("🔗 Extraindo o áudio do vídeo...")

    audio_file = None
    try:
        audio_file = await asyncio.to_thread(baixar_audio, url)
        await update.message.reply_text("🎧 Áudio extraído! Analisando com Inteligência Artificial...")

        extensao = os.path.splitext(audio_file)[1].lower()
        resultado_ia = await asyncio.to_thread(processar_ia, audio_file, extensao)

        if len(resultado_ia) > 4000:
            resultado_ia = resultado_ia[:4000] + "\n\n[Resumo cortado por excesso de tamanho]"

        await update.message.reply_text(f"✨ **Resumo:**\n\n{resultado_ia}", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Erro ao processar: {e}")
        await update.message.reply_text(f"❌ Ocorreu um erro: {str(e)}")
        
    finally:
        if audio_file and os.path.exists(audio_file):
            os.remove(audio_file)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("🤖 Bot rodando focado em Instagram e TikTok...")
    app.run_polling()

import os
import sys
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
import yt_dlp
from groq import Groq

# Carrega variáveis de ambiente
load_dotenv()

# Ajuste de Loop
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Configuração de logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")

# Inicializa o cliente da Groq
groq_client = Groq(api_key=GROQ_API_KEY)

def baixar_audio(url):
    """Baixa o áudio exclusivamente do Instagram e TikTok"""
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
        # O yt-dlp pode mudar a extensão baseada no postprocessor
        base, _ = os.path.splitext(filename)
        return base + ".m4a"

def processar_ia(caminho_arquivo):
    """Utiliza a Groq: Whisper para transcrição e LLaMA 3 para resumo"""
    
    logging.info("Enviando áudio para transcrição (Whisper)...")
    
    # 1. Transcreve o áudio para texto
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

    logging.info("Áudio transcrito. Gerando resumo com LLaMA 3...")

    # 2. Envia o texto para o LLM resumir
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
        model="openai/gpt-oss-20b",
        temperature=0.5,
        max_tokens=2000
    )

    return resposta_llm.choices[0].message.content

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Olá! Envie um link do **Instagram** ou **TikTok** e eu farei a transcrição e o resumo usando a tecnologia ultra-rápida da Groq.",
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
        await update.message.reply_text("🎧 Áudio extraído! Transcrevendo e resumindo com IA (Groq)...")

        resultado_ia = await asyncio.to_thread(processar_ia, audio_file)

        if len(resultado_ia) > 4000:
            resultado_ia = resultado_ia[:4000] + "\n\n[Resumo cortado por excesso de tamanho]"

        try:
            await update.message.reply_text(f"✨ **Resumo:**\n\n{resultado_ia}", parse_mode="Markdown")
        except Exception as telegram_err:
            if "parse entities" in str(telegram_err).lower():
                logging.warning("Falha na formatação Markdown. Enviando texto puro.")
                await update.message.reply_text(f"✨ Resumo:\n\n{resultado_ia}")
            else:
                raise telegram_err

    except Exception as e:
        logging.error(f"Erro ao processar: {e}")
        await update.message.reply_text(f"❌ Ocorreu um erro: {str(e)}")
        
    finally:
        # Verifica se o arquivo final m4a ou os temporários existem e os apaga
        if audio_file and os.path.exists(audio_file):
            os.remove(audio_file)
        
        # Limpa possíveis arquivos residuais baixados antes da conversão do ffmpeg
        arquivos_na_pasta = os.listdir('.')
        for arquivo in arquivos_na_pasta:
            if arquivo.startswith('audio_temp.') and arquivo != audio_file:
                os.remove(arquivo)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("🤖 Bot rodando (Foco: Insta/TikTok | IA: Groq + Whisper + LLaMA 3)...")
    app.run_polling()

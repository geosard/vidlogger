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

# Carrega variáveis de ambiente
load_dotenv()

# Ajuste de Loop (evita erros em algumas plataformas)
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Configuração de logs no terminal
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

def baixar_audio(url):
    """Baixa o áudio selecionando o cookie e cliente corretos conforme a plataforma"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audio_temp.%(ext)s',
        'ffmpeg_location': FFMPEG_PATH,
        'quiet': True,
        'overwrites': True,
        'noplaylist': True
    }
    
    if "instagram.com" in url:
        ydl_opts['cookiefile'] = 'cookies_inst.txt'
    else:
        # YouTube: Usa cookies e força clientes de TV/iOS para evitar a barreira web (The page needs to be reloaded)
        ydl_opts['cookiefile'] = 'cookies_yt.txt'
        ydl_opts['extractor_args'] = {'youtube': ['player_client=ios,tv']}
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename

def processar_ia(caminho_arquivo, extensao):
    """Faz upload para o Google com alternância (fallback) automática entre motores de IA"""
    max_tentativas_por_modelo = 3
    # Lista de motores de IA em ordem de prioridade
    modelos_disponiveis = [
        'gemini-1.5-flash',       # Mais rápido e balanceado
        'gemini-1.5-pro',         # Mais inteligente e potente
        'gemini-1.5-flash-8b'     # Mais leve, ótimo para backup
    ]
    arquivo_gemini = None
    
    try:
        logging.info("Fazendo upload do arquivo para o Gemini...")
        arquivo_gemini = ai_client.files.upload(file=caminho_arquivo)
        
        # O bot vai testar cada motor da lista
        for modelo in modelos_disponiveis:
            for tentativa in range(max_tentativas_por_modelo):
                try:
                    logging.info(f"Tentando analisar com o motor: {modelo} (Tentativa {tentativa + 1})")
                    
                    response = ai_client.models.generate_content(
                        model=modelo,
                        contents=[
                            arquivo_gemini,
                            (
                                "Por favor, analise este áudio de vídeo. "
                                "Forneça primeiro um resumo executivo claro com os principais pontos discutidos, "
                                "e em seguida adicione os tópicos de destaque. Responda em Português."
                            )
                        ]
                    )
                    return response.text # Devolve o texto e encerra a função se der certo
                    
                except Exception as e:
                    erro_str = str(e)
                    # Verifica se é erro de servidor ocupado (503) ou cota (429)
                    if "503" in erro_str or "429" in erro_str:
                        if tentativa < max_tentativas_por_modelo - 1:
                            logging.warning(f"Motor {modelo} ocupado. Aguardando 10s...")
                            time.sleep(10)
                        else:
                            logging.warning(f"Motor {modelo} indisponível. Alternando para o próximo...")
                            break # Sai do loop de tentativas e passa para o próximo modelo da lista
                    else:
                        # Se for um erro crítico diferente de sobrecarga, interrompe
                        raise e
        
        # Se passar por todos os modelos e todos falharem
        raise Exception("Todos os motores de IA estão sobrecarregados no momento. Tente novamente em alguns minutos.")
        
    finally:
        # Garante a exclusão do arquivo da nuvem para não gastar sua cota de armazenamento
        if arquivo_gemini:
            try:
                ai_client.files.delete(name=arquivo_gemini.name)
                logging.info("Arquivo deletado do Gemini com sucesso.")
            except Exception as e:
                logging.error(f"Erro ao deletar arquivo do Gemini: {e}")

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
        await update.message.reply_text("Por favor, envie um link válido de vídeo público.")
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
    
    print("🤖 Bot rodando com motor IA inteligente, fallback e cookies nativos...")
    app.run_polling()
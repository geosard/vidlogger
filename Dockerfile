# Usa uma versão leve do Python
FROM python:3.11-slim

# Instala o FFmpeg no sistema Linux do servidor
RUN apt-get update && apt-get install -y ffmpeg

# Define a pasta de trabalho dentro do servidor
WORKDIR /app

# Copia os ficheiros e instala as bibliotecas
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o resto do código
COPY . .

# Comando que o servidor vai executar para rodar o bot
CMD ["python", "bot.py"]
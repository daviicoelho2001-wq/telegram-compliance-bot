# Empacota o bot Python + (opcional) o servidor local do Telegram Bot API no mesmo
# container, pra rodar de graça no Render sem precisar de dois serviços separados.
# O ffmpeg não é mais instalado via apt aqui — vem embutido pelo pacote Python
# imageio-ffmpeg (requirements.txt), então não depende de nada do sistema operacional.

FROM aiogram/telegram-bot-api:latest AS botapi

FROM python:3.11-slim
WORKDIR /app

# binário oficial do telegram-bot-api, copiado da imagem acima — só é usado se
# TELEGRAM_LOCAL_BOT_API=true estiver setado nas variáveis de ambiente
#
# A imagem aiogram/telegram-bot-api é baseada em Alpine (musl libc), enquanto esta
# imagem final é Debian (glibc) — os dois libc não são compatíveis, então além do
# binário também precisamos copiar o loader musl e as libs dinâmicas que ele usa
# (confirmado via ldd na imagem de origem). Os caminhos não colidem com os do
# Debian, que ficam em /usr/lib/<arch>-linux-gnu/, não direto em /usr/lib/. O nome
# do loader musl muda por arquitetura (aarch64 no Mac, x86_64 no Render) — o
# wildcard cobre as duas sem precisar fixar a arquitetura de build.
COPY --from=botapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
COPY --from=botapi /lib/ld-musl-*.so.1 /lib/
COPY --from=botapi /usr/lib/libssl.so.3 /usr/lib/libssl.so.3
COPY --from=botapi /usr/lib/libcrypto.so.3 /usr/lib/libcrypto.so.3
COPY --from=botapi /usr/lib/libz.so.1 /usr/lib/libz.so.1
COPY --from=botapi /usr/lib/libstdc++.so.6 /usr/lib/libstdc++.so.6
COPY --from=botapi /usr/lib/libgcc_s.so.1 /usr/lib/libgcc_s.so.1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

CMD ["./start.sh"]

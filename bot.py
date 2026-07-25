"""
Bot de Telegram — versão 100% gratuita (Groq + Tavily, sem Anthropic).

Recebe áudio, vídeo ou texto de experts/afiliados (Luva Bet / F12 Bet) e devolve um
parecer de compliance, usando o pipeline em pipeline.py.

Local (teste): python bot.py  -> roda em long polling automaticamente.
Produção (Render free tier): defina WEBHOOK_URL -> o bot sobe em modo webhook.
"""
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from groq import Groq
from telegram import Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

import pipeline

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("compliance-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

# opcional: se setado, o bot também posta cada parecer completo nesse canal/grupo do
# Telegram. Como o disco do Render free tier é apagado a cada redeploy, isso vira seu
# arquivo permanente e gratuito de auditoria (o histórico do canal nunca some).
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")

# opcional: aponta pra um servidor local do Telegram Bot API (telegram-bot-api rodando
# via Docker) pra destravar o limite de 20MB no download de arquivo que a API pública
# dos bots impõe. Sem isso, vídeos/áudios acima de 20MB são recusados com uma mensagem
# clara em vez de travar o bot. Veja README-deploy.md, seção "vídeos grandes".
LOCAL_BOT_API = os.environ.get("TELEGRAM_LOCAL_BOT_API", "false").lower() == "true"
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "http://localhost:8081")

MAX_TELEGRAM_CHUNK = 3500  # Telegram corta em 4096 caracteres; damos margem de segurança


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    limite = "2GB (servidor local ativo)" if LOCAL_BOT_API else "20MB (limite padrão do Telegram pra bots)"
    await update.message.reply_text(
        "Oi! Manda um áudio, vídeo ou texto de um expert e eu devolvo o parecer de "
        "compliance (Luva Bet / F12 Bet), cruzando com a legislação vigente.\n\n"
        f"Limite de arquivo agora: {limite}.\n\n"
        "Comandos:\n"
        "/atualizar_compliance — força a atualização da base legal agora"
    )


async def atualizar_compliance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Atualizando base de compliance (pode levar ~30s)...")
    try:
        pipeline.get_dossie(force=True)
        await update.message.reply_text("Base de compliance atualizada.")
    except Exception as exc:
        logger.exception("Falha ao atualizar dossiê")
        await update.message.reply_text(f"Não consegui atualizar agora: {exc}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_pipeline(update, context, content_text=update.message.text, is_transcript=False)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    tg_file = None
    suffix = ".bin"

    try:
        if msg.voice:
            tg_file = await msg.voice.get_file()
            suffix = ".ogg"
        elif msg.audio:
            tg_file = await msg.audio.get_file()
            suffix = ".mp3"
        elif msg.video:
            tg_file = await msg.video.get_file()
            suffix = ".mp4"
        elif msg.video_note:
            tg_file = await msg.video_note.get_file()
            suffix = ".mp4"
        elif msg.document:
            tg_file = await msg.document.get_file()
            suffix = Path(msg.document.file_name or "arquivo.bin").suffix or ".bin"
    except BadRequest as exc:
        if "File is too big" in str(exc):
            if LOCAL_BOT_API:
                # não deveria acontecer com o servidor local (limite sobe pra 2GB) — se
                # aconteceu, o servidor local provavelmente não está no ar
                await msg.reply_text(
                    "Esse arquivo é grande e o servidor local do Telegram não respondeu "
                    "como esperado. Confere se o container do telegram-bot-api está rodando."
                )
            else:
                await msg.reply_text(
                    "Esse arquivo passa do limite de 20MB que a API pública do Telegram "
                    "permite baixar pra bots. Duas saídas: manda só o áudio (bem mais leve), "
                    "ou configure o servidor local (veja README-deploy.md, seção 'vídeos "
                    "grandes') pra remover esse limite."
                )
        else:
            logger.exception("Falha ao baixar arquivo do Telegram")
            await msg.reply_text(f"Não consegui baixar esse arquivo: {exc}")
        return
    except TelegramError as exc:
        logger.exception("Falha de rede ao baixar arquivo do Telegram")
        await msg.reply_text(f"Não consegui baixar esse arquivo (erro de rede/timeout): {exc}")
        return

    if tg_file is None:
        await msg.reply_text("Não reconheci esse tipo de arquivo. Manda áudio, vídeo ou texto.")
        return

    await msg.reply_text("Recebido. Transcrevendo...")

    tmp_path = None
    owns_file = True
    try:
        if LOCAL_BOT_API and getattr(tg_file, "file_path", None) and os.path.exists(tg_file.file_path):
            # em modo local o telegram-bot-api já deixa o arquivo em disco — não precisa
            # baixar de novo via HTTP, só ler direto de lá (mais rápido, sem limite de 20MB)
            tmp_path = tg_file.file_path
            owns_file = False
        else:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

        transcript = pipeline.transcribe_audio(groq_client, tmp_path)
    except Exception as exc:
        logger.exception("Falha na transcrição")
        await msg.reply_text(f"Não consegui transcrever esse arquivo: {exc}")
        return
    finally:
        if owns_file and tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    await _run_pipeline(update, context, content_text=transcript, is_transcript=True)


async def _run_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE, content_text: str, is_transcript: bool):
    msg = update.message
    user = update.effective_user

    if not content_text or not content_text.strip():
        await msg.reply_text("Não veio nenhum conteúdo pra analisar.")
        return

    await msg.reply_text("Cruzando com a base de compliance e analisando...")

    try:
        dossie = pipeline.get_dossie()
        meta = {
            "expert": user.full_name if user else "desconhecido",
            "canal": "Telegram",
            "data": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
        parecer = pipeline.analyze_content(groq_client, dossie, content_text, meta)
        pipeline.save_parecer(parecer, meta["expert"])
    except Exception as exc:
        logger.exception("Falha na análise")
        await msg.reply_text(f"Não consegui gerar o parecer agora: {exc}")
        return

    # 1) veredito em destaque, curto, primeiro — pra bater o olho sem rolar a tela
    veredito = pipeline.extract_veredito(parecer)
    veredito_line = pipeline.extract_veredito_line(parecer)
    await msg.reply_text(f"Parecer pronto.\n\n{veredito_line}")

    # 2) transcrição (se houver) + parecer completo, em pedaços
    full_response = parecer
    if is_transcript:
        preview = content_text[:500] + ("..." if len(content_text) > 500 else "")
        full_response = f"Transcrição (prévia):\n{preview}\n\n{parecer}"

    for i in range(0, len(full_response), MAX_TELEGRAM_CHUNK):
        await msg.reply_text(full_response[i : i + MAX_TELEGRAM_CHUNK])

    # 3) se não foi aprovado de cara, aciona o copywriter — e revisa o roteiro gerado com a
    # MESMA régua do analista antes de mandar pra você, tentando de novo se ainda achar problema
    roteiro = None
    revisao_aprovada = None
    if veredito in ("reprovado", "ajustes"):
        await msg.reply_text("Isso precisa de ajuste — gerando um roteiro alternativo e revisando com a mesma régua de compliance...")
        try:
            resultado = pipeline.generate_and_verify_copy(groq_client, dossie, content_text, parecer, meta)
            roteiro = resultado["roteiro"]
            revisao_aprovada = resultado["aprovado"]
            pipeline.save_roteiro(roteiro, meta["expert"])

            if revisao_aprovada:
                selo = f"✅ Roteiro revisado e aprovado pela mesma régua de compliance (tentativa {resultado['tentativas']}/{pipeline.MAX_COPY_ATTEMPTS})."
            else:
                selo = (
                    f"⚠️ Depois de {resultado['tentativas']} tentativa(s), a revisão automática ainda "
                    "encontrou pontos de atenção nesse roteiro — dá uma olhada manual antes de publicar. "
                    "Acho melhor mandar pra revisão humana neste caso."
                )
            await msg.reply_text(selo)
            await msg.reply_text("Roteiro sugerido:")
            for i in range(0, len(roteiro), MAX_TELEGRAM_CHUNK):
                await msg.reply_text(roteiro[i : i + MAX_TELEGRAM_CHUNK])

            if not revisao_aprovada:
                await msg.reply_text("O que a última revisão ainda apontou:")
                pendencias = resultado["revisao_parecer"]
                for i in range(0, len(pendencias), MAX_TELEGRAM_CHUNK):
                    await msg.reply_text(pendencias[i : i + MAX_TELEGRAM_CHUNK])
        except Exception as exc:
            logger.exception("Falha ao gerar/revisar roteiro")
            await msg.reply_text(f"Não consegui gerar o roteiro alternativo agora: {exc}")

    # 4) Guardião — última checagem antes de qualquer envio, olhando parecer + roteiro
    # juntos como um pacote só. Roda sempre, mesmo quando o conteúdo foi aprovado de cara
    # (nesse caso revisa só o parecer, sem roteiro).
    guardiao_parecer = None
    guardiao_veredito = "desconhecido"
    try:
        guardiao_parecer = pipeline.guardian_review(dossie, parecer, roteiro or "", meta)
        guardiao_veredito = pipeline.extract_guardiao_veredito(guardiao_parecer)
    except Exception as exc:
        logger.exception("Falha na revisão final do Guardião")

    if guardiao_parecer:
        selo_guardiao = {"pronto": "✅", "ressalva": "⚠️", "nao_pronto": "❌"}.get(guardiao_veredito, "❓")
        await msg.reply_text(f"{selo_guardiao} Revisão final do Guardião:")
        for i in range(0, len(guardiao_parecer), MAX_TELEGRAM_CHUNK):
            await msg.reply_text(guardiao_parecer[i : i + MAX_TELEGRAM_CHUNK])

    # 5) arquivo permanente no canal de log, se configurado
    if LOG_CHANNEL_ID:
        header = f"Parecer — {meta['expert']} — {meta['data']}\n\n"
        archive_text = header + parecer
        if roteiro:
            status = "aprovado na revisão automática" if revisao_aprovada else "AINDA PRECISA DE REVISÃO HUMANA"
            archive_text += f"\n\n---\nRoteiro sugerido ({status}):\n\n{roteiro}"
        if guardiao_parecer:
            archive_text += f"\n\n---\n{guardiao_parecer}"
        for i in range(0, len(archive_text), MAX_TELEGRAM_CHUNK):
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=archive_text[i : i + MAX_TELEGRAM_CHUNK])


def main():
    # timeouts generosos: com o servidor local ativo, arquivos até 2GB podem levar
    # bem mais que o default (~5s) da lib pra baixar. get_updates precisa de uma
    # instância própria de HTTPXRequest (não pode reusar a de request()).
    timeout_kwargs = dict(connect_timeout=30.0, read_timeout=300.0, write_timeout=300.0, pool_timeout=30.0)
    builder = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(HTTPXRequest(**timeout_kwargs))
        .get_updates_request(HTTPXRequest(**timeout_kwargs))
    )
    if LOCAL_BOT_API:
        logger.info("Usando servidor local do Telegram Bot API em %s (sem limite de 20MB)", LOCAL_BOT_API_URL)
        builder = (
            builder.base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
            .local_mode(True)
        )
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("atualizar_compliance", atualizar_compliance))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL,
            handle_media,
        )
    )

    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", "10000"))
        path = "telegram-webhook"
        full_url = f"{webhook_url.rstrip('/')}/{path}"
        logger.info("Bot iniciado em modo webhook: %s (porta %s)", full_url, port)
        app.run_webhook(listen="0.0.0.0", port=port, url_path=path, webhook_url=full_url)
    else:
        logger.info("Bot iniciado em modo polling (uso local)")
        app.run_polling()


if __name__ == "__main__":
    main()

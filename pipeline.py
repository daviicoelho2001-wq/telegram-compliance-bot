"""
Pipeline de compliance — versão 100% gratuita.

Tudo aqui roda em cima de free tiers sem cartão de crédito:
- Groq (LLM de texto + transcrição de áudio/vídeo) — https://console.groq.com
- Tavily (busca na web para manter o dossiê legal atualizado) — https://tavily.com

Usado pelo bot.py.
"""
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from groq import APIStatusError, RateLimitError

logger = logging.getLogger("compliance-bot.usage")

# contador simples de consumo, só pra você acompanhar o quanto perto dos limites de free
# tier o bot está chegando (reseta a cada restart do processo — não é persistido em disco).
_usage_stats = {
    "chat_requests": 0,
    "chat_prompt_tokens": 0,
    "chat_completion_tokens": 0,
    "chat_total_tokens": 0,
    "transcription_requests": 0,
    "transcription_audio_seconds": 0.0,
}

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PARECERES_DIR = DATA_DIR / "pareceres"
ROTEIROS_DIR = DATA_DIR / "roteiros"
DOSSIE_PATH = DATA_DIR / "dossie-compliance-bets.md"
DOSSIE_MAX_AGE_DAYS = int(os.environ.get("DOSSIE_MAX_AGE_DAYS", "7"))

PESQUISADOR_PROMPT = (BASE_DIR / "prompts" / "pesquisador_prompt.txt").read_text(encoding="utf-8")
ANALISTA_PROMPT = (BASE_DIR / "prompts" / "analista_prompt.txt").read_text(encoding="utf-8")
COPYWRITER_PROMPT = (BASE_DIR / "prompts" / "copywriter_prompt.txt").read_text(encoding="utf-8")
PLAYBOOK_COPY = (BASE_DIR / "prompts" / "playbook_copy.txt").read_text(encoding="utf-8")
GUARDIAO_PROMPT = (BASE_DIR / "prompts" / "guardiao_prompt.txt").read_text(encoding="utf-8")

# llama-3.3-70b-versatile tem limites de free tier bem documentados (30 rpm / 1000 req dia /
# 12k tokens por minuto). Se sua equipe tiver volume alto e começar a bater rate limit, troque
# para "llama-3.1-8b-instant" (mais permissivo: 14.400 req/dia) via variável de ambiente GROQ_MODEL,
# ou avalie "openai/gpt-oss-120b" para respostas de raciocínio mais forte.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")  # opcional — sem ela, o dossiê não é verificado por busca

# quantas vezes o copywriter tenta de novo se a revisão automática (segunda passada do
# analista, agora em cima do roteiro gerado) encontrar problema. Cada tentativa = 1 chamada
# de copy + 1 de revisão no Groq, então não vale exagerar aqui num free tier.
MAX_COPY_ATTEMPTS = int(os.environ.get("MAX_COPY_ATTEMPTS", "2"))


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PARECERES_DIR.mkdir(parents=True, exist_ok=True)
    ROTEIROS_DIR.mkdir(parents=True, exist_ok=True)


def dossie_is_stale() -> bool:
    if not DOSSIE_PATH.exists():
        return True
    mtime = datetime.fromtimestamp(DOSSIE_PATH.stat().st_mtime)
    return datetime.now() - mtime > timedelta(days=DOSSIE_MAX_AGE_DAYS)


def _tavily_search_context() -> str:
    """Roda algumas buscas fixas no Tavily (free tier: 1.000 créditos/mês, sem cartão) e
    devolve um bloco de texto com os resultados, para o Groq sintetizar o dossiê em cima disso."""
    if not TAVILY_API_KEY:
        return ""

    from tavily import TavilyClient

    client = TavilyClient(api_key=TAVILY_API_KEY)
    mes_ano = datetime.now().strftime("%B de %Y")
    queries = [
        f"Portaria SPA/MF apostas publicidade {mes_ano}",
        "Portaria Interministerial MF SECOM MJSP publicidade bets",
        "CONAR regras publicidade apostas esportivas",
        f"SPA/MF fiscalização influenciadores afiliados apostas {datetime.now().year}",
        "nova lei projeto restringir propaganda apostas online Brasil",
    ]

    blocks = []
    for q in queries:
        try:
            result = client.search(query=q, max_results=4, search_depth="basic", topic="news")
        except Exception as exc:
            blocks.append(f"### Busca: {q}\n[falhou: {exc}]")
            continue
        lines = [f"### Busca: {q}"]
        for item in result.get("results", []):
            lines.append(f"- {item.get('title')} ({item.get('url')}, publicado: {item.get('published_date', 'n/d')})")
            content = (item.get("content") or "")[:800]
            lines.append(f"  {content}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _parse_wait_seconds(message: str):
    """Groq manda o tempo de espera no formato '2h25m10.848s' (TPD/RPD, teto diário) ou só
    '11.96s' (TPM, teto por minuto) — precisa dos dois formatos, senão um erro de horas vira
    uma espera de segundos e o retry fica tentando de novo sem chance de dar certo."""
    match = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", message)
    if not match:
        return None
    h, m, s = match.groups()
    total = float(s)
    if m:
        total += int(m) * 60
    if h:
        total += int(h) * 3600
    return total


def _groq_chat(groq_client, system_prompt: str, user_msg: str, max_tokens: int = 3500, max_retries: int = 3) -> str:
    """Uma análise de vídeo longo pode disparar várias chamadas grandes seguidas (transcrição
    + análise + copywriter + revisão), o que estoura fácil o teto de tokens/minuto do free
    tier do Groq. Em vez de propagar o erro pro usuário, espera o tempo que o próprio Groq
    sugere na mensagem de erro e tenta de novo."""
    for attempt in range(max_retries):
        try:
            completion = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=max_tokens,
                temperature=0.2,
            )
            usage = getattr(completion, "usage", None)
            if usage:
                _usage_stats["chat_requests"] += 1
                _usage_stats["chat_prompt_tokens"] += usage.prompt_tokens
                _usage_stats["chat_completion_tokens"] += usage.completion_tokens
                _usage_stats["chat_total_tokens"] += usage.total_tokens
                logger.info(
                    "Groq chat (%s): %d tokens (prompt %d + completion %d) | acumulado na sessão: "
                    "%d requests, %d tokens",
                    GROQ_MODEL, usage.total_tokens, usage.prompt_tokens, usage.completion_tokens,
                    _usage_stats["chat_requests"], _usage_stats["chat_total_tokens"],
                )
            return completion.choices[0].message.content.strip()
        except APIStatusError as exc:
            if exc.status_code == 413:
                # "Request too large" — o prompt sozinho (dossiê + playbook + conteúdo) já
                # passa do teto de tokens/minuto do modelo. Não é passageiro: tentar de novo
                # não muda o tamanho do prompt, então nunca vai funcionar sem reduzir o
                # conteúdo enviado ou trocar pra um modelo com TPM maior.
                raise RuntimeError(
                    f"Esse pedido é grande demais pro limite de tokens/minuto do modelo "
                    f"'{GROQ_MODEL}' ({exc}). Troque GROQ_MODEL pra 'llama-3.3-70b-versatile' "
                    "no .env (TPM maior) ou reduza o conteúdo enviado."
                ) from exc
            if not isinstance(exc, RateLimitError):
                raise
            wait_s = _parse_wait_seconds(str(exc))
            if wait_s is not None and wait_s > 90:
                # não é um pico passageiro de TPM (resolve em segundos) — é o teto diário
                # (TPD/RPD) do modelo, que só libera depois de horas. Esperar bloqueado no
                # processo não faz sentido aqui — melhor avisar com uma saída clara.
                horas, minutos = divmod(int(wait_s // 60), 60)
                tempo_fmt = f"{horas}h{minutos:02d}min" if horas else f"{minutos}min"
                raise RuntimeError(
                    f"Limite diário do Groq pra '{GROQ_MODEL}' foi atingido — libera de novo em "
                    f"~{tempo_fmt}. Se precisar continuar agora, confira em "
                    "https://console.groq.com/settings/limits outro modelo com budget diário livre."
                ) from exc
            if attempt == max_retries - 1:
                raise
            time.sleep((wait_s or 15.0) + 1)


def refresh_dossie(groq_client) -> str:
    """Roda o agente pesquisador (Tavily + Groq) e salva o dossiê atualizado."""
    _ensure_dirs()
    search_context = _tavily_search_context()

    if search_context:
        user_msg = (
            "Hoje é " + datetime.now().strftime("%d/%m/%Y") + ".\n\n"
            "Abaixo estão resultados de busca recentes sobre regulação de apostas no Brasil. "
            "Use-os para gerar o dossiê conforme suas instruções de sistema, citando as fontes "
            "que fizerem sentido.\n\n" + search_context
        )
    else:
        user_msg = (
            "Hoje é " + datetime.now().strftime("%d/%m/%Y") + ". Nenhuma busca na web foi feita "
            "nesta execução (TAVILY_API_KEY não configurada). Gere o dossiê com o que você já sabe, "
            "mas deixe MUITO claro, logo no topo, que este conteúdo não foi verificado por busca "
            "recente e pode estar desatualizado — recomende checagem manual."
        )

    dossie_text = _groq_chat(groq_client, PESQUISADOR_PROMPT, user_msg, max_tokens=2200)
    if dossie_text:
        DOSSIE_PATH.write_text(dossie_text, encoding="utf-8")
    return dossie_text


def get_dossie(groq_client, force: bool = False) -> str:
    if force or dossie_is_stale():
        try:
            return refresh_dossie(groq_client)
        except Exception as exc:  # nunca deixa a análise travar por falha na atualização
            if DOSSIE_PATH.exists():
                return DOSSIE_PATH.read_text(encoding="utf-8") + (
                    f"\n\n[aviso: falha ao atualizar dossiê agora ({exc}); usando última versão salva]"
                )
            raise
    return DOSSIE_PATH.read_text(encoding="utf-8")


def _format_segments(transcription) -> str:
    segments = getattr(transcription, "segments", None)
    if not segments:
        return getattr(transcription, "text", str(transcription))

    lines = []
    for seg in segments:
        if isinstance(seg, dict):
            start = seg.get("start", 0)
            text = seg.get("text", "")
        else:
            start = getattr(seg, "start", 0)
            text = getattr(seg, "text", "")
        mm = int(start) // 60
        ss = int(start) % 60
        lines.append(f"[{mm:02d}:{ss:02d}] {text.strip()}")
    return "\n".join(lines)


def _extract_audio_track(input_path: str) -> str:
    """Usa ffmpeg (via imageio-ffmpeg — binário empacotado no pip, sem precisar instalar
    nada no sistema) pra tirar só a faixa de áudio do arquivo original, comprimida e leve.

    Isso resolve dois problemas de uma vez: o teto de 25MB do Groq no free tier de
    transcrição (que existe mesmo com o servidor local do Telegram destravando o
    download), e o desperdício de mandar o vídeo inteiro quando só o áudio importa pra
    transcrição. Se der qualquer erro (ex: arquivo já é só áudio, ou ffmpeg falhar),
    devolve o arquivo original sem mudar nada — best effort, nunca quebra o fluxo.
    """
    try:
        import imageio_ffmpeg

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return input_path

    output_path = f"{input_path}.audio.mp3"
    cmd = [
        ffmpeg_exe,
        "-y",
        "-i", input_path,
        "-vn",              # descarta a faixa de vídeo, se houver
        "-acodec", "libmp3lame",
        "-ac", "1",          # mono
        "-ar", "16000",      # 16kHz — de sobra pra fala, mantém o arquivo pequeno
        "-b:a", "32k",       # bitrate baixo o suficiente pra caber até ~2h de fala em 25MB
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception:
        pass
    return input_path


def transcribe_audio(groq_client, file_path: str, language: str = "pt") -> str:
    audio_path = _extract_audio_track(file_path)
    try:
        with open(audio_path, "rb") as f:
            transcription = groq_client.audio.transcriptions.create(
                file=f,
                model="whisper-large-v3-turbo",
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                language=language,
                temperature=0.0,
            )
    finally:
        if audio_path != file_path and os.path.exists(audio_path):
            os.unlink(audio_path)

    duration = getattr(transcription, "duration", None)
    _usage_stats["transcription_requests"] += 1
    if duration:
        _usage_stats["transcription_audio_seconds"] += float(duration)
    logger.info(
        "Groq transcrição: %.1fs de áudio | acumulado na sessão: %d requests, %.1fs de áudio",
        float(duration or 0.0),
        _usage_stats["transcription_requests"], _usage_stats["transcription_audio_seconds"],
    )
    return _format_segments(transcription)


def analyze_content(groq_client, dossie_text: str, content_text: str, meta: dict) -> str:
    user_msg = (
        "## Dossiê de compliance vigente\n"
        f"{dossie_text}\n\n"
        "## Metadados\n"
        f"Expert: {meta.get('expert', 'desconhecido')}\n"
        f"Canal: {meta.get('canal', 'Telegram')}\n"
        f"Data: {meta.get('data', datetime.now().strftime('%d/%m/%Y %H:%M'))}\n\n"
        "## Conteúdo a analisar\n"
        f"{content_text}\n"
    )
    return _groq_chat(groq_client, ANALISTA_PROMPT, user_msg, max_tokens=1400)


def save_parecer(parecer_text: str, expert_name: str) -> Path:
    _ensure_dirs()
    safe_name = "".join(c if c.isalnum() else "_" for c in expert_name)[:40] or "expert"
    fname = PARECERES_DIR / f"parecer-{safe_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    fname.write_text(parecer_text, encoding="utf-8")
    return fname


def extract_veredito(parecer_text: str) -> str:
    """Lê a linha 'VEREDITO: ...' do parecer e devolve 'reprovado' | 'ajustes' | 'aprovado' | 'desconhecido'."""
    import re

    match = re.search(r"veredito:\s*(.+)", parecer_text, re.IGNORECASE)
    line = (match.group(1) if match else parecer_text).lower()
    if "reprovado" in line:
        return "reprovado"
    if "ajuste" in line:
        return "ajustes"
    if "aprovado" in line:
        return "aprovado"
    return "desconhecido"


def extract_veredito_line(parecer_text: str) -> str:
    """Devolve só a linha do veredito (com emoji), pra mandar como mensagem curta e destacada."""
    import re

    match = re.search(r"^.*veredito:.*$", parecer_text, re.IGNORECASE | re.MULTILINE)
    return match.group(0).strip() if match else parecer_text[:200]


def generate_compliant_copy(groq_client, dossie_text: str, original_content: str, parecer_text: str, meta: dict) -> str:
    """Roda o agente copywriter: escreve um roteiro alternativo já compliant, pronto pra usar."""
    user_msg = (
        "## Dossiê de compliance vigente\n"
        f"{dossie_text}\n\n"
        "## Playbook de gatilhos e vocabulário de copy\n"
        f"{PLAYBOOK_COPY}\n\n"
        "## Conteúdo original do expert\n"
        f"{original_content}\n\n"
        "## Parecer de compliance (achados a resolver)\n"
        f"{parecer_text}\n\n"
        "## Metadados\n"
        f"Expert: {meta.get('expert', 'desconhecido')}\n"
        f"Canal: {meta.get('canal', 'Telegram')}\n"
        f"Data: {meta.get('data', datetime.now().strftime('%d/%m/%Y %H:%M'))}\n"
    )
    return _groq_chat(groq_client, COPYWRITER_PROMPT, user_msg, max_tokens=2000)


def generate_and_verify_copy(
    groq_client,
    dossie_text: str,
    original_content: str,
    parecer_text: str,
    meta: dict,
    max_attempts: int = None,
):
    """Gera o roteiro com o copywriter e revisa esse roteiro com a MESMA régua do analista
    (rodando analyze_content de novo, agora em cima do texto que o copywriter escreveu).

    Se a revisão não aprovar de primeira, manda os achados de volta pro copywriter como
    feedback e tenta de novo, até max_attempts vezes. Devolve um dicionário com:
    - roteiro: o texto final gerado
    - aprovado: True/False — se a última revisão deu aprovado
    - tentativas: quantas rodadas de copy+revisão foram feitas
    - revisao_parecer: o parecer da última revisão (pra mostrar o que ainda falta, se for o caso)
    """
    attempts_limit = max_attempts or MAX_COPY_ATTEMPTS
    feedback = ""
    roteiro = ""
    revisao_parecer = ""
    revisao_veredito = "desconhecido"
    tentativas = 0

    for attempt in range(1, attempts_limit + 1):
        tentativas = attempt
        insumo_para_copy = parecer_text
        if feedback:
            insumo_para_copy = (
                parecer_text
                + f"\n\n## A revisão automática (tentativa {attempt - 1}) do roteiro anterior "
                "encontrou os pontos abaixo — resolva TODOS eles nesta nova versão, sem perder "
                "o que já estava bom:\n"
                + feedback
            )

        roteiro = generate_compliant_copy(groq_client, dossie_text, original_content, insumo_para_copy, meta)

        # revisão: roda o analista de novo, agora em cima do ROTEIRO GERADO, não do conteúdo original
        revisao_meta = dict(meta)
        revisao_meta["canal"] = f"{meta.get('canal', 'Telegram')} (revisão de roteiro gerado)"
        revisao_parecer = analyze_content(groq_client, dossie_text, roteiro, revisao_meta)
        revisao_veredito = extract_veredito(revisao_parecer)

        if revisao_veredito == "aprovado":
            break
        feedback = revisao_parecer

    return {
        "roteiro": roteiro,
        "aprovado": revisao_veredito == "aprovado",
        "tentativas": tentativas,
        "revisao_parecer": revisao_parecer,
    }


def save_roteiro(roteiro_text: str, expert_name: str) -> Path:
    _ensure_dirs()
    safe_name = "".join(c if c.isalnum() else "_" for c in expert_name)[:40] or "expert"
    fname = ROTEIROS_DIR / f"roteiro-{safe_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    fname.write_text(roteiro_text, encoding="utf-8")
    return fname


def guardian_review(groq_client, dossie_text: str, parecer_text: str, roteiro_text: str, meta: dict) -> str:
    """Roda o agente Guardião: última checagem de qualidade do pacote inteiro (parecer +
    roteiro, se houver) antes de qualquer envio ao Telegram. Não refaz a análise de
    compliance do zero — só valida consistência, vocabulário, trocas de palavra e
    completude entre as etapas anteriores."""
    roteiro_bloco = roteiro_text if roteiro_text else (
        "(nenhum roteiro foi gerado — o conteúdo original já foi aprovado sem necessidade de ajuste)"
    )
    user_msg = (
        "## Dossiê de compliance vigente\n"
        f"{dossie_text}\n\n"
        "## Parecer de compliance (Analista/Curador)\n"
        f"{parecer_text}\n\n"
        "## Roteiro sugerido pelo Copywriter (com trocas de palavra, se houver)\n"
        f"{roteiro_bloco}\n\n"
        "## Metadados\n"
        f"Expert: {meta.get('expert', 'desconhecido')}\n"
        f"Canal: {meta.get('canal', 'Telegram')}\n"
        f"Data: {meta.get('data', datetime.now().strftime('%d/%m/%Y %H:%M'))}\n"
    )
    return _groq_chat(groq_client, GUARDIAO_PROMPT, user_msg, max_tokens=700)


def extract_guardiao_veredito(guardiao_text: str) -> str:
    """Lê a linha 'PRONTO PARA ENVIO: ...' e devolve 'pronto' | 'ressalva' | 'nao_pronto' | 'desconhecido'."""
    import re

    match = re.search(r"pronto para envio:\s*(.+)", guardiao_text, re.IGNORECASE)
    line = (match.group(1) if match else guardiao_text).lower()
    if "não" in line or "nao" in line:
        return "nao_pronto"
    if "ressalva" in line:
        return "ressalva"
    if "sim" in line:
        return "pronto"
    return "desconhecido"

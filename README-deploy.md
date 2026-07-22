## O que mudou nesta versão: zero custo

Essa versão troca a Anthropic API (paga) por **Groq** (LLM de texto + transcrição) e
**Tavily** (busca na web) — os dois têm free tier permanente, sem cartão de crédito.
A hospedagem também trocou de Fly.io (pago) para **Render free tier**. Resultado:
R$ 0/mês pra rodar, com uma única ressalva explicada abaixo.

Atualização: o `Dockerfile` voltou a ser usado — agora ele empacota, opcionalmente, o
servidor local do Telegram (seção "Vídeos/áudios acima de 20MB" no fim deste arquivo).
Se você não precisar mandar arquivo grande, pode ignorar essa parte e nada muda no
resto do fluxo.

## Passo 1 — Criar as 3 contas grátis

1. **Telegram**: fale com `@BotFather` no Telegram → `/newbot` → copie o token
   (formato `123456:ABC-...`).
2. **Groq** (LLM + transcrição): crie conta em https://console.groq.com → API Keys →
   gere uma chave. Sem cartão de crédito.
3. **Tavily** (busca, opcional mas recomendado): crie conta em https://tavily.com →
   gere uma chave. 1.000 buscas grátis por mês — sem isso, o dossiê de compliance
   não é verificado por busca real, só pelo conhecimento que o modelo já tem (menos
   confiável).

## Passo 2 — Testar localmente

```bash
cd telegram-compliance-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edite e cole TELEGRAM_BOT_TOKEN, GROQ_API_KEY, TAVILY_API_KEY
python bot.py
```

Sem `WEBHOOK_URL` no `.env`, ele roda em polling — igual ao terminal, só pra testar.
Manda uma mensagem de texto ou um áudio pro bot no Telegram pra conferir.

## Passo 3 — Deixar rodando 24/7 de graça, no Render

1. Suba esta pasta (`telegram-compliance-bot/`) para um repositório no GitHub
   (pode ser privado).
2. Em https://render.com, crie conta grátis e escolha **New + → Blueprint**,
   apontando para esse repositório. O Render vai ler o `render.yaml` que já está
   aqui e configurar o serviço sozinho.
3. Quando pedir as variáveis de ambiente marcadas `sync: false`, preencha:
   - `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY`, `TAVILY_API_KEY` (as mesmas do passo 1)
   - `WEBHOOK_URL`: a URL pública que o Render vai te dar pro serviço (algo como
     `https://compliance-bets-bot.onrender.com`) — ele mostra essa URL assim que o
     serviço é criado; se pedir antes de você saber, crie o serviço primeiro, pegue
     a URL, depois volte em Environment e adicione essa variável.
4. Deploy. Pronto — o bot fica no ar sem depender do seu computador.

### A ressalva do plano grátis

O Render free "dorme" o serviço depois de 15 minutos sem receber mensagem. Quando
alguém manda a primeira mensagem depois disso, o bot demora uns 30-60 segundos pra
"acordar" e responder — só na primeira mensagem depois de um período parado. Nas
seguintes, responde normal. Pra um bot de uso interno da equipe (não é atendimento
ao cliente em tempo real), isso costuma ser um custo aceitável pelo R$ 0/mês. Se um
dia incomodar, dá pra passar pro plano Starter ($7/mês) e ele fica sempre ligado sem
esse delay.

## Sobre persistência (dossiê e histórico de pareceres)

O disco do Render free tier é apagado a cada novo deploy/restart — então o dossiê
salvo em `data/` e o histórico de pareceres em `data/pareceres/` não sobrevivem
para sempre. Isso não quebra o bot (o dossiê é regenerado sozinho quando está
ausente ou velho), mas você perde o **histórico** de pareceres antigos entre deploys.

Solução gratuita: configure `LOG_CHANNEL_ID` com o ID de um canal ou grupo privado
do Telegram que só você e compliance acompanham. O bot posta uma cópia de cada
parecer completo lá — o histórico do Telegram nunca é apagado, então esse canal vira
seu arquivo de auditoria permanente, sem custo nenhum. Pra pegar o ID de um canal:
adicione o bot `@userinfobot` ou `@RawDataBot` ao canal, ele te devolve o
`chat_id` (geralmente um número negativo tipo `-1001234567890`).

## Custo real esperado

- Groq: R$ 0 (free tier: 30 req/min, ~1.000-14.400 req/dia dependendo do modelo).
- Tavily: R$ 0 (1.000 buscas/mês; o dossiê só é atualizado a cada 7 dias por
  padrão, então isso é usado bem devagar).
- Render: R$ 0 (free tier de Web Service).
- Telegram: sempre grátis.

Se o volume da equipe crescer muito e vocês baterem algum rate limit do Groq, dá
pra trocar o modelo (`GROQ_MODEL=llama-3.1-8b-instant` no `.env`/Render, tem limite
diário bem mais alto) antes de pensar em qualquer plano pago.

## Vídeos/áudios acima de 20MB

Por padrão, a API pública do Telegram só deixa bots baixarem arquivos de até 20MB —
isso é um limite da própria plataforma, não do nosso código. O Groq (transcrição)
também tem um teto próprio de 25MB no free tier. O bot já resolve a segunda parte
sozinho, sempre: antes de mandar pro Groq, ele extrai só o áudio do vídeo (via
`imageio-ffmpeg`, sem precisar instalar nada no sistema), o que deixa o arquivo bem
menor mesmo quando o vídeo original é grande.

Já o limite de 20MB do Telegram só é removido se você ligar o **servidor local do
Telegram Bot API** (sobe pra até 2GB). Isso é opcional — sem ele, o bot recusa
arquivos grandes com uma mensagem clara em vez de travar.

### Testar localmente (Docker)

1. Instale o Docker Desktop, se ainda não tiver: `brew install --cask docker`, depois
   abra o app Docker uma vez (pede permissão do macOS na primeira vez).
2. Pegue `TELEGRAM_API_ID` e `TELEGRAM_API_HASH` em https://my.telegram.org/apps —
   login com o **seu número de telefone pessoal** (não é o token do bot), crie um
   "app" qualquer (nome e descrição não importam), copie os dois valores.
3. Suba o servidor local:

```bash
docker run -d -p 8081:8081 --name=telegram-bot-api --restart=always \
  -v telegram-bot-api-data:/var/lib/telegram-bot-api \
  -e TELEGRAM_API_ID=SEU_API_ID \
  -e TELEGRAM_API_HASH=SEU_API_HASH \
  -e TELEGRAM_LOCAL=1 \
  aiogram/telegram-bot-api:latest
```

4. No `.env`, descomente e preencha `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, e
   `TELEGRAM_LOCAL_BOT_API=true`.
5. Reinicie o bot (`python bot.py`). No `/start` ele já mostra "Limite de arquivo
   agora: 2GB" quando o servidor local está ativo.

### Produção (Render)

O `Dockerfile` já empacota o binário do `telegram-bot-api` junto com o bot. No
`render.yaml`, troque `TELEGRAM_LOCAL_BOT_API` pra `"true"` e preencha
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` nas variáveis de ambiente do serviço no
Render. Como os dois processos rodam dentro do mesmo container gratuito, vídeos
muito grandes (bem acima de 300-500MB) podem deixar o serviço lento ou instável no
free tier — pra volume normal de conteúdo de expert, deve rodar tranquilo.

## Controle de acesso

Segue em aberto, como você definiu antes — qualquer pessoa que souber o @ do bot
consegue usar. Fica registrado como ponto de atenção pra revisitar antes de abrir
pro time todo, já que o conteúdo é sensível.

## Próximos passos possíveis

- Lista de acesso por Telegram ID.
- Agregação automática de reincidência por expert (hoje cada parecer é um arquivo
  separado, sem cruzamento automático entre eles).
- Planilha de tracking (posso montar um .xlsx a partir dos pareceres salvos).

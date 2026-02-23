# Viral Video Cutter API

API para gerar cortes virais de videos automaticamente ou manualmente. Processa videos do Google Drive e entrega os resultados no Drive + webhook.

---

## Deploy Rapido (Docker)

### 1. Puxe a imagem

```bash
docker pull fernandofeier/api-cortes:latest
```

### 2. Configure as variaveis de ambiente

Crie um arquivo `.env` com as seguintes variaveis:

| Variavel | Obrigatorio | Descricao |
|----------|-------------|-----------|
| `LICENSE_KEY` | Sim | Chave de licenca (fornecida na compra) |
| `GEMINI_API_KEY` | Sim | Chave da API Google Gemini ([aistudio.google.com](https://aistudio.google.com/apikey)) |
| `APP_BASE_URL` | Sim | URL publica da API (ex: `https://api.seudominio.com`) |
| `GEMINI_MODEL` | Nao | Modelo Gemini para analise e transcricao (default: `gemini-3-flash-preview`) |
| `DEEPINFRA_API_KEY` | Nao | Chave DeepInfra para legendas via Whisper (mais preciso). Se vazio, usa Gemini |
| `MAX_UPLOAD_SIZE_MB` | Nao | Limite de tamanho de video em MB (default: `2000`) |

Exemplo de `.env`:
```env
LICENSE_KEY=sua-chave-de-licenca
GEMINI_API_KEY=AIzaSy...
APP_BASE_URL=https://api.seudominio.com
```

> A `LICENSE_KEY` e usada tanto como chave de autenticacao (header `X-API-Key`) quanto como identificador da licenca.

### 3. Suba o container

**Com Docker Compose** (recomendado):

Crie um `docker-compose.yml`:

```yaml
services:
  api:
    image: fernandofeier/api-cortes:latest
    container_name: api-cortes
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      - ./credentials:/app/credentials
    restart: unless-stopped
```

```bash
docker compose up -d
```

**Com Docker run:**

```bash
mkdir -p credentials
docker run -d \
  --name api-cortes \
  --env-file .env \
  -v $(pwd)/credentials:/app/credentials \
  -p 8000:8000 \
  --restart unless-stopped \
  fernandofeier/api-cortes:latest
```

Verifique que esta rodando:

```bash
curl http://localhost:8000/
# {"status":"ok","version":"1.0.0","license":"valid"}
```

### 4. Configure o Google Drive (OAuth2)

1. Acesse [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
2. Crie um projeto e ative a **Google Drive API**
3. Crie credenciais OAuth2 do tipo **"Web application"**
4. Adicione como redirect URI: `{APP_BASE_URL}/auth/drive/callback`
   (ex: `https://api.seudominio.com/auth/drive/callback`)
5. Baixe o JSON (`client_secret.json`)

**Envie as credenciais via API:**

```bash
curl -X POST {APP_BASE_URL}/v1/upload-credentials \
  -H "X-API-Key: SUA_LICENSE_KEY" \
  -F "file=@client_secret.json"
```

**Autorize o Google Drive no navegador:**

Acesse no navegador: `{APP_BASE_URL}/auth/drive?key=SUA_LICENSE_KEY`

Faca login com sua conta Google e autorize o acesso. Pronto — o token sera salvo automaticamente.

---

## Endpoints

Todas as chamadas `/v1/*` exigem o header `X-API-Key` com sua `LICENSE_KEY`.

### GET /

Health check. Retorna status da API e da licenca (valida em tempo real contra o servidor).

```json
{
  "status": "ok",
  "version": "1.0.0",
  "license": "valid"
}
```

### POST /v1/process

Corte automatico com IA. Envia um video e recebe os melhores momentos cortados.

```json
{
  "file_id": "1ABC123def456",
  "webhook_url": "https://seu-servidor.com/webhook",
  "drive_folder_id": "1P1c90AFvvS2j-ZJajiVskuusu0WrsLc3",
  "gemini_prompt_instruction": "Foque em momentos engracados",
  "options": {
    "layout": "blur_zoom",
    "max_clips": 3,
    "zoom_level": 1400,
    "fade_duration": 1.0,
    "width": 1080,
    "height": 1920,
    "mirror": true,
    "speed": 1.07,
    "color_filter": true,
    "pitch_shift": 1.03,
    "background_noise": 0.03,
    "ghost_effect": true,
    "dynamic_zoom": true,
    "face_tracking": true,
    "captions": true,
    "caption_style": "bold"
  }
}
```

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `file_id` | string | Sim | ID do arquivo no Drive (ou URL completa) |
| `webhook_url` | string | Sim | URL para receber o resultado via POST |
| `drive_folder_id` | string | Nao | Pasta no Drive para upload. Se omitido, salva na raiz |
| `gemini_prompt_instruction` | string | Nao | Instrucao extra para a IA |
| `options` | object | Nao | Opcoes de processamento (veja abaixo) |

### POST /v1/manual-cut

Corte manual — voce envia os timestamps, cada clip vira um video separado.

```json
{
  "file_id": "1ABC123def456",
  "webhook_url": "https://seu-servidor.com/webhook",
  "clips": [
    {"start": "5:52", "end": "6:10", "title": "Momento engracado"},
    {"start": "8:29", "end": "10:24"}
  ],
  "options": {
    "layout": "blur_zoom",
    "face_tracking": true,
    "captions": true
  }
}
```

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `file_id` | string | Sim | ID do arquivo no Drive (ou URL completa) |
| `webhook_url` | string | Sim | URL para receber o resultado via POST |
| `drive_folder_id` | string | Nao | Pasta no Drive para upload |
| `clips` | array | Sim | Array de clips com timestamps (1 a 20) |
| `clips[].start` | string/float | Sim | Inicio — `"5:52"` ou `352` (segundos) |
| `clips[].end` | string/float | Sim | Fim — `"6:10"` ou `370` (segundos) |
| `clips[].title` | string | Nao | Titulo do clip |
| `options` | object | Nao | Opcoes de processamento |

### POST /v1/manual-edit

Edicao manual — combina multiplos segmentos em **um unico video** com transicoes de crossfade.

```json
{
  "file_id": "1ABC123def456",
  "webhook_url": "https://seu-servidor.com/webhook",
  "title": "Melhores momentos",
  "segments": [
    {"start": "1:20", "end": "1:55"},
    {"start": "5:52", "end": "6:10"},
    {"start": "12:00", "end": "12:45"}
  ],
  "options": {
    "layout": "blur_zoom",
    "fade_duration": 1.0,
    "face_tracking": true,
    "captions": true
  }
}
```

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `file_id` | string | Sim | ID do arquivo no Drive (ou URL completa) |
| `webhook_url` | string | Sim | URL para receber o resultado via POST |
| `drive_folder_id` | string | Nao | Pasta no Drive para upload |
| `title` | string | Nao | Titulo do video de saida |
| `segments` | array | Sim | Array de segmentos para combinar (1 a 20) |
| `segments[].start` | string/float | Sim | Inicio do segmento |
| `segments[].end` | string/float | Sim | Fim do segmento |
| `options` | object | Nao | Opcoes de processamento |

### GET /v1/status/{job_id}

Consulta o status de um job.

```json
{
  "job_id": "550e8400-...",
  "status": "processing",
  "progress_message": "Gerando Corte 1/2: 'Momento viral'...",
  "elapsed_seconds": 45.2
}
```

**Status possiveis:** `queued` → `downloading` → `analyzing` → `processing` → `uploading` → `finishing` → `completed` | `error` | `cancelled`

### DELETE /v1/status/{job_id}

Cancela um job em andamento. O cancelamento e cooperativo — a operacao atual (FFmpeg, download, upload) termina antes do job parar.

```bash
curl -X DELETE {APP_BASE_URL}/v1/status/{job_id} \
  -H "X-API-Key: SUA_LICENSE_KEY"
```

**Resposta:**
```json
{
  "job_id": "550e8400-...",
  "status": "cancellation_requested",
  "message": "Cancellation requested. Current stage: processing"
}
```

**Regras:**
- Funciona em qualquer status exceto `completed`, `error` e `cancelled` (retorna 422)
- Apos cancelamento, um webhook e enviado com `"status": "cancelled"`
- Jobs que ja terminaram nao podem ser cancelados

### POST /v1/revalidate

Forca revalidacao imediata da licenca contra o servidor. Util apos alteracoes na licenca.

```bash
curl -X POST {APP_BASE_URL}/v1/revalidate \
  -H "X-API-Key: SUA_LICENSE_KEY"
```

**Resposta:**
```json
{
  "valid": true,
  "user_name": "Fernando",
  "expires_at": "2026-12-31T23:59:59+00:00"
}
```

> Este endpoint funciona mesmo quando a licenca esta marcada como invalida (usa autenticacao leve).

### POST /v1/upload-credentials

Envia o `client_secret.json` do Google OAuth via API.

```bash
curl -X POST {APP_BASE_URL}/v1/upload-credentials \
  -H "X-API-Key: SUA_LICENSE_KEY" \
  -F "file=@client_secret.json"
```

### GET /auth/drive?key=SUA_LICENSE_KEY

Pagina web para autorizar o Google Drive via navegador.

---

## Opcoes de Processamento

Disponiveis em todos os endpoints via campo `options`:

| Campo | Tipo | Default | Descricao |
|-------|------|---------|-----------|
| `layout` | string | `blur_zoom` | Preset de layout (veja tabela abaixo) |
| `max_clips` | int | 1 | Quantidade de cortes (so `/v1/process`, requer video > 10 min) |
| `zoom_level` | int | 1400 | Largura do zoom no foreground em pixels (so `blur_zoom`) |
| `fade_duration` | float | 1.0 | Duracao da transicao entre segmentos (segundos) |
| `width` | int | 1080 | Largura do video de saida |
| `height` | int | 1920 | Altura do video de saida |
| `mirror` | bool | false | Espelhar video horizontalmente |
| `speed` | float | 1.0 | Velocidade de reproducao (1.07 recomendado para anti-copyright) |
| `color_filter` | bool | false | Filtro sutil de cor para alterar fingerprint visual |
| `pitch_shift` | float | 1.0 | Tom do audio sem alterar velocidade (1.03 = 3% mais agudo) |
| `background_noise` | float | 0.0 | Volume de ruido rosa de fundo (0.03 = 3%) |
| `ghost_effect` | bool | false | Pulso de brilho periodico para quebrar fingerprint temporal |
| `dynamic_zoom` | bool | false | Zoom pulsante sutil (0-2%) para alterar fingerprint espacial |
| `face_tracking` | bool | false | Rastreamento facial para manter rostos centralizados no crop vertical |
| `captions` | bool | false | Gerar legendas automaticas (burned-in) |
| `caption_style` | string | `classic` | Estilo visual das legendas: `classic`, `bold`, `box` |

### Layouts

| Layout | Descricao |
|--------|-----------|
| `blur_zoom` | Fundo blur + video com zoom centralizado (9:16) |
| `vertical` | Corte vertical simples do centro, sem blur |
| `horizontal` | Mantem o formato original do video |
| `blur` | Fundo blur + video original centralizado (sem zoom) |

### Face Tracking (`face_tracking: true`)

Quando ativado, o sistema detecta rostos no video e ajusta o crop vertical dinamicamente para manter o rosto centralizado. Ideal para podcasts e videos com apresentadores.

- Funciona nos layouts `vertical` e `blur_zoom`
- Usa MediaPipe para deteccao facial com suavizacao de camera (sem pulos bruscos)
- Se nenhum rosto for encontrado, usa o crop centralizado padrao

### Legendas (`captions: true`)

Quando ativado, o audio do clip e transcrito automaticamente e as legendas sao queimadas no video. Se o video nao tiver fala, retorna sem legendas normalmente.

**Provedores de transcricao:**
- **DeepInfra Whisper** (recomendado): Timestamps word-level com agrupamento inteligente por pausas na fala. Requer `DEEPINFRA_API_KEY`.
- **Gemini**: Fallback automatico quando DeepInfra nao esta configurado.

**Estilos de legenda** (`caption_style`):

| Estilo | Visual | Melhor para |
|--------|--------|-------------|
| `classic` | Arial 48, branco, outline preto | Uso geral, limpo |
| `bold` | Arial Black 52, UPPERCASE, outline grossa | Impacto viral, TikTok |
| `box` | Arial 48, fundo preto semi-transparente | Legibilidade maxima |

### Anti-Copyright

Opcoes para evitar deteccao automatica de direitos autorais (Content ID do YouTube, etc). Content ID e ~70% baseado em audio, entao as tecnicas de audio sao as mais eficazes.

**Audio (alta eficacia):**

| Opcao | O que faz | Eficacia |
|-------|-----------|----------|
| `pitch_shift: 1.03` | Altera tom da voz 3% sem mudar velocidade | **Muito alta** (muda espectrograma) |
| `speed: 1.07` | Acelera 7% — altera fingerprint de audio e timing | **Alta** (audio + visual) |
| `background_noise: 0.03` | Ruido rosa a 3% volume — nova impressao digital sonora | **Alta** (quase inaudivel) |

**Video (media eficacia):**

| Opcao | O que faz | Eficacia |
|-------|-----------|----------|
| `color_filter: true` | Ajuste sutil de brilho, contraste e saturacao | Media-alta (visual) |
| `ghost_effect: true` | Pulso de brilho quase invisivel a cada 11s | Media-alta (temporal) |
| `dynamic_zoom: true` | Zoom pulsante 0-2% a cada 5s | Media (espacial) |
| `mirror: true` | Espelha o video horizontalmente | Media (visual) |
| `layout: "blur_zoom"` | Muda composicao visual (ja e o default) | Media (visual) |

**Recomendacao para maxima protecao:**
```json
"options": {
  "mirror": true,
  "speed": 1.07,
  "color_filter": true,
  "pitch_shift": 1.03,
  "background_noise": 0.03,
  "ghost_effect": true,
  "dynamic_zoom": true
}
```

### Presets de Plataforma

Ao usar `max_clips`, a IA automaticamente otimiza cada corte para a plataforma ideal:

| max_clips | Comportamento |
|-----------|---------------|
| 1 | 1 clip universal, ate 70s |
| 2+ | 1 clip YouTube Shorts (ate 70s) + restantes TikTok/Instagram (ate 2min 40s) |

O campo `platform` e retornado no webhook para cada clip: `"youtube_shorts"`, `"tiktok_instagram"`, ou `"universal"`.

---

## Webhooks

**Sucesso:**
```json
{
  "job_id": "550e8400-...",
  "status": "completed",
  "original_file_id": "1ABC123def456",
  "result": {
    "total_clips": 1,
    "generated_clips": [
      {
        "corte_number": 1,
        "title": "Momento dramatico",
        "platform": "youtube_shorts",
        "total_duration": 53.0,
        "file_id": "1XYZ789...",
        "file_name": "viral-550e8400-corte1.mp4",
        "web_view_link": "https://drive.google.com/file/d/1XYZ789.../view",
        "segments": [
          {"start": 12.5, "end": 38.0, "description": "Hook: revelacao surpresa"},
          {"start": 78.0, "end": 105.5, "description": "Climax emocional"}
        ],
        "output_size_mb": 8.45
      }
    ]
  }
}
```

**Cancelamento:**
```json
{
  "job_id": "550e8400-...",
  "status": "cancelled",
  "original_file_id": "1ABC123def456"
}
```

**Erro:**
```json
{
  "job_id": "550e8400-...",
  "status": "error",
  "original_file_id": "1ABC123def456",
  "error": {
    "message": "Descricao do erro",
    "type": "RuntimeError"
  }
}
```

---

## Licenca

Esta API requer uma licenca valida para funcionar. A licenca e validada em tempo real a cada chamada.

**Como funciona:**
- A `LICENSE_KEY` no `.env` e sua chave de acesso
- Cada chamada valida a licenca contra o servidor antes de processar
- Se a licenca for desativada, o bloqueio e imediato na proxima chamada
- Use `POST /v1/revalidate` para verificar o status da licenca manualmente

**Codigos de erro:**

| HTTP | Descricao |
|------|-----------|
| 401 | Chave ausente ou invalida |
| 403 | Licenca invalida ou expirada |
| 503 | LICENSE_KEY nao configurada |

---

## Bot do Telegram (Opcional)

Bot que recebe videos no Telegram e faz upload direto ao Google Drive.

### Configuracao

1. Crie um bot via [@BotFather](https://t.me/BotFather) → copie o token
2. Acesse [my.telegram.org](https://my.telegram.org) → copie `API_ID` e `API_HASH`
3. Descubra seu User ID via [@userinfobot](https://t.me/userinfobot)

Adicione ao `.env`:

| Variavel | Obrigatorio | Descricao |
|----------|-------------|-----------|
| `TELEGRAM_BOT_TOKEN` | Sim | Token do bot (BotFather) |
| `TELEGRAM_API_ID` | Sim | API ID (my.telegram.org) |
| `TELEGRAM_API_HASH` | Sim | API Hash (my.telegram.org) |
| `TELEGRAM_ALLOWED_USERS` | Sim | IDs dos usuarios permitidos (separados por virgula) |
| `TELEGRAM_DEFAULT_DRIVE_FOLDER` | Nao | Pasta padrao no Drive |
| `TELEGRAM_DEFAULT_WEBHOOK_URL` | Nao | URL padrao de webhook apos upload |

Se `TELEGRAM_BOT_TOKEN` estiver vazio, o bot nao inicia e a API funciona normalmente.

### Comandos

| Comando | Descricao |
|---------|-----------|
| `/start` | Boas-vindas e instrucoes |
| `/pasta <folder_id>` | Define a pasta do Drive |
| `/pasta` | Mostra a pasta atual |
| `/webhook <url>` | Define URL de notificacao apos upload |
| `/webhook` | Mostra o webhook atual |
| `/webhook off` | Desativa o webhook |

### Como Usar

1. Configure as variaveis e suba o container
2. Abra o bot no Telegram
3. Envie `/pasta <folder_id>` para definir a pasta (opcional)
4. Envie um video — o bot baixa e envia ao Drive automaticamente
5. Se tiver webhook configurado, recebe notificacao com detalhes do upload

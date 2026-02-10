# Viral Video Cutter API

API para gerar cortes virais de videos automaticamente ou manualmente. Processa videos do Google Drive e entrega os resultados no Drive + webhook.

---

## Deploy Rapido (Docker)

### 1. Clone e configure

```bash
git clone <url-do-repo>
cd api-cortes
cp .env.example .env
```

Edite o `.env`:

| Variavel | Obrigatorio | Descricao |
|----------|-------------|-----------|
| `GEMINI_API_KEY` | Sim | Chave da API Google Gemini ([aistudio.google.com](https://aistudio.google.com/apikey)) |
| `API_KEY` | Sim | Chave para autenticar chamadas a API |
| `APP_BASE_URL` | Sim | URL publica da API (ex: `https://api.seudominio.com`) |
| `GEMINI_MODEL` | Nao | Modelo Gemini para analise e transcricao (default: `gemini-3-flash-preview`) |
| `DEEPINFRA_API_KEY` | Nao | Chave DeepInfra para legendas via Whisper (mais preciso). Se vazio, usa Gemini |
| `MAX_UPLOAD_SIZE_MB` | Nao | Limite de tamanho de video em MB (default: `2000`) |

### 2. Configure o Google Drive (OAuth2)

1. Acesse [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
2. Crie um projeto e ative a **Google Drive API**
3. Crie credenciais OAuth2:
   - **Docker local**: tipo "Desktop app"
   - **Painel (Easypanel, Coolify, etc)**: tipo "Web application" com redirect URI `{APP_BASE_URL}/auth/drive/callback`
4. Baixe o JSON

**Enviar credenciais via API (paineis sem terminal):**

```bash
curl -X POST {APP_BASE_URL}/v1/upload-credentials \
  -H "X-API-Key: SUA_API_KEY" \
  -F "file=@client_secret.json"
```

**Autorizar o Drive:**

- **Via terminal**: `python3 scripts/auth_drive.py`
- **Via navegador**: acesse `{APP_BASE_URL}/auth/drive?key=SUA_API_KEY`

### 3. Suba o container

```bash
docker compose up -d --build
curl http://localhost:8000/
# {"status":"ok","version":"1.0.0"}
```

---

## Endpoints

Todas as chamadas `/v1/*` exigem o header `X-API-Key`.

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
    "mirror": false,
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

**Status possiveis:** `queued` → `downloading` → `analyzing` → `processing` → `uploading` → `finishing` → `completed` | `error`

### POST /v1/upload-credentials

Envia o `client_secret.json` do Google OAuth via API.

```bash
curl -X POST http://localhost:8000/v1/upload-credentials \
  -H "X-API-Key: sua-api-key" \
  -F "file=@client_secret.json"
```

### GET /auth/drive?key=SUA_API_KEY

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
| `captions` | bool | false | Gerar legendas automaticas (burned-in) |
| `caption_style` | string | `classic` | Estilo visual das legendas: `classic`, `bold`, `box` |

### Layouts

| Layout | Descricao |
|--------|-----------|
| `blur_zoom` | Fundo blur + video com zoom centralizado (9:16) |
| `vertical` | Corte vertical simples do centro, sem blur |
| `horizontal` | Mantem o formato original do video |
| `blur` | Fundo blur + video original centralizado (sem zoom) |

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

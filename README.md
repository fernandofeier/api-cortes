# Viral Video Cutter API

API para gerar cortes virais automaticamente. Envia um video do Google Drive, a IA (Gemini) identifica os melhores momentos, o FFmpeg corta e estiliza em formato 9:16 (TikTok/Reels/Shorts) com fundo blur e transicoes, e o resultado e enviado de volta ao Drive + webhook.

## Arquitetura

```
POST /v1/process (file_id + webhook_url)
        |
        v
   202 Accepted (job_id)
        |
        v  [background]
   Download do Drive → Gemini analisa → FFmpeg corta → Upload pro Drive → Webhook POST
```

- **FastAPI** + BackgroundTasks (sem Redis/Celery)
- **Google Gemini** para analise de video com IA
- **FFmpeg** para corte, transicoes xfade e estilo visual 9:16
- **Google Drive** para download/upload via OAuth2
- **Worker unico** (uvicorn --workers 1) pois BackgroundTasks roda no mesmo processo

---

## Deploy Rapido (Docker)

### 1. Clone o repositorio

```bash
git clone <url-do-repo>
cd api-cortes
```

### 2. Configure o .env

```bash
cp .env.example .env
```

Edite o `.env`:

| Variavel | Obrigatorio | Descricao |
|----------|-------------|-----------|
| `GEMINI_API_KEY` | Sim | Chave da API Google Gemini ([aistudio.google.com](https://aistudio.google.com/apikey)) |
| `GEMINI_MODEL` | Nao | Modelo Gemini (default: `gemini-2.5-flash`) |
| `API_KEY` | Sim | Chave para autenticar chamadas a API |
| `APP_BASE_URL` | Sim* | URL publica da API (ex: `https://api.seudominio.com`). *Necessario para OAuth via painel |
| `MAX_UPLOAD_SIZE_MB` | Nao | Limite de tamanho de video em MB (default: `2000`). Gemini suporta ate 2 GB |
| `GOOGLE_DRIVE_TOKEN_JSON` | Nao | Caminho do token OAuth2 (default: `/app/credentials/token.json`) |
| `TEMP_DIR` | Nao | Diretorio temporario (default: `/tmp/video-cutter`) |
| `LOG_LEVEL` | Nao | Nivel de log (default: `INFO`) |

### 3. Configure o Google Drive (OAuth2)

Voce precisa de credenciais OAuth2 para o Google Drive.

#### Criando as credenciais no Google Cloud Console

1. Acesse [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
2. Crie um projeto (ou use um existente)
3. Ative a **Google Drive API** em "APIs & Services" > "Enable APIs"
4. Va em "Credentials" > "Create Credentials" > "OAuth client ID"
5. Se pedido, configure a tela de consentimento (tipo "External", adicione seu email como test user)

**Escolha o tipo de cliente conforme seu deploy:**

| Deploy | Tipo de cliente OAuth | Redirect URI |
|--------|----------------------|--------------|
| Docker local (terminal) | Desktop app | Nenhum necessario |
| Easypanel / Coolify / Portainer | Web application | `{APP_BASE_URL}/auth/drive/callback` |

6. Baixe o JSON (botao de download no Google Cloud Console)

#### Enviando o client_secret.json

**Opcao A — Via volume (Docker local):**

```bash
mkdir -p credentials
# Coloque o client_secret.json na pasta credentials/
```

**Opcao B — Via API (paineis sem terminal):**

```bash
curl -X POST {APP_BASE_URL}/v1/upload-credentials \
  -H "X-API-Key: SUA_API_KEY" \
  -F "file=@client_secret.json"
```

O arquivo sera salvo automaticamente no local correto dentro do container, com o nome correto.

#### Autorizando o Drive

**Opcao A — Via terminal (Docker local):**

```bash
pip3 install google-auth-oauthlib
python3 scripts/auth_drive.py
# Um navegador abrira → autorize → token.json sera gerado
```

**Opcao B — Via navegador (paineis sem terminal):**

1. Certifique-se que `APP_BASE_URL` esta correto no `.env`
2. Suba o container
3. Envie o `client_secret.json` via API (passo anterior)
4. Acesse no navegador: `{APP_BASE_URL}/auth/drive?key=SUA_API_KEY`
5. Sera redirecionado ao Google → autorize → token salvo automaticamente

### 4. Suba o container

```bash
docker compose up -d --build
```

Verifique se esta rodando:

```bash
curl http://localhost:8000/
# {"status":"ok","version":"1.0.0"}
```

---

## Deploy em Paineis (Easypanel, Coolify, Portainer)

### Easypanel

1. Crie um servico do tipo **Docker**
2. Aponte para o repositorio Git
3. Em **Environment Variables**, adicione todas as variaveis do `.env`
4. Em **Volumes**, monte `./credentials` em `/app/credentials`
5. Configure o dominio e defina `APP_BASE_URL` com o dominio completo
6. Acesse `{APP_BASE_URL}/auth/drive?key=SUA_API_KEY` para autorizar o Drive

### Coolify

1. Crie um novo recurso > Docker Compose
2. Cole o conteudo do `docker-compose.yml`
3. Configure as variaveis de ambiente
4. Monte o volume de credentials
5. Adicione o dominio e configure `APP_BASE_URL`
6. Autorize o Drive via navegador

### Portainer

1. Stacks > Add Stack
2. Use o docker-compose.yml (Git ou upload)
3. Configure as variaveis de ambiente
4. Certifique-se que o volume `credentials` esta acessivel
5. Autorize o Drive via navegador

### Cloudflare Tunnel

Para usar com Cloudflare Tunnel, descomente a secao `networks` no `docker-compose.yml`:

```yaml
services:
  api:
    # ... (configuracao existente)
    networks:
      - tunnel

networks:
  tunnel:
    external: true
```

---

## Endpoints

### Autenticacao

Todas as chamadas `/v1/*` exigem o header:

```
X-API-Key: sua-api-key-aqui
```

### GET /

Health check (publico, sem autenticacao).

```bash
curl http://localhost:8000/
```

```json
{"status": "ok", "version": "1.0.0"}
```

### POST /v1/process

Inicia o processamento de um video. Retorna 202 imediatamente.

**Headers:**
```
Content-Type: application/json
X-API-Key: sua-api-key
```

**Body (minimo):**
```json
{
  "file_id": "1ABC123def456",
  "webhook_url": "https://seu-servidor.com/webhook"
}
```

**Body (completo com opcoes):**
```json
{
  "file_id": "https://drive.google.com/file/d/1ABC123def456/view",
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
    "mirror": false
  }
}
```

**Campos do body:**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `file_id` | string | Sim | ID do arquivo no Drive (ou URL completa do Drive) |
| `webhook_url` | string | Sim | URL para receber o resultado via POST |
| `drive_folder_id` | string | Nao | ID da pasta no Google Drive para upload dos cortes. Se omitido, salva na raiz do Drive |
| `gemini_prompt_instruction` | string | Nao | Instrucao extra para a IA na analise |
| `options` | object | Nao | Opcoes de processamento (veja abaixo) |

**Campos de `options`:**

| Campo | Tipo | Default | Min | Max | Descricao |
|-------|------|---------|-----|-----|-----------|
| `layout` | string | `blur_zoom` | - | - | Preset de layout do video (veja tabela abaixo) |
| `max_clips` | int | 1 | 1 | 10 | Quantidade de cortes (multi-clip requer video > 10 min) |
| `zoom_level` | int | 1400 | 500 | 3000 | Largura do zoom no foreground (pixels, so aplica no layout `blur_zoom`) |
| `fade_duration` | float | 1.0 | 0.0 | 5.0 | Duracao da transicao entre segmentos (segundos) |
| `width` | int | 1080 | 360 | 3840 | Largura do video de saida |
| `height` | int | 1920 | 360 | 3840 | Altura do video de saida |
| `mirror` | bool | false | - | - | Espelhar video horizontalmente (anti-copyright) |

**Layouts disponiveis:**

| Layout | Descricao |
|--------|-----------|
| `blur_zoom` | **Padrao.** Fundo blur + video com zoom centralizado + formato vertical 9:16 |
| `vertical` | Corte vertical simples do centro do video, sem blur |
| `horizontal` | Mantem o formato original do video, sem alterar resolucao |
| `blur` | Fundo blur + video original centralizado (sem zoom) |

**Resposta 202:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "accepted",
  "message": "Video processing started. Results will be sent to https://..."
}
```

### POST /v1/manual-cut

Corte manual de video — sem IA, voce envia os timestamps exatos. Cada clip vira um video separado no Drive.

**Headers:**
```
Content-Type: application/json
X-API-Key: sua-api-key
```

**Body:**
```json
{
  "file_id": "1ABC123def456",
  "webhook_url": "https://seu-servidor.com/webhook",
  "drive_folder_id": "1P1c90AFvvS2j-ZJajiVskuusu0WrsLc3",
  "clips": [
    {"start": "5:52", "end": "6:10", "title": "Momento engracado"},
    {"start": "8:29", "end": "10:24"}
  ],
  "options": {
    "layout": "blur_zoom",
    "mirror": false
  }
}
```

**Campos do body:**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `file_id` | string | Sim | ID do arquivo no Drive (ou URL completa) |
| `webhook_url` | string | Sim | URL para receber o resultado via POST |
| `drive_folder_id` | string | Nao | Pasta no Drive para upload. Se omitido, salva na raiz |
| `clips` | array | Sim | Array de clips com timestamps (1 a 20 clips) |
| `clips[].start` | string/float | Sim | Inicio do clip — `"5:52"` ou `352` (segundos) |
| `clips[].end` | string/float | Sim | Fim do clip — `"6:10"` ou `370` (segundos) |
| `clips[].title` | string | Nao | Titulo opcional do clip |
| `options` | object | Nao | Opcoes de layout, mirror, etc (mesmas do `/v1/process`) |

**Resposta 202:**
```json
{
  "job_id": "550e8400-...",
  "status": "accepted",
  "message": "Manual cut started (2 clips). Results will be sent to https://..."
}
```

**Webhook de sucesso:**
```json
{
  "job_id": "550e8400-...",
  "status": "completed",
  "original_file_id": "1ABC123def456",
  "result": {
    "total_clips": 2,
    "generated_clips": [
      {
        "clip_number": 1,
        "title": "Momento engracado",
        "file_id": "1XYZ789...",
        "file_name": "clip-550e8400-1.mp4",
        "web_view_link": "https://drive.google.com/file/d/1XYZ789.../view",
        "start": 352.0,
        "end": 370.0,
        "output_size_mb": 3.21
      }
    ]
  }
}
```

### POST /v1/manual-edit

Edicao manual de video — combina multiplos segmentos em **um unico video** com transicoes de crossfade entre eles. Funciona como a IA faz, porem com timestamps manuais.

**Headers:**
```
Content-Type: application/json
X-API-Key: sua-api-key
```

**Body:**
```json
{
  "file_id": "1ABC123def456",
  "webhook_url": "https://seu-servidor.com/webhook",
  "drive_folder_id": "1P1c90AFvvS2j-ZJajiVskuusu0WrsLc3",
  "title": "Melhores momentos",
  "segments": [
    {"start": "1:20", "end": "1:55"},
    {"start": "5:52", "end": "6:10"},
    {"start": "12:00", "end": "12:45"}
  ],
  "options": {
    "layout": "blur_zoom",
    "fade_duration": 1.0,
    "mirror": false
  }
}
```

**Campos do body:**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|-------------|-----------|
| `file_id` | string | Sim | ID do arquivo no Drive (ou URL completa) |
| `webhook_url` | string | Sim | URL para receber o resultado via POST |
| `drive_folder_id` | string | Nao | Pasta no Drive para upload. Se omitido, salva na raiz |
| `title` | string | Nao | Titulo do video de saida |
| `segments` | array | Sim | Array de segmentos para combinar (1 a 20) |
| `segments[].start` | string/float | Sim | Inicio do segmento — `"5:52"` ou `352` (segundos) |
| `segments[].end` | string/float | Sim | Fim do segmento — `"6:10"` ou `370` (segundos) |
| `options` | object | Nao | Opcoes de layout, fade_duration, mirror, etc (mesmas do `/v1/process`) |

**Resposta 202:**
```json
{
  "job_id": "550e8400-...",
  "status": "accepted",
  "message": "Manual edit started (3 segments → 1 video). Results will be sent to https://..."
}
```

**Webhook de sucesso:**
```json
{
  "job_id": "550e8400-...",
  "status": "completed",
  "original_file_id": "1ABC123def456",
  "result": {
    "title": "Melhores momentos",
    "file_id": "1XYZ789...",
    "file_name": "edit-550e8400.mp4",
    "web_view_link": "https://drive.google.com/file/d/1XYZ789.../view",
    "segments": [
      {"start": 80.0, "end": 115.0},
      {"start": 352.0, "end": 370.0},
      {"start": 720.0, "end": 765.0}
    ],
    "total_segments": 3,
    "output_size_mb": 12.34
  }
}
```

### GET /v1/status/{job_id}

Consulta o status de um job em andamento.

```bash
curl -H "X-API-Key: sua-api-key" http://localhost:8000/v1/status/{job_id}
```

**Resposta:**
```json
{
  "job_id": "550e8400-...",
  "status": "processing",
  "progress_message": "Gerando Corte 1/2: 'Momento viral'...",
  "elapsed_seconds": 45.2
}
```

**Status possiveis:** `queued` → `downloading` → `analyzing` → `processing` → `uploading` → `finishing` → `completed` | `error`

### Webhook Payloads

**Sucesso:**
```json
{
  "job_id": "550e8400-...",
  "status": "completed",
  "original_file_id": "1ABC123def456",
  "result": {
    "total_clips": 2,
    "generated_clips": [
      {
        "corte_number": 1,
        "title": "Momento dramatico",
        "file_id": "1XYZ789...",
        "file_name": "viral-550e8400-corte1.mp4",
        "web_view_link": "https://drive.google.com/file/d/1XYZ789.../view",
        "segments": [
          {"start": 12.5, "end": 38.0, "description": "Hook: revelacao surpresa"},
          {"start": 78.0, "end": 105.5, "description": "Climax emocional"}
        ],
        "output_size_mb": 8.45
      }
    ],
    "usage": {
      "input_tokens": 48250,
      "output_tokens": 320,
      "total_tokens": 48570,
      "model": "gemini-3-flash-preview",
      "estimated_cost_usd": 0.025085,
      "estimated_cost_brl": 0.1455
    }
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
    "message": "Gemini returned no viable cortes",
    "type": "RuntimeError"
  }
}
```

### POST /v1/upload-credentials

Envia o `client_secret.json` do Google OAuth via API (para paineis sem acesso a volumes).

**Headers:**
```
X-API-Key: sua-api-key
```

```bash
curl -X POST http://localhost:8000/v1/upload-credentials \
  -H "X-API-Key: sua-api-key" \
  -F "file=@client_secret.json"
```

**Resposta 200:**
```json
{
  "status": "ok",
  "message": "client_secret.json saved successfully. Now authorize Google Drive at: ..."
}
```

### GET /auth/drive?key=SUA_API_KEY

Pagina web para autorizar o Google Drive via navegador (para paineis sem terminal).

### GET /auth/drive/callback

Callback do OAuth2 (Google redireciona automaticamente para ca).

---

## Seguranca

- **API Key**: Toda chamada `/v1/*` exige `X-API-Key` no header. Comparacao timing-safe contra ataques de temporalizacao.
- **Credenciais Google**: Ficam no servidor (`.env` + `credentials/`), nunca expostas ao cliente.
- **OAuth2 token**: Refresh token salvo em `token.json` dentro do container, auto-refresh quando expira.
- **Gemini cleanup**: Arquivos uploaded ao Gemini sao sempre deletados (try/finally), mesmo em caso de erro. Timeout de 10 min no processamento.
- **Limpeza de disco**: Todos os arquivos temporarios (downloads, cortes) sao apagados apos cada job (sucesso ou erro). No startup, restos de execucoes anteriores sao removidos automaticamente.
- **Webhook retry**: Backoff exponencial (2s, 4s, 8s), sem retry em erros 4xx.
- **Limite de tamanho**: Videos acima do limite configurado (`MAX_UPLOAD_SIZE_MB`) sao rejeitados antes do upload ao Gemini.

---

## Limpeza de Arquivos

A API garante que **nenhum arquivo temporario persista no disco**:

1. **Durante o processamento**: Cada job cria uma pasta temporaria isolada (`job-XXXXXXXX-...`)
2. **Apos conclusao (sucesso ou erro)**: A pasta e todo seu conteudo sao apagados no bloco `finally` do pipeline
3. **No startup**: Ao iniciar, o container limpa automaticamente qualquer pasta `job-*` que tenha sobrado de crashes anteriores
4. **Gemini File API**: Arquivos uploaded ao Gemini sao deletados imediatamente apos a analise (ou em caso de erro)

---

## Estrutura do Projeto

```
api-cortes/
├── main.py                    # FastAPI app, endpoints, auth
├── core/
│   ├── config.py              # Configuracao via .env (pydantic-settings)
│   └── job_store.py           # Job tracking in-memory
├── services/
│   ├── auth_service.py        # OAuth2 web flow para paineis
│   ├── orchestrator.py        # Pipeline: download → analyze → cut → upload → webhook
│   ├── video_engine.py        # FFmpeg filter_complex builder
│   ├── gemini_service.py      # Gemini AI video analysis
│   └── drive_service.py       # Google Drive download/upload
├── utils/
│   └── webhook_sender.py      # Webhook POST com retry
├── scripts/
│   └── auth_drive.py          # OAuth2 via terminal (uso local)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Limites e Consideracoes

- **Duracao dos cortes**: Cada corte tem no maximo 80 segundos (2-4 segmentos de 10-40s)
- **Multi-clip**: Requer video com mais de 10 minutos. Videos curtos geram 1 corte independente do `max_clips`
- **Formato de saida**: MP4, H.264, AAC, 9:16 vertical
- **Worker unico**: Processa um video por vez. Para escalar, use multiplas instancias atras de um load balancer
- **Job store in-memory**: Status dos jobs se perde ao reiniciar o container. Jobs expiram automaticamente apos 3 dias
- **Gemini file processing timeout**: 10 minutos maximo de espera
- **Tamanho maximo de video**: Configuravel via `MAX_UPLOAD_SIZE_MB` (default: 2000 MB). O Gemini File API suporta ate 2 GB

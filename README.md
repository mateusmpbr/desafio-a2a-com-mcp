# A Ponte: um agente A2A com MCP por dentro

Dois processos:

- **`servidor-mcp/`**: servidor MCP da Central de Salas, Streamable HTTP em `:7301/mcp`, revisão `2026-07-28`, SDK oficial Python `mcp==2.2.0`. Tem três tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`), o resource `politica://uso` e o ciclo de MRTR na reserva.
- **`agente/`**: agente A2A v1.0 (JSON-RPC 2.0 sobre HTTP) em `:7300`, com o card em `/.well-known/agent-card.json` e `SendMessage`/`GetTask` em `/a2a`. Por dentro, ele é um host MCP que fala com o servidor por HTTP usando o cliente do mesmo SDK.

O enunciado original do desafio está em [INSTRUCTIONS.md](INSTRUCTIONS.md).

## Como rodar

Pré-requisito: Python 3.10 ou superior. Os comandos abaixo usam o [uv](https://docs.astral.sh/uv/), que respeita as versões travadas em `uv.lock`. Se ele não estiver instalado: `curl -LsSf https://astral.sh/uv/install.sh | sh` (ou `pip install --user uv`).

```bash
git clone <url-do-fork> desafio-a2a-com-mcp
cd desafio-a2a-com-mcp
uv sync
```

**Terminal 1, servidor MCP.** A chave de integridade do `requestState` vem de `REQUEST_STATE_SECRET`, com no mínimo 32 bytes aleatórios. Gere a sua e exporte no terminal do servidor; o servidor não sobe sem ela. Para reiniciar o servidor e continuar aceitando os `requestState` já emitidos, reaproveite o mesmo valor.

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
uv run python servidor-mcp/servidor.py
```

**Terminal 2, agente:**

```bash
uv run python agente/agente.py
```

**Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

<details>
<summary>Sem uv: venv + pip</summary>

```bash
python3 -m venv .venv            # no Debian/Ubuntu pode exigir: sudo apt install python3-venv
.venv/bin/pip install -r requirements.txt
# terminal 1
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
.venv/bin/python servidor-mcp/servidor.py
# terminal 2
.venv/bin/python agente/agente.py
```

O `requirements.txt` é exportado do `uv.lock` e traz as mesmas versões travadas.
</details>

Variáveis opcionais, com os padrões esperados pelo validador:

| Variável | Padrão | Processo |
|---|---|---|
| `MCP_PORT` | `7301` | servidor MCP |
| `AGENT_PORT` | `7300` | agente |
| `MCP_URL` | `http://localhost:7301/mcp` | agente (onde está o servidor MCP) |
| `AGENT_URL` | `http://localhost:7300` | agente (URL pública publicada no card) |

O stderr do servidor MCP registra uma linha por request, antes de qualquer validação do SDK, então os requests recusados também aparecem:

```
2026-09-24T15:33:59.525 [mcp] method=tools/call id=6 traceparent=00-9cb16f06...-8575ed784c3e26e8-01 name=reservar_sala caps={"elicitation":{"form":{}}}
2026-09-24T15:33:59.530 [mcp] method=tools/call id=7 traceparent=00-9cb16f06...-d886994ef973854e-01 name=reservar_sala caps={"elicitation":{"form":{}}} retry=sim
```

## Onde a ponte acontece

A ponte está em [agente/ponte.py](agente/ponte.py). O agente faz o `tools/call` com [`HostMCP.chamar_tool`](agente/host_mcp.py#L117), que usa `session.call_tool(..., allow_input_required=True)`. Isso devolve o `InputRequiredResult` cru, sem o driver automático do SDK e sem callback de elicitation, então nada responde à pergunta no lugar do cliente A2A. Em [`Ponte._traduzir`](agente/ponte.py#L116), um `InputRequiredResult` segue para [`Ponte._pausar`](agente/ponte.py#L140). É ali que o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`: o agente lê a única entrada de `inputRequests`, tira as opções do `enum` (ou do `const`) do `requestedSchema`, guarda em [`Pausa`](agente/tarefas.py) a chave atribuída pelo servidor, a tool, os argumentos originais e o `requestState` sem abrir nada, e publica exatamente a linha `alternativas: ...`. O `requestState` volta ao servidor em [`Ponte.continuar`](agente/ponte.py#L83): `escolha=<id>` vira `action: accept` e `escolha=recusar` vira `action: decline`, e o agente repete o `tools/call` original com a mesma chave em `inputResponses` e o `requestState` ecoado byte a byte ([linha 108](agente/ponte.py#L108)). É um request JSON-RPC novo, com id novo cunhado pelo SDK. Uma escolha fora do `enum` não chega ao servidor: a Task continua em `INPUT_REQUIRED` e a linha de alternativas se repete.

Do lado do servidor, a pergunta nasce no resolver [`escolha_de_sala`](servidor-mcp/servidor.py#L195), que é o mecanismo de MRTR de primeira classe do SDK Python (`Resolve` + `Elicit`). Sem conflito, ele devolve `None` e a reserva segue. Com conflito e alternativas, ele devolve um `Elicit`, e o SDK termina a resposta com `resultType: input_required`, sem abrir canal de volta. Sem alternativas, a tool devolve `isError` com `Sem alternativas disponiveis no intervalo`. No retry, o resolver roda de novo, recalcula a mesma pergunta, e o SDK injeta a resposta que veio em `inputResponses`.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do próprio SDK: `RequestStateSecurity(keys=[REQUEST_STATE_SECRET])` instala o `RequestStateBoundary`, que sela todo `requestState` que sai e verifica todo que volta. O selo é AES-256-GCM (AEAD, então cifra e autentica) com chave derivada do segredo por HKDF-SHA256. O envelope selado traz emissão, expiração, audiência (`central-de-salas`), método, nome da tool e um digest dos argumentos. Qualquer caractere trocado quebra a tag GCM, e o request volta com `-32602 Invalid or expired requestState`. O motivo real (`seal`, `expired`, `request binding`) vai só para o log do servidor. A chave vem do ambiente, nunca do código: sem `REQUEST_STATE_SECRET`, ou com menos de 32 bytes, o servidor se recusa a subir.

**Validade.** São 10 minutos (`VALIDADE_DO_ESTADO` em [servidor.py](servidor-mcp/servidor.py)), dentro da faixa de 5 a 30 minutos pedida.

**Sem estado no servidor entre as rodadas.** Entre o `input_required` e o retry, o servidor não guarda nada. O selo carrega o vínculo com o pedido original (tool e digest dos argumentos) e a pergunta feita, e o retry reapresenta os argumentos, que precisam bater com o digest selado. Por isso um retry funciona depois de reiniciar o servidor MCP, desde que ele volte com o mesmo `REQUEST_STATE_SECRET`. Argumentos adulterados no retry fazem o SDK rejeitar o estado com `-32602` (`request binding`), que é um dos dois caminhos aceitos pelo enunciado. O conflito e as alternativas são recalculados no retry, e o SDK só aceita a resposta se a pergunta recalculada for idêntica à que foi feita. Se as alternativas mudaram nesse meio-tempo, o servidor pergunta de novo e a Task volta a pausar com a lista nova.

**Capability e headers.** A checagem de `_meta` obrigatório (`-32602`/HTTP 400), de headers espelhados (`-32020`) e de capability de elicitation (`-32021` com `data.requiredCapabilities`/HTTP 400) é feita pelo transporte e pelos resolvers do SDK. Do lado do agente, o cliente do SDK sempre carimba `protocolVersion`, `clientInfo` e `clientCapabilities` no `_meta` e espelha `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` nos headers.

**Limitação do SDK documentada.** O `ClientSession` do `mcp==2.2.0` só anuncia elicitation quando recebe um `elicitation_callback`, e nesse caso anuncia `form` **e** `url` (`ClientSession._build_capabilities` em `mcp/client/session.py` do pacote, trecho `ElicitationCapability(form=FormElicitationCapability(), url=UrlElicitationCapability()) if self._elicitation_callback is not _default_elicitation_callback`). O agente não pode registrar callback, senão o cliente responde à elicitation sozinho e a Task nunca pausa, e ele só sabe traduzir form mode. Por isso [`_SessaoSoFormulario`](agente/host_mcp.py) sobrescreve só esse método para anunciar exatamente `{"elicitation": {"form": {}}}`. O resto do protocolo continua com o SDK.

**Estado das Tasks.** As Tasks ficam em memória no processo do agente ([agente/tarefas.py](agente/tarefas.py)), em um dicionário por id. Cada Task tem sua própria `Pausa`, o que isola o `requestState` de duas Tasks pausadas ao mesmo tempo, e seu próprio `asyncio.Lock`, que serializa continuações da mesma Task sem bloquear as outras. A `Pausa` fica fora do que `Task.para_wire()` serializa, então o `requestState` nunca aparece em resposta A2A. A Task passa por `SUBMITTED` e `WORKING` antes de terminar. Estado terminal é definitivo: `SendMessage` para uma Task `COMPLETED`, `CANCELED` ou `FAILED` recebe `-32004` (`UnsupportedOperationError`), e para um id desconhecido recebe `-32001` (`TaskNotFoundError`).

**Trace context.** O agente lê o header `traceparent` do `SendMessage` e guarda o trace-id na Task. Todo request MCP daquela Task (`tools/list`, `resources/read`, `tools/call` e o retry) leva no `_meta` um `traceparent` com o mesmo trace-id e um span-id novo. Se o cliente A2A não mandar o header, a Task ganha um trace-id novo.

**Host MCP.** Antes de cada `tools/call` inicial, o agente faz um `tools/list`, confere que `reservar_sala` está entre as tools descobertas e lê `politica://uso`. A versão da primeira linha do resource vai para o campo `politica` do artifact. O cliente do SDK fica aberto durante toda a vida do processo, mas com a versão fixada em `2026-07-28` e o cache desligado: cada request é autocontido e nada é inferido de request anterior.

**Sem LLM.** O pedido é interpretado por expressão regular no formato fixo `reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>`, e a resposta à pausa, no formato `escolha=<id>|recusar`. Não há SDK de provedor de LLM nas dependências.

## Saída do validador

Última execução, com os dois processos recém-iniciados a partir de uma cópia limpa:

```
trace-id desta execucao: f2fca935d2141bb8f6160cdda6440929
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

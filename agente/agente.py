"""O agente como servidor A2A v1.0 (binding JSON-RPC 2.0 sobre HTTP).

    python agente/agente.py

- `GET  /.well-known/agent-card.json`: Agent Card
- `POST /a2a`: `SendMessage` e `GetTask`
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from host_mcp import HostMCP, TraceContext
from ponte import Ponte
from tarefas import FAILED, INPUT_REQUIRED, Task, Tarefas

PORTA = int(os.environ.get("AGENT_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENT_URL", f"http://localhost:{PORTA}").rstrip("/")
URL_MCP = os.environ.get("MCP_URL", "http://localhost:7301/mcp")

# Codigos de erro JSON-RPC: os da especificacao e os da A2A v1.0.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

CARD: dict[str, Any] = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty",
                "escolha=sala-fusca",
                "escolha=recusar",
            ],
        }
    ],
}


class ErroRPC(Exception):
    def __init__(self, codigo: int, mensagem: str, dados: Any = None) -> None:
        super().__init__(mensagem)
        self.codigo, self.mensagem, self.dados = codigo, mensagem, dados

    def para_wire(self) -> dict[str, Any]:
        erro: dict[str, Any] = {"code": self.codigo, "message": self.mensagem}
        if self.dados is not None:
            erro["data"] = self.dados
        return erro


tarefas = Tarefas()
host = HostMCP(URL_MCP)
ponte = Ponte(host)


async def _executar(task: Task, passo: Awaitable[None]) -> None:
    """Uma falha inesperada termina a Task em FAILED em vez de deixa-la presa em WORKING."""
    try:
        await passo
    except Exception as erro:
        traceback.print_exc(file=sys.stderr)
        if not task.terminal:
            task.pausa = None
            task.transitar(FAILED, f"Erro interno do agente: {type(erro).__name__}")


def _texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    partes = mensagem.get("parts")
    if not isinstance(partes, list) or not partes:
        raise ErroRPC(INVALID_PARAMS, "message.parts precisa ser uma lista nao vazia")
    textos = [p["text"] for p in partes if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if not textos:
        raise ErroRPC(INVALID_PARAMS, "message.parts precisa conter ao menos uma parte de texto")
    return "\n".join(textos)


async def send_message(params: dict[str, Any], request: Request) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict):
        raise ErroRPC(INVALID_PARAMS, "params.message e obrigatorio")
    texto = _texto_da_mensagem(mensagem)
    trace = TraceContext.do_header(request.headers.get("traceparent"))
    task_id = mensagem.get("taskId")

    if task_id is None:
        task = tarefas.nova(trace or TraceContext.novo(), mensagem.get("contextId"))
        async with task.trava:
            task.registrar_usuario(mensagem)
            await _executar(task, ponte.iniciar(task, texto))
            return {"task": task.para_wire()}

    task = tarefas.buscar(str(task_id))
    if task is None:
        raise ErroRPC(TASK_NOT_FOUND, "Task not found", {"taskId": task_id})
    # A trava serializa continuacoes da mesma Task; Tasks diferentes seguem em paralelo.
    async with task.trava:
        if task.terminal:
            raise ErroRPC(
                UNSUPPORTED_OPERATION,
                f"Task {task.id} esta em estado terminal ({task.estado}) e nao aceita novas mensagens",
                {"taskId": task.id, "state": task.estado},
            )
        if task.estado != INPUT_REQUIRED:
            raise ErroRPC(UNSUPPORTED_OPERATION, f"Task {task.id} nao esta aguardando entrada ({task.estado})")
        task.registrar_usuario(mensagem)
        await _executar(task, ponte.continuar(task, texto))
        return {"task": task.para_wire()}


async def get_task(params: dict[str, Any], request: Request) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str):
        raise ErroRPC(INVALID_PARAMS, "params.id e obrigatorio")
    task = tarefas.buscar(task_id)
    if task is None:
        raise ErroRPC(TASK_NOT_FOUND, "Task not found", {"taskId": task_id})
    limite = params.get("historyLength")
    return {"task": task.para_wire(limite if isinstance(limite, int) else None)}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


async def endpoint_a2a(request: Request) -> JSONResponse:
    id_rpc: Any = None
    try:
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            raise ErroRPC(PARSE_ERROR, "Parse error") from None
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
            raise ErroRPC(INVALID_REQUEST, "Invalid Request")
        id_rpc = corpo.get("id")
        metodo = METODOS.get(corpo["method"])
        if metodo is None:
            raise ErroRPC(METHOD_NOT_FOUND, f"Method not found: {corpo['method']}")
        params = corpo.get("params") or {}
        if not isinstance(params, dict):
            raise ErroRPC(INVALID_PARAMS, "params precisa ser um objeto")
        print(f"[agente] a2a {corpo['method']} id={json.dumps(id_rpc)}", file=sys.stderr, flush=True)
        resultado = await metodo(params, request)
        return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "result": resultado})
    except ErroRPC as erro:
        return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "error": erro.para_wire()})


async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(CARD)


@asynccontextmanager
async def ciclo_de_vida(app: Starlette) -> AsyncIterator[None]:
    await host.abrir()
    try:
        yield
    finally:
        await host.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", endpoint_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


def main() -> None:
    print(f"agente A2A em {URL_PUBLICA}/a2a, servidor MCP em {URL_MCP}", file=sys.stderr)
    uvicorn.run(app, host=os.environ.get("AGENT_HOST", "0.0.0.0"), port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()

"""Registro de cada request recebido, no stderr.

Fica na borda ASGI, antes do SDK, para registrar tambem os requests que o SDK
recusa antes de chegar ao handler (por exemplo, `_meta` sem os campos obrigatorios).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _linha(corpo: bytes) -> str:
    try:
        req = json.loads(corpo)
    except ValueError:
        return "corpo nao e JSON"
    if not isinstance(req, dict):
        return "corpo nao e um objeto JSON-RPC"
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    meta: dict[str, Any] = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    alvo = params.get("name") or params.get("uri")
    partes = [
        f"method={req.get('method')}",
        f"id={json.dumps(req.get('id'))}",
        f"traceparent={meta.get('traceparent', '-')}",
    ]
    if alvo:
        partes.append(f"name={alvo}")
    capacidades = meta.get("io.modelcontextprotocol/clientCapabilities", "-")
    partes.append(f"caps={json.dumps(capacidades, separators=(',', ':'))}")
    if "requestState" in params:
        partes.append("retry=sim")
    return " ".join(partes)


class RegistroDeRequests:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        mensagens: list[Message] = []
        corpo = b""
        while True:
            mensagem = await receive()
            mensagens.append(mensagem)
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break

        agora = datetime.now().isoformat(timespec="milliseconds")
        print(f"{agora} [mcp] {_linha(corpo)}", file=sys.stderr, flush=True)

        async def reenviar() -> Message:
            return mensagens.pop(0) if mensagens else await receive()

        await self.app(scope, reenviar, send)

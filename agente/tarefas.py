"""Tasks A2A em memoria: identidade, estado, historico e produto.

O `requestState` de uma Task pausada mora em `Pausa`, fora do que `para_wire()`
serializa: ele nunca aparece no card, no artifact ou em mensagem ao cliente A2A.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from host_mcp import TraceContext

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = frozenset({COMPLETED, CANCELED, FAILED})


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Pausa:
    """Tudo o que o agente precisa para repetir o `tools/call` original.

    `request_state` e opaco: guardado e ecoado byte a byte, nunca aberto.
    """

    tool: str
    argumentos: dict[str, Any]
    chave: str
    campo: str
    opcoes: list[str]
    request_state: str


@dataclass
class Task:
    id: str
    context_id: str
    trace: TraceContext
    estado: str = SUBMITTED
    mensagem_status: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    atualizado_em: str = field(default_factory=_agora)
    versao_politica: str | None = None
    pausa: Pausa | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def registrar_usuario(self, mensagem: dict[str, Any]) -> None:
        self.historico.append({**mensagem, "taskId": self.id, "contextId": self.context_id})

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.terminal:
            raise RuntimeError(f"Task {self.id} ja terminou em {self.estado}")
        self.estado = estado
        self.atualizado_em = _agora()
        self.mensagem_status = None
        if texto is not None:
            mensagem = {
                "messageId": _id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.mensagem_status = mensagem
            self.historico.append(mensagem)
        if self.terminal:
            self.pausa = None

    def anexar_artifact(self, nome: str, texto: str) -> None:
        self.artifacts.append({"artifactId": _id("art"), "name": nome, "parts": [{"text": texto}]})

    def para_wire(self, limite_historico: int | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado, "timestamp": self.atualizado_em}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        historico = self.historico
        if limite_historico is not None:
            historico = historico[-limite_historico:] if limite_historico > 0 else []
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(historico),
            "artifacts": list(self.artifacts),
        }


class Tarefas:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def nova(self, trace: TraceContext, context_id: str | None = None) -> Task:
        task = Task(id=_id("task"), context_id=context_id or _id("ctx"), trace=trace)
        self._tasks[task.id] = task
        return task

    def buscar(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

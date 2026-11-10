"""O vigia do host (`implantacao/producao/vigia.sh`), com `docker` e `curl` de mentira.

O Docker sem Swarm nao reinicia conteiner `unhealthy` (P0 #11): sem o vigia, o
HEALTHCHECK ficava vermelho para sempre e nada acontecia. Aqui o script roda de
verdade, no bash, e o que se confere e o que ele MANDOU fazer -- reiniciar, pingar,
ou ficar quieto.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
VIGIA = RAIZ / "implantacao" / "producao" / "vigia.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or "system32" in (BASH or "").lower(),
    reason="precisa de um bash POSIX (Git Bash no Windows, bash no Linux)",
)

FALSO_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >>"$FALSO_LOG"
case "$1" in
  inspect)
    case "$*" in
      *State.Running*) echo true ;;
      *Health.Log*) echo "HTTP Error 503" ;;
      *) cat "$FALSO_ESTADO" ;;
    esac ;;
  restart) exit 0 ;;
esac
"""

FALSO_CURL = """#!/usr/bin/env bash
echo "curl $*" >>"$FALSO_LOG"
"""


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for nome, corpo in (("docker", FALSO_DOCKER), ("curl", FALSO_CURL)):
        alvo = bin_dir / nome
        alvo.write_text(corpo, encoding="utf-8", newline="\n")
        alvo.chmod(0o755)
    pasta = tmp_path / "crypto"
    pasta.mkdir()
    estado = tmp_path / "estado"
    log = tmp_path / "chamadas.log"
    log.write_text("", encoding="utf-8")
    falso_estado = tmp_path / "health"

    def rodar(
        saude: str, *, url: str | None = "https://vigia.example/abc", **extra: str
    ) -> list[str]:
        falso_estado.write_text(saude + "\n", encoding="utf-8", newline="\n")
        (pasta / ".env").write_text(
            f"PASTA={pasta.as_posix()}\n" + (f"VIGIA_URL={url}\n" if url else ""),
            encoding="utf-8",
            newline="\n",
        )
        antes = log.read_text(encoding="utf-8").splitlines()
        ambiente = {
            **os.environ,
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}",
            "PASTA": pasta.as_posix(),
            "VIGIA_ESTADO": estado.as_posix(),
            "FALSO_LOG": log.as_posix(),
            "FALSO_ESTADO": falso_estado.as_posix(),
            **extra,
        }
        resultado = subprocess.run(
            [BASH, VIGIA.as_posix()], env=ambiente, capture_output=True, text=True, timeout=60
        )
        assert resultado.returncode == 0, resultado.stderr
        return log.read_text(encoding="utf-8").splitlines()[len(antes) :]

    return rodar


def _reinicios(chamadas: list[str]) -> list[str]:
    return [c for c in chamadas if c.startswith("docker restart")]


def _pings(chamadas: list[str]) -> list[str]:
    return [c for c in chamadas if c.startswith("curl")]


def test_saudavel_pinga_e_nao_reinicia(host):
    chamadas = host("healthy")
    assert _reinicios(chamadas) == []
    assert len(_pings(chamadas)) == 1
    assert "/fail" not in _pings(chamadas)[0]


def test_sem_url_nao_pinga_nada(host):
    assert _pings(host("healthy", url=None)) == []


def test_unhealthy_reinicia_so_na_terceira_seguida(host):
    primeira = host("unhealthy")
    segunda = host("unhealthy")
    terceira = host("unhealthy")

    assert _reinicios(primeira) == [] and _reinicios(segunda) == []
    assert _reinicios(terceira) == ["docker restart -t 60 crypto-prd"]
    # Nenhum ping de vida com o sistema doente: o dead-man tem que poder disparar.
    for chamadas in (primeira, segunda, terceira):
        assert all("/fail" in p for p in _pings(chamadas)), chamadas


def test_uma_verificacao_saudavel_zera_a_contagem(host):
    host("unhealthy")
    host("unhealthy")
    host("healthy")
    assert _reinicios(host("unhealthy")) == []


def test_para_de_reiniciar_depois_do_teto_por_hora(host):
    reinicios = 0
    for _ in range(5):
        reinicios += len(_reinicios(host("unhealthy", VIGIA_LIMITE="1")))
    assert reinicios == 3, "reiniciar em laco esconde o problema no ruido (D25)"


@pytest.mark.parametrize("estado", ["starting", "ausente", "sem-healthcheck"])
def test_subida_ou_parada_pedida_nao_reinicia_nem_pinga(host, estado):
    for _ in range(3):
        chamadas = host(estado)
        assert _reinicios(chamadas) == []
        assert _pings(chamadas) == []


def test_o_healthcheck_da_imagem_usa_a_sonda_do_sistema_vivo():
    """Sem `?sonda=1` a rota responde 200 com os agentes mortos (a tela precisa disso)."""
    dockerfile = (RAIZ / "Dockerfile").read_text(encoding="utf-8")
    healthcheck = dockerfile[dockerfile.index("HEALTHCHECK") :].split("\nSTOPSIGNAL")[0]
    assert "/crypto-traders/api/health?sonda=1" in healthcheck


def test_o_timer_do_vigia_roda_o_script():
    servico = (RAIZ / "implantacao/producao/systemd/crypto-vigia.service").read_text(
        encoding="utf-8"
    )
    timer = (RAIZ / "implantacao/producao/systemd/crypto-vigia.timer").read_text(
        encoding="utf-8"
    )
    assert "implantacao/producao/vigia.sh" in servico
    assert "OnUnitActiveSec=1min" in timer

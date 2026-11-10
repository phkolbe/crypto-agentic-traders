"""`implantacao/producao/implantar.sh` com `docker` de mentira: a volta automatica.

Antes, o sistema novo que nao ficava saudavel em 3 min fazia o script sair com 1 e
deixava a versao nova (quebrada) no ar; a anterior nem era registrada. Agora ele volta
sozinho para a ultima versao que passou numa implantacao -- e o banco decide como:

- schema igual (o `migrar` da nova nao aplicou migracao): so a imagem volta;
- schema mudou, fora do `live`: restaura o dump feito antes do `migrar` e volta;
- schema mudou, em `live`: NAO restaura sozinho (apagaria ordens e trades reais que a
  nova gravou desde o dump e deixaria o sistema parado); diz os comandos e sai com 1.

O script roda de verdade, no bash, com o `backup.sh` e o `restaurar.sh` de verdade; o
`docker` falso guarda o que "esta rodando" e a versao do schema num diretorio.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PRODUCAO = RAIZ / "implantacao" / "producao"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or "system32" in (BASH or "").lower(),
    reason="precisa de um bash POSIX (Git Bash no Windows, bash no Linux)",
)

FALSO_DOCKER = r"""#!/usr/bin/env bash
echo "docker $* [VERSAO=${VERSAO_CRYPTO:-}]" >>"$FALSO_LOG"
D="$FALSO_DIR"
case "$*" in
  compose*" pull"*) ;;
  compose*" up -d --wait postgres"*) ;;
  compose*" up -d --no-deps crypto"*) echo "${VERSAO_CRYPTO:?}" >"$D/rodando" ;;
  compose*" up -d crypto"*)
    [ "${FALSO_MIGRAR_FALHA:-0}" = 0 ] || exit 1
    echo nova >"$D/rodando"
    [ -z "${FALSO_SCHEMA_DEPOIS:-}" ] || echo "$FALSO_SCHEMA_DEPOIS" >"$D/schema" ;;
  compose*" up -d tailscale"*) ;;
  *"information_schema.tables"*) echo "${FALSO_TABELAS:-12}" ;;
  *"schema_versao"*) cat "$D/schema" 2>/dev/null || echo "${FALSO_SCHEMA:-2}" ;;
  *"pg_dump"*) printf 'PGDMP-falso' ;;
  *"pg_restore"*) cat >/dev/null; rm -f "$D/schema" ;;
  *"Config.Image"*)
    [ -n "${FALSO_IMAGEM_ATUAL:-}" ] || exit 1
    echo "ghcr.io/phkolbe/crypto:$FALSO_IMAGEM_ATUAL" ;;
  *"State.Health.Status"*)
    if [ "$(cat "$D/rodando" 2>/dev/null)" = nova ]; then
      echo "${FALSO_SAUDE_NOVA:-healthy}"
    else
      echo "${FALSO_SAUDE_ANTERIOR:-healthy}"
    fi ;;
  *"State.Running"*) echo false ;;
  *"python -c"*)
    if [ "$(cat "$D/rodando" 2>/dev/null)" = nova ]; then
      echo "${FALSO_TRAVA_NOVA:-403}"
    else
      echo 403
    fi ;;
esac
exit 0
"""

FALSO_REGISTRO = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >>"$FALSO_LOG"
exit 0
"""


@pytest.fixture
def servidor(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    falsos = (("docker", FALSO_DOCKER), ("logger", FALSO_REGISTRO), ("sleep", FALSO_REGISTRO))
    for nome, corpo in falsos:
        alvo = bin_dir / nome
        alvo.write_text(corpo, encoding="utf-8", newline="\n")
        alvo.chmod(0o755)
    pasta = tmp_path / "crypto"
    (pasta / "backups").mkdir(parents=True)
    estado = tmp_path / "docker"
    estado.mkdir()
    log = tmp_path / "chamadas.log"

    def rodar(
        *args: str,
        versao: str = "v0.3.0",
        modo: str = "dry_run",
        no_ar: str | None = "v0.2.0",
        **extra: str,
    ):
        (pasta / ".env").write_text(
            f"PASTA={pasta.as_posix()}\nVERSAO_CRYPTO={versao}\nTS_HOSTNAME=crypto\n"
            "TAILNET=exemplo.ts.net\nCRYPTO_LOGIN=dono@exemplo.com\nRESTIC_REPOSITORY=\n",
            encoding="utf-8",
            newline="\n",
        )
        (pasta / "crypto.env").write_text(f"TRADING_MODE={modo}\n", encoding="utf-8", newline="\n")
        if no_ar is not None:
            (pasta / "versao-no-ar").write_text(no_ar + "\n", encoding="utf-8", newline="\n")
        (estado / "rodando").write_text("anterior\n", encoding="utf-8", newline="\n")
        log.write_text("", encoding="utf-8")
        ambiente = {
            **os.environ,
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}",
            "PASTA": pasta.as_posix(),
            "FALSO_LOG": log.as_posix(),
            "FALSO_DIR": estado.as_posix(),
            # Uma verificacao de saude so: o laco de 3 min com `docker` falso no Git Bash
            # do Windows levava minutos por teste.
            "IMPLANTAR_ESPERA_SEGUNDOS": "3",
            **extra,
        }
        ambiente.pop("VERSAO_CRYPTO", None)
        resultado = subprocess.run(
            [BASH, (PRODUCAO / "implantar.sh").as_posix(), *args],
            env=ambiente,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return resultado, log.read_text(encoding="utf-8").splitlines()

    rodar.pasta = pasta  # type: ignore[attr-defined]
    return rodar


def _com(chamadas: list[str], trecho: str) -> list[str]:
    return [c for c in chamadas if trecho in c]


def _no_ar(servidor) -> str:
    return (servidor.pasta / "versao-no-ar").read_text(encoding="utf-8").strip()


def test_implantacao_saudavel_registra_a_versao_e_nao_volta(servidor):
    resultado, chamadas = servidor()
    assert resultado.returncode == 0, resultado.stderr
    assert _com(chamadas, "--no-deps") == []
    assert _no_ar(servidor) == "v0.3.0"


def test_nova_doente_com_schema_igual_volta_so_a_imagem(servidor):
    resultado, chamadas = servidor(FALSO_SAUDE_NOVA="unhealthy")

    assert resultado.returncode == 1
    (volta,) = _com(chamadas, "up -d --no-deps crypto")
    assert "[VERSAO=v0.2.0]" in volta
    assert _com(chamadas, "pg_restore") == [], "schema igual: o banco nao e tocado"
    assert "VOLTOU para v0.2.0" in resultado.stderr
    # A anterior volta conferida: a trava de acesso e testada de novo.
    i_volta = chamadas.index(volta)
    assert _com(chamadas[i_volta:], "python -c")
    # O registro continua apontando a ultima versao que passou.
    assert _no_ar(servidor) == "v0.2.0"
    # O no Tailscale nao e mexido numa implantacao que falhou.
    assert _com(chamadas, "up -d tailscale") == []


def test_sem_registro_a_anterior_vem_do_conteiner_que_rodava(servidor):
    resultado, chamadas = servidor(
        no_ar=None, FALSO_SAUDE_NOVA="unhealthy", FALSO_IMAGEM_ATUAL="v0.1.9"
    )
    assert resultado.returncode == 1
    (volta,) = _com(chamadas, "up -d --no-deps crypto")
    assert "[VERSAO=v0.1.9]" in volta


def test_migrar_que_falha_tambem_volta(servidor):
    resultado, chamadas = servidor(FALSO_MIGRAR_FALHA="1")
    assert resultado.returncode == 1
    (volta,) = _com(chamadas, "up -d --no-deps crypto")
    assert "[VERSAO=v0.2.0]" in volta


def test_trava_aberta_na_nova_volta(servidor):
    """A nova saudavel que aceita pedido sem o login e pior que a nova doente."""
    resultado, chamadas = servidor(FALSO_TRAVA_NOVA="200")
    assert resultado.returncode == 1
    assert _com(chamadas, "up -d --no-deps crypto")
    assert _no_ar(servidor) == "v0.2.0"


def test_schema_migrado_fora_do_live_restaura_o_dump_de_antes_e_volta(servidor):
    resultado, chamadas = servidor(FALSO_SAUDE_NOVA="unhealthy", FALSO_SCHEMA_DEPOIS="3")

    assert resultado.returncode == 1
    i_dump = next(i for i, c in enumerate(chamadas) if "pg_dump" in c)
    i_migrar = next(i for i, c in enumerate(chamadas) if "up -d crypto" in c)
    i_restore = next(i for i, c in enumerate(chamadas) if "pg_restore" in c)
    i_volta = next(i for i, c in enumerate(chamadas) if "up -d --no-deps crypto" in c)
    assert i_dump < i_migrar < i_restore < i_volta
    restauracao = chamadas[i_restore]
    assert "-d crypto " in restauracao and "--single-transaction" in restauracao
    assert "[VERSAO=v0.2.0]" in chamadas[i_volta]
    assert "VOLTOU para v0.2.0" in resultado.stderr


def test_schema_migrado_em_live_nao_restaura_sozinho(servidor):
    resultado, chamadas = servidor(
        modo="live", FALSO_SAUDE_NOVA="unhealthy", FALSO_SCHEMA_DEPOIS="3"
    )

    assert resultado.returncode == 1
    assert _com(chamadas, "pg_restore") == []
    assert _com(chamadas, "--no-deps") == []
    assert "restaurar.sh --confirmar" in resultado.stderr
    assert "VERSAO_CRYPTO=v0.2.0" in resultado.stderr
    assert any("-p user.err" in c for c in _com(chamadas, "logger"))


def test_primeira_implantacao_nao_tem_para_onde_voltar(servidor):
    resultado, chamadas = servidor(
        "--sem-backup", no_ar=None, FALSO_SAUDE_NOVA="unhealthy", FALSO_TABELAS="0"
    )
    assert resultado.returncode == 1
    assert _com(chamadas, "--no-deps") == []
    assert "sem versao anterior" in resultado.stderr.lower()


def test_mesma_versao_nao_volta_para_ela_mesma(servidor):
    resultado, chamadas = servidor(no_ar="v0.3.0", FALSO_SAUDE_NOVA="unhealthy")
    assert resultado.returncode == 1
    assert _com(chamadas, "--no-deps") == []


def test_anterior_que_tambem_nao_sobe_e_dita(servidor):
    resultado, _ = servidor(FALSO_SAUDE_NOVA="unhealthy", FALSO_SAUDE_ANTERIOR="unhealthy")
    assert resultado.returncode == 1
    assert "nem a v0.2.0" in resultado.stderr.lower()
    assert "VOLTOU" not in resultado.stderr

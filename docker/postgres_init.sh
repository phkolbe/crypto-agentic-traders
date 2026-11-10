#!/bin/sh
# Primeira subida do Postgres do Crypto (so roda com o volume vazio): o database
# `crypto` e dois usuarios, como o Postgres da plataforma faz para cada servico.
#
#   crypto_dono  dono do database e do schema: e com ele que roda o
#                `crypto-traders migrar` (cria tabelas e gatilhos append-only).
#   crypto       usuario da aplicacao: so le e grava LINHAS. Sem TRIGGER e sem
#                CREATE, nao derruba a propria trilha de auditoria.
#
# As senhas vem do ambiente do conteiner (CRYPTO_SENHA_DONO, CRYPTO_SENHA_APP).
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v dono="$CRYPTO_SENHA_DONO" -v app="$CRYPTO_SENHA_APP" <<'SQL'
CREATE ROLE crypto_dono LOGIN PASSWORD :'dono' NOSUPERUSER NOCREATEROLE NOCREATEDB;
CREATE ROLE crypto LOGIN PASSWORD :'app' NOSUPERUSER NOCREATEROLE NOCREATEDB;
CREATE DATABASE crypto OWNER crypto_dono;
REVOKE ALL ON DATABASE crypto FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE crypto TO crypto;
SQL

# Tudo o que o dono criar no schema ja nasce com leitura e escrita para a
# aplicacao (o `migrar --usuario-aplicacao crypto` concede de novo, idempotente).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname crypto <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO crypto;
ALTER DEFAULT PRIVILEGES FOR ROLE crypto_dono IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crypto;
ALTER DEFAULT PRIVILEGES FOR ROLE crypto_dono IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO crypto;
SQL

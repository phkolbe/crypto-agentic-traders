-- Habilita o TimescaleDB no banco recem-criado.
-- As hypertables em si sao criadas por init_db() no codigo, depois que as
-- tabelas existem (ver crypto_traders/db/session.py).
CREATE EXTENSION IF NOT EXISTS timescaledb;

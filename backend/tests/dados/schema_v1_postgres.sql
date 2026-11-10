-- Schema da VERSAO 1 (linha de base), como o `crypto-traders migrar` o deixava
-- no Postgres ate 09/11/2026: `create_all` + gatilhos, sem tabela de versao.
-- Gerado com `pg_dump --schema-only --no-owner --no-privileges` de um banco
-- migrado pelo codigo da main (597e27d), sem comentarios e sem SET. Usado por
-- `tests/test_migracoes.py` para criar o banco "antigo" e migra-lo.
--
-- NAO editar a mao: e o retrato do schema dos bancos que ja existem.

CREATE FUNCTION public.audit_log_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'audit_log e append-only: UPDATE/DELETE recusado'; END $$;

CREATE FUNCTION public.historico_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'risk_events e append-only: UPDATE/DELETE recusado'; END $$;

CREATE FUNCTION public.trades_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'trades e append-only: UPDATE recusado, e DELETE so de lancamento manual (origin manual)'; END $$;

CREATE FUNCTION public.trades_delete_apenas_manual() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN IF OLD.origin <> 'manual' THEN RAISE EXCEPTION 'trades e append-only: UPDATE recusado, e DELETE so de lancamento manual (origin manual)'; END IF; RETURN OLD; END $$;

CREATE TABLE public.agent_runs (
    id integer NOT NULL,
    agent character varying(64) NOT NULL,
    state character varying(16) NOT NULL,
    "timestamp" timestamp with time zone NOT NULL,
    detail text
);

CREATE SEQUENCE public.agent_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.agent_runs_id_seq OWNED BY public.agent_runs.id;

CREATE TABLE public.audit_log (
    id integer NOT NULL,
    "timestamp" timestamp with time zone NOT NULL,
    actor character varying(64) NOT NULL,
    action character varying(64) NOT NULL,
    target character varying(128),
    before json NOT NULL,
    after json NOT NULL,
    detail text
);

CREATE SEQUENCE public.audit_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;

CREATE TABLE public.candles (
    exchange character varying(32) NOT NULL,
    symbol character varying(32) NOT NULL,
    timeframe character varying(8) NOT NULL,
    open_time timestamp with time zone NOT NULL,
    open numeric(38,18) NOT NULL,
    high numeric(38,18) NOT NULL,
    low numeric(38,18) NOT NULL,
    close numeric(38,18) NOT NULL,
    volume numeric(38,18) NOT NULL
);

CREATE TABLE public.notification_config (
    id integer NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    email_enabled boolean NOT NULL,
    email_to character varying(320),
    whatsapp_enabled boolean NOT NULL,
    whatsapp_to character varying(32)
);

CREATE TABLE public.onchain_metrics (
    id integer NOT NULL,
    metric character varying(32) NOT NULL,
    day date NOT NULL,
    value double precision NOT NULL,
    fetched_at timestamp with time zone NOT NULL
);

CREATE SEQUENCE public.onchain_metrics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.onchain_metrics_id_seq OWNED BY public.onchain_metrics.id;

CREATE TABLE public.orders (
    id character varying(32) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    client_order_id character varying(64) NOT NULL,
    exchange_order_id character varying(64),
    signal_id character varying(32),
    risk_event_id character varying(32) NOT NULL,
    exchange character varying(32) NOT NULL,
    symbol character varying(32) NOT NULL,
    side character varying(8) NOT NULL,
    order_type character varying(16) NOT NULL,
    quantity numeric(38,18) NOT NULL,
    price numeric(38,18),
    notional numeric(38,18) NOT NULL,
    stop_loss numeric(38,18),
    take_profit numeric(38,18),
    status character varying(24) NOT NULL,
    filled_quantity numeric(38,18) NOT NULL,
    average_price numeric(38,18),
    strategy character varying(64),
    mode character varying(16) NOT NULL,
    error text,
    raw json NOT NULL
);

CREATE TABLE public.portfolio_snapshots (
    id character varying(32) NOT NULL,
    "timestamp" timestamp with time zone NOT NULL,
    total_value numeric(38,18) NOT NULL,
    cash_value numeric(38,18) NOT NULL,
    positions_value numeric(38,18) NOT NULL,
    realized_pnl numeric(38,18) NOT NULL,
    unrealized_pnl numeric(38,18) NOT NULL,
    allocations json NOT NULL,
    positions json NOT NULL,
    mode character varying(16) NOT NULL
);

CREATE TABLE public.risk_config (
    id integer NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    "values" json NOT NULL,
    circuit_breaker_active boolean NOT NULL,
    circuit_breaker_reason text,
    circuit_breaker_tripped_at timestamp with time zone
);

CREATE TABLE public.risk_events (
    id character varying(32) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    event_type character varying(32) NOT NULL,
    signal_id character varying(32),
    decision character varying(16),
    reasons json NOT NULL,
    approved_quantity numeric(38,18),
    approved_notional numeric(38,18),
    stop_loss numeric(38,18),
    take_profit numeric(38,18),
    snapshot json NOT NULL
);

CREATE TABLE public.signals (
    id character varying(32) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    exchange character varying(32) NOT NULL,
    symbol character varying(32) NOT NULL,
    timeframe character varying(8) NOT NULL,
    strategy character varying(64) NOT NULL,
    direction character varying(8) NOT NULL,
    confidence numeric(5,4) NOT NULL,
    reason text NOT NULL,
    reference_price numeric(38,18) NOT NULL,
    indicators json NOT NULL
);

CREATE TABLE public.trades (
    id character varying(32) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    executed_at timestamp with time zone NOT NULL,
    exchange character varying(32) NOT NULL,
    symbol character varying(32) NOT NULL,
    side character varying(8) NOT NULL,
    quantity numeric(38,18) NOT NULL,
    price numeric(38,18) NOT NULL,
    notional numeric(38,18) NOT NULL,
    fee numeric(38,18) NOT NULL,
    fee_currency character varying(16),
    origin character varying(16) NOT NULL,
    order_id character varying(32),
    signal_id character varying(32),
    strategy character varying(64),
    mode character varying(16) NOT NULL,
    realized_pnl numeric(38,18),
    notes text
);

CREATE TABLE public.trading_config (
    id integer NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    "values" json NOT NULL
);

ALTER TABLE ONLY public.agent_runs ALTER COLUMN id SET DEFAULT nextval('public.agent_runs_id_seq'::regclass);

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);

ALTER TABLE ONLY public.onchain_metrics ALTER COLUMN id SET DEFAULT nextval('public.onchain_metrics_id_seq'::regclass);

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.candles
    ADD CONSTRAINT candles_pkey PRIMARY KEY (exchange, symbol, timeframe, open_time);

ALTER TABLE ONLY public.notification_config
    ADD CONSTRAINT notification_config_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.onchain_metrics
    ADD CONSTRAINT onchain_metrics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.portfolio_snapshots
    ADD CONSTRAINT portfolio_snapshots_pkey PRIMARY KEY (id, "timestamp");

ALTER TABLE ONLY public.risk_config
    ADD CONSTRAINT risk_config_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.risk_events
    ADD CONSTRAINT risk_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.signals
    ADD CONSTRAINT signals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.trading_config
    ADD CONSTRAINT trading_config_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.onchain_metrics
    ADD CONSTRAINT uq_onchain_metric_day UNIQUE (metric, day);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT uq_order_client_id UNIQUE (client_order_id);

CREATE INDEX ix_agent_runs_agent_ts ON public.agent_runs USING btree (agent, "timestamp");

CREATE INDEX ix_audit_ts ON public.audit_log USING btree ("timestamp");

CREATE INDEX ix_onchain_metric_day ON public.onchain_metrics USING btree (metric, day);

CREATE INDEX ix_orders_created ON public.orders USING btree (created_at);

CREATE INDEX ix_risk_events_created ON public.risk_events USING btree (created_at);

CREATE INDEX ix_signals_created ON public.signals USING btree (created_at);

CREATE INDEX ix_snapshots_ts ON public.portfolio_snapshots USING btree ("timestamp");

CREATE INDEX ix_trades_executed ON public.trades USING btree (executed_at);

CREATE INDEX ix_trades_symbol ON public.trades USING btree (symbol);

CREATE TRIGGER audit_log_sem_truncate BEFORE TRUNCATE ON public.audit_log FOR EACH STATEMENT EXECUTE FUNCTION public.audit_log_append_only();

CREATE TRIGGER audit_log_sem_update_delete BEFORE DELETE OR UPDATE ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.audit_log_append_only();

CREATE TRIGGER risk_events_sem_truncate BEFORE TRUNCATE ON public.risk_events FOR EACH STATEMENT EXECUTE FUNCTION public.historico_append_only();

CREATE TRIGGER risk_events_sem_update_delete BEFORE DELETE OR UPDATE ON public.risk_events FOR EACH ROW EXECUTE FUNCTION public.historico_append_only();

CREATE TRIGGER trades_sem_delete_de_agente BEFORE DELETE ON public.trades FOR EACH ROW EXECUTE FUNCTION public.trades_delete_apenas_manual();

CREATE TRIGGER trades_sem_truncate BEFORE TRUNCATE ON public.trades FOR EACH STATEMENT EXECUTE FUNCTION public.trades_append_only();

CREATE TRIGGER trades_sem_update BEFORE UPDATE ON public.trades FOR EACH ROW EXECUTE FUNCTION public.trades_append_only();

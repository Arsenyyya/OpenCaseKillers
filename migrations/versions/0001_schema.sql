-- Immutable schema snapshot for migration 0001. Do not import current application models here.

CREATE TABLE principals (
	id VARCHAR(100) NOT NULL,
	token_hash VARCHAR(64),
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (token_hash)
);

CREATE TABLE products (
	id VARCHAR(60) NOT NULL,
	owner VARCHAR(100) NOT NULL,
	terms JSONB NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE provider_slots (
	id VARCHAR(60) NOT NULL,
	provider VARCHAR(30) NOT NULL,
	token VARCHAR(40),
	until TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX provider_capacity ON provider_slots (provider, until);

CREATE TABLE audit (
	event VARCHAR(60) NOT NULL,
	actor VARCHAR(100) NOT NULL,
	ref VARCHAR(180) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id)
);

CREATE TABLE cursors (
	position VARCHAR(200) NOT NULL,
	initialized_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id)
);

CREATE TABLE digests (
	period VARCHAR(50) NOT NULL,
	snapshot JSONB NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id),
	UNIQUE (product_id, period, content_hash)
);

CREATE TABLE grants (
	actor VARCHAR(100) NOT NULL,
	role VARCHAR(30) NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id),
	FOREIGN KEY(actor) REFERENCES principals (id),
	UNIQUE (product_id, actor, role)
);

CREATE TABLE groups (
	kind VARCHAR(30) NOT NULL,
	need VARCHAR(500) NOT NULL,
	context VARCHAR(500) NOT NULL,
	revision INTEGER NOT NULL,
	provisional BOOLEAN NOT NULL,
	active BOOLEAN NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	first_seen TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id)
);

CREATE INDEX groups_active ON groups (product_id, active, kind);

CREATE TABLE metric_events (
	name VARCHAR(60) NOT NULL,
	value FLOAT NOT NULL,
	channel VARCHAR(30) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id)
);

CREATE TABLE product_leases (
	token VARCHAR(40),
	until TIMESTAMP WITH TIME ZONE,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id)
);

CREATE TABLE receipts (
	correlation_key VARCHAR(250) NOT NULL,
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id),
	UNIQUE (correlation_key)
);

CREATE TABLE tickets (
	source_id VARCHAR(60) NOT NULL,
	external_id VARCHAR(100) NOT NULL,
	source_seq BIGINT NOT NULL,
	source_version VARCHAR(100) NOT NULL,
	current_revision INTEGER NOT NULL,
	origin_id VARCHAR(180) NOT NULL,
	duplicate_of VARCHAR(180),
	deleted BOOLEAN NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	channel VARCHAR(30) NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id),
	FOREIGN KEY(product_id, duplicate_of) REFERENCES tickets (product_id, id),
	UNIQUE (product_id, source_id, external_id)
);

CREATE INDEX ticket_changes ON tickets (product_id, updated_at);

CREATE TABLE cards (
	group_id VARCHAR(180) NOT NULL,
	revision INTEGER NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	snapshot JSONB NOT NULL,
	status VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	viewed_at TIMESTAMP WITH TIME ZONE,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id, group_id) REFERENCES groups (product_id, id),
	UNIQUE (product_id, group_id)
);

CREATE TABLE memberships (
	group_id VARCHAR(180) NOT NULL,
	origin_id VARCHAR(180) NOT NULL,
	first_seen TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id, group_id) REFERENCES groups (product_id, id),
	UNIQUE (product_id, group_id, origin_id)
);

CREATE INDEX memberships_window ON memberships (product_id, group_id, first_seen);

CREATE TABLE revisions (
	ticket_id VARCHAR(180) NOT NULL,
	number INTEGER NOT NULL,
	source_version VARCHAR(100) NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	raw JSONB,
	segments JSONB,
	status VARCHAR(30) NOT NULL,
	coverage JSONB NOT NULL,
	normalization_version VARCHAR(40) NOT NULL,
	masking_version VARCHAR(40) NOT NULL,
	model_id VARCHAR(60),
	prompt_version VARCHAR(60),
	schema_version VARCHAR(40) NOT NULL,
	model_calls INTEGER NOT NULL,
	usage JSONB NOT NULL,
	processed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id, ticket_id) REFERENCES tickets (product_id, id),
	UNIQUE (product_id, ticket_id, number)
);

CREATE TABLE decisions (
	card_id VARCHAR(180) NOT NULL,
	revision INTEGER NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	actor VARCHAR(100) NOT NULL,
	decision VARCHAR(30) NOT NULL,
	reason VARCHAR(500) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id, card_id) REFERENCES cards (product_id, id),
	UNIQUE (product_id, card_id, revision)
);

CREATE TABLE jobs (
	revision_id VARCHAR(180),
	kind VARCHAR(30) NOT NULL,
	dedupe_key VARCHAR(220) NOT NULL,
	priority INTEGER NOT NULL,
	status VARCHAR(30) NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE NOT NULL,
	attempts INTEGER NOT NULL,
	claim_token VARCHAR(40),
	lease_until TIMESTAMP WITH TIME ZONE,
	heartbeat TIMESTAMP WITH TIME ZONE,
	error_code VARCHAR(60),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id) REFERENCES products (id),
	FOREIGN KEY(product_id, revision_id) REFERENCES revisions (product_id, id),
	UNIQUE (product_id, dedupe_key)
);

CREATE INDEX jobs_claim ON jobs (product_id, kind, status, priority, available_at);

CREATE TABLE media_cache (
	revision_id VARCHAR(180) NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	processor_version VARCHAR(100) NOT NULL,
	settings_hash VARCHAR(64) NOT NULL,
	segments JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id, revision_id) REFERENCES revisions (product_id, id),
	UNIQUE (product_id, revision_id, content_hash, processor_version, settings_hash)
);

CREATE TABLE needs (
	revision_id VARCHAR(180) NOT NULL,
	group_id VARCHAR(180) NOT NULL,
	ticket_id VARCHAR(180) NOT NULL,
	origin_id VARCHAR(180) NOT NULL,
	kind VARCHAR(30) NOT NULL,
	need VARCHAR(500) NOT NULL,
	context VARCHAR(500) NOT NULL,
	urgency VARCHAR(30) NOT NULL,
	evidence JSONB NOT NULL,
	eligible BOOLEAN NOT NULL,
	first_seen TIMESTAMP WITH TIME ZONE NOT NULL,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id, revision_id) REFERENCES revisions (product_id, id),
	FOREIGN KEY(product_id, group_id) REFERENCES groups (product_id, id),
	FOREIGN KEY(product_id, ticket_id) REFERENCES tickets (product_id, id),
	UNIQUE (product_id, revision_id, group_id)
);

CREATE TABLE outbox (
	card_id VARCHAR(180) NOT NULL,
	decision_id VARCHAR(180) NOT NULL,
	revision INTEGER NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	correlation_key VARCHAR(250) NOT NULL,
	status VARCHAR(40) NOT NULL,
	external_id VARCHAR(100),
	sent_at TIMESTAMP WITH TIME ZONE,
	product_id VARCHAR(60) NOT NULL,
	id VARCHAR(180) NOT NULL,
	PRIMARY KEY (product_id, id),
	FOREIGN KEY(product_id, card_id) REFERENCES cards (product_id, id),
	FOREIGN KEY(product_id, decision_id) REFERENCES decisions (product_id, id),
	UNIQUE (correlation_key)
);

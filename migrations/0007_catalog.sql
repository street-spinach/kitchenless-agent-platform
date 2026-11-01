-- Grocery catalog and restaurant menus (architecture item 13). Additive only.
-- No vector columns (architecture §1): the extension from infra/postgres/init.sh stays unused.
CREATE TABLE catalog_items (
  agent_id    text        NOT NULL REFERENCES agent_cards,
  sku         text        NOT NULL,
  name        text        NOT NULL,
  brand       text        NULL,
  ingredients text        NOT NULL DEFAULT '',
  allergens   text[]      NOT NULL DEFAULT '{}',
  diets       text[]      NOT NULL DEFAULT '{}',
  nutrition   jsonb       NOT NULL DEFAULT '{}',
  price_minor bigint      NOT NULL,
  currency    char(3)     NOT NULL,
  in_stock    boolean     NOT NULL DEFAULT true,
  search      tsvector    GENERATED ALWAYS AS (to_tsvector('simple',
                name || ' ' || coalesce(brand, '') || ' ' || ingredients)) STORED,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, sku)
);

CREATE INDEX ON catalog_items USING gin (allergens);
CREATE INDEX ON catalog_items USING gin (search);

-- The same columns and indexes, for restaurants.
CREATE TABLE menu_items (
  agent_id    text        NOT NULL REFERENCES agent_cards,
  sku         text        NOT NULL,
  name        text        NOT NULL,
  brand       text        NULL,
  ingredients text        NOT NULL DEFAULT '',
  allergens   text[]      NOT NULL DEFAULT '{}',
  diets       text[]      NOT NULL DEFAULT '{}',
  nutrition   jsonb       NOT NULL DEFAULT '{}',
  price_minor bigint      NOT NULL,
  currency    char(3)     NOT NULL,
  in_stock    boolean     NOT NULL DEFAULT true,
  search      tsvector    GENERATED ALWAYS AS (to_tsvector('simple',
                name || ' ' || coalesce(brand, '') || ' ' || ingredients)) STORED,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, sku)
);

CREATE INDEX ON menu_items USING gin (allergens);
CREATE INDEX ON menu_items USING gin (search);

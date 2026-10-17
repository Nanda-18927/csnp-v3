-- ====================================================================
-- CRIME SIGHT NOIR - DATABASE SCHEMA (SQLite)
-- Single source of truth: executed by backend/database.py at startup.
-- ====================================================================

CREATE TABLE IF NOT EXISTS police_stations (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ps_code  TEXT NOT NULL UNIQUE,
            name     TEXT NOT NULL,
            district TEXT NOT NULL,
            state    TEXT NOT NULL,
            lat      REAL NOT NULL,
            lng      REAL NOT NULL,
            sector   TEXT
        );
        CREATE TABLE IF NOT EXISTS officers (
            id    TEXT PRIMARY KEY,
            name  TEXT NOT NULL,
            squad TEXT,
            rank  TEXT
        );
        CREATE TABLE IF NOT EXISTS criminals (
            id       TEXT PRIMARY KEY,
            name     TEXT NOT NULL,
            age      INTEGER,
            address  TEXT,
            photo_id TEXT,
            alias    TEXT,
            -- Source dataset columns (South India criminal tracking export)
            gender    TEXT,
            city      TEXT,
            state     TEXT,
            photo_url TEXT,
            crime_type TEXT,
            status    TEXT,
            station   TEXT,
            officer   TEXT,
            case_ref  TEXT,
            lat       REAL,
            lng       REAL
        );
        -- Criminal relationship graph: two records linked when they share a
        -- handling officer, a station, a city or a modus operandi.
        CREATE TABLE IF NOT EXISTS criminal_links (
            a        TEXT NOT NULL,
            b        TEXT NOT NULL,
            kind     TEXT NOT NULL,
            strength INTEGER DEFAULT 1,
            label    TEXT,
            PRIMARY KEY (a, b, kind)
        );
        CREATE TABLE IF NOT EXISTS cases (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            fir_number         TEXT NOT NULL UNIQUE,
            station_id         INTEGER,
            officer_id         TEXT,
            criminal_id        TEXT,
            assign_id          TEXT,
            case_code          TEXT,
            status             TEXT NOT NULL,
            severity           TEXT NOT NULL,
            crime_type         TEXT NOT NULL,
            crime_count        INTEGER DEFAULT 1,
            completion_pct     INTEGER DEFAULT 0,
            handling           TEXT,
            investigation_type TEXT,
            arrest_date        TEXT,
            photo_resolution   TEXT,
            remarks            TEXT,
            transfers          INTEGER DEFAULT 0,
            experience_years   INTEGER DEFAULT 0,
            FOREIGN KEY (station_id)  REFERENCES police_stations(id),
            FOREIGN KEY (officer_id)  REFERENCES officers(id),
            FOREIGN KEY (criminal_id) REFERENCES criminals(id)
        );
        CREATE INDEX IF NOT EXISTS idx_cases_station  ON cases(station_id);
        CREATE INDEX IF NOT EXISTS idx_cases_criminal ON cases(criminal_id);
        CREATE INDEX IF NOT EXISTS idx_stations_state ON police_stations(state);
        CREATE INDEX IF NOT EXISTS idx_links_a ON criminal_links(a);
        CREATE INDEX IF NOT EXISTS idx_links_b ON criminal_links(b);
        CREATE TABLE IF NOT EXISTS users (
            username    TEXT PRIMARY KEY,
            password    TEXT NOT NULL,
            role        TEXT NOT NULL,
            display     TEXT NOT NULL,
            station_code TEXT,
            badge       TEXT,
            rank        TEXT,
            station_name TEXT,
            district    TEXT,
            state       TEXT,
            squad       TEXT,
            official_id TEXT,
            designation TEXT,
            department  TEXT,
            zone        TEXT,
            -- Contact routes for password reset. At least one is required.
            email       TEXT,
            phone       TEXT
        );
        -- indexes on users.email / users.phone are created by database.py
        -- after the column migration, because an existing table may not have
        -- those columns yet when this script first runs.

        -- Password reset tokens. Only the hash is stored, so a leaked database
        -- cannot be used to complete a reset. One row per request; `used_at`
        -- marks a token as spent so a link only ever works once.
        CREATE TABLE IF NOT EXISTS password_resets (
            username    TEXT NOT NULL,
            token_hash  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used_at     TEXT,
            PRIMARY KEY (username, token_hash)
        );

-- Safe to retry after a metadata-lock timeout; no existing rows are removed.
SET SESSION lock_wait_timeout = 10;
CREATE TABLE IF NOT EXISTS workspaces (
    guild_id BIGINT UNSIGNED PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    default_priority VARCHAR(32) NOT NULL DEFAULT 'growth',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @day23_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'projects'
       AND column_name = 'owner_guild_id') = 0,
    'ALTER TABLE projects ADD COLUMN owner_guild_id BIGINT UNSIGNED NULL',
    'SELECT 1'
);
PREPARE day23_statement FROM @day23_ddl;
EXECUTE day23_statement;
DEALLOCATE PREPARE day23_statement;

SET @day23_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 'projects'
       AND index_name = 'idx_projects_owner_status') = 0,
    'CREATE INDEX idx_projects_owner_status ON projects (owner_guild_id, status)',
    'SELECT 1'
);
PREPARE day23_statement FROM @day23_ddl;
EXECUTE day23_statement;
DEALLOCATE PREPARE day23_statement;

SET @day23_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 'meetings'
       AND index_name = 'idx_meetings_guild_status_updated') = 0,
    'CREATE INDEX idx_meetings_guild_status_updated ON meetings (guild_id, status, updated_at)',
    'SELECT 1'
);
PREPARE day23_statement FROM @day23_ddl;
EXECUTE day23_statement;
DEALLOCATE PREPARE day23_statement;

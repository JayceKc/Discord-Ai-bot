-- Day 25: safe to retry; historical meetings remain legacy.

SET SESSION lock_wait_timeout = 10;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'decision_owner_user_id') = 0,
    'ALTER TABLE meetings ADD COLUMN decision_owner_user_id BIGINT UNSIGNED NULL', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'decision_status') = 0,
    'ALTER TABLE meetings ADD COLUMN decision_status VARCHAR(32) NOT NULL DEFAULT ''legacy''', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'decision_version') = 0,
    'ALTER TABLE meetings ADD COLUMN decision_version BIGINT UNSIGNED NOT NULL DEFAULT 0', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'storage_version') = 0,
    'ALTER TABLE meetings ADD COLUMN storage_version BIGINT UNSIGNED NOT NULL DEFAULT 0', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'user_decision') = 0,
    'ALTER TABLE meetings ADD COLUMN user_decision JSON NULL', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'candidate_proposal') = 0,
    'ALTER TABLE meetings ADD COLUMN candidate_proposal JSON NULL', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'candidate_proposal_metrics') = 0,
    'ALTER TABLE meetings ADD COLUMN candidate_proposal_metrics JSON NULL', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'candidate_review') = 0,
    'ALTER TABLE meetings ADD COLUMN candidate_review JSON NULL', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND column_name = 'decision_messages') = 0,
    'ALTER TABLE meetings ADD COLUMN decision_messages JSON NULL', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

SET @day25_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE()
        AND table_name = 'meetings' AND index_name = 'idx_meetings_decision_status') = 0,
    'CREATE INDEX idx_meetings_decision_status ON meetings (decision_status, guild_id)', 'SELECT 1');
PREPARE day25_statement FROM @day25_ddl;
EXECUTE day25_statement;
DEALLOCATE PREPARE day25_statement;

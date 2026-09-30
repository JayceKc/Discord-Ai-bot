-- DAY 22：MySQL 8 初始 schema。資料庫帳號只需目標 database 的 CRUD 權限。
CREATE TABLE IF NOT EXISTS projects (
    id VARCHAR(32) PRIMARY KEY,
    category VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    budget BIGINT UNSIGNED NOT NULL,
    deadline DATE NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_projects_status (status),
    INDEX idx_projects_category (category),
    INDEX idx_projects_deadline (deadline)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS project_requirements (
    project_id VARCHAR(32) NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (project_id, position),
    CONSTRAINT fk_requirements_project FOREIGN KEY (project_id)
        REFERENCES projects (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS project_acceptance_criteria (
    project_id VARCHAR(32) NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (project_id, position),
    CONSTRAINT fk_acceptance_project FOREIGN KEY (project_id)
        REFERENCES projects (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS requirement_changes (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    description TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_at DATE NOT NULL,
    status VARCHAR(32) NOT NULL,
    INDEX idx_requirement_changes_project_date (project_id, requested_at),
    CONSTRAINT fk_requirement_changes_project FOREIGN KEY (project_id)
        REFERENCES projects (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guild_current_projects (
    guild_id BIGINT UNSIGNED PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_guild_current_project FOREIGN KEY (project_id)
        REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS meetings (
    id CHAR(36) PRIMARY KEY,
    guild_id BIGINT UNSIGNED NOT NULL,
    project_id VARCHAR(32) NOT NULL,
    requirement TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    current_step_index SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    meeting_context JSON NOT NULL,
    applied_requirement_change JSON NULL,
    proposal_draft JSON NULL,
    proposal_metrics JSON NULL,
    review_result JSON NULL,
    revision_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    revision_agent_name VARCHAR(100) NULL,
    revision_output JSON NULL,
    final_proposal JSON NULL,
    final_proposal_metrics JSON NULL,
    error TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_meetings_guild_updated (guild_id, updated_at),
    INDEX idx_meetings_project_status (project_id, status),
    CONSTRAINT fk_meetings_project FOREIGN KEY (project_id)
        REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS meeting_steps (
    meeting_id CHAR(36) NOT NULL,
    sequence_no SMALLINT UNSIGNED NOT NULL,
    agent_name VARCHAR(100) NOT NULL,
    step_order SMALLINT UNSIGNED NOT NULL,
    round_number SMALLINT UNSIGNED NOT NULL,
    status VARCHAR(32) NOT NULL,
    input_text MEDIUMTEXT NULL,
    output_data JSON NULL,
    input_characters INT UNSIGNED NULL,
    output_characters INT UNSIGNED NULL,
    prompt_tokens INT UNSIGNED NULL,
    completion_tokens INT UNSIGNED NULL,
    max_output_tokens INT UNSIGNED NULL,
    execution_time_seconds DECIMAL(12,3) NULL,
    error TEXT NULL,
    PRIMARY KEY (meeting_id, sequence_no),
    INDEX idx_meeting_steps_order (meeting_id, round_number, step_order),
    CONSTRAINT fk_meeting_steps_meeting FOREIGN KEY (meeting_id)
        REFERENCES meetings (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guild_latest_meetings (
    guild_id BIGINT UNSIGNED PRIMARY KEY,
    meeting_id CHAR(36) NOT NULL,
    CONSTRAINT fk_guild_latest_meeting FOREIGN KEY (meeting_id)
        REFERENCES meetings (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

#!/bin/sh
# Docker Desktop 的 port mapping 有時會讓主機端連線被 MySQL 視為 localhost。
# 建立同樣只有目標 database CRUD 權限的本機帳號；密碼不會寫入檔案或日誌。
set -eu

escaped_password=$(printf '%s' "$MYSQL_PASSWORD" | sed "s/'/''/g")

MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot <<SQL
CREATE USER IF NOT EXISTS 'ai_company_app'@'localhost' IDENTIFIED BY '${escaped_password}';
ALTER USER 'ai_company_app'@'localhost' IDENTIFIED BY '${escaped_password}';
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_company.* TO 'ai_company_app'@'localhost';
FLUSH PRIVILEGES;
SQL

-- =====================================================================
-- 智能睡眠监测后端 - v2.5：wechat_openid 加唯一索引
-- 用法（TiDB Cloud SQL 控制台 / DBeaver / mysql CLI 执行）：
--   mysql -h <tidb_host> -P 4000 \
--     -u <user> -p'<password>' test < v2_5_wechat_openid_unique.sql
-- =====================================================================

-- 给 wechat_openid 加唯一索引（幂等：IF NOT EXISTS 在 MySQL 不直接支持，先删再重建）
-- TiDB / MySQL 8 支持 DROP INDEX IF EXISTS
SET @idx_exists := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'users'
    AND INDEX_NAME = 'ix_users_wechat_openid'
);
SET @sql := IF(@idx_exists > 0,
  'DROP INDEX ix_users_wechat_openid ON users',
  'SELECT "index not found, skip" AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE UNIQUE INDEX ix_users_wechat_openid ON users(wechat_openid);

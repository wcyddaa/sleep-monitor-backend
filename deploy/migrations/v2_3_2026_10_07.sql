-- =====================================================================
-- 智能睡眠监测后端 - 增量迁移：v2.3 (2026-10-07)
--   1) devices 表加 cfg_json TEXT 列（端侧三开关配置）
--   2) 新增 sleep_frames 表（每 30s 一帧的端侧结构化数据）
--
-- 用法（任选其一）：
--   A. 让 ORM 自动建：删 sleep_frames 表后重启 app 即可（create_all 会建）
--   B. 手动跑（TiDB / MySQL 8 都兼容）：
--        mysql -h <host> -P 4000 -u <user> -p<pass> test < deploy/migrations/v2_3_2026_10_07.sql
-- =====================================================================

-- 1) devices 加 cfg_json
--    MySQL / TiDB 都不支持 IF NOT EXISTS for ADD COLUMN, 用 INFORMATION_SCHEMA 判断
SET @col_exists := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'devices'
    AND COLUMN_NAME = 'cfg_json'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE devices ADD COLUMN cfg_json TEXT NULL AFTER last_seen',
  'SELECT "cfg_json already exists" AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2) 新建 sleep_frames
CREATE TABLE IF NOT EXISTS sleep_frames (
  id              INT          NOT NULL AUTO_INCREMENT,
  device_id       INT          NOT NULL,
  window_start_ts BIGINT       NOT NULL,
  window_end_ts   BIGINT       NOT NULL,
  stage           INT          NOT NULL DEFAULT -1,
  heart_rate      INT          NOT NULL DEFAULT -1,
  resp_rate       INT          NOT NULL DEFAULT -1,
  motion          INT          NOT NULL DEFAULT -1,
  snore_seconds   INT          NOT NULL DEFAULT -1,
  flags           INT          NOT NULL DEFAULT 0,
  raw_json        TEXT         NULL,
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_frame_device_start (device_id, window_start_ts),
  KEY ix_frame_device_end (device_id, window_end_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- 3) 兼容旧 deploy/tidb_schema.sql: 把新增列追加到该文件底部，让新部署一次到位
--    （如果你的部署是全新库，删库重建 / drop table 后 create_all 更省事）

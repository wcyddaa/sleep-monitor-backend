-- =====================================================================
-- 智能睡眠监测后端 - TiDB(MySQL协议) 建表 / 升级 SQL
-- 目标库：DATABASE_URL 中的 test 库
--   mysql+asyncmy://<user>:***@<tidb_host>:4000/test
--
-- 说明：
--  1) 后端启动时会自动 Base.metadata.create_all 建表，以下 DDL 用于
--     「提前在云端数据库准备表结构」或「给已存在库补新增列」。
--  2) TiDB 新建库若未启用外键，可去掉 FOREIGN KEY 约束行（应用层自会处理归属/级联）。
-- =====================================================================

-- ---------------- users ----------------
CREATE TABLE IF NOT EXISTS users (
  id              INT          NOT NULL AUTO_INCREMENT,
  username        VARCHAR(64)  NOT NULL,
  email           VARCHAR(128) NULL,
  hashed_password VARCHAR(255) NOT NULL,
  full_name       VARCHAR(128) NULL,
  wechat_openid   VARCHAR(128) NULL,
  is_active       TINYINT(1)   NOT NULL DEFAULT 1,
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_username (username),
  UNIQUE KEY uq_users_email (email),
  UNIQUE KEY uq_users_wechat_openid (wechat_openid),
  KEY ix_users_email (email),
  KEY ix_users_wechat_openid (wechat_openid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- ---------------- devices ----------------
CREATE TABLE IF NOT EXISTS devices (
  id          INT          NOT NULL AUTO_INCREMENT,
  device_id   VARCHAR(64)  NOT NULL,
  name        VARCHAR(128) NOT NULL,
  device_type VARCHAR(32)  NOT NULL DEFAULT 'sleep_monitor',
  owner_id    INT          NULL,   -- 为空=待归属：设备已上云/上报，尚未被用户认领
  is_online   TINYINT(1)   NOT NULL DEFAULT 0,
  last_seen   DATETIME(6)  NULL,
  created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_devices_device_id (device_id),
  KEY ix_devices_owner_id (owner_id),
  CONSTRAINT fk_devices_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- ---------------- sleep_records ----------------
CREATE TABLE IF NOT EXISTS sleep_records (
  id                     INT         NOT NULL AUTO_INCREMENT,
  user_id                INT         NULL,   -- 为空=来自待归属设备的原始记录（先落库不丢）
  device_id              INT         NOT NULL,
  start_time             DATETIME(6) NOT NULL,
  end_time               DATETIME(6) NOT NULL,
  raw_data               JSON        NOT NULL,
  analysis               JSON        NOT NULL,
  report                 TEXT        NULL,
  sleep_quality_score    FLOAT       NULL,
  avg_heart_rate         FLOAT       NULL,
  avg_respiratory_rate   FLOAT       NULL,
  apnea_risk             VARCHAR(16) NULL,
  created_at             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_sleep_user_start (user_id, start_time),
  KEY ix_sleep_device_end (device_id, end_time),
  CONSTRAINT fk_sleep_user   FOREIGN KEY (user_id)   REFERENCES users(id)   ON DELETE CASCADE,
  CONSTRAINT fk_sleep_device FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- ---------------- device_commands (新增：设备控制指令队列) ----------------
CREATE TABLE IF NOT EXISTS device_commands (
  id         INT         NOT NULL AUTO_INCREMENT,
  device_id  INT         NOT NULL,
  command    VARCHAR(64) NOT NULL,
  value      JSON        NULL,
  status     VARCHAR(16) NOT NULL DEFAULT 'pending',   -- pending | done | failed
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_cmd_device_status (device_id, status),
  CONSTRAINT fk_cmd_device FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- =====================================================================
-- 已存在的库只需执行下面的增量迁移（新库可跳过）
-- =====================================================================
-- 为存量 users 表补充 wechat_openid 列（微信一键登录用）
-- ALTER TABLE users ADD COLUMN wechat_openid VARCHAR(128) NULL AFTER full_name;
-- ALTER TABLE users ADD UNIQUE KEY uq_users_wechat_openid (wechat_openid);
-- 设备待归属：owner_id 允许为空（webhook 自动注册用）
-- ALTER TABLE devices MODIFY COLUMN owner_id INT NULL;
-- 待归属设备的原始记录：user_id 允许为空（先落库不丢）
-- ALTER TABLE sleep_records MODIFY COLUMN user_id INT NULL;
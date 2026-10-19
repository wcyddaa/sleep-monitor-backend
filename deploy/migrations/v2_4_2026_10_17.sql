-- =====================================================================
-- 智能睡眠监测后端 - 增量迁移：v2.4 (2026-10-17)
--   用户资料扩展 + 消息系统 + 每日报告摘要（长期趋势用）
--
--   1) users 加 nickname / avatar_url / wx_unionid
--   2) 新建 user_messages（用户通知：设备离线 / 重度鼾声 / 呼吸暂停 / 报告完成）
--   3) 新建 daily_report_summaries（每日报告智能体的简化 JSON，给趋势用）
--
-- 用法（任选其一）：
--   A. 让 ORM 自动建：删除目标表后重启 app 即可（create_all 会建）
--   B. 手动跑：
--        mysql -h <host> -P 4000 -u <user> -p<pass> test < deploy/migrations/v2_4_2026_10_17.sql
-- =====================================================================

-- 1) users 加 nickname / avatar_url / wx_unionid
SET @col_nick := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'nickname'
);
SET @sql := IF(@col_nick = 0,
  'ALTER TABLE users ADD COLUMN nickname VARCHAR(64) NULL AFTER full_name',
  'SELECT "nickname already exists" AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_avatar := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'avatar_url'
);
SET @sql := IF(@col_avatar = 0,
  'ALTER TABLE users ADD COLUMN avatar_url VARCHAR(512) NULL AFTER nickname',
  'SELECT "avatar_url already exists" AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_unionid := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'wx_unionid'
);
SET @sql := IF(@col_unionid = 0,
  'ALTER TABLE users ADD COLUMN wx_unionid VARCHAR(128) NULL UNIQUE AFTER wechat_openid',
  'SELECT "wx_unionid already exists" AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 历史数据兜底：把 full_name 复制到 nickname（NULL 时前端才有显示）
UPDATE users SET nickname = full_name WHERE nickname IS NULL AND full_name IS NOT NULL;
UPDATE users SET nickname = username WHERE nickname IS NULL;

-- 2) user_messages 表
CREATE TABLE IF NOT EXISTS user_messages (
  id              INT          NOT NULL AUTO_INCREMENT,
  user_id         INT          NOT NULL,
  -- 消息类型
  --   device_offline    : 设备连续 N 分钟无帧
  --   snore_heavy       : 单帧 snore_sec >= 25
  --   apnea_high        : 报告给出 apnea_risk=high
  --   report_ready      : 报告智能体跑完
  --   device_bound      : 绑定新设备
  type            VARCHAR(32)  NOT NULL,
  -- 严重度：info / warning / error
  severity        VARCHAR(16)  NOT NULL DEFAULT 'info',
  title           VARCHAR(128) NOT NULL,
  content         TEXT         NOT NULL,
  -- 关联资源（可选）
  ref_device_id   INT          NULL,
  ref_record_id   INT          NULL,
  ref_frame_id    INT          NULL,
  read_at         DATETIME(6)  NULL,
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_msg_user_created (user_id, created_at),
  KEY ix_msg_user_unread  (user_id, read_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- 3) daily_report_summaries 表（长期趋势智能体的输入）
--    一条记录 = 某用户在某 device 上某 [day_start, day_end) 24h 区间的报告摘要
CREATE TABLE IF NOT EXISTS daily_report_summaries (
  id              INT          NOT NULL AUTO_INCREMENT,
  user_id         INT          NOT NULL,
  device_id       INT          NOT NULL,
  -- 24h 区间的起止（秒，epoch）
  -- 锚点：每天 12:00 切分；[day_start_ts, day_start_ts + 86400) 是一天
  day_start_ts    BIGINT       NOT NULL,
  day_end_ts      BIGINT       NOT NULL,
  -- 区间内有多少 30s 帧
  frame_count     INT          NOT NULL DEFAULT 0,
  -- 缺失比例（1 - frame_count/2880），0 = 全程有数据，1 = 全程没数据
  data_completeness REAL       NOT NULL DEFAULT 0,
  -- 简化结构化摘要（daily_report_graph 的输出，JSON 字符串）
  -- 包含：sleep_duration_min / sleep_quality_score / deep_pct / light_pct / rem_pct / wake_pct
  --       / avg_heart_rate / avg_respiratory_rate / snore_minutes / apnea_risk / notes
  summary_json    TEXT         NULL,
  -- 自然语言一句话（给前端卡片展示）
  summary_text    VARCHAR(512) NULL,
  generated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_daily_user_device_window (user_id, device_id, day_start_ts),
  KEY ix_daily_user_generated (user_id, generated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

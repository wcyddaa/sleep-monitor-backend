-- =====================================================================
-- 智能睡眠监测后端 - 建管理员账号（需在 TiDB 控制台手动执行）
--
-- 用法：
--   mysql -h <tidb_host> -P 4000 -u <user> -p<password> <dbname> -e "$(cat seed_admin_user.sql)"
--   或粘贴到 TiDB Cloud / DBeaver / DataGrip 的 SQL 控制台执行
-- =====================================================================

INSERT INTO users (
    username,
    email,
    hashed_password,
    full_name,
    nickname,
    avatar_url,
    wechat_openid,
    wx_unionid,
    is_active,
    created_at
) VALUES (
    'admin',
    NULL,
    '$pbkdf2-sha256$29000$oRTCWAsBACAEoLRWKgWAcA$zbvJW9mTp8CFakjbJI6oXWPTM8MCMpbhhX0hZkZpW5M',
    '管理员',
    'admin',
    NULL,
    NULL,
    NULL,
    1,
    NOW(6)
)
ON DUPLICATE KEY UPDATE id = id;  -- 已存在则跳过，不报错

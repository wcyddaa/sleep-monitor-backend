# 提示词与 RAG 设计 v2

> 适用：`projects/sleep-monitor-backend`，云函数部署（OPENAI_API_KEY 真实环境变量）。
> 关联组件：`app/agents/llm.py` · `app/agents/report_graph.py` · `app/agents/data_graph.py` · `app/agents/chat_graph.py` · `app/rag/retriever.py`

---

## 1. 设计目标

1. **报告有据**：每段报告都能追溯到 analysis 字段或 RAG 上下文，不编造数据。
2. **历史可比**：自动拉近 30 天同评分区间的 5 条历史报告作对比，让用户感知趋势。
3. **风险兜底**：apnea_risk=high 强制建议线下就医；不打医学确诊。
4. **风格可控**：detailed / summary / pediatric 三档；pediatric 语气更柔和。
5. **真实 API**：不再用 Mock；key 缺失时仅启动期 warn（生产应 fail-fast 在云函数启动时）。

---

## 2. RAG 数据流

```
                ┌────────────────────────────────────────────────┐
                │ Cloud Function (sleep-monitor-backend)         │
report_graph:   │   retrieve (MySQL)                             │
  ┌─────────┐   │   - 近 30 天                                   │
  │ START   │──►│   - 同 user_id                                  │
  └────┬────┘   │   - 评分区间 ±0.1                              │
       │        │   - 鼾声分钟数 ≥ 当前                         │
       │        │   - exclude_id = 当前 record_id                │
       ▼        │   → top 5 → state.rag_context                  │
  retrieve      │                                                │
       │        └────────────────────────────────────────────────┘
       ▼
  build_prompt
       │  system  = SYSTEM_PROMPT_TEMPLATE (五段结构)
       │  human   = 用户信息 + rag_context + analysis JSON
       ▼
  generate (真实 LLM)
       │
       ▼
  polish (截断 4000 字)
       │
       ▼
     END
```

---

## 3. 关键提示词（节选）

```text
【结构】必须严格按以下五段输出，使用【】标记：
  【总评】1-2 句概括本晚睡眠质量评分（0-100）与所在区间（差/一般/良好/优秀）。
  【关键指标】列出本晚核心数值：心率均值/最低、呼吸均值、鼾声分钟数、
              深睡/浅睡/REM 占比、翻身次数。每条带单位；缺失的写「未提供」不要编。
  【对比历史】基于下方「历史相似案例」段，给出与近 30 天同区间的对比（更优/相当/变差）。
  【异常事件分析】只分析 analysis 中真实存在的字段，没有就写「无明显异常」。
  【个性化建议】3-5 条可执行建议；任何医学高风险情况必须建议线下就医。

【硬约束】
  1) 严禁捏造数据：所有数字必须来自 analysis 或 RAG 上下文；不存在的写「未提供」。
  2) 严禁下医学诊断：能用「可能」「倾向」「建议关注」就别用「确诊」「是病」。
  3) 如本晚 apnea_risk=high，必须在文末加一句「建议尽快线下就医做睡眠呼吸监测」。
  4) 文末不写「我是 AI」「本报告由模型生成」之类元信息。
```

---

## 4. 检索策略

| 调用方 | 检索器 | 字段 | 限制 |
|---|---|---|---|
| `report_graph.retrieve` | `retrieve_similar_reports` | score_band ±0.1 + snore_minutes ≥ 当前 | 30 天 / Top-5 |
| `chat_graph` (后续) | `retrieve_by_keyword` | `report LIKE %kw%` | 90 天 / Top-5 |
| `data_graph` (后续) | `retrieve_by_metrics` | apnea_risk + score_band | 30 天 / Top-5 |

所有检索都 `exclude_id=current`，避免报告引用自己。

---

## 5. 失败兜底

- 真实 LLM 失败 → 走 `_fallback_report`（无 RAG 上下文，标注「LLM 调用失败，已切换为兜底报告」）。
- MySQL 失败 → 报告无 RAG 段，其余正常生成。
- 真实 LLM 启动但 key 缺失 → 启动期 warn；用 `MockChatModel`（仅开发模式）。

---

## 6. 切换与扩展示例

**调整报告字数**：改 `STYLE_LABELS` 的提示词描述（如 `200-400 字`）。

**让报告引用更多历史**：把 `retrieve_similar_reports` 的 `limit=5` 改为 `limit=10`，并在 build_prompt 里加更多摘要槽位。

**接 embedding 做语义检索**：在 `app/rag/retriever.py` 加 `retrieve_by_embedding(query, top_k=5)`，用 `sentence-transformers` 计算余弦相似；mysql 存 embedding 列。

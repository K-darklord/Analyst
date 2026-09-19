# TradingAgents Fork — 现状分析与未来规划

> 本文档记录 K-darklord/TradingAgents fork 的当前状态、已知问题和未来规划。
> 改动明细见 [CHANGELOG.md](CHANGELOG.md)，文件清单与合并方案见 [FORK_CHANGES.md](FORK_CHANGES.md)。

---

## 一、当前状态总览

### 1.1 数据层覆盖矩阵

| 市场 | 市场数据 (OHLCV) | 财报 (三张表) | 公司信息 | 新闻 | 情绪 |
|---|---|---|---|---|---|
| **A 股** | tushare → akshare | tushare → akshare | tushare | tushare (新闻/研报) | 雪球 → 东方财富股吧 |
| **港股** | tushare(1/hr) → akshare(Sina) | **tushare** (500元/年权限) → yfinance | tushare hk_basic | tushare (A股视角) | 雪球 |
| **美股** | yfinance | yfinance | yfinance | yfinance | reddit → stocktwits |

**数据源链路（按优先级）**：

```
A_SHARE:  market_data=tushare(1)→akshare(2)   fundamentals=tushare(1)→akshare(2)
          news=tushare(1)                      sentiment=xueqiu(1)→eastmoney_guba(2)
          macro_data=tushare(1)

HK:       market_data=tushare(1)→akshare(2)→yfinance(3)
          fundamentals=tushare(1)→yfinance(2)
          news=tushare(1)   sentiment=xueqiu(1)   macro_data=tushare(1)

US:       market_data=yfinance(1)   fundamentals=yfinance(1)   news=yfinance(1)
          sentiment=reddit(1)→stocktwits(2)
```

### 1.2 Dashboard 功能

| 功能 | 状态 | 说明 |
|---|---|---|
| Bloomberg 深色主题 | ✅ | 关键/非关键信息分区，琥珀色边框强调关键区 |
| 持仓管理 | ✅ | 增删改 + JSON 持久化 (`~/.tradingagents/dashboard/portfolio.json`) |
| 自选股列表 | ✅ | 中文公司名显示，增删改 |
| Key Signals | ✅ | ENTRY/TARGET/STOP LOSS、R/R、基本面表、正负要点着色 |
| 字体调节 | ✅ | A-/A/A+，localStorage 持久化 |
| 数据变化高亮 | ✅ | 旧值删除线 + NEW 徽章 + 脉冲动画 |
| Run All 进度 | ✅ | SSE 实时进度面板 |
| PDF 导出 | ✅ | `/print?ticker=XXX`，A4 优化 |
| 反转预警 | ✅ | 每只股票检测 |
| 每日自动跑 | ✅ | cron 周一至周五 09:00 北京时间 |

### 1.3 已分析股票

| Ticker | 名称 | 分析状态 | 数据完整度 |
|---|---|---|---|
| NVDA | 英伟达 | ✅ 已分析 | 美股，yfinance 限流但可跑通 |
| GOOGL | 谷歌 | ✅ 已分析 | 同上 |
| TSLA | 特斯拉 | ✅ 已分析 | 同上 |
| 688520.SH | 神州细胞 | ✅ 已分析 | A股全量 |
| 688521.SH | 芯原股份 | ✅ 已分析 | A股全量 |
| 01810.HK | 小米集团 | ✅ 已分析 | 港股全量（含 tushare 财报） |
| 09988.HK | 阿里巴巴 | ✅ 已分析 | 港股全量 |
| 02228.HK | 晶泰科技 | ✅ 已分析 | 港股全量 |

---

## 二、已知问题

### 2.1 美股 yfinance 限流（中等影响）

- **现象**：美股 market_data / fundamentals / news 全部走 yfinance，Yahoo Finance 频繁返回 429
- **影响**：美股分析慢（每次重试 14s × 多次），偶尔失败
- **根因**：Yahoo Finance 对无 cookie 的请求限流，且无 tushare/akshare 兜底
- **可选方案**：
  - 开通 tushare 美股日线 (2000元/年) + 美股财报 (500元/年)
  - 接入 akshare 美股接口（`stock_us_fundamental` 等）
  - 使用 yfinance 的 cookie 认证模式

### 2.2 雪球适配器稳定性（低影响）

- **现象**：当前用 `undetected-chromedriver` + Chrome Profile，启动慢 (~15-20s)、依赖 Chrome 版本、profile 拷贝占磁盘
- **影响**：每次情绪分析启动开销大
- **可选方案**（按推荐度排序）：
  1. 持久浏览器会话（复用已启动的 Chrome）
  2. Cookie 自动刷新（解决 Keychain 弹窗问题）
  3. 雪球移动 API（可能 WAF 策略不同）
  4. 第三方雪球数据代理

### 2.3 港股新闻覆盖不足（低影响）

- **现象**：HK news 走 tushare，但 tushare 新闻主要是 A 股视角，港股个股新闻覆盖有限
- **影响**：港股新闻内容偏少，但雪球/股吧情绪数据可弥补
- **可选方案**：
  - 开通 tushare 新闻资讯独立权限 (1000元/年) — 含港股
  - 接入 akshare 港股新闻接口

### 2.4 港股 tushare hk_daily 1次/小时限制（已绕过）

- **现象**：tushare `hk_daily` 接口频次限制 1次/小时，与积分无关
- **应对**：fork_patches 中港股市场数据主路径走 akshare (新浪源)，不限流；tushare 仅在 registry 层作为候选

---

## 三、未来规划

### Phase 5 — 美股数据源优化（优先级：高）

- [ ] 评估 akshare 美股接口作为 yfinance 兜底
- [ ] 或开通 tushare 美股日线 + 财报权限
- [ ] 目标：美股分析不再因 yfinance 限流而变慢/失败

### Phase 6 — 雪球适配器优化（优先级：中）

- [ ] 实现持久浏览器会话，避免每次启动 Chrome
- [ ] 或探索雪球移动 API / 第三方代理
- [ ] 目标：情绪数据获取从 ~20s 降到 <5s

### Phase 7 — 港股新闻增强（优先级：低）

- [ ] 接入 akshare 港股新闻或开通 tushare 新闻资讯
- [ ] 目标：港股个股新闻覆盖度提升

### Phase 8 — 架构完善（优先级：中）

- [ ] 将 US sentiment (reddit/stocktwits) 包装为 registry adapter，统一架构
- [ ] 验证 diff_highlights.js 的 runContentDiff 在所有 analyst card 上正确触发
- [ ] 完善回测功能对接 A股/港股数据

### Phase 9 — 上游合并准备（优先级：中）

- [ ] 定期 fetch upstream main，检查是否有同路径文件冲突
- [ ] 验证 interface.py 的 FORK EXTENSION 块在 upstream 更新后仍能正确合并
- [ ] 保持 FORK_CHANGES.md 与实际代码同步

---

## 四、权限与成本现状

| 权限 | 状态 | 年成本 | 说明 |
|---|---|---|---|
| tushare 积分 | ✅ 10000 分 | 已购 | A股全量数据 |
| tushare 港股财报 | ✅ 已开通 | 500元 | hk_income/balancesheet/cashflow |
| tushare 新闻资讯 | ❌ 未开通 | 1000元 | 快讯/长篇新闻（含港股） |
| tushare 美股日线 | ❌ 未开通 | 2000元 | 美股日线+估值+复权 |
| tushare 美股财报 | ❌ 未开通 | 500元 | 美股三张表 |
| 雪球登录 | ✅ 已有 Chrome Profile | 0 | 通过 undetected-chromedriver |

---

---

## 六、未来架构：FundTeam 与 TradingAgents 分离

> **核心理念**：TradingAgents 专注个股深度分析（Analyst），保持上游纯净以便 merge；
> FundTeam 承担数据层、Strategist、Manager、Dashboard，通过信号合并做最终决策。

### 6.1 角色定位

| 角色 | 职责 | 决策风格 | 所属 |
|---|---|---|---|
| **Analyst** | 个股深度判断（财报/新闻/情绪/技术面） | 主观 + 信息驱动，偏短期 | TradingAgents |
| **Strategist** | 数据结构异常检测（RMT/相关矩阵/板块结构） | 纯数据驱动，偏中长期 | FundTeam |
| **Manager** | 结合两者信号做仓位管理 | 信号合并后执行 | FundTeam |

### 6.2 双信号共识机制

```
Analyst 信号 (主观/个股)         Strategist 信号 (数据/结构)
    │                                │
    ├─ 个股 rating (Buy/Hold/Sell)   ├─ 市场压力指数 (λ_max/MP)
    ├─ ENTRY/TARGET/STOP LOSS        ├─ 板块轮动方向
    ├─ 正负要点                      └─ 牛熊状态 (趋势/震荡/危机)
    │                                │
    └────────────┬───────────────────┘
                 ▼
        ┌──────────────────┐
        │   Manager 仲裁    │
        │                  │
        │  信号共识 → 执行  │
        │  信号冲突 → 标记  │
        └──────────────────┘
```

**执行原则**：Analyst 和 Strategist 信号**方向一致时才执行**；冲突时标记为 REVIEW，人工审核或降低仓位。

### 6.3 模块迁移规划

#### 留在 TradingAgents（纯 Analyst，保持上游可 merge）

- `agents/` — 全部分析 agent（market/sentiment/news/fundamentals）
- `graph/` — 多 agent 编排
- `prompts/` — 分析框架
- `dataflows/interface.py` — 数据工具抽象接口定义
- `dataflows/base_adapter.py` — 适配器抽象类（plugin contract）

#### 迁移到 FundTeam

| 模块 | 迁移动机 |
|---|---|
| `dashboard/` | 可视化属于 FundTeam 展示层 |
| `dataflows/adapters/` (tushare, akshare, xueqiu, guba) | 中国市场数据源，非通用能力 |
| `dataflows/registry.py`, `ticker_router.py`, `normalizer.py` | 数据源路由层 |
| `dataflows/fork_patches.py` | 数据源路由 hack，应在 FundTeam 层解决 |
| `config/data_sources.yaml` | 数据源配置 |

#### 灰色地带

- `dataflows/interface.py` 的 FORK EXTENSION 委托块：当前是改上游文件；
  未来应改为 **FundTeam 外部注入数据实现**，TradingAgents 只定义接口。
- `dataflows/base_adapter.py`：纯抽象类可留在 TradingAgents 作为 plugin contract。

### 6.4 理想依赖关系

```
FundTeam (主项目)
  ├── tradingagents  (pip install / submodule，不改源码)
  │     └── 暴露：agent graph + 数据接口定义 (BaseAdapter)
  ├── data_layer/    (tushare, akshare, xueqiu, guba 适配器，注入到 tradingagents)
  ├── strategist/    (RMT, 板块轮动, 牛熊识别 — 纯数据驱动)
  ├── manager/       (仓位管理，合并 analyst + strategist 信号)
  └── dashboard/     (可视化)
```

**关键机制**：TradingAgents 的 `route_to_vendor` 应支持**外部注入数据源**，
而非内部硬编码 vendor chain。FundTeam 注册中国数据源，TradingAgents 保持通用。

### 6.5 当前可做的准备

1. **减少对 `interface.py` 的修改** — 当前 registry 委托已是最小改动，符合方向
2. **dashboard 通过 API 调用 TradingAgents** — 不深入改 agent 内部
3. **adapter 插件化** — 现有 `@register_adapter` 已是插件式，未来只需把 adapter 文件移到 FundTeam
4. **减少 fork_patches 的 monkey-patch** — 长期应将 load_ohlcv 重构为通过 registry 分发

---

## 七、Strategist 技术方案：ORCA + omd_finance 组合

> **定位**：ORCA 做中期趋势方向预测，omd_finance 做极端风险结构预警，
> 两者组合形成"方向 + 尾部风险"双信号，作为 Analyst 个股信号的 regime override。

### 7.1 技术选型

| 组件 | 项目 | 作用 | 业绩/能力 |
|---|---|---|---|
| **趋势预测** | [ORCA](https://arxiv.org/html/2604.17251) | 10 天 horizon rally/crash 概率 → 5 体制 | Sharpe 1.13, CAGR 15.6%, MaxDD -7.5% |
| **尾部预警** | [omd_finance](https://github.com/ighalp/omd_finance) | 相关矩阵谱坍塌检测，危机前兆 | 2008 内生危机可提前预警 |

**为什么选这两个**：
- ORCA 是唯一有完整回测业绩的体制检测项目，且用 RMT + 谱图论特征
- omd_finance 专注"相关结构坍塌"这个危机前兆信号，与 ORCA 的趋势预测互补
- 两者结合可识别"背离"场景（趋势看涨但结构坍塌 = 顶部背离）

### 7.2 信号组合矩阵

| ORCA 趋势 | omd 结构 | 综合判断 | 仓位动作 |
|---|---|---|---|
| Rally | 稳定 | 健康上涨 | 顺势加仓 |
| Rally | 坍塌 | **假涨/顶部背离** | 减仓/止盈 |
| Crash | 稳定 | 正常回调 | 小幅减仓 |
| Crash | 坍塌 | **系统性危机** | 清仓/对冲 |
| Neutral | 稳定 | 震荡 | 听 Analyst |
| Neutral | 坍塌 | **结构断裂前兆** | 降低风险敞口 |

**核心价值**：两个"背离"场景——
- ORCA 涨 + omd 塌 = 危机前最后逃顶机会（2008 模式）
- ORCA 跌 + omd 稳 = 技术性回调，不必恐慌

### 7.3 Strategist 输出结构

```json
{
  "horizon": "1M",
  "trend": {
    "rally_prob": 0.72,
    "crash_prob": 0.08,
    "regime": "rally"
  },
  "tail_risk": {
    "spectral_collapse_score": 0.15,
    "state": "stable"
  },
  "combined_signal": "bullish",
  "sector_forecasts": {
    "semiconductors": {"direction": "up", "confidence": 0.65}
  }
}
```

### 7.4 实施注意事项

1. **时间尺度适配**：ORCA 原 horizon 为 10 天，Strategist 需要 1-3 个月。
   方案：延长预测 horizon 重新训练，或将 10 天信号做滚动聚合
   （连续 N 天 rally_prob > 0.6 → 中期趋势确立）
2. **omd_finance 封装**：原项目是研究框架，需将谱坍塌检测封装为可调用接口
3. **数据需求**：
   - ORCA：24 个 ETF 日收益（美股现成；A 股需对应宽基/行业 ETF）
   - omd：全市场截面（S&P 500 或沪深 300 成分股）
4. **A 股适配**：两项目均基于美股，A 股涨跌停限制会扭曲相关性，需重新校准阈值
5. **可复用工具库**：
   - `scikit-rmt` — RMT 实现（Marchenko-Pastur、MP-PCA 去噪）
   - `cpz-quant` — 协方差去噪、组合优化

## 五、技术债务

1. **fork_patches.py 的 monkey-patch** — 虽然有效但属于运行时 hack，长期应将 load_ohlcv 重构为通过 registry 分发
2. **akshare_backend.py 的本地 strip_suffix** — 与 ticker_router.py 的 strip_suffix 功能重复，应统一
3. **US sentiment 直连** — 未经 registry，架构不一致
4. **xueqiu_adapter 的 Chrome profile 拷贝** — 每次启动拷贝整个 profile 目录，磁盘开销大

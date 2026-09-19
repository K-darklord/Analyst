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

## 五、技术债务

1. **fork_patches.py 的 monkey-patch** — 虽然有效但属于运行时 hack，长期应将 load_ohlcv 重构为通过 registry 分发
2. **akshare_backend.py 的本地 strip_suffix** — 与 ticker_router.py 的 strip_suffix 功能重复，应统一
3. **US sentiment 直连** — 未经 registry，架构不一致
4. **xueqiu_adapter 的 Chrome profile 拷贝** — 每次启动拷贝整个 profile 目录，磁盘开销大

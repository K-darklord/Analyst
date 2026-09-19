"""Sentiment analyst — multi-source sentiment analysis for a target ticker.

Previously named ``social_media_analyst``. Renamed and redesigned because
the old version had a prompt that demanded social-media analysis but the
only tool available was Yahoo Finance news — which led LLMs to fabricate
Reddit/X/StockTwits content under prompt pressure (verified live).

The redesigned agent pre-fetches three complementary data sources before
the LLM is invoked and injects them into the prompt as structured blocks:

  1. News headlines     — Yahoo Finance (institutional framing)
  2. StockTwits messages — retail-trader posts indexed by cashtag, with
                           user-labeled Bullish/Bearish sentiment tags
  3. Reddit posts        — r/wallstreetbets, r/stocks, r/investing

Each source is trimmed to the analysis window. These text feeds serve recent
items and are not archived as of a past date, so sentiment inputs for a
historical run are not guaranteed to be point-in-time.

The agent does not use tool-calling; the data is in the prompt from
turn 0. Output uses the structured-output pattern (json_schema for
OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic), falling
back to free-text generation for providers that lack native support, so
the sentiment header (band + score + confidence) is deterministic across
runs and providers instead of free-form per-model prose.

See: https://github.com/TauricResearch/TradingAgents/issues/557
See: https://github.com/TauricResearch/TradingAgents/issues/796
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_news,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.reddit import fetch_reddit_posts
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages
from tradingagents.dataflows.ticker_router import is_ashare, is_hk


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def _fetch_ashare_capital_flow(ticker: str, start_date: str, end_date: str) -> str:
    """Fetch A-share capital-flow data as a sentiment proxy.

    A-share stocks have no Reddit/StockTwits coverage. We use tushare's
    moneyflow (资金流向), margin_detail (融资融券), and top_list (龙虎榜)
    as proxy sentiment signals:
      - Net money flow = retail/institutional buying pressure
      - Margin balance change = leveraged sentiment
      - Top list appearances = institutional/游资 activity

    Returns a markdown block for the sentiment prompt.
    """
    try:
        import os
        import tushare as ts
        import pandas as pd
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            return "<unavailable: TUSHARE_TOKEN not set>"
        pro = ts.pro_api(timeout=30)
        ts_start = start_date.replace("-", "")
        ts_end = end_date.replace("-", "")
    except Exception as e:
        return f"<unavailable: tushare init failed: {e}>"

    sections = []

    # 1. Money flow (资金流向) — daily net inflow/outflow
    try:
        mf = pro.moneyflow(ts_code=ticker, start_date=ts_start, end_date=ts_end)
        if mf is not None and not mf.empty and "net_mf_amount" in mf.columns:
            mf = mf.sort_values("trade_date")
            net_vals = pd.to_numeric(mf["net_mf_amount"], errors="coerce").dropna()
            # tushare net_mf_amount is in 千元; /10 -> 万元
            total_net = net_vals.sum() / 10
            avg_net = net_vals.mean() / 10 if len(net_vals) else 0
            pos_days = (net_vals > 0).sum()
            neg_days = (net_vals < 0).sum()
            sections.append(
                f"### 资金流向 (Money Flow)\n"
                f"- 区间净流入: {total_net:+.0f} 万元\n"
                f"- 日均净流入: {avg_net:+.0f} 万元\n"
                f"- 净流入天数: {pos_days} / 净流出天数: {neg_days}\n"
                f"- 近期明细 (单位: 千元):\n"
                + mf[["trade_date", "buy_sm_amount", "sell_sm_amount", "net_mf_amount"]]
                .tail(10)
                .to_string(index=False)
            )
    except Exception as e:
        sections.append(f"### 资金流向 (Money Flow)\n<unavailable: {e}>")

    # 2. Margin trading (融资融券) — leveraged position changes
    try:
        mg = pro.margin_detail(ts_code=ticker, start_date=ts_start, end_date=ts_end)
        if mg is not None and not mg.empty and "rzye" in mg.columns:
            mg = mg.sort_values("trade_date")
            rzye = pd.to_numeric(mg["rzye"], errors="coerce").dropna()  # 融资余额
            if len(rzye) >= 2:
                change = (rzye.iloc[-1] - rzye.iloc[0]) / 1e4
                sections.append(
                    f"### 融资融券 (Margin Trading)\n"
                    f"- 最新融资余额: {rzye.iloc[-1]/1e4:.0f} 万元\n"
                    f"- 区间融资余额变化: {change:+.0f} 万元\n"
                    f"- 融资余额上升 = 杠杆资金看多；下降 = 杠杆资金看空"
                )
    except Exception as e:
        sections.append(f"### 融资融券 (Margin Trading)\n<unavailable: {e}>")

    # 3. Top list (龙虎榜) — institutional/游资 activity
    try:
        # top_list needs trade_date, not ts_code+range. Scan recent trade dates.
        top_entries = []
        for d in pd.date_range(start_date, end_date, freq="B"):
            ds = d.strftime("%Y%m%d")
            try:
                tl = pro.top_list(trade_date=ds)
                if tl is not None and not tl.empty:
                    row = tl[tl["ts_code"] == ticker]
                    if not row.empty:
                        top_entries.append(row.iloc[0])
            except Exception:
                continue
        if top_entries:
            sections.append(
                f"### 龙虎榜 (Top List)\n"
                f"- 区间上榜次数: {len(top_entries)}\n"
                f"- 龙虎榜出现 = 机构/游资大幅交易，情绪信号强烈\n"
                + "\n".join(
                    f"  {e['trade_date']}: 买卖额 {e.get('amount', 'N/A')}"
                    for e in top_entries[:5]
                )
            )
    except Exception as e:
        sections.append(f"### 龙虎榜 (Top List)\n<unavailable: {e}>")

    if not sections:
        return "<no capital-flow data available>"
    return "\n\n".join(sections)


def _fetch_registry_sentiment(ticker: str, start_date: str, end_date: str) -> str:
    """Fetch sentiment via the DataSourceRegistry (xueqiu, eastmoney_guba).

    Walks the YAML-configured sentiment chain for the ticker's market.
    Returns the first successful source's output, or a sentinel if all
    sources fail (so the caller can fall back to the capital-flow proxy).
    """
    try:
        from tradingagents.dataflows.registry import get_registry
        from tradingagents.dataflows.ticker_router import route as route_market
        from tradingagents.dataflows.errors import NoMarketDataError

        reg = get_registry()
        reg.import_adapters()
        market = route_market(ticker)
        return reg.dispatch(
            "sentiment", market, capability="sentiment",
            symbol=ticker, start_date=start_date, end_date=end_date,
        )
    except Exception as e:
        return f"<unavailable: sentiment registry fetch failed: {e}>"


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it).
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)

        # Pre-fetch all three sources. Each fetcher degrades gracefully and
        # returns a string (no exceptions surface from here), so the LLM
        # always sees something — either real data or a clear placeholder.
        news_block = get_news.func(ticker, start_date, end_date)

        # A-share / HK stocks have no Reddit/StockTwits coverage. Try the
        # registry sentiment chain (xueqiu, eastmoney_guba) first; fall back
        # to a tushare capital-flow proxy (资金流/融资融券/龙虎榜) if both fail.
        is_ashare_or_hk = is_ashare(ticker) or is_hk(ticker)
        if is_ashare_or_hk:
            social_block = _fetch_registry_sentiment(ticker, start_date, end_date)
            capital_flow_block = _fetch_ashare_capital_flow(ticker, start_date, end_date)
            stocktwits_block = "<unavailable: StockTwits does not cover A-share/HK tickers>"
            reddit_block = "<unavailable: Reddit does not cover A-share/HK tickers>"
        else:
            capital_flow_block = ""
            # Pass the analysis window so a historical run trims social posts to it
            # instead of leaking today's chatter into a backtest (#1220).
            stocktwits_block = fetch_stocktwits_messages(
                ticker, limit=30, start_date=start_date, end_date=end_date
            )
            reddit_block = fetch_reddit_posts(ticker, start_date=start_date, end_date=end_date)

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            stocktwits_block=stocktwits_block,
            reddit_block=reddit_block,
            capital_flow_block=capital_flow_block,
            social_block=social_block if is_ashare_or_hk else "",
            is_ashare_or_hk=is_ashare_or_hk,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Report what your tools support; another agent decides the trade."
                    # No tool-calling here: the data is pre-fetched into the
                    # prompt, so tool-range wording would only invite a
                    # hallucinated tool call (#1130).
                    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}"
                    " " + NO_EXTERNAL_TOOLS +
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
    capital_flow_block: str = "",
    social_block: str = "",
    is_ashare_or_hk: bool = False,
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    if is_ashare_or_hk:
        return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} (A-share/HK stock) covering the period from {start_date} to {end_date}.

Sentiment is derived from three complementary sources: news flow, social discussion (雪球/东方财富股吧), and capital-flow data (资金流向/融资融券/龙虎榜) as a proxy for retail/institutional sentiment.

## Data sources (pre-fetched, in this prompt)

### News headlines — company announcements + research reports + market news
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### Social discussion — 雪球 (Xueqiu) / 东方财富股吧 (Eastmoney Guba)
Retail-investor posts. Titles and bodies reflect retail sentiment directly. If unavailable, the capital-flow proxy below becomes the primary signal.

<start_of_social>
{social_block}
<end_of_social>

### Capital-flow sentiment proxy — tushare moneyflow + margin_detail + top_list
Fast-moving signal. Net money flow reflects retail/institutional buying pressure; margin balance changes reflect leveraged sentiment; top-list (龙虎榜) appearances indicate large institutional/游资 trades.

<start_of_capital_flow>
{capital_flow_block}
<end_of_capital_flow>

## How to analyze this data (best practices)

1. **Read the net money-flow direction as the primary sentiment signal.** Sustained net inflow = bullish pressure; sustained net outflow = bearish pressure. The inflow/outflow day ratio indicates consistency.

2. **Margin balance change is a leveraged-sentiment confirm.** Rising 融资余额 (margin balance) = leveraged buyers adding; falling = leveraged buyers reducing. Divergence between money flow and margin direction is itself a signal.

3. **Social post sentiment.** Read 雪球/股吧 post titles and bodies for bullish/bearish framing. Recurring bullish or bearish themes across posts indicate retail consensus.

4. **Top-list (龙虎榜) appearances are high-conviction signals.** A stock appearing on the 龙虎榜 means large players (institutions/游资) traded it heavily.

5. **Cross-source divergence matters.** If news flow is negative but money flow is strongly positive, institutional buying may be front-running a turnaround (or retail chasing while institutions distribute).

6. **Identify recurring narrative themes** in the news and posts — earnings, policy changes, sector rotation, M&A, etc.

7. **Be honest about data limits.** If social posts are unavailable and capital-flow data is thin, flag lower confidence explicitly.

8. **Identify catalysts and risks** — upcoming earnings, policy events, sector trends, lock-up expiries, etc.

9. **Past sentiment is not predictive.** Frame conclusions as signal for the trader to weigh, not a price call.

## Output fields

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish.
- **overall_score**: 0 (max bearish) to 10 (max bullish); 5 = neutral.
- **confidence**: low / medium / high.
- **narrative**: Source-by-source breakdown, divergences, dominant narrative, catalysts/risks, and a markdown summary table of key sentiment signals.

{get_language_instruction()}"""

    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on three complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — r/wallstreetbets, r/stocks, r/investing (past 7 days)
Community discussion, without vote or comment counts. Subreddit character matters (r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term).

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Read Reddit posts for substance.** The feed carries no vote or comment counts, so judge a post by its body excerpt, not its title alone, and do not infer engagement.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

7. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)

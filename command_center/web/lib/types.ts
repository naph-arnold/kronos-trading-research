/* Shapes returned by command_center/services.py (subset used by the UI). */

export interface Quote { time: string; bid: number; ask: number; age_s: number; kind: string }

export interface MarketStatus {
  market: string; label: string; venue: string; available: boolean; runner: string;
  last_heartbeat?: string | null; last_heartbeat_age_s?: number | null; last_quote?: Quote | null;
  last_bar_open?: string | null; connection?: string | null; market_open?: boolean | null; data?: string | null;
  quotes?: string | null; new_entries?: string | null; calendar?: string | null; entry_block_reasons?: string[];
  last_market_data_at?: string | null; last_successful_poll_at?: string | null; connection_failures?: number | null;
  websocket_reconnects?: number | null; last_error?: string | null;
  demo_account?: { checked_at: string; balance: number; equity: number; positions: number; orders: number; kind: string };
  controls?: Controls;
}

export interface Controls { kill_file: boolean; stop_file: boolean; breaker_trips: unknown[] }

export interface StrategyMetrics {
  trades: number; wins: number; losses: number; win_rate_pct: number | null; profit_factor: number | null;
  net_pnl: number; net_return_pct: number; max_drawdown_pct: number; approved: number; rejected: number;
  rejection_reasons: Record<string, number>; mean_r: number | null;
  costs: { spread_and_slippage: number; commission: number; swap: number };
  pnl: { today: number; cumulative: number; equity: number; marked: boolean };
}

export interface BookRow {
  book_id: string; strategy_key: string; initial_equity: number; commission_per_lot_round_trip: number;
  trades: number; net_pnl: number; net_return_pct: number; max_drawdown_pct: number; win_rate_pct: number | null;
  profit_factor: number | null; approved: number; rejected: number; primary: boolean;
  pnl: { today: number; cumulative: number; equity: number; marked: boolean };
}

export interface Rejection { book: string; signal_id: string; at: string; reasons: string[]; categories: string[] }

export interface StrategyPanel {
  id: string; name: string; market: string; strategy_key: string; lifecycle: string; lifecycle_detail: string;
  launch_approved?: boolean; last_heartbeat?: string | null; last_activity?: string | null;
  signals?: { total: number; forward: number; by_status: Record<string, number> } | null;
  primary_book?: string | null; metrics: StrategyMetrics | null; books?: BookRow[]; rejections?: Rejection[];
  open_positions?: Record<string, unknown>; controls?: Controls;
  risk_limits?: Record<string, string> | null;
}

export interface RiskStatus {
  state: string; limits: { risk_per_trade_pct: number | null; daily_loss_halt_pct: number | null; aggregate_open_risk_pct: number | null };
  controls: Controls; max_open_risk_pct_of_equity: number; halted_accounts_today: string[];
  daily: { account: string; day: string; halted: boolean; start_equity: number; last_equity: number }[];
}

export interface Health {
  uptime?: { runner_uptime_pct: number; heartbeats: number; gaps_over_3_min: { from: string; to: string; minutes: number }[];
    last_heartbeat: string | null; fresh_data_pct_while_open: number | null; heartbeats_disconnected: number };
  disconnections?: Record<string, number>; data_gaps?: Record<string, unknown>;
  signals?: { in_window_by_status: Record<string, number>; total_by_status: Record<string, number> };
  decisions?: { in_window: number; approved: number; rejected: number; rejection_reasons: Record<string, number> };
  trades?: { closed_in_window: number; closed_total: number; open_positions: number };
  integrity?: { preregistration_consistent: boolean; live_bars: number; unseen_window_starts: string };
}

export interface Account { market: string; book: string; strategy: string; cost_scenario: string; trades: number;
  realized: number; equity: number; return_pct: number }

export interface Overview {
  generated_at: string; read_only: boolean; orders_possible: boolean;
  markets: Record<string, MarketStatus>; health: Record<string, Health>; risk: Record<string, RiskStatus>;
  alerts: { market: string; text: string }[];
  strategies: StrategyPanel[]; accounts: Account[];
}

export interface Candle { time: number; open: number; high: number; low: number; close: number; volume: number | null; source: string }

export interface ChartSignal { time: number; bar_close: string; signal_id: string; strategy: string; control: boolean;
  status: string; side: string; entry: number | null; stop: number | null; take_profit: number | null;
  decisions: { approved: number; rejected: number } }

export interface ChartTrade { book: string; strategy: string; signal_id: string; side: string; entry_time: number;
  entry: number | null; exit_time: number; exit: number | null; stop: number | null; take_profit: number | null;
  pnl: number | null; r: number | null; exit_reason: string | null }

export interface ChartData {
  timeframe: string; candles: Candle[]; has_volume: boolean; volume_label?: string; last_candle_partial: boolean;
  segments: { kind: string; file: string; base_timeframe: string; from: string; to: string; aggregated: boolean }[];
  notes: string[];
  indicators: { ema20: { time: number; value: number }[]; ema50: { time: number; value: number }[]; trend?: string };
  structure: { time: number; kind: string; price: number; label: string }[];
  signals: ChartSignal[]; trades: ChartTrade[];
  positions: { book: string; strategy: string; side: string; entry: number | null; entry_time: number; stop: number | null; take_profit: number | null }[];
  opening_ranges: { signal_id: string; from: number; to: number; low: number; high: number; session: string }[];
  outliers?: { time: number; low: number; high: number; wick: number; median_range: number }[];
}

export interface SignalRow { signal_id: string; strategy: string; control: boolean; status: string; bar_close: string;
  side: string; entry: number | null; stop: number | null; take_profit: number | null;
  context: Record<string, unknown> | null; params_hash: string | null;
  decisions: { book: string; approved: boolean; reasons: string[]; at: string; size: string | null; planned_risk: string | null }[];
  trades: { book: string; pnl: number | null; r: number | null; exit_reason: string | null; closed_at: string }[] }

export interface GrokRoom {
  status: string; activated: boolean; env_activation_flag: boolean; xai_key_present: boolean; model: string | null;
  provider: string; daily_usd_budget: number | null; daily_call_budget: number | null;
  pricing: { input: number; output: number; source?: string } | null;
  trigger: { mode: string; max_signal_age_hours: number; max_reviews_per_run: number; note?: string } | null;
  roles: { role: string; brief: string }[]; advisory_only: boolean;
  stores: { dir: string; calls_by_transport: Record<string, number>; usd_today: number | null;
    summaries: { subject: string; at: string; runner: string; recommendation: string; body: Record<string, unknown> }[];
    outputs: { subject: string; role: string; at: string; valid: boolean; error: string | null }[];
    audit: { ok: boolean; records?: number; problems: string[] } | null }[];
}

/* ---- History, equity and signal detail (V1.1) ---- */

export interface HistoryPage<T> { runner: string; label: string; kind: string; available: boolean; total: number;
  offset?: number; rows: T[]; note?: string }

export interface HSignal { at: string; signal_id: string; strategy: string; status: string; side: string;
  entry: number | null; stop: number | null; take_profit: number | null; approved: number; rejected: number; traded: boolean }
export interface HDecision { at: string; book: string; strategy: string | null; signal_id: string; approved: boolean;
  size: number | null; planned_risk: number | null; reasons: string[]; categories: string[] }
export interface HTrade { at: string; opened_at: string | null; book: string; strategy: string | null; signal_id: string;
  side: string | null; size: number | null; entry: number | null; exit: number | null; stop: number | null;
  take_profit: number | null; gross_pnl: number | null; costs: { spread_slippage_cost: number | null; commission: number | null; swap: number | null };
  pnl: number | null; r: number | null; mfe_r: number | null; mae_r: number | null; exit_reason: string | null;
  planned_risk: number | null; loss_beyond_planned: number | null }
export interface HEvent { at: string; event: string; class: string; detail: string }

export interface EquityBook { book: string; strategy: string; initial_equity: number; commission: number; points: number;
  equity: { time: number; value: number }[]; drawdown_pct: { time: number; value: number }[]; last: number; max_drawdown_pct: number }
export interface EquityData { runner: string; available: boolean; books: EquityBook[]; note?: string }

export interface SignalDetail {
  runner: string; label: string; signal_id: string; strategy: string; status: string; bar_close: string; side: string;
  entry: number | null; stop: number | null; take_profit: number | null; generated_at: string | null;
  strategy_version: string | null; interval: string; context: Record<string, unknown> | null; params_hash: string | null;
  dataset_id: string | null;
  decisions: { book: string; approved: boolean; at: string; size: number | null; planned_risk: number | null;
    reasons: string[]; categories: string[]; equity_at_decision: number | null; initial_equity: number | null }[];
  trades: { book: string; side: string | null; size: number | null; opened_at: string | null; closed_at: string | null;
    entry: number | null; exit: number | null; gross_pnl: number | null; spread_slippage_cost: number | null;
    commission: number | null; swap: number | null; pnl: number | null; r: number | null; mfe_r: number | null;
    mae_r: number | null; exit_reason: string | null }[];
  open_positions: { book: string; entry: number | null; stop: number | null; take_profit: number | null; opened_at: string | null }[];
  chart: ChartData; grok_reviews: { subject: string; at: string; runner: string; recommendation: string; body: Record<string, unknown> }[];
  evidence_note: string | null;
}

export interface Meta { runners: Record<string, { label: string; market: string }>;
  strategies: { id: string; name: string; market: string; strategy_key: string }[] }

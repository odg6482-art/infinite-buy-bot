# Google Colab에서 처음 실행할 때는 별도 셀에서 먼저 실행하세요:
# !pip install -q python-telegram-bot==22.1 nest_asyncio yfinance matplotlib

import json
import math
import os
import sqlite3
import datetime
import matplotlib.pyplot as plt
import io
import shutil
from dataclasses import dataclass, asdict, field
from typing import List, Optional

# !pip install python-telegram-bot nest_asyncio yfinance

import nest_asyncio
nest_asyncio.apply()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yfinance as yf

BOT_TOKEN = os.getenv("BOT_TOKEN")

VERSION = "4.4"
SUPPORTED_SYMBOLS = ("TQQQ", "SOXL")

# =========================
# 환율 캐시 (버그8: 중복 API 호출 방지)
# 프로세스 내에서 5분간 캐싱
# =========================
_rate_cache: dict = {"value": 1350.0, "ts": 0.0}
_RATE_TTL = 300  # 5분

def get_usd_krw_rate() -> float:
    import time
    now = time.time()
    if now - _rate_cache["ts"] < _RATE_TTL:
        return _rate_cache["value"]
    try:
        ticker = yf.Ticker("USDKRW=X")
        todays_data = ticker.history(period='1d')
        if not todays_data.empty:
            rate = float(todays_data['Close'].iloc[-1])
            _rate_cache["value"] = rate
            _rate_cache["ts"] = now
            return rate
    except Exception:
        pass
    return _rate_cache["value"]


# =========================
# DB 설정
# =========================
DB_FILE = "trading_history.db"

def db_connect():
    """SQLite 연결 안정화: 코랩/로컬에서 DB lock을 줄이기 위해 timeout과 WAL을 사용."""
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn

def init_db():
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS trades
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                       timestamp TEXT, symbol TEXT, type TEXT,
                       price REAL, qty REAL, amount REAL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS cycles
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                       timestamp TEXT, symbol TEXT, principal REAL,
                       ending_cash REAL, profit REAL, profit_percent REAL)''')
    conn.commit()
    conn.close()

def log_trade(symbol: str, trade_type: str, price: float, qty: float):
    conn = db_connect()
    cursor = conn.cursor()
    amount = price * qty
    cursor.execute(
        "INSERT INTO trades (timestamp, symbol, type, price, qty, amount) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.datetime.now().isoformat(), symbol, trade_type, price, qty, amount)
    )
    conn.commit()
    conn.close()

def log_cycle_result(symbol: str, principal: float, ending_cash: float):
    profit = ending_cash - principal
    profit_percent = (profit / principal * 100) if principal > 0 else 0.0
    conn = db_connect()
    conn.execute(
        "INSERT INTO cycles (timestamp, symbol, principal, ending_cash, profit, profit_percent) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.datetime.now().isoformat(), symbol, principal, ending_cash, profit, profit_percent)
    )
    conn.commit()
    conn.close()

def get_cycle_summary(symbol: str):
    conn = db_connect()
    rows = conn.execute(
        "SELECT profit, profit_percent FROM cycles WHERE symbol = ? ORDER BY id",
        (symbol,)
    ).fetchall()
    conn.close()
    return rows

init_db()

# 구버전에서 남아있을 수 있는 UPRO 상태파일 정리
for _old in ("state_UPRO.json", "state_UPRO.backup.json"):
    if os.path.exists(_old):
        try:
            os.remove(_old)
        except Exception:
            pass


# =========================
# 종목별 상태파일
# =========================
def get_state_file(symbol: str) -> str:
    return f"state_{symbol}.json"


# =========================
# 전략 상태 모델
# =========================
@dataclass
class StrategyState:
    symbol: str
    split_count: int
    principal: float
    mode: str = "IDLE"  # IDLE, GENERAL_FIRST_HALF, GENERAL_SECOND_HALF, REVERSE
    t_value: float = 0.0
    cash_remaining: float = 0.0
    quantity: float = 0.0
    avg_price: float = 0.0
    last_close: float = 0.0
    last5_closes: List[float] = field(default_factory=list)

    big_price: Optional[float] = None
    lower_bands: List[float] = field(default_factory=list)

    reverse_day_count: int = 0
    reverse_exit_ready: bool = False
    reverse_entry_date: Optional[str] = None  # 리버스 진입일 (첫날 판정용)

    active: bool = True

    # UI/UX 개선: 종목별 사이클 성과/이어가기 관리
    cycle_no: int = 1
    initial_principal: float = 0.0
    compound_mode: str = "선택안함"  # COMPOUND, SIMPLE, 선택안함
    total_realized_profit: float = 0.0
    last_cycle_profit: float = 0.0
    best_cycle_profit: float = 0.0
    cycle_start_date: Optional[str] = None

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(data: dict):
        data.setdefault("reverse_entry_date", None)
        data.setdefault("cycle_no", 1)
        data.setdefault("initial_principal", data.get("principal", 0.0))
        data.setdefault("compound_mode", "선택안함")
        data.setdefault("total_realized_profit", 0.0)
        data.setdefault("last_cycle_profit", 0.0)
        data.setdefault("best_cycle_profit", 0.0)
        data.setdefault("cycle_start_date", datetime.date.today().isoformat())
        return StrategyState(**data)


# =========================
# 상태 저장/불러오기
# =========================
def get_backup_state_file(symbol: str) -> str:
    return f"state_{symbol}.backup.json"

def save_state(state: StrategyState):
    """상태파일을 원자적으로 저장하고 직전 상태를 백업합니다."""
    path = get_state_file(state.symbol)
    backup_path = get_backup_state_file(state.symbol)
    tmp_path = f"{path}.tmp"

    if os.path.exists(path):
        try:
            shutil.copy2(path, backup_path)
        except Exception:
            pass

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _load_state_from_path(path: str) -> Optional[StrategyState]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    valid_keys = StrategyState.__dataclass_fields__.keys()
    data = {k: v for k, v in data.items() if k in valid_keys}
    return StrategyState.from_dict(data)

def load_state(symbol: str) -> Optional[StrategyState]:
    """항상 symbol을 명시해서 호출. 파일 손상 시 백업에서 복구합니다."""
    if symbol not in SUPPORTED_SYMBOLS:
        return None

    path = get_state_file(symbol)
    backup_path = get_backup_state_file(symbol)
    if not os.path.exists(path):
        return None

    try:
        return _load_state_from_path(path)
    except Exception:
        if os.path.exists(backup_path):
            try:
                state = _load_state_from_path(backup_path)
                if state:
                    save_state(state)
                return state
            except Exception:
                return None
        return None


def get_active_symbols() -> List[str]:
    """현재 상태파일이 존재하는 종목 목록 반환"""
    return [s for s in SUPPORTED_SYMBOLS if os.path.exists(get_state_file(s))]


def clear_state(symbol: str):
    for path in (get_state_file(symbol), get_backup_state_file(symbol)):
        if os.path.exists(path):
            os.remove(path)




def restore_previous_state(symbol: str) -> str:
    """직전 저장 상태로 복구합니다. 체결 입력 실수 등에 대한 되돌리기 장치."""
    if symbol not in SUPPORTED_SYMBOLS:
        return "지원하지 않는 종목입니다."

    path = get_state_file(symbol)
    backup_path = get_backup_state_file(symbol)

    if not os.path.exists(backup_path):
        return f"[{symbol}] 복구할 이전 상태가 없습니다."

    try:
        if os.path.exists(path):
            shutil.copy2(path, f"{path}.before_undo")
        shutil.copy2(backup_path, path)
        return f"↩️ [{symbol}] 직전 상태로 복구했습니다.\n\n현재 상태를 눌러 잔금/수량/T값을 확인하세요."
    except Exception as e:
        return f"[{symbol}] 복구 중 오류가 발생했습니다: {e}"

# =========================
# 공통 유틸
# =========================
def round2(x: float) -> float:
    return round(x + 1e-12, 2)


def format_mode(mode: str) -> str:
    mapping = {
        "IDLE": "대기",
        "GENERAL_FIRST_HALF": "일반모드 전반전",
        "GENERAL_SECOND_HALF": "일반모드 후반전",
        "REVERSE": "리버스모드",
    }
    return mapping.get(mode, mode)


def get_mode(split_count: int, t_value: float, quantity: float) -> str:
    if quantity <= 0:
        return "IDLE"
    if t_value > split_count - 1:
        return "REVERSE"
    if t_value < split_count / 2:
        return "GENERAL_FIRST_HALF"
    return "GENERAL_SECOND_HALF"


def weighted_avg_price(old_qty: float, old_avg: float, buy_qty: float, buy_price: float) -> float:
    total_qty = old_qty + buy_qty
    if total_qty <= 0:
        return 0.0
    return (old_qty * old_avg + buy_qty * buy_price) / total_qty


def format_usd(value: float) -> str:
    return f"${round2(value):,.2f}"

def format_krw(value: float) -> str:
    return f"₩{int(value):,}"

def format_usd_with_sign(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${round2(abs(value)):,.2f}"

def display_width(s: object) -> int:
    import unicodedata
    value = str(s)
    width = 0
    for ch in value:
        if unicodedata.east_asian_width(ch) in ("F", "W"):
            width += 2
        else:
            width += 1
    return width

def pad_text(value: object, width: int) -> str:
    value = str(value)
    return value + " " * max(0, width - display_width(value))

def compact_lines(rows, widths) -> str:
    lines = []
    for row in rows:
        line = "  ".join(pad_text(row[i], widths[i]) for i in range(len(row)))
        lines.append(line.rstrip())
    return "\n".join(lines)

def calc_cycle_days_used(state: StrategyState) -> int:
    try:
        start_date = datetime.date.fromisoformat(state.cycle_start_date) if state.cycle_start_date else datetime.date.today()
    except Exception:
        start_date = datetime.date.today()
    return max(1, (datetime.date.today() - start_date).days + 1)

def build_cycle_clear_text(state: StrategyState, total_profit: float, profit_percent: float) -> str:
    days_used = calc_cycle_days_used(state)
    summary_box = compact_lines(
        [
            ["📅 진행기간", f"{days_used}일"],
            ["💰 이번수확", f"{format_usd_with_sign(total_profit)} ({profit_percent:+.2f}%)"],
            ["🌱 누적수익", format_usd_with_sign(state.total_realized_profit)],
            ["🍀 다음시드머니", format_usd(state.cash_remaining)],
        ],
        [14, 22]
    )
    return (
        f"\n\n🎉✨ <b>[{state.symbol} 사이클 클리어]</b> ✨🎉\n"
        f"━━━━━━━━━━━━━━\n"
        f"🏆 <b>{state.cycle_no}회차 미션 완료!</b>\n\n"
        f"<pre>{summary_box}</pre>\n\n"
        f"🚀 <b>다음 사이클은 어떻게 할까?</b>"
    )

def calc_position_metrics(state: StrategyState, rate: float) -> dict:
    """현재 평가금액/미실현손익/총손익을 계산합니다."""
    market_price = state.last_close if state.last_close > 0 else state.avg_price
    position_value = state.quantity * market_price
    cost_basis = state.quantity * state.avg_price
    unrealized_profit = position_value - cost_basis
    total_equity = state.cash_remaining + position_value
    total_profit = total_equity - state.principal
    total_profit_rate = (total_profit / state.principal * 100) if state.principal > 0 else 0.0
    unrealized_rate = (unrealized_profit / cost_basis * 100) if cost_basis > 0 else 0.0
    return {
        "market_price": market_price,
        "position_value": position_value,
        "cost_basis": cost_basis,
        "unrealized_profit": unrealized_profit,
        "unrealized_rate": unrealized_rate,
        "total_equity": total_equity,
        "total_profit": total_profit,
        "total_profit_rate": total_profit_rate,
        "position_value_krw": position_value * rate,
        "total_equity_krw": total_equity * rate,
        "total_profit_krw": total_profit * rate,
    }

def build_order_checklist(state: StrategyState) -> str:
    mode = get_mode(state.split_count, state.t_value, state.quantity)
    lines = ["", "🔔 [주문 체크리스트]"]
    lines.append("□ 전일 종가/최근 종가 입력 확인")
    if mode == "IDLE":
        lines.append("□ 첫 매수 큰수 가격 확인")
        lines.append("□ LOC 매수 주문 입력")
    elif mode in ("GENERAL_FIRST_HALF", "GENERAL_SECOND_HALF"):
        lines.append("□ 별지점 가격 확인")
        lines.append("□ 하단 밴드 사용 여부 확인")
        lines.append("□ LOC 매수 주문 입력")
        lines.append("□ 쿼터 익절 지정가 매도 입력")
        lines.append("□ 최종 익절 지정가 매도 입력")
    elif mode == "REVERSE":
        lines.append("□ MOC 매도 수량 확인")
        lines.append("□ 리버스 5일 종가 입력 확인")
        lines.append("□ 리버스 쿼터매수 주문 입력")
        lines.append("□ 일반모드 복귀 조건 확인")
    lines.append("□ 주문 입력 후 체결 여부 확인")
    return "\n".join(lines)


# =========================
# 전략 계산 함수
# =========================
def calc_star_rate(symbol: str, split_count: int, t_value: float) -> float:
    if symbol == "TQQQ":
        if split_count == 20:
            return (15 - 1.5 * t_value) / 100
        elif split_count == 40:
            return (15 - 0.75 * t_value) / 100
    elif symbol == "SOXL":
        if split_count == 20:
            return (20 - 2 * t_value) / 100
        elif split_count == 40:
            return (20 - 1.0 * t_value) / 100
    raise ValueError("지원하지 않는 symbol/split 조합")


def calc_general_star_price(avg_price: float, symbol: str, split_count: int, t_value: float) -> float:
    star_rate = calc_star_rate(symbol, split_count, t_value)
    return round2(avg_price * (1 + star_rate))


def calc_general_star_buy_price(avg_price: float, symbol: str, split_count: int, t_value: float) -> float:
    """라오어 V4.0 기준: 매수 LOC는 별지점보다 0.01달러 낮게, 매도는 별지점 그대로 사용."""
    return max(0.01, round2(calc_general_star_price(avg_price, symbol, split_count, t_value) - 0.01))


def calc_general_buy_attempt_amount(cash_remaining: float, split_count: int, t_value: float) -> float:
    remain_turn = split_count - t_value
    if remain_turn <= 0:
        raise ValueError("일반모드 범위를 초과했습니다.")
    return cash_remaining / remain_turn


def calc_final_sell_price(symbol: str, avg_price: float) -> float:
    if symbol == "TQQQ":
        return round2(avg_price * 1.15)
    elif symbol == "SOXL":
        return round2(avg_price * 1.20)
    raise ValueError("지원하지 않는 종목")


def calc_reverse_star_price_from_5day_avg(closes: List[float]) -> float:
    if len(closes) != 5:
        raise ValueError("직전 5거래일 종가 5개가 필요합니다.")
    return round2(sum(closes) / 5)


def calc_reverse_sell_qty(quantity: float, split_count: int) -> float:
    divisor = 10 if split_count == 20 else 20
    return math.floor(quantity / divisor)


def apply_general_quarter_sell_t(t_value: float) -> float:
    return t_value * 0.75


def apply_general_buy_t(t_value: float, half: bool) -> float:
    return t_value + (0.5 if half else 1.0)


def apply_reverse_sell_t(t_value: float, split_count: int) -> float:
    if split_count == 20:
        return t_value * 0.9
    elif split_count == 40:
        return t_value * 0.95
    raise ValueError("지원하지 않는 분할")


def apply_reverse_buy_t(t_value: float, split_count: int) -> float:
    return t_value + (split_count - t_value) * 0.25


def should_exit_reverse(symbol: str, close_price: float, avg_price: float) -> bool:
    if symbol == "TQQQ":
        return close_price > avg_price * 0.85
    elif symbol == "SOXL":
        return close_price > avg_price * 0.80
    raise ValueError("지원하지 않는 종목")


def reset_to_idle(state: StrategyState):
    """버그5: 사이클 종료 시 모든 필드를 IDLE로 초기화"""
    state.quantity = 0.0
    state.avg_price = 0.0
    state.t_value = 0.0
    state.mode = "IDLE"
    state.reverse_day_count = 0
    state.reverse_exit_ready = False
    state.reverse_entry_date = None
    state.last5_closes = []
    state.lower_bands = []
    state.big_price = None


# =========================
# 텍스트 생성
# (버그3: build_plan_text는 state를 저장하지 않음 - 읽기 전용)
# =========================
def build_status_text(state: StrategyState, rate: float) -> str:
    """현재 상태 버튼: 주문표의 기본정보만 간결하게 표시."""
    mode = get_mode(state.split_count, state.t_value, state.quantity)
    market_price = state.last_close if state.last_close > 0 else state.avg_price
    profit_rate = ((market_price - state.avg_price) / state.avg_price * 100) if state.avg_price > 0 and market_price > 0 else 0.0
    current_split = min(state.split_count, max(1, int(math.floor(state.t_value)) + 1))

    return (
        f"📊 <b>[🩷 성장일지 - {state.symbol}]</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📌 <b>기본정보</b>\n"
        f"📍 <b>종목:</b> {state.symbol}\n"
        f"🔄 <b>사이클:</b> {state.cycle_no}회차\n"
        f"🧩 <b>분할 진행:</b> {current_split} / {state.split_count}분할\n"
        f"🟢 <b>모드:</b> {format_mode(mode)}\n"
        f"🔢 <b>현재 T값:</b> {state.t_value:.4f}\n"
        f"💵 <b>평단가:</b> {format_usd(state.avg_price)} ({format_krw(state.avg_price * rate)})\n"
        f"📉 <b>전일 종가:</b> {format_usd(state.last_close)}\n"
        f"📈 <b>현재 수익률:</b> {profit_rate:.2f}%\n"
        f"💰 <b>남은 현금:</b> {format_usd(state.cash_remaining)} ({format_krw(state.cash_remaining * rate)})"
    )


def build_order_confirmation_table(state: StrategyState, rate: float) -> str:
    """오늘 주문을 전량매수/절반매수 기준으로 한눈에 확인하는 확정표."""
    mode = get_mode(state.split_count, state.t_value, state.quantity)
    lines = []
    lines.append("🎯 [오늘 주문 확정표]")
    lines.append(f"📌 종목: {state.symbol} | 모드: {format_mode(mode)} | T값: {state.t_value:.4f}")
    lines.append(f"📊 평단: {format_usd(state.avg_price)} | 전일/최근 종가: {format_usd(state.last_close)} | 잔금: {format_usd(state.cash_remaining)}")

    if mode == "IDLE":
        if state.last_close <= 0:
            lines.append("⚠️ 아직 전일 종가가 입력되지 않아 주문 확정표를 만들 수 없습니다.")
            lines.append("➡️ 설정 → 종가 입력 후 다시 '오늘 계획'을 확인하세요.")
            return "\n".join(lines)

        first_big_price = state.big_price if state.big_price else round2(state.last_close * 1.10)
        first_attempt_amount = round2(state.principal / state.split_count)
        first_qty = math.floor(first_attempt_amount / first_big_price) if first_big_price > 0 else 0
        actual_amount = round2(first_big_price * first_qty)
        lines.append("\n🟢 [새 사이클 첫 매수]")
        lines.append(f"🛒 LOC 매수 | 주문가격: {format_usd(first_big_price)} | 수량: {first_qty}주 | 예상 최대금액: {format_usd(actual_amount)}")
        lines.append(f"💵 1회 매수 시도금액: {format_usd(first_attempt_amount)}")
        lines.append("📥 체결 후 봇 입력: '전량매수 체결' → 체결가와 체결수량 입력")
        lines.append("⚠️ 수량은 주문가격 기준으로 보수적으로 계산했습니다.")
        return "\n".join(lines)

    if mode in ("GENERAL_FIRST_HALF", "GENERAL_SECOND_HALF"):
        star_price = calc_general_star_price(state.avg_price, state.symbol, state.split_count, state.t_value)
        star_buy_price = calc_general_star_buy_price(state.avg_price, state.symbol, state.split_count, state.t_value)
        buy_attempt_amount = calc_general_buy_attempt_amount(state.cash_remaining, state.split_count, state.t_value)
        final_sell_price = calc_final_sell_price(state.symbol, state.avg_price)
        quarter_sell_qty = round(state.quantity * 0.25, 2)
        final_sell_qty = round(state.quantity - quarter_sell_qty, 2)

        lines.append("\n🛒 [매수 주문 - 공식 기준]")
        lines.append(f"💵 오늘 1회 매수 시도금액: {format_usd(buy_attempt_amount)}")

        full_total_qty = 0
        full_total_amount = 0.0

        if mode == "GENERAL_FIRST_HALF":
            star_amount = buy_attempt_amount / 2
            avg_amount = buy_attempt_amount / 2
            star_qty = math.floor(star_amount / star_buy_price) if star_buy_price > 0 else 0
            avg_qty = math.floor(avg_amount / state.avg_price) if state.avg_price > 0 else 0
            orders = [("⭐ 별지점 LOC 매수", star_buy_price, star_qty, star_amount), ("🟡 평단 LOC 매수", state.avg_price, avg_qty, avg_amount)]
            for i, band in enumerate(state.lower_bands, start=1):
                orders.append((f"📉 하단밴드{i} LOC 매수", band, 1, band))
        else:
            star_qty = math.floor(buy_attempt_amount / star_buy_price) if star_buy_price > 0 else 0
            orders = [("⭐ 별지점 LOC 매수", star_buy_price, star_qty, buy_attempt_amount)]
            for i, band in enumerate(state.lower_bands, start=1):
                orders.append((f"📉 하단밴드{i} LOC 매수", band, 1, band))

        for label, price, qty, assigned in orders:
            amount = qty * price
            full_total_qty += qty
            full_total_amount += amount
            lines.append(f"🟢 {label} | 가격: {format_usd(price)} | 수량: {qty}주 | 배정/참고금액: {format_usd(assigned)} | 최대금액: {format_usd(amount)}")

        full_avg = (full_total_amount / full_total_qty) if full_total_qty > 0 else 0.0
        lines.append(f"✅ 매수 합계 참고 | 평균입력가: {format_usd(full_avg)} | 총수량: {full_total_qty}주 | 총금액: {format_usd(full_total_amount)}")
        lines.append("📥 체결 시 봇 입력: 여러 가격이 체결되면 평균체결가와 총수량을 입력")

        lines.append("\n💰 [매도 주문]")
        lines.append(f"🧩 쿼터 익절 지정가 매도 | 가격: {format_usd(star_price)} | 수량: {quarter_sell_qty}주")
        lines.append(f"🏁 최종 익절 지정가 매도 | 가격: {format_usd(final_sell_price)} | 수량: {final_sell_qty}주")

        lines.append("\n🧭 [모드/방어 경고]")
        if state.t_value < state.split_count / 2:
            lines.append(f"🟢 전반전 구간입니다. 후반전 전환까지 약 {state.split_count/2 - state.t_value:.2f}T 남았습니다.")
        else:
            lines.append("🟠 후반전/소진 구간입니다. 잔금 관리와 하단밴드 체결 여부를 더 보수적으로 확인하세요.")
        reverse_trigger = state.split_count - 1
        if state.t_value >= reverse_trigger - 2:
            lines.append(f"🔴 리버스 진입 임박: 현재 T값 {state.t_value:.2f}, 기준 {reverse_trigger} 초과 시 리버스입니다.")
        else:
            lines.append(f"🛡️ 리버스 기준까지 여유: 약 {reverse_trigger - state.t_value:.2f}T")

        lines.append("\n⚠️ 여러 주문이 각각 다른 가격에 체결되면 평균체결가 = 총체결금액 ÷ 총체결수량으로 입력하세요.")
        return "\n".join(lines)

    if mode == "REVERSE":
        reverse_sell_qty = calc_reverse_sell_qty(state.quantity, state.split_count)
        reverse_buy_amount = round2(state.cash_remaining / 4)
        lines.append("\n🔻 [리버스 방어 주문]")
        lines.append(f"🔴 MOC 매도 | 수량: {reverse_sell_qty}주")
        today = datetime.date.today().isoformat()
        is_first_reverse_day = (state.reverse_entry_date == today)
        if is_first_reverse_day:
            lines.append("🟡 리버스 첫날입니다. 오늘은 매수 주문 없이 MOC 매도만 확인합니다.")
        else:
            if len(state.last5_closes) == 5:
                reverse_star = calc_reverse_star_price_from_5day_avg(state.last5_closes)
                qty = math.floor(reverse_buy_amount / reverse_star) if reverse_star > 0 else 0
                lines.append(f"🟢 LOC 리버스 쿼터매수 | 가격: {format_usd(reverse_star)} | 수량: {qty}주 | 배분금액: {format_usd(reverse_buy_amount)}")
                lines.append("📥 체결 시 봇 입력: '전량매수 체결' → 체결가와 수량 입력")
            else:
                lines.append("⚠️ 리버스 5일 종가가 없습니다. 설정 → 리버스 5일 종가 입력을 먼저 진행하세요.")
        if state.avg_price > 0:
            exit_rate = 0.85 if state.symbol == "TQQQ" else 0.80
            exit_price = round2(state.avg_price * exit_rate)
            lines.append(f"🛡️ 일반모드 복귀 기준: 종가가 {format_usd(exit_price)} 초과")
        return "\n".join(lines)

    lines.append("⚠️ 주문 확정표를 만들 수 없는 상태입니다.")
    return "\n".join(lines)


def build_plan_text(state: StrategyState, rate: float) -> str:
    """
    🩷 오늘의 미션 💚
    - 텔레그램 코드블록 보라색 박스 안에 들어가는 짧은 가로형 UI
    - 기본정보는 '현재 상태' 버튼으로 분리
    - 모드 전환이 필요할 때만 짧게 알림
    """
    import unicodedata

    mode = get_mode(state.split_count, state.t_value, state.quantity)

    def usd(x: float) -> str:
        return f"${round2(x):,.2f}"

    def safe_floor(amount: float, price: float) -> int:
        if price <= 0 or amount <= 0:
            return 0
        return math.floor(amount / price)

    def display_width(s: object) -> int:
        value = str(s)
        width = 0
        for ch in value:
            if unicodedata.east_asian_width(ch) in ("F", "W"):
                width += 2
            else:
                width += 1
        return width

    def pad(value: object, width: int) -> str:
        value = str(value)
        return value + " " * max(0, width - display_width(value))

    def compact_lines(rows, widths) -> str:
        """모바일에서 안 깨지게 테두리 없이 짧은 가로형 표를 만듭니다."""
        lines = []
        for row in rows:
            line = "  ".join(pad(row[i], widths[i]) for i in range(len(row)))
            lines.append(line.rstrip())
        return "\n".join(lines)

    def mode_change_notice(current_t: float) -> str:
        old_m = get_mode(state.split_count, current_t, state.quantity)
        half_t = apply_general_buy_t(current_t, half=True)
        full_t = apply_general_buy_t(current_t, half=False)
        half_m = get_mode(state.split_count, half_t, max(state.quantity, 1))
        full_m = get_mode(state.split_count, full_t, max(state.quantity, 1))

        notices = []
        if old_m != half_m:
            notices.append(f"⚠️ 절반매수 체결 시 <b>{format_mode(half_m)}</b> 전환 예정")
        if old_m != full_m:
            notices.append(f"🚨 전량매수 체결 시 <b>{format_mode(full_m)}</b> 전환 예정")
        return "\n".join(notices)

    def total_order_summary(orders):
        total_qty = sum(o["qty"] for o in orders)
        total_amount = sum(o["qty"] * o["price"] for o in orders)
        avg_fill = (total_amount / total_qty) if total_qty > 0 else 0
        return total_qty, total_amount, avg_fill

    title = f"🩷<b>[오늘의 미션 - {state.symbol}]</b>💚"

    # ── IDLE: 새 사이클 시작 전 ──
    if mode == "IDLE":
        if state.last_close <= 0:
            return (
                f"{title}\n"
                f"━━━━━━━━━━━━━━\n"
                f"⚪ 새 사이클 시작 전입니다.\n\n"
                f"✅ 먼저 <b>설정 → 종가 입력</b>을 완료한 뒤 다시 확인하세요.\n"
                f"💡 첫 매수 큰수는 기본적으로 전일 종가 +10%를 참고합니다."
            )

        first_big_price = state.big_price if state.big_price else round2(state.last_close * 1.10)
        first_attempt_amount = round2(state.principal / state.split_count)
        first_qty = safe_floor(first_attempt_amount, first_big_price)

        buy_box = compact_lines(
            [
                ["🟢 첫매수", usd(first_big_price), f"{first_qty}주", usd(first_attempt_amount)]
            ],
            [10, 8, 5, 8]
        )

        return (
            f"{title}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🛒 <b>첫 매수 미션</b>\n"
            f"<pre>{buy_box}</pre>\n"
            f"💡 체결 후 <b>'전량매수 체결'</b>로 입력하면 됩니다.\n"
            f"기준 환율: ₩{rate:.2f}/$"
        )

    # ── 일반모드 ──
    if mode in ("GENERAL_FIRST_HALF", "GENERAL_SECOND_HALF"):
        star_price = calc_general_star_price(state.avg_price, state.symbol, state.split_count, state.t_value)
        star_buy_price = calc_general_star_buy_price(state.avg_price, state.symbol, state.split_count, state.t_value)
        buy_attempt_amount = calc_general_buy_attempt_amount(state.cash_remaining, state.split_count, state.t_value)

        quarter_sell_qty = round(state.quantity * 0.25, 2)
        final_sell_qty = round(state.quantity - quarter_sell_qty, 2)
        final_sell_price = calc_final_sell_price(state.symbol, state.avg_price)

        buy_orders = []
        if mode == "GENERAL_FIRST_HALF":
            star_block = round2(buy_attempt_amount / 2)
            avg_block = round2(buy_attempt_amount / 2)
            buy_orders.append({
                "icon": "🟢",
                "name": "별지점",
                "price": star_buy_price,
                "qty": safe_floor(star_block, star_buy_price),
                "amount": star_block,
                "fill_type": "절반",
            })
            buy_orders.append({
                "icon": "🟡",
                "name": "평단가",
                "price": state.avg_price,
                "qty": safe_floor(avg_block, state.avg_price),
                "amount": avg_block,
                "fill_type": "절반",
            })
            for idx, band in enumerate(state.lower_bands, start=1):
                buy_orders.append({
                    "icon": "📉",
                    "name": f"하단{idx}",
                    "price": band,
                    "qty": 1,
                    "amount": band,
                    "fill_type": "추가",
                })
            buy_title = "🛒 <b>매수 미션 - 전반전</b>"
            note = (
                "💡 한쪽만 체결되면 <b>'절반매수 체결'</b>, "
                "별지점+평단가 둘 다 체결되면 <b>'전량매수 체결'</b>로 입력하세요."
            )
        else:
            buy_orders.append({
                "icon": "🟢",
                "name": "별지점",
                "price": star_buy_price,
                "qty": safe_floor(buy_attempt_amount, star_buy_price),
                "amount": round2(buy_attempt_amount),
                "fill_type": "전량",
            })
            for idx, band in enumerate(state.lower_bands, start=1):
                buy_orders.append({
                    "icon": "📉",
                    "name": f"하단{idx}",
                    "price": band,
                    "qty": 1,
                    "amount": band,
                    "fill_type": "추가",
                })
            buy_title = "🛒 <b>매수 미션 - 후반전</b>"
            note = (
                "💡 계획한 LOC 매수가 대부분 체결되면 <b>'전량매수 체결'</b>, "
                "일부만 체결되면 <b>'절반매수 체결'</b>로 입력하세요."
            )

        buy_rows = [
            [f"{o['icon']} {o['name']}", usd(o["price"]), f"{o['qty']}주", o["fill_type"]]
            for o in buy_orders
        ]
        buy_box = compact_lines(buy_rows, [10, 8, 5, 4])

        total_qty, total_amount, avg_fill = total_order_summary(buy_orders)

        sell_rows = [
            ["🧩 쿼터", usd(star_price), f"{quarter_sell_qty}주"],
            ["🏁 최종", usd(final_sell_price), f"{final_sell_qty}주"],
        ]
        sell_box = compact_lines(sell_rows, [8, 8, 7])

        notice = mode_change_notice(state.t_value)
        notice_block = (
            f"\n\n🧭 <b>모드 전환 알림</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"{notice}"
            if notice else ""
        )

        return (
            f"{title}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{buy_title}\n"
            f"<pre>{buy_box}</pre>\n"
            f"📦 <b>미션 요약</b>\n"
            f"총 예정수량: <b>{total_qty}주</b>\n"
            f"총 예정금액: <b>{usd(total_amount)}</b>\n"
            f"평균입력가: <b>{usd(avg_fill)}</b>\n\n"
            f"💰 <b>매도 미션</b>\n"
            f"<pre>{sell_box}</pre>"
            f"{notice_block}\n\n"
            f"{note}\n"
            f"기준 환율: ₩{rate:.2f}/$"
        )

    # ── 리버스 모드 ──
    if mode == "REVERSE":
        reverse_sell_qty = calc_reverse_sell_qty(state.quantity, state.split_count)
        reverse_buy_amount = round2(state.cash_remaining / 4)

        today = datetime.date.today().isoformat()
        is_first_reverse_day = (state.reverse_entry_date == today)

        reverse_exit_price = 0.0
        if state.avg_price > 0:
            reverse_exit_price = round2(state.avg_price * (0.85 if state.symbol == "TQQQ" else 0.80))

        sell_box = compact_lines(
            [["🔻 MOC", f"{reverse_sell_qty}주", "장마감"]],
            [8, 7, 6]
        )

        msg = (
            f"{title}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🛡️ <b>리버스 방어 미션</b>\n"
            f"<pre>{sell_box}</pre>"
        )

        if reverse_sell_qty <= 0:
            msg += "\n⚠️ MOC 매도수량이 0주입니다. 보유수량을 확인하세요.\n"

        if is_first_reverse_day:
            msg += (
                "\n⭐ <b>리버스 매수 미션</b>\n"
                "🚫 리버스 진입 첫날은 매수 없이 MOC 매도만 확인하세요.\n"
            )
        else:
            if len(state.last5_closes) == 5:
                reverse_star = calc_reverse_star_price_from_5day_avg(state.last5_closes)
                qty_at_rev_star = safe_floor(reverse_buy_amount, reverse_star)
                buy_box = compact_lines(
                    [["🟢 리버스", usd(reverse_star), f"{qty_at_rev_star}주", usd(reverse_buy_amount)]],
                    [10, 8, 5, 8]
                )
                msg += (
                    "\n⭐ <b>리버스 매수 미션</b>\n"
                    f"<pre>{buy_box}</pre>"
                )
            else:
                msg += (
                    "\n⭐ <b>리버스 매수 미션</b>\n"
                    "⚠️ 최근 5거래일 종가가 없습니다.\n"
                    "설정 → <b>리버스 5일 종가 입력</b> 후 다시 확인하세요.\n"
                )

        msg += (
            f"\n🧭 <b>복귀 기준</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"종가가 <b>{usd(reverse_exit_price)} 초과</b>하면 일반모드 복귀 준비\n"
            f"현재 판정: {'✅ 복귀 준비 충족' if state.reverse_exit_ready else '⏳ 아직 미충족'}\n"
            f"기준 환율: ₩{rate:.2f}/$"
        )

        return msg

    return "계획을 계산할 수 없습니다."


# =========================
# 전략 명령 함수
# =========================
def cmd_new(symbol: str, split_count: int, principal: float) -> str:
    symbol = symbol.upper()
    if symbol not in ("TQQQ", "SOXL"):
        return "종목은 TQQQ 또는 SOXL만 지원합니다."
    if split_count not in (20, 40):
        return "분할은 20 또는 40만 지원합니다."
    if principal <= 0:
        return "시작원금은 0보다 커야 합니다."

    state = StrategyState(
        symbol=symbol,
        split_count=split_count,
        principal=principal,
        cash_remaining=principal,
        quantity=0.0,
        avg_price=0.0,
        t_value=0.0,
        last_close=0.0,
        mode="IDLE",
        cycle_no=1,
        initial_principal=principal,
        compound_mode="선택안함",
        total_realized_profit=0.0,
        last_cycle_profit=0.0,
        best_cycle_profit=0.0,
        cycle_start_date=datetime.date.today().isoformat(),
    )
    save_state(state)

    rate = get_usd_krw_rate()
    return (
        "새 사이클 생성완료. 행운을 빌어요 !🍀\n"
        f"종목: {symbol}\n"
        f"분할: {split_count}\n"
        f"시작원금: ${principal} (₩{int(principal * rate):,} 환산)\n\n"
        "다음으로 '종가 입력' 버튼을 눌러 전일 종가를 넣고, '오늘 계획'을 확인하세요."
    )


def cmd_status(symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return f"[{symbol}] 진행 중인 사이클이 없습니다. '새 사이클'부터 시작하세요."
    # 버그3: 조회 시 mode 동기화만 하고 저장은 상태 변경이 있을 때만
    current_mode = get_mode(state.split_count, state.t_value, state.quantity)
    if state.mode != current_mode:
        state.mode = current_mode
        save_state(state)
    rate = get_usd_krw_rate()
    return build_status_text(state, rate)


def cmd_plan(symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return f"[{symbol}] 진행 중인 사이클이 없습니다. '새 사이클'부터 시작하세요."
    rate = get_usd_krw_rate()
    return build_plan_text(state, rate)  # 중복 주문표 제거


def cmd_set_close(price: float, symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."
    if price <= 0:
        return "종가는 0보다 커야 합니다."

    state.last_close = price

    if state.mode == "REVERSE" and state.avg_price > 0:
        state.reverse_exit_ready = should_exit_reverse(state.symbol, price, state.avg_price)

    save_state(state)

    extra = ""
    if state.mode == "REVERSE":
        extra = f"\n리버스 종료 조건 충족 여부: {'충족' if state.reverse_exit_ready else '미충족'}"

    return f"[{symbol}] 종가 ${round2(price)} 반영 완료.{extra}"


def cmd_set_last5_close(raw: str, symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."

    try:
        closes = [round2(float(x.strip())) for x in raw.split(",") if x.strip()]
    except ValueError:
        return "형식이 잘못되었습니다. 예: 31.1,30.4,29.8,30.0,30.7"

    if len(closes) != 5:
        return "직전 5거래일 종가를 정확히 5개 입력하세요."

    state.last5_closes = closes
    save_state(state)

    star_price = calc_reverse_star_price_from_5day_avg(closes)
    return (
        f"[{symbol}] 최근 5거래일 종가 저장 완료: {closes}\n"
        f"리버스 별지점(5일 평균): ${star_price}"
    )


def cmd_set_big_price(price: float, symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."
    if price <= 0:
        return "큰수 가격은 0보다 커야 합니다."
    state.big_price = price
    save_state(state)
    return f"[{symbol}] 큰수 가격 ${round2(price)} 설정 완료."


def cmd_clear_big_price(symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."
    state.big_price = None
    save_state(state)
    return f"[{symbol}] 큰수 가격 설정을 해제했습니다."


def cmd_set_lower_bands(raw: str, symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."

    try:
        bands = [round2(float(x.strip())) for x in raw.split(",") if x.strip()]
    except ValueError:
        return "형식이 잘못되었습니다. 예: 67.40,59.91,53.92"

    if not bands:
        return "최소 1개 이상의 가격을 입력하세요."

    state.lower_bands = bands
    save_state(state)
    return f"[{symbol}] 하단 밴드 설정 완료: {bands}"


def cmd_clear_lower_bands(symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."
    state.lower_bands = []
    save_state(state)
    return f"[{symbol}] 하단 밴드를 초기화했습니다."


def apply_buy_fill(state: StrategyState, price: float, qty: float, half: bool) -> str:
    if price <= 0 or qty <= 0:
        return "가격과 수량은 0보다 커야 합니다."

    amount = price * qty
    if amount > state.cash_remaining + 1e-9:
        return f"잔금 부족: 필요 ${round2(amount)}, 현재 잔금 ${round2(state.cash_remaining)}"

    old_mode = get_mode(state.split_count, state.t_value, state.quantity)

    state.avg_price = weighted_avg_price(state.quantity, state.avg_price, qty, price)
    state.quantity += qty
    state.cash_remaining -= amount

    if old_mode == "IDLE":
        state.t_value = apply_general_buy_t(state.t_value, half=False)
    elif old_mode in ("GENERAL_FIRST_HALF", "GENERAL_SECOND_HALF"):
        state.t_value = apply_general_buy_t(state.t_value, half=half)
    elif old_mode == "REVERSE":
        state.t_value = apply_reverse_buy_t(state.t_value, state.split_count)

    new_mode = get_mode(state.split_count, state.t_value, state.quantity)

    # 리버스 신규 진입 시 진입일 기록
    if old_mode != "REVERSE" and new_mode == "REVERSE":
        state.reverse_entry_date = datetime.date.today().isoformat()

    state.mode = new_mode
    save_state(state)
    log_trade(state.symbol, "BUY", price, qty)

    fill_type = "절반매수" if half else "전량매수"
    rate = get_usd_krw_rate()

    return (
        f"[{state.symbol} 매수 체결 반영 완료]\n"
        f"구분: {fill_type}\n"
        f"체결가: ${round2(price)}\n"
        f"수량: {qty}주\n"
        f"금액: ${round2(amount)} (₩{int(amount * rate):,})\n"
        f"새 평단: ${round2(state.avg_price)}\n"
        f"새 보유수량: {state.quantity}주\n"
        f"새 잔금: ${round2(state.cash_remaining)}\n"
        f"새 T값: {state.t_value:.6f}\n"
        f"새 모드: {format_mode(state.mode)}"
    )


def cmd_fill_buy_half(price: float, qty: float, symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."
    return apply_buy_fill(state, price, qty, half=True)


def cmd_fill_buy_full(price: float, qty: float, symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."
    return apply_buy_fill(state, price, qty, half=False)


def cmd_fill_sell(price: float, qty: float, symbol: str) -> str:
    state = load_state(symbol)
    if not state:
        return "먼저 새 사이클을 생성하세요."
    if price <= 0 or qty <= 0:
        return "가격과 수량은 0보다 커야 합니다."
    if qty > state.quantity + 1e-9:
        return f"보유수량 부족: 매도수량 {qty}, 현재 보유 {state.quantity}"

    amount = price * qty
    # 버그2: old_mode를 상태 변경 전에 먼저 확정
    old_mode = get_mode(state.split_count, state.t_value, state.quantity)

    state.quantity -= qty
    state.cash_remaining += amount

    rate = get_usd_krw_rate()
    congrats_msg = ""

    # 버그5: 사이클 종료 시 모든 필드 초기화
    if state.quantity <= 1e-9:
        total_profit = state.cash_remaining - state.principal
        profit_percent = (total_profit / state.principal) * 100
        state.last_cycle_profit = round2(total_profit)
        state.total_realized_profit = round2(state.total_realized_profit + total_profit)
        state.best_cycle_profit = max(state.best_cycle_profit, round2(total_profit))
        congrats_msg = build_cycle_clear_text(state, total_profit, profit_percent)
        log_cycle_result(state.symbol, state.principal, state.cash_remaining)
        reset_to_idle(state)
        # cash_remaining과 principal은 유지 (잔금 확인용)
        state.cash_remaining = state.cash_remaining  # no-op, 명시적 표현
    else:
        # T값 조정
        if old_mode in ("GENERAL_FIRST_HALF", "GENERAL_SECOND_HALF"):
            state.t_value = apply_general_quarter_sell_t(state.t_value)

        elif old_mode == "REVERSE":
            state.t_value = apply_reverse_sell_t(state.t_value, state.split_count)
            state.reverse_day_count = max(1, state.reverse_day_count + 1)

            # 리버스 종료 → 후반전 복귀
            if state.reverse_exit_ready:
                state.reverse_day_count = 0
                state.reverse_exit_ready = False
                state.reverse_entry_date = None
                state.mode = "GENERAL_SECOND_HALF"
            else:
                new_mode = get_mode(state.split_count, state.t_value, state.quantity)
                # 리버스 → 리버스 신규 진입 감지 (이론상 발생 안 하지만 방어)
                if old_mode != "REVERSE" and new_mode == "REVERSE":
                    state.reverse_entry_date = datetime.date.today().isoformat()
                state.mode = new_mode
        else:
            state.mode = get_mode(state.split_count, state.t_value, state.quantity)

    save_state(state)
    log_trade(state.symbol, "SELL", price, qty)

    return (
        f"[{state.symbol} 매도 체결 반영 완료]{congrats_msg}\n"
        f"체결가: ${round2(price)}\n"
        f"수량: {qty}주\n"
        f"금액: ${round2(amount)} (₩{int(amount * rate):,})\n"
        f"새 보유수량: {state.quantity}주\n"
        f"새 잔금: ${round2(state.cash_remaining)}\n"
        f"새 T값: {state.t_value:.6f}\n"
        f"새 모드: {format_mode(state.mode)}"
    )


def cmd_reset(symbol: str) -> str:
    clear_state(symbol)
    return f"[{symbol}] 사이클 데이터를 초기화했습니다."


# =========================
# 거래 내역 조회 (버그7: 종목별 필터)
# =========================
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.user_data.get("active_symbol")
    if not symbol:
        await update.callback_query.message.reply_text("뭘로 부자될래 🤑", reply_markup=main_menu_keyboard())
        return

    conn = db_connect()
    rows = conn.cursor().execute(
        "SELECT id, type, price, qty FROM trades WHERE symbol = ? ORDER BY id DESC LIMIT 10",
        (symbol,)
    ).fetchall()
    conn.close()

    if not rows:
        await update.callback_query.message.reply_text(f"[{symbol}] 기록된 거래 내역이 없습니다.")
        return

    msg = f"[{symbol} 최근 거래 내역 (최대 10건)]\n"
    for r in rows:
        msg += f"ID:{r[0]} | {r[1]} | ${r[2]} x {r[3]}주\n"

    keyboard = [[InlineKeyboardButton("🆕 새 사이클 시작", callback_data="menu_new"),
         InlineKeyboardButton("🧹 처음부터 다시 시작", callback_data="menu_reset")],
        [InlineKeyboardButton("🏠 메인 메뉴", callback_data="menu_main")]]
    await update.callback_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


# =========================
# 리포트 (종목별 필터)
# =========================
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.user_data.get("active_symbol")
    if not symbol:
        await update.callback_query.message.reply_text("뭘로 부자될래 🤑", reply_markup=main_menu_keyboard())
        return

    state = load_state(symbol)
    if not state:
        await update.callback_query.message.reply_text(f"[{symbol}] 진행 중인 사이클이 없습니다.", reply_markup=main_menu_keyboard())
        return

    rate = get_usd_krw_rate()
    metrics = calc_position_metrics(state, rate)

    current_split = min(state.split_count, max(1, int(math.floor(state.t_value)) + 1))
    investment_amount = metrics["position_value"]
    profit_rate = metrics["total_profit_rate"]

    report_text = (
        f"🩷 <b>{symbol} 성장일지</b> 💚\n\n"
        f"🌱 <b>사이클</b> {state.cycle_no}회차\n"
        f"🧩 <b>{current_split} / {state.split_count}</b> 분할 진행중\n"
        f"💰 <b>현재 투자금</b> : {format_usd(investment_amount)}\n"
        f"💵 <b>평단가</b> : {format_usd(state.avg_price)}\n"
        f"📈 <b>수익률</b> : {profit_rate:+.2f}%"
    )

    conn = db_connect()
    rows = conn.cursor().execute(
        "SELECT timestamp, type, amount FROM trades WHERE symbol = ? ORDER BY id",
        (symbol,)
    ).fetchall()
    conn.close()

    if not rows:
        await update.callback_query.message.reply_text(
            report_text + "\n\n아직 거래 기록이 없어 그래프는 다음 체결부터 만들어져요 🌱",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML"
        )
        return

    # 누적 체결 흐름: 매수는 씨앗 심기(+), 매도는 회수(-)로 반영
    labels = []
    values = []
    cumulative = 0.0
    max_points = 12
    for idx, (_, trade_type, amount) in enumerate(rows, start=1):
        if trade_type == "BUY":
            cumulative += amount
        elif trade_type == "SELL":
            cumulative -= amount
        labels.append(str(idx))
        values.append(cumulative)

    # 너무 많은 점은 최근 max_points개만 표시
    labels = labels[-max_points:]
    values = values[-max_points:]

    # 보기 좋은 한글 폰트 자동 선택
    font_candidates = [
        "/usr/share/fonts/truetype/nanum/NanumSquareRoundR.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    font_path = next((p for p in font_candidates if os.path.exists(p)), None)
    if font_path:
        try:
            import matplotlib.font_manager as fm
            fm.fontManager.addfont(font_path)
            font_name = fm.FontProperties(fname=font_path).get_name()
            plt.rcParams["font.family"] = font_name
        except Exception:
            pass
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(8, 4.6), dpi=160)
    fig.patch.set_facecolor("#FFF7FB")
    ax.set_facecolor("#FFFFFF")

    x = list(range(len(values)))
    ax.plot(x, values, marker="o", linewidth=2.8, color="#FF8AC8")
    ax.fill_between(x, values, min(values + [0]), color="#FFCEE7", alpha=0.45)

    ax.set_title(f"🌱 {symbol} 투자 씨앗 성장 그래프", fontsize=15, pad=14, color="#333333")
    ax.set_xlabel("체결 순서", fontsize=10)
    ax.set_ylabel("누적 투자금($)", fontsize=10)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.grid(True, alpha=0.22)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # 마지막 값 강조
    if values:
        ax.annotate(
            f"{format_usd(values[-1])}",
            xy=(x[-1], values[-1]),
            xytext=(8, 8),
            textcoords="offset points",
            fontsize=10,
            color="#333333",
            bbox=dict(boxstyle="round,pad=0.35", fc="#FFF0F8", ec="#FF8AC8", alpha=0.95)
        )

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)

    await update.callback_query.message.reply_text(
        report_text,
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML"
    )
    await update.callback_query.message.reply_photo(
        photo=InputFile(buf, filename=f"{symbol}_growth_report.png")
    )


# =========================
# 홈 대시보드 UI
# =========================
def build_dashboard_text(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> str:
    """/start와 메인 메뉴에서 보여줄 홈 대시보드.
    상태파일이 있는 활성 종목만 표시하므로 TQQQ만 운용 중이면 SOXL은 숨깁니다.
    """
    rate = get_usd_krw_rate()
    active = get_active_symbols()
    selected = context.user_data.get("active_symbol") if context else None

    lines = [
        f"🤖 무한매수봇 V{VERSION}",
        "━━━━━━━━━━━━━━",
    ]

    if not active:
        lines += [
            "📭 진행 중인 사이클이 없습니다.",
            "",
            "아래 [새 사이클] 버튼으로 TQQQ 또는 SOXL을 시작하세요.",
            "━━━━━━━━━━━━━━",
        ]
        return "\n".join(lines)

    for symbol in active:
        state = load_state(symbol)
        if not state:
            continue
        current_mode = get_mode(state.split_count, state.t_value, state.quantity)
        if state.mode != current_mode:
            state.mode = current_mode
            save_state(state)

        metrics = calc_position_metrics(state, rate)
        cycle_rows = get_cycle_summary(symbol)
        db_cycle_profit = sum(r[0] for r in cycle_rows) if cycle_rows else 0.0
        total_realized = state.total_realized_profit if state.total_realized_profit else db_cycle_profit
        icon = "🟦" if symbol == "TQQQ" else "🟥"
        selected_mark = " ✅ 선택중" if selected == symbol else ""

        lines += [
            f"{icon} {symbol}{selected_mark}",
            f"🔁 사이클: #{state.cycle_no} | {state.compound_mode}",
            f"📍 모드: {format_mode(state.mode)}",
            f"🔢 T값: {state.t_value:.4f} / {state.split_count}",
            f"💵 평단: {format_usd(state.avg_price)} | 보유: {state.quantity}주",
            f"💰 잔금: {format_usd(state.cash_remaining)}",
            f"📈 총 손익: {format_usd(metrics['total_profit'])} ({metrics['total_profit_rate']:.2f}%)",
            f"🏆 누적 실현손익: {format_usd(total_realized)}",
            "━━━━━━━━━━━━━━",
        ]

    if selected:
        lines.append(f"현재 선택 종목: {selected}")
    else:
        lines.append("⚠️ 종목을 선택하세요. 활성 종목이 1개면 자동 선택됩니다.")
    return "\n".join(lines)


def next_cycle_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 복리 모드", callback_data=f"cycle_next_compound_{symbol}")],
        [InlineKeyboardButton("➕ 단리 모드", callback_data=f"cycle_next_simple_{symbol}")],
        [InlineKeyboardButton("💰 직접 입력", callback_data=f"cycle_next_custom_{symbol}")],
        [InlineKeyboardButton("🏠 메인 대시보드", callback_data="menu_main")],
    ])


def cmd_continue_cycle(symbol: str, compound: bool) -> str:
    state = load_state(symbol)
    if not state:
        return f"[{symbol}] 이어갈 사이클 데이터가 없습니다. 새 사이클을 생성하세요."
    if state.quantity > 1e-9:
        return f"[{symbol}] 아직 보유수량이 남아 있어 다음 사이클을 시작할 수 없습니다."

    previous_cash = state.cash_remaining
    previous_principal = state.principal
    base_principal = state.initial_principal if state.initial_principal > 0 else previous_principal

    if compound:
        next_principal = round2(previous_cash)
        state.compound_mode = "복리"
    else:
        next_principal = round2(base_principal)
        state.compound_mode = "단리"

    state.cycle_no += 1
    state.principal = next_principal
    state.cash_remaining = next_principal
    state.quantity = 0.0
    state.avg_price = 0.0
    state.t_value = 0.0
    state.mode = "IDLE"
    state.big_price = None
    state.lower_bands = []
    state.last5_closes = []
    state.reverse_day_count = 0
    state.reverse_exit_ready = False
    state.reverse_entry_date = None
    state.cycle_start_date = datetime.date.today().isoformat()
    save_state(state)

    rate = get_usd_krw_rate()
    return (
        f"🚀 [{symbol}] 다음 사이클 생성 완료\n"
        f"━━━━━━━━━━━━━━\n"
        f"🔁 사이클: #{state.cycle_no}\n"
        f"⚙️ 방식: {state.compound_mode}\n"
        f"💰 시작원금: {format_usd(next_principal)} ({format_krw(next_principal * rate)})\n\n"
        f"다음으로 [설정] → 종가 입력 후 [오늘 계획]을 확인하세요."
    )


def cmd_change_principal(symbol: str, new_principal: float) -> str:
    """새 사이클 시작 전(IDLE, 보유수량 0) 원금 수정."""
    state = load_state(symbol)
    if not state:
        return f"[{symbol}] 진행 중인 사이클이 없습니다."
    if new_principal <= 0:
        return "시작원금은 0보다 커야 합니다."

    mode = get_mode(state.split_count, state.t_value, state.quantity)
    if state.quantity > 1e-9 or mode != "IDLE":
        return (
            f"[{symbol}] 이미 매수가 시작된 사이클은 원금을 직접 바꿀 수 없습니다.\n"
            f"필요하면 이전 상태 복구 또는 새 사이클을 이용하세요."
        )

    old_principal = state.principal
    state.principal = round2(new_principal)
    state.cash_remaining = round2(new_principal)

    # 단리 기준금도 사용자가 원금을 직접 바꾸면 새 기준으로 업데이트
    if state.initial_principal <= 0 or state.compound_mode == "단리":
        state.initial_principal = round2(new_principal)

    save_state(state)
    rate = get_usd_krw_rate()
    return (
        f"💰 [{symbol}] 시작원금 수정 완료\n"
        f"━━━━━━━━━━━━━━\n"
        f"이전 원금: {format_usd(old_principal)}\n"
        f"새 원금: {format_usd(state.principal)} ({format_krw(state.principal * rate)})\n\n"
        f"이제 [설정] → 종가 입력 후 [오늘 계획]을 확인하세요."
    )


# =========================
# 버튼 UI
# =========================
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 새 사이클 시작", callback_data="menu_new")],
        [InlineKeyboardButton("🌱 성장일지", callback_data="menu_status"),
         InlineKeyboardButton("🩷 오늘의 미션 💚", callback_data="menu_plan")],
        [InlineKeyboardButton("✍️ 체결 입력", callback_data="menu_fill"),
         InlineKeyboardButton("⚙️ 설정", callback_data="menu_settings")],
        [InlineKeyboardButton("🔄 종목 선택", callback_data="menu_select_symbol"),
         InlineKeyboardButton("↩️ 되돌리기", callback_data="menu_restore")]
    ])


def symbol_select_keyboard(active_symbols: List[str], prefix: str = "select_symbol", selected: Optional[str] = None) -> InlineKeyboardMarkup:
    """종목 선택 키보드 - 활성 종목만 표시"""
    buttons = [
        [InlineKeyboardButton(f"{'✅ ' if s == selected else ''}{s}", callback_data=f"{prefix}_{s}")]
        for s in active_symbols
    ]
    buttons.append([InlineKeyboardButton("🏠 메인 대시보드", callback_data="menu_main")])
    return InlineKeyboardMarkup(buttons)


def fill_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("절반매수 체결", callback_data="fill_buy_half"),
         InlineKeyboardButton("전량매수 체결", callback_data="fill_buy_full")],
        [InlineKeyboardButton("매도 체결", callback_data="fill_sell")],
        [InlineKeyboardButton("🆕 새 사이클 시작", callback_data="menu_new"),
         InlineKeyboardButton("🧹 처음부터 다시 시작", callback_data="menu_reset")],
        [InlineKeyboardButton("🏠 메인 메뉴", callback_data="menu_main")]
    ])


def settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("종가 입력", callback_data="set_close"),
         InlineKeyboardButton("리버스 5일 종가 입력", callback_data="set_last5_close")],
        [InlineKeyboardButton("큰수 설정", callback_data="set_big_price"),
         InlineKeyboardButton("큰수 해제", callback_data="clear_big_price")],
        [InlineKeyboardButton("하단 밴드 설정", callback_data="set_lower_bands"),
         InlineKeyboardButton("하단 밴드 초기화", callback_data="clear_lower_bands")],
        [InlineKeyboardButton("💰 시작원금 수정", callback_data="set_principal")],
        [InlineKeyboardButton("🆕 새 사이클 시작", callback_data="menu_new"),
         InlineKeyboardButton("🧹 처음부터 다시 시작", callback_data="menu_reset")],
        [InlineKeyboardButton("🏠 메인 메뉴", callback_data="menu_main")]
    ])


def new_cycle_symbol_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("TQQQ", callback_data="new_symbol_TQQQ"),
         InlineKeyboardButton("SOXL", callback_data="new_symbol_SOXL")],
        [InlineKeyboardButton("🆕 새 사이클 시작", callback_data="menu_new"),
         InlineKeyboardButton("🧹 처음부터 다시 시작", callback_data="menu_reset")],
        [InlineKeyboardButton("🏠 메인 메뉴", callback_data="menu_main")]
    ])


def new_cycle_split_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("20분할", callback_data="new_split_20"),
         InlineKeyboardButton("40분할", callback_data="new_split_40")],
        [InlineKeyboardButton("🆕 새 사이클 시작", callback_data="menu_new"),
         InlineKeyboardButton("🧹 처음부터 다시 시작", callback_data="menu_reset")],
        [InlineKeyboardButton("🏠 메인 메뉴", callback_data="menu_main")]
    ])


def restore_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ 직전 상태로 복구", callback_data="restore_confirm_yes"),
         InlineKeyboardButton("취소", callback_data="menu_main")]
    ])


def reset_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("초기화 실행", callback_data="reset_confirm_yes"),
         InlineKeyboardButton("취소", callback_data="menu_main")]
    ])


# =========================
# 대화 상태 관리
# =========================
def set_pending_action(context: ContextTypes.DEFAULT_TYPE, action: str):
    context.user_data["pending_action"] = action


def get_pending_action(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    return context.user_data.get("pending_action")


def clear_pending_action(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_action", None)


def get_active_symbol(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    return context.user_data.get("active_symbol")


def require_symbol(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """active_symbol 반환. 없으면 None."""
    return context.user_data.get("active_symbol")


# =========================
# 봇 핸들러
# =========================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(context)
    active = get_active_symbols()
    if len(active) == 1 and not context.user_data.get("active_symbol"):
        context.user_data["active_symbol"] = active[0]
    elif context.user_data.get("active_symbol") not in active:
        context.user_data.pop("active_symbol", None)

    await update.message.reply_text(
        build_dashboard_text(context),
        reply_markup=main_menu_keyboard()
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(context)
    active = get_active_symbols()
    if len(active) == 1 and not context.user_data.get("active_symbol"):
        context.user_data["active_symbol"] = active[0]
    elif context.user_data.get("active_symbol") not in active:
        context.user_data.pop("active_symbol", None)

    await update.message.reply_text(
        build_dashboard_text(context),
        reply_markup=main_menu_keyboard()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_main":
        clear_pending_action(context)
        active = get_active_symbols()
        if len(active) == 1 and not context.user_data.get("active_symbol"):
            context.user_data["active_symbol"] = active[0]
        elif context.user_data.get("active_symbol") not in active:
            context.user_data.pop("active_symbol", None)
        await query.message.reply_text(build_dashboard_text(context), reply_markup=main_menu_keyboard())
        return

    # ── 이전 상태 복구 ──
    if data == "menu_restore":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("먼저 '종목 선택' 버튼으로 종목을 선택하세요.", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(
            f"↩️ [{symbol}] 직전 상태로 복구할까요?\n\n체결 입력 실수처럼 방금 전 상태로 되돌리고 싶을 때 사용하세요.",
            reply_markup=restore_confirm_keyboard()
        )
        return

    if data == "restore_confirm_yes":
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("먼저 '종목 선택' 버튼으로 종목을 선택하세요.", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(restore_previous_state(symbol), reply_markup=main_menu_keyboard())
        return

    # ── 종목 선택 (버그1/6) ──
    if data == "menu_select_symbol":
        active = get_active_symbols()
        if not active:
            await query.message.reply_text(
                "아직 진행 중인 사이클이 없습니다. 아래 버튼으로 새 사이클을 시작하세요 😉",
                reply_markup=new_cycle_symbol_keyboard()
            )
            return
        await query.message.reply_text(
            "🩷 어떤 친구를 볼까? 종목을 선택해줘 💚",
            reply_markup=symbol_select_keyboard(active, prefix="select_symbol", selected=context.user_data.get("active_symbol"))
        )
        return

    if data.startswith("select_symbol_"):
        symbol = data.replace("select_symbol_", "")
        context.user_data["active_symbol"] = symbol
        await query.message.reply_text(
            f"✅ 종목 [{symbol}] 선택됨.",
            reply_markup=main_menu_keyboard()
        )
        return

    # ── 사이클 종료 후 다음 사이클 이어가기 ──
    if data.startswith("cycle_next_custom_"):
        symbol = data.replace("cycle_next_custom_", "")
        context.user_data["active_symbol"] = symbol
        set_pending_action(context, "await_cycle_custom_principal")
        await query.message.reply_text(
            f"💰 [{symbol}] 다음 사이클 시작원금을 숫자로 입력하세요.\n예: 6500",
            reply_markup=main_menu_keyboard()
        )
        return


    if data.startswith("cycle_next_compound_"):
        symbol = data.replace("cycle_next_compound_", "")
        context.user_data["active_symbol"] = symbol
        await query.message.reply_text(cmd_continue_cycle(symbol, compound=True), reply_markup=main_menu_keyboard())
        return

    if data.startswith("cycle_next_simple_"):
        symbol = data.replace("cycle_next_simple_", "")
        context.user_data["active_symbol"] = symbol
        await query.message.reply_text(cmd_continue_cycle(symbol, compound=False), reply_markup=main_menu_keyboard())
        return

    # ── 거래내역 / 리포트 ──
    if data == "menu_history":
        await cmd_history(update, context)
        return

    if data == "menu_report":
        await cmd_report(update, context)
        return

    # ── 새 사이클 ──
    if data == "menu_new":
        clear_pending_action(context)
        await query.message.reply_text("종목을 선택하세요.", reply_markup=new_cycle_symbol_keyboard())
        return

    if data.startswith("new_symbol_"):
        symbol = data.replace("new_symbol_", "")
        context.user_data["new_symbol"] = symbol
        await query.message.reply_text(
            f"종목: {symbol}\n이제 분할을 선택하세요.",
            reply_markup=new_cycle_split_keyboard()
        )
        return

    if data.startswith("new_split_"):
        split_count = int(data.replace("new_split_", ""))
        context.user_data["new_split"] = split_count
        set_pending_action(context, "await_principal")
        await query.message.reply_text(
            f"분할: {split_count}\n시작원금을 숫자로 입력하세요. (달러 단위, 예: 1000)"
        )
        return

    # ── 상태 / 계획 / 초기화 ──
    if data == "menu_status":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("먼저 '종목 선택' 버튼으로 종목을 선택하세요.", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(cmd_status(symbol), reply_markup=main_menu_keyboard(), parse_mode="HTML")
        return

    if data == "menu_plan":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("먼저 '종목 선택' 버튼으로 종목을 선택하세요.", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(cmd_plan(symbol), reply_markup=main_menu_keyboard(), parse_mode="HTML")
        return

    if data == "menu_fill":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("먼저 '종목 선택' 버튼으로 종목을 선택하세요.", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(f"체결 입력 메뉴입니다. [{symbol}]", reply_markup=fill_menu_keyboard())
        return

    if data == "menu_settings":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("먼저 '종목 선택' 버튼으로 종목을 선택하세요.", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(f"설정 메뉴입니다. [{symbol}]", reply_markup=settings_menu_keyboard())
        return

    if data == "menu_reset":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("먼저 '종목 선택' 버튼으로 종목을 선택하세요.", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(f"[{symbol}] 정말 초기화할까요?", reply_markup=reset_confirm_keyboard())
        return

    if data == "reset_confirm_yes":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("종목이 선택되지 않았습니다.", reply_markup=main_menu_keyboard())
            return
        context.user_data.pop("active_symbol", None)
        await query.message.reply_text(cmd_reset(symbol), reply_markup=main_menu_keyboard())
        return

    # ── 체결 입력 ──
    if data == "fill_buy_half":
        set_pending_action(context, "await_fill_buy_half_price")
        await query.message.reply_text("절반매수 체결가를 입력하세요. 예: 43.13")
        return

    if data == "fill_buy_full":
        set_pending_action(context, "await_fill_buy_full_price")
        await query.message.reply_text("전량매수 체결가를 입력하세요. 예: 39.71")
        return

    if data == "fill_sell":
        set_pending_action(context, "await_fill_sell_price")
        await query.message.reply_text("매도 체결가를 입력하세요. 예: 43.14")
        return

    # ── 설정 ──
    if data == "set_principal":
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("뭘로 부자될래 🤑", reply_markup=main_menu_keyboard())
            return
        set_pending_action(context, "await_change_principal")
        await query.message.reply_text(
            f"💰 [{symbol}] 새 시작원금을 숫자로 입력하세요.\n\n이미 매수가 시작된 사이클은 원금 수정이 제한됩니다.",
            reply_markup=settings_menu_keyboard()
        )
        return

    if data == "set_close":
        set_pending_action(context, "await_set_close")
        await query.message.reply_text("종가를 입력하세요. 예: 45.93")
        return

    if data == "set_last5_close":
        set_pending_action(context, "await_set_last5_close")
        await query.message.reply_text("최근 5거래일 종가를 쉼표로 입력하세요. 예: 31.1,30.4,29.8,30.0,30.7")
        return

    if data == "set_big_price":
        set_pending_action(context, "await_set_big_price")
        await query.message.reply_text("큰수 가격을 입력하세요. 예: 33.33")
        return

    if data == "clear_big_price":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("뭘로 부자될래 🤑", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(cmd_clear_big_price(symbol), reply_markup=settings_menu_keyboard())
        return

    if data == "set_lower_bands":
        set_pending_action(context, "await_set_lower_bands")
        await query.message.reply_text("하단 밴드를 쉼표로 입력하세요. 예: 67.40,59.91,53.92")
        return

    if data == "clear_lower_bands":
        clear_pending_action(context)
        symbol = require_symbol(context)
        if not symbol:
            await query.message.reply_text("뭘로 부자될래 🤑", reply_markup=main_menu_keyboard())
            return
        await query.message.reply_text(cmd_clear_lower_bands(symbol), reply_markup=settings_menu_keyboard())
        return

    await query.message.reply_text("알 수 없는 메뉴입니다. 메인 메뉴로 돌아갑니다.", reply_markup=main_menu_keyboard())


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    action = get_pending_action(context)
    symbol = context.user_data.get("active_symbol")

    if not action:
        await update.message.reply_text("💎메뉴판입니둥💎", reply_markup=main_menu_keyboard())
        return

    try:
        if action == "await_principal":
            principal = float(text)
            new_symbol = context.user_data.get("new_symbol")
            split_count = context.user_data.get("new_split")
            if not new_symbol or not split_count:
                clear_pending_action(context)
                await update.message.reply_text("새 사이클 정보가 누락됐습니다. 다시 시작하세요.", reply_markup=main_menu_keyboard())
                return
            result = cmd_new(new_symbol, split_count, principal)
            # 새 사이클 생성 시 해당 종목 자동 선택
            context.user_data["active_symbol"] = new_symbol
            clear_pending_action(context)
            context.user_data.pop("new_symbol", None)
            context.user_data.pop("new_split", None)
            await update.message.reply_text(result, reply_markup=main_menu_keyboard())
            return

        if action == "await_cycle_custom_principal":
            principal = float(text)
            if not symbol:
                clear_pending_action(context)
                await update.message.reply_text("뭘로 부자될래 🤑", reply_markup=main_menu_keyboard())
                return
            # 다음 사이클을 사용자가 직접 입력한 원금으로 생성
            state = load_state(symbol)
            if not state:
                clear_pending_action(context)
                await update.message.reply_text(f"[{symbol}] 사이클 데이터가 없습니다.", reply_markup=main_menu_keyboard())
                return
            if state.quantity > 1e-9:
                clear_pending_action(context)
                await update.message.reply_text(f"[{symbol}] 아직 보유수량이 남아 있어 다음 사이클을 시작할 수 없습니다.", reply_markup=main_menu_keyboard())
                return
            state.cycle_no += 1
            state.principal = round2(principal)
            state.cash_remaining = round2(principal)
            state.quantity = 0.0
            state.avg_price = 0.0
            state.t_value = 0.0
            state.mode = "IDLE"
            state.big_price = None
            state.lower_bands = []
            state.last5_closes = []
            state.reverse_day_count = 0
            state.reverse_exit_ready = False
            state.reverse_entry_date = None
            state.cycle_start_date = datetime.date.today().isoformat()
            state.compound_mode = "직접입력"
            save_state(state)
            clear_pending_action(context)
            rate = get_usd_krw_rate()
            await update.message.reply_text(
                f"🚀 [{symbol}] 직접 입력 원금으로 다음 사이클 생성 완료\n"
                f"━━━━━━━━━━━━━━\n"
                f"🔁 사이클: #{state.cycle_no}\n"
                f"💰 시작원금: {format_usd(state.principal)} ({format_krw(state.principal * rate)})\n\n"
                f"다음으로 [설정] → 종가 입력 후 [오늘 계획]을 확인하세요.",
                reply_markup=main_menu_keyboard()
            )
            return


        # 이하 모든 action은 active_symbol 필요
        if not symbol:
            clear_pending_action(context)
            await update.message.reply_text("뭘로 부자될래 🤑", reply_markup=main_menu_keyboard())
            return

        if action == "await_set_close":
            price = float(text)
            result = cmd_set_close(price, symbol)
            clear_pending_action(context)
            await update.message.reply_text(result, reply_markup=settings_menu_keyboard())
            return

        if action == "await_set_last5_close":
            result = cmd_set_last5_close(text, symbol)
            clear_pending_action(context)
            await update.message.reply_text(result, reply_markup=settings_menu_keyboard())
            return

        if action == "await_set_big_price":
            price = float(text)
            result = cmd_set_big_price(price, symbol)
            clear_pending_action(context)
            await update.message.reply_text(result, reply_markup=settings_menu_keyboard())
            return

        if action == "await_change_principal":
            principal = float(text)
            result = cmd_change_principal(symbol, principal)
            clear_pending_action(context)
            await update.message.reply_text(result, reply_markup=settings_menu_keyboard())
            return

        if action == "await_set_lower_bands":
            result = cmd_set_lower_bands(text, symbol)
            clear_pending_action(context)
            await update.message.reply_text(result, reply_markup=settings_menu_keyboard())
            return

        if action == "await_fill_buy_half_price":
            context.user_data["temp_buy_half_price"] = float(text)
            set_pending_action(context, "await_fill_buy_half_qty")
            await update.message.reply_text("절반매수 수량을 입력하세요. 예: 3")
            return

        if action == "await_fill_buy_half_qty":
            price = float(context.user_data["temp_buy_half_price"])
            qty = float(text)
            result = cmd_fill_buy_half(price, qty, symbol)
            clear_pending_action(context)
            context.user_data.pop("temp_buy_half_price", None)
            await update.message.reply_text(result, reply_markup=fill_menu_keyboard())
            return

        if action == "await_fill_buy_full_price":
            context.user_data["temp_buy_full_price"] = float(text)
            set_pending_action(context, "await_fill_buy_full_qty")
            await update.message.reply_text("전량매수 수량을 입력하세요. 예: 6")
            return

        if action == "await_fill_buy_full_qty":
            price = float(context.user_data["temp_buy_full_price"])
            qty = float(text)
            result = cmd_fill_buy_full(price, qty, symbol)
            clear_pending_action(context)
            context.user_data.pop("temp_buy_full_price", None)
            await update.message.reply_text(result, reply_markup=fill_menu_keyboard())
            return

        if action == "await_fill_sell_price":
            context.user_data["temp_sell_price"] = float(text)
            set_pending_action(context, "await_fill_sell_qty")
            await update.message.reply_text("매도 수량을 입력하세요. 예: 31.5")
            return

        if action == "await_fill_sell_qty":
            price = float(context.user_data["temp_sell_price"])
            qty = float(text)
            result = cmd_fill_sell(price, qty, symbol)
            clear_pending_action(context)
            context.user_data.pop("temp_sell_price", None)
            if "사이클 클리어" in result:
                await update.message.reply_text(result, reply_markup=next_cycle_keyboard(symbol), parse_mode="HTML")
            else:
                await update.message.reply_text(result, reply_markup=fill_menu_keyboard())
            return

    except Exception as e:
        clear_pending_action(context)
        context.user_data.pop("temp_buy_half_price", None)
        context.user_data.pop("temp_buy_full_price", None)
        context.user_data.pop("temp_sell_price", None)
        await update.message.reply_text(
            f"입력 처리 중 오류가 발생했습니다: {e}\n다시 메뉴에서 진행하세요.",
            reply_markup=main_menu_keyboard()
        )


# =========================
# 직접 명령어
# =========================
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = require_symbol(context)
    if not symbol:
        await update.message.reply_text("종목을 먼저 선택하세요 (/menu → 종목 선택).", reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text(cmd_status(symbol), reply_markup=main_menu_keyboard(), parse_mode="HTML")


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = require_symbol(context)
    if not symbol:
        await update.message.reply_text("종목을 먼저 선택하세요 (/menu → 종목 선택).", reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text(cmd_plan(symbol), reply_markup=main_menu_keyboard(), parse_mode="HTML")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = require_symbol(context)
    if not symbol:
        await update.message.reply_text("종목을 먼저 선택하세요 (/menu → 종목 선택).", reply_markup=main_menu_keyboard())
        return
    context.user_data.pop("active_symbol", None)
    await update.message.reply_text(cmd_reset(symbol), reply_markup=main_menu_keyboard())


# =========================
# 메인 실행
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("reset", reset_command))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print(f"무한매수법 봇 V{VERSION} 실행 중...")
    app.run_polling()


if __name__ == "__main__":
    main()

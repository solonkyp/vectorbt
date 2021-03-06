import numpy as np
import pandas as pd
from numba import njit, b1, i1, i8, f8
from numba.types import UniTuple
from vectorbt.timeseries import pct_change_nb, fillna_nb, expanding_max_nb, diff_nb
from vectorbt.decorators import *
from vectorbt.timeseries import TimeSeries
from vectorbt.signals import Signals
import plotly.graph_objects as go
from vectorbt.widgets import FigureWidget

__all__ = ['Portfolio']

# ############# Numba functions ############# #


@njit(f8(i8, i8, f8, f8, b1[:, :], b1[:, :], f8[:, :], b1), cache=True)
def signals_order_func_np(i, col, run_cash, run_shares, entries, exits, volume, accumulate):
    """Order function to buy/sell based on signals."""
    if run_shares > 0:
        if entries[i, col] and not exits[i, col]:
            if accumulate:
                return volume[i, col]
        elif not entries[i, col] and exits[i, col]:
            return -volume[i, col]
    else:
        if entries[i, col] and not exits[i, col]:
            return volume[i, col]
        elif not entries[i, col] and exits[i, col]:
            if accumulate:
                return -volume[i, col]
    return 0.


@njit(f8(i8, i8, f8, f8, f8[:, :], b1), cache=True)
def orders_order_func_np(i, col, run_cash, run_shares, orders, is_target):
    """Buy/sell the amount of shares specified by orders."""
    if is_target:
        return orders[i, col] - run_shares
    else:
        return orders[i, col]


@njit
def portfolio_np(ts, investment, slippage, commission, order_func_np, *args):
    """Calculate portfolio value in cash and shares."""
    cash = np.empty_like(ts)
    shares = np.empty_like(ts)

    for col in range(ts.shape[1]):
        run_cash = investment
        run_shares = 0
        for i in range(ts.shape[0]):
            volume = order_func_np(i, col, run_cash, run_shares, *args)  # the amount of shares to buy/sell
            if volume > 0:
                # Buy volume
                adj_price = ts[i, col] * (1 + slippage[i, col])  # slippage applies on price
                req_cash = volume * adj_price
                req_cash /= (1 - commission[i, col])  # total cash required for this volume
                if req_cash <= run_cash:  # sufficient cash
                    run_shares += volume
                    run_cash -= req_cash
                else:  # not sufficient cash, volume will be less than requested
                    adj_cash = run_cash
                    adj_cash *= (1 - commission[i, col])  # commission in % applies on transaction volume
                    run_shares += adj_cash / adj_price
                    run_cash = 0
            elif volume < 0:
                # Sell volume
                adj_price = ts[i, col] * (1 - slippage[i, col])
                adj_shares = min(run_shares, abs(volume))
                adj_cash = adj_shares * adj_price
                adj_cash *= (1 - commission[i, col])
                run_shares -= adj_shares
                run_cash += adj_cash
            cash[i, col] = run_cash
            shares[i, col] = run_shares

    return cash, shares


@njit(UniTuple(f8[:, :], 2)(f8[:, :], f8, f8[:, :], f8[:, :], b1[:, :], b1[:, :], f8[:, :], b1), cache=True)
def portfolio_from_signals_np(ts, investment, slippage, commission, entries, exits, volume, accumulate):
    """Calculate portfolio value using signals."""
    return portfolio_np(ts, investment, slippage, commission, signals_order_func_np, entries, exits, volume, accumulate)


@njit(UniTuple(f8[:, :], 2)(f8[:, :], f8, f8[:, :], f8[:, :], f8[:, :], b1), cache=True)
def portfolio_from_orders_np(ts, investment, slippage, commission, orders, is_target):
    """Calculate portfolio value using orders."""
    return portfolio_np(ts, investment, slippage, commission, orders_order_func_np, orders, is_target)


@njit(b1(f8[:, :]), cache=True)
def detect_order_accumulation_nb(trades):
    """Detect accumulation of orders, that is, position is being increased/decreased gradually.

    When it happens, it's not easy to calculate P/L of a position anymore."""
    for col in range(trades.shape[1]):
        entry_i = -1
        position = False
        for i in range(trades.shape[0]):
            if trades[i, col] > 0:
                if position:
                    return True
                entry_i = i
                position = True
            elif trades[i, col] < 0:
                if not position:
                    return True
                if trades[entry_i, col] != abs(trades[i, col]):
                    return True
                position = False
    return False


@njit
def apply_on_positions(trades, apply_func, *args):
    """Apply a function on each position."""
    if detect_order_accumulation_nb(trades):
        raise ValueError("Order accumulation detected. Cannot calculate performance per position.")
    out = np.full_like(trades, np.nan)

    for col in range(trades.shape[1]):
        entry_i = -1
        position = False
        for i in range(trades.shape[0]):
            if position and trades[i, col] < 0:
                out[i, col] = apply_func(entry_i, i, col, trades, *args)
                position = False
            elif not position and trades[i, col] > 0:
                entry_i = i
                position = True
            if position and i == trades.shape[0] - 1: # unrealized
                out[i, col] = apply_func(entry_i, i, col, trades, *args)
    return out


_profits_nb = njit(lambda entry_i, exit_i, col, trades, equity: equity[exit_i, col] - equity[entry_i, col])
_returns_nb = njit(lambda entry_i, exit_i, col, trades, equity: equity[exit_i, col] / equity[entry_i, col] - 1)


@njit(f8[:, :](f8[:, :], f8[:, :]), cache=True)
def position_profits_nb(trades, equity):
    """Calculate P/L per position."""
    return apply_on_positions(trades, _profits_nb, equity)


@njit(f8[:, :](f8[:, :], f8[:, :]), cache=True)
def position_returns_nb(trades, equity):
    """Calculate returns per trade."""
    return apply_on_positions(trades, _returns_nb, equity)


@njit
def reduce_on_mask_nb(a, func_nb, mask):
    """Perform reducing operation on mask."""
    b = np.full(a.shape[1], np.nan)
    for col in range(a.shape[1]):
        if mask[:, col].any():
            b[col] = func_nb(a[:, col][mask[:, col]])
    return b


_sum_1d_nb = njit(lambda x: np.sum(x))
_mean_1d_nb = njit(lambda x: np.mean(x))


@njit(f8[:](f8[:, :], b1[:, :]), cache=True)
def sum_on_mask_nb(a, mask):
    """Sum of values at mask."""
    return reduce_on_mask_nb(a, _sum_1d_nb, mask)


@njit(f8[:](f8[:, :], b1[:, :]), cache=True)
def mean_on_mask_nb(a, mask):
    """Mean of values at mask."""
    return reduce_on_mask_nb(a, _mean_1d_nb, mask)

# ############# TimeSeries subclasses ############# #


class TradeSeries(TimeSeries):
    """TradeSeries holds the number of shares bought/sold at each time step."""

    @to_2d('self')
    def detect_order_accumulation(self):
        """Detect accumulation of orders."""
        return detect_order_accumulation_nb(self)

    @to_2d('self')
    @to_2d('ts')
    @broadcast('self', 'ts')
    @has_type('ts', TimeSeries)
    @have_same_shape('self', 'index', along_axis=0)
    def plot(self,
             ts,
             column=None,
             index=None,
             buy_scatter_kwargs={},
             sell_scatter_kwargs={},
             fig=None,
             **ts_kwargs):
        if column is None:
            if self.shape[1] == 1:
                column = 0
            else:
                raise ValueError("For an array with multiple columns, you must pass a column index")
        ts = ts[:, column]
        trades = self[:, column]
        sell_mask = trades < 0
        buy_mask = trades > 0
        if index is None:
            index = np.arange(trades.shape[0])

        # Plot TimeSeries
        fig = ts.plot(index=index, fig=fig, **ts_kwargs)

        # Plot markets
        buy_scatter = go.Scatter(
            x=index[buy_mask],
            y=ts[buy_mask],
            customdata=trades[buy_mask],
            hovertemplate='(%{x}, %{y})<br>%{customdata:.6g}',
            mode='markers',
            marker=dict(
                symbol='triangle-up',
                color='limegreen',
                size=10
            ),
            name='Buy'
        )
        buy_scatter.update(**buy_scatter_kwargs)
        fig.add_trace(buy_scatter)
        sell_scatter = go.Scatter(
            x=index[sell_mask],
            y=ts[sell_mask],
            customdata=trades[sell_mask],
            hovertemplate='(%{x}, %{y})<br>%{customdata:.6g}',
            mode='markers',
            marker=dict(
                symbol='triangle-down',
                color='orangered',
                size=10
            ),
            name='Sell'
        )
        sell_scatter.update(**sell_scatter_kwargs)
        fig.add_trace(sell_scatter)

        return fig


class TradePLSeries(TimeSeries):
    """TradePLSeries holds the profit/loss at each position end, everything else is NaN."""

    @cached_property
    def win_mask(self):
        return np.asarray(np.greater(self, 0, where=~np.isnan(self)) & ~np.isnan(self))

    @cached_property
    def loss_mask(self):
        return np.asarray(np.less(self, 0, where=~np.isnan(self)) & ~np.isnan(self))

    @cached_property
    def position_mask(self):
        return np.asarray(~np.isnan(self))

    @cached_property
    def sum_win(self):
        """Sum of wins."""
        sum_win = np.asarray(sum_on_mask_nb(self, self.win_mask))
        sum_win[np.isnan(sum_win) & self.position_mask.any(axis=0)] = 0. # nan only if no positions
        return sum_win

    @cached_property
    def sum_loss(self):
        """Sum of losses (always positive)."""
        sum_loss = np.asarray(np.abs(sum_on_mask_nb(self, self.loss_mask)))
        sum_loss[np.isnan(sum_loss) & self.position_mask.any(axis=0)] = 0.
        return sum_loss

    @cached_property
    def avg_win(self):
        """Average win."""
        avg_win = np.asarray(mean_on_mask_nb(self, self.win_mask))
        avg_win[np.isnan(avg_win) & self.position_mask.any(axis=0)] = 0.
        return avg_win

    @cached_property
    def avg_loss(self):
        """Average loss (always positive)."""
        avg_loss = np.asarray(np.abs(mean_on_mask_nb(self, self.loss_mask)))
        avg_loss[np.isnan(avg_loss) & self.position_mask.any(axis=0)] = 0.
        return avg_loss

    @cached_property
    def win_prob(self):
        """Fraction of wins."""
        return np.asarray(np.sum(self.win_mask, axis=0) / np.sum(self.position_mask, axis=0))

    @cached_property
    def loss_prob(self):
        """Fraction of losses."""
        return np.asarray(np.sum(self.loss_mask, axis=0) / np.sum(self.position_mask, axis=0))

    @have_same_shape('self', 'index', along_axis=0)
    def plot(self,
             column=None,
             index=None,
             profit_scatter_kwargs={},
             loss_scatter_kwargs={},
             fig=None,
             **layout_kwargs):
        if column is None:
            if self.shape[1] == 1:
                column = 0
            else:
                raise ValueError("For an array with multiple columns, you must pass a column index")
        position_profits = self[:, column]
        profits = position_profits.copy()
        profits[position_profits <= 0] = np.nan
        losses = position_profits.copy()
        losses[position_profits >= 0] = np.nan
        if index is None:
            index = np.arange(position_profits.shape[0])

        # Set up figure
        if fig is None:
            fig = FigureWidget()
            fig.update_layout(showlegend=True)
            fig.update_layout(**layout_kwargs)

        # Plot markets
        profit_scatter = go.Scatter(
            x=index,
            y=profits,
            mode='markers',
            marker=dict(
                symbol='circle',
                color='green',
                size=10
            ),
            name='Profit'
        )
        profit_scatter.update(**profit_scatter_kwargs)
        fig.add_trace(profit_scatter)
        loss_scatter = go.Scatter(
            x=index,
            y=losses,
            mode='markers',
            marker=dict(
                symbol='circle',
                color='red',
                size=10
            ),
            name='Loss'
        )
        loss_scatter.update(**loss_scatter_kwargs)
        fig.add_trace(loss_scatter)

        # Set up axes
        maxval = np.nanmax(np.abs(position_profits))
        space = 0.1 * 2 * maxval
        fig.update_layout(
            yaxis=dict(
                range=[-(maxval+space), maxval+space]
            ),
            shapes=[dict(
                type="line",
                xref="paper",
                yref='y',
                x0=0, x1=1, y0=0, y1=0,
                line=dict(
                    color="grey",
                    width=2,
                    dash="dot",
                ))]
        )

        return fig

# ############# Main class ############# #


class Portfolio():

    @has_type('ts', TimeSeries)
    @to_type('cash', TimeSeries)
    @to_type('shares', TimeSeries)
    @to_2d('ts')
    @have_same_shape('ts', 'cash')
    @have_same_shape('ts', 'shares')
    @have_same_shape('ts', 'slippage')
    @have_same_shape('ts', 'commission')
    def __init__(self, ts, cash, shares, investment, slippage, commission):
        self.ts = ts
        self.cash = cash
        self.shares = shares
        self.investment = investment
        self.slippage = slippage
        self.commission = commission

    # ############# Class methods ############# #

    @classmethod
    @has_type('ts', TimeSeries)
    @has_type('entries', Signals)
    @has_type('exits', Signals)
    @to_2d('ts')
    @to_2d('entries')
    @to_2d('exits')
    @broadcast('ts', 'entries', 'exits', 'volume', 'slippage', 'commission')
    @to_dtype('volume', np.float64)
    @to_type('investment', float)
    @to_dtype('slippage', np.float64)
    @to_dtype('commission', np.float64)
    def from_signals(cls, ts, entries, exits, volume=np.inf, accumulate=False, investment=1., slippage=0., commission=0.):
        """Build portfolio based on entry and exit signals and the corresponding volume.

        Set volume to the number of shares to buy/sell.
        Set volume to np.inf to buy/sell everything.
        Set accumulate to False to avoid producing new orders if already in the market."""
        cash, shares = portfolio_from_signals_np(
            ts, investment, slippage, commission, entries, exits, volume, accumulate)
        return cls(ts, cash, shares, investment, slippage, commission)

    @classmethod
    @has_type('ts', TimeSeries)
    @to_2d('ts')
    @broadcast('ts', 'orders', 'slippage', 'commission')
    @to_dtype('orders', np.float64)
    @to_type('investment', float)
    @to_dtype('slippage', np.float64)
    @to_dtype('commission', np.float64)
    def from_orders(cls, ts, orders, is_target=False, investment=1., slippage=0., commission=0.):
        """Build portfolio based on orders.

        Set an orders element to positive/negative number - a number of shares to buy/sell.
        Set is_target to True to specify the target amount of shares to hold."""
        cash, shares = portfolio_from_orders_np(ts, investment, slippage, commission, orders, is_target)
        return cls(ts, cash, shares, investment, slippage, commission)

    @classmethod
    @has_type('ts', TimeSeries)
    @to_2d('ts')
    @broadcast('ts', 'slippage', 'commission')
    @to_type('investment', float)
    @to_dtype('slippage', np.float64)
    @to_dtype('commission', np.float64)
    def from_order_func(cls, ts, order_func_np, *args, investment=1., slippage=0., commission=0.):
        cash, shares = portfolio_np(ts, investment, slippage, commission, order_func_np, *args)
        return cls(ts, cash, shares, investment, slippage, commission)

    # ############# TimeSeries properties ############# #

    @cached_property
    def equity(self):
        return TimeSeries(self.cash + self.shares * self.ts)

    @cached_property
    def equity_in_shares(self):
        return TimeSeries(self.equity / self.ts)

    @cached_property
    def returns(self):
        return TimeSeries(pct_change_nb(self.equity))

    @cached_property
    def drawdown(self):
        return TimeSeries(1 - self.equity / expanding_max_nb(self.equity))

    @cached_property
    def trades(self):
        trades = fillna_nb(diff_nb(self.shares), 0)
        trades[0, :] = self.shares[0, :]
        return TradeSeries(trades)

    @cached_property
    def position_profits(self):
        return TradePLSeries(position_profits_nb(self.trades, self.equity))

    @cached_property
    def position_returns(self):
        return TradePLSeries(position_returns_nb(self.trades, self.equity))

    # ############# Performance properties ############# #

    @cached_property
    def profit_factor(self):
        return np.asarray(self.position_profits.sum_win / self.position_profits.sum_loss)

    @cached_property
    def appt(self):
        """Average profitability per trade (APPT)

        For every trade you place, you are likely to win/lose this amount.
        What matters is that your APPT comes up positive."""
        return np.asarray(self.position_profits.win_prob * self.position_profits.avg_win
                          - self.position_profits.loss_prob * self.position_profits.avg_loss)

    @cached_property
    def total_net_profit(self):
        return np.asarray(self.equity[-1, :] - self.investment)

    @cached_property
    def total_return(self):
        return np.asarray(self.total_net_profit / self.investment)

    @cached_property
    def mdd(self):
        """A maximum drawdown (MDD) is the maximum observed loss from a peak 
        to a trough of a portfolio, before a new peak is attained."""
        return np.asarray(np.max(self.drawdown, axis=0))

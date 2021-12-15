#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Gym trading env
Unlike live engine or backtest engine, where event loops are driven by live ticks or historical ticks,
here it is driven by step function, similar to
https://github.com/openai/gym/blob/master/gym/envs/classic_control/cartpole.py
The sequence is
1. obs <- env.reset()      # env; obs = OHLCV + technical indicators + holdings
2. action <- pi(obs)       # agent; action = target weights
3. news_obs, reward, <- step(action)      # env
    3.a action to orders   # sum(holdings) * new weights
    3.b fill orders        # portfolio rotates into new holdings
repeat 2, and 3 to interact between agent and env
"""
import numpy as np
from numpy.lib.npyio import load
import pandas as pd
import gym


class PortfolioWeightsBox(gym.spaces.Box):
    """
    gym Box with constraints on weights across cash+n_assets
    w >= 0, sum(w) = 1, no short sell allowed
    """
    def __init__(self, low, high, shape : np.int32=1, dtype=np.float32, seed=None) -> None:
        assert shape >= 1
        self.n_assets = shape+1     # add cash
        super().__init__(low, high, (self.n_assets,), dtype, seed)
    
    def sample(self) -> np.array:
        return np.random.dirichlet(alpha=self.n_assets * [1])
    
    def contains(self, x) -> bool:
        if len(x) != self.n_assets:
            return False
        if not np.testing.assert_allclose(sum(x), 1.0):
            return False
        return True


class TradingEnv(gym.Env):
    """
    Description:
        backtest gym engine
        it doesn't normalize; and expects a normalization layer
    Observation:
        Type: Box(lookback_window, n_assets*5+2)
        lookback_window x (n_assets*(ohlcv) + cash+npv)
        TODO: append trades, commissions, standing orders, etc
        TODO: stop/limit orders
    Actions:
        Type: Box(n_assets + 1)
        portfolio weights [w1,w2...w_k, cash_weight], add up to one
    Reward:
        pnl every day, similar to space-invaders
    Starting State:
        random timestamp between start_date and (end_date - run_window)
    Episode Termination:
        after predefined window
        If broke, no orders will send
    """
    def __init__(self, df_obs_scaled : pd.DataFrame, df_exch : pd.DataFrame):
        assert df_obs_scaled.shape[0] == df_exch.shape[0]

        self._df_obs_scaled = df_obs_scaled     # observation; scaled outside along with TA indicators
        self._df_exch = df_exch                 # exch
        self._df_positions = df_exch * 0.0      # positions plus cash plus nav
        self._df_positions['Cash'] = 0.0
        self._df_positions['NAV'] = 0.0

        self._asset_syms = df_exch.columns
        self._n_assets = len(self._asset_syms)
        self._n_features = df_obs_scaled.shape[1] / self._n_assets      # assume same features across assets

        self._inital_cash = 100_000.0
        self._cash = self._inital_cash
        self._commission_rate = 0.0
        self._look_back = 10                        # observation lookback history
        self._warmup = 50                           # observation ramp-up due to e.g. 10 periods are required to cacl MA(10)
        self._maxsteps = 252                        # max steps in one episode

        self._max_price_scaler = 1.0
        self._max_volume_scaler = 1.0
        self._max_nav_scaler = 1.0
        self._max_position_scaler = 1.0
        
        self._init_step = 0
        self._current_step = 0

        # action_space = 0 ~ 100%; buy or sell up to TARGET percentage of nav
        self.action_space = PortfolioWeightsBox(low=0.0, high=1.0, shape=self._n_assets, dtype=np.float32)
        # first col is open, second col is high, ..., last col is nav
        self.observation_space = gym.spaces.Box(low=-np.inf, high=np.inf, shape=(self._look_back, df_obs_scaled.shape[1]+1), dtype=np.float32)
                    
    def set_cash(self, cash: np.float32=100_000.0):
        self._inital_cash = cash
        self._cash = self._inital_cash

    def set_commission(self, comm: np.float32=0.0001):
        """
        commission plus slippage
        """
        self._commission_rate = comm

    def set_steps(self, n_lookback : np.int32=10, n_warmup : np.int32=50, n_maxsteps: np.int32=252):
        self._lookback = n_lookback
        self._warmup = n_warmup
        self._maxsteps = n_maxsteps

    def set_feature_scaling(self, max_price_scaler : np.float32=1.0, max_volume_scaler : np.float32=1.0, max_nav_scaler : np.float32=1.0, max_position_scaler : np.float32=1.0):
        self._max_price_scaler = max_price_scaler
        self._max_volume_scaler = max_volume_scaler
        self._max_nav_scaler = max_nav_scaler
        self._max_position_scaler = max_position_scaler

    def _get_observation(self):
        """
        return an array of size self._lookback x features.
        Each column is a feature; last feature is NAV.
        Row is in time ascending order. That is, last row is self._current_step.
        """
        obs = self._df_obs_scaled.iloc[self._current_step-self._look_back+1:self._current_step+1].values
        obs = np.append(obs, self._df_positions.iloc[self._current_step-self._look_back+1:self._current_step+1][['NAV']].values, axis=1)

        return obs

    def step(self, action):
        """
        move one step to the next timestamp, accordingly to action
        assume hft condition: execution at today 15:59:59, after observing today's ohl and (almost) close.
        execution immediately using market or market on close, no slippage.
        e.g., assume on 12/31/2019, 1/2/2020, and 1/3/2020 prices are $95, $100, $110. respectively. 
        The state or observation is prices of last two days. 
        We start on 1/2/2020 with $100,000.
        Then
        1. on 1/2/2020, obs = [$95, $100]         # obs <- env.reset()
        2. on 1/2/2020, based on the up-trend observation, we decide to buy. 
            Our allocation action w is [50%, 50%], or half in cash, half in stock.
        3. on 1/2/2020, the step function is      # news_obs, reward <- step(action)
            3.a buy order of $5,000 or 50 shares, filled at $100
            3.b stock market environment transits to 1/3/2020, new observation is [$100, $110];
                our stock is now worth $5,500, and total asset NAV is $10,500, or reward $500.
        :param action:
        :return:
        """
        done = False

        # de-scale
        current_size = (self._df_positions.iloc[self._current_step][self._asset_syms] * self._max_position_scaler).astype(np.int32)
        current_cash = self._df_positions.iloc[self._current_step]['Cash'] * self._max_nav_scaler
        current_price = self._df_exch.iloc[self._current_step]

        # rebalance
        current_nav = current_cash + (current_price * current_size).sum()       # should equal to the nav column
        new_size = (np.floor(current_nav * action[:-1] / current_price)).astype(np.int32)       # odd size allowed; action[-1] is cash
        delta_size = new_size - current_size
        current_commission = (np.abs(delta_size) * current_price * self._commission_rate).sum()
        new_cash = current_cash - (delta_size * current_price).sum() - current_commission

        # move to next timestep
        self._current_step += 1
        new_price = self._df_exch.iloc[self._current_step]
        new_nav = new_cash + (new_price * new_size).sum()
        reward = ((new_price - current_price) * new_size).sum() - current_commission     # commission is penalty
        info = {'step': self._current_step, 'time': self._df_obs_scaled.index[self._current_step],
                'old_price': current_price.to_dict(), 'old position': current_size.to_dict(), 'old_cash': current_cash, 'old_nav': current_nav,
                'price': new_price.to_dict(), 'position': new_size.to_dict(), 'cash': new_cash, 'nav': new_nav,
                'transaction': delta_size.to_dict(), 'commission': current_commission, 'nav_diff': new_nav-current_nav}     # reward = new_nav - current_nav

        # scale back
        reward = reward / self._max_nav_scaler
        self._df_positions.loc[self._df_positions.index[self._current_step], self._asset_syms] = new_size / self._max_position_scaler
        self._df_positions['Cash'][self._current_step] = new_cash / self._max_nav_scaler
        self._df_positions['NAV'][self._current_step] = new_nav / self._max_nav_scaler

        if self._current_step - self._init_step >= self._maxsteps:      # e.g. init=3, current=7, _maxsteps=4
            done = True
            self._current_step = self._warmup - 1
            self._init_step = self._warmup - 1

        # s'
        new_state = self._get_observation()

        return new_state, reward, done, info

    def reset(self):
        """
        random start time
        """
        self._cash = self._inital_cash / self._max_nav_scaler
        self._df_positions = df_exch * 0.0
        self._df_positions['Cash'] = 0.0
        self._df_positions['NAV'] = 0.0
        self._current_step = np.random.randint(low=self._warmup-1, high=self._df_obs_scaled.shape[0]-self._maxsteps)    # low (inclusive) to high (exclusive)
        self._init_step = self._current_step
        self._df_positions['Cash'][:self._current_step+1] = self._cash
        self._df_positions['NAV'][:self._current_step+1] = self._cash

        # return current_step
        return self._get_observation()

    def render(self, mode='human'):
        pass

    def play(self):
        """
        Matplotlib animation
        """
        pass

    def close(self):
        pass


def load_data(max_price_scaler: np.float32=1.0, max_volume_scaler: np.float32=1.0):
    from datetime import timedelta
    import ta

    sd = '2015'
    ed = '2020'
    syms = ['SPY', 'AAPL']
    df_obs = pd.DataFrame()             # observation
    df_exch = pd.DataFrame()            # exchange; for order match

    for sym in syms:
        df = pd.read_csv(f'../data/{sym}.csv', index_col=0)
        df.index = pd.to_datetime(df.index) + timedelta(hours=15, minutes=59, seconds=59)
        df = df[sd:ed]

        df_exch = pd.concat([df_exch, df['Close'].rename(sym)], axis=1)

        df['Open'] = df['Adj Close'] / df['Close'] * df['Open'] / max_price_scaler
        df['High'] = df['Adj Close'] / df['Close'] * df['High'] / max_price_scaler
        df['Low'] = df['Adj Close'] / df['Close'] * df['Low'] / max_price_scaler
        df['Volume'] = df['Adj Close'] / df['Close'] * df['Volume'] / max_volume_scaler
        df['Close'] = df['Adj Close'] / max_volume_scaler
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
        df.columns = [f'{sym}:{c.lower()}' for c in df.columns]

        macd = ta.trend.MACD(close=df[f'{sym}:close'])
        df[f'{sym}:macd'] = macd.macd()
        df[f'{sym}:macd_diff'] = macd.macd_diff()
        df[f'{sym}:macd_signal'] = macd.macd_signal()

        rsi = ta.momentum.RSIIndicator(close=df[f'{sym}:close'])
        df[f'{sym}:rsi'] = rsi.rsi()

        bb = ta.volatility.BollingerBands(close=df[f'{sym}:close'], window=20, window_dev=2)
        df[f'{sym}:bb_bbm'] = bb.bollinger_mavg()
        df[f'{sym}:bb_bbh'] = bb.bollinger_hband()
        df[f'{sym}:bb_bbl'] = bb.bollinger_lband()

        atr = ta.volatility.AverageTrueRange(high=df[f'{sym}:high'], low=df[f'{sym}:low'], close=df[f'{sym}:close'])
        df[f'{sym}:atr'] = atr.average_true_range()

        df_obs = pd.concat([df_obs, df], axis=1)

    return df_obs, df_exch


if __name__ == '__main__':
    look_back = 10
    cash = 100_000.0
    max_price_scaler = 5_000.0
    max_volume_scaler = 1.5e10
    max_nav_scaler = 5.0 * cash
    max_position_scaler = max_nav_scaler / 1_000.0

    df_obs, df_exch = load_data(max_price_scaler, max_volume_scaler)

    trading_env = TradingEnv(df_obs, df_exch)
    trading_env.set_cash(cash)
    trading_env.set_commission(0.0001)
    trading_env.set_steps(n_lookback=10, n_warmup=50, n_maxsteps=250)
    trading_env.set_feature_scaling(max_price_scaler, max_volume_scaler, max_nav_scaler, max_position_scaler)
    o1 = trading_env.reset()

    # trading_env._current_step = look_back-1        # ignore randomness
    while True:
        action = trading_env.action_space.sample()
        o2, reward, done, info = trading_env.step(action)
        print(action, reward * max_nav_scaler, info)
        if done:
            break
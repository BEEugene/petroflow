"""Implements core-to-log matching algorithm."""

from itertools import product
from collections import namedtuple

import multiprocess as mp
import numpy as np
from scipy.optimize import minimize
from scipy.interpolate import interp1d


Shift = namedtuple("Shift", ["depth_from", "depth_to", "sequence_delta", "interval_deltas", "loss"])


def trunc(x, n_decimals=0):
    """Return integer parts and `n_decimals` decimal places of values in `x`.

    Parameters
    ----------
    values : numpy.ndarray
        An array to truncate.
    n_decimals : non-negative int
        The number of decimal places to keep. Defauls to 0.

    Returns
    -------
    values : numpy.ndarray
        Truncated array.
    """
    return np.trunc(x * 10**n_decimals) / (10**n_decimals)


def select_contigious_intervals(df, max_gap=0):
    """Split a depth-ranged `DataFrame` into a list of `DataFrame`s with no
    more than `max_gap` gap in depth ranges in each.

    Parameters
    ----------
    df : pandas.DataFrame
        A `DataFrame` to split.
    max_gap : non-negative float
        Max gap in depth ranges in all resulting `DataFrame`s. Defauls to 0.

    Returns
    -------
    df_list : list of pandas.DataFrame
        Split `DataFrame`.
    """
    split_indices = np.where((df["DEPTH_FROM"] - df["DEPTH_TO"].shift()) > max_gap)[0]
    return np.split(df, split_indices)


def generate_init_deltas(bi_n_lith_ints, bi_gap_lengths, sequence_delta_from, sequence_delta_to, sequence_delta_step):
    """Generate initial deltas to start optimization from.

    First element of each delta array is a shift of the sequence. It takes all
    values from `sequence_delta_from` to `sequence_delta_to` with a step of
    `sequence_delta_step`.

    All other elements indicate gap sizes before each lithology interval of
    the sequence. They are initialized in three different ways:
    - All unrecovered core is located in the end of the boring interval
    - All unrecovered core is equally distributed between all lithology
      intervals
    - All unrecovered core is located in the beginning of the boring interval

    Finally, all combinations of sequence deltas and gaps are returned.

    Parameters
    ----------
    bi_n_lith_ints : list of int
        The number of lithology intervals in each boring interval of the
        sequence.
    bi_gap_lengths : list of float
        The length of unrecovered core from each boring interval of the
        sequence.
    sequence_delta_from : float
        Start of the grid of initial shifts in meters.
    sequence_delta_to : float
        End of the grid of initial shifts in meters.
    sequence_delta_step : float
        Step of the grid of initial shifts in meters.

    Returns
    -------
    deltas : list of numpy.ndarray
        A list of initial deltas.
    """
    interval_deltas = []
    for n, gap_length in zip(bi_n_lith_ints, bi_gap_lengths):
        interval_deltas.append([np.zeros(n),  # Unrecovered core in the end
                                np.full(n, gap_length / (n + 1)),  # Unrecovered core distributed equally
                                np.array([gap_length] + [0] * (n - 1))])  # Unrecovered core in the beginning
    interval_deltas = [np.concatenate(delta) for delta in zip(*interval_deltas)]
    segment_delta = np.arange(sequence_delta_from, sequence_delta_to, sequence_delta_step)
    return [np.concatenate([[d1], d2]) for d1, d2 in product(segment_delta, interval_deltas)]


def loss(deltas, bi_n_lith_ints, core_depths, log_interpolator, core_log):
    """Calculate optimization loss as negative correlation between well log
    and core log.

    Parameters
    ----------
    deltas : numpy.ndarray
        Boring sequence shift and gap sizes before each lithology interval of
        the sequence.
    bi_n_lith_ints : list of int
        The number of lithology intervals in each boring interval of the
        sequence.
    core_depths : list of numpy.ndarray
        Depths of core plugs for each lithology interval of the sequence.
    log_interpolator : scipy.interpolate.interp1d
        Well log interpolator.
    core_log : numpy.ndarray
        Core log values at corresponding `core_depths`.

    Returns
    -------
    loss : float
        Negative correlation between well log and core log.
    """
    segment_delta = deltas[0]
    interval_deltas = np.concatenate([np.cumsum(d) for d in np.split(deltas[1:], np.cumsum(bi_n_lith_ints)[:-1])])
    interval_deltas += segment_delta
    shifted_depths = []
    for depths, _deltas in zip(core_depths, interval_deltas):
        shifted_depths.append(depths + _deltas)
    shifted_depths = np.concatenate(shifted_depths)
    well_log = np.nan_to_num(log_interpolator(shifted_depths))
    # TODO: find out why NaNs appear
    return -np.corrcoef(well_log, core_log)[0, 1]


def match_boring_sequence(boring_sequence, lithology_intervals, well_log, core_log, max_shift,
                          delta_from, delta_to, delta_step, max_iter, timeout):
    """Perform core-to-log matching of a boring sequence by shifting core
    samples in order to maximize correlation between well and core logs.

    The function generates a grid of initial guesses and runs optimization
    procedure from each grid node.

    Parameters
    ----------
    boring_sequence : pandas.DataFrame
        Boring sequence to match. Contains depth ranges of all boring
        intervals in the sequence and their core recoveries.
    lithology_intervals : pandas.DataFrame
        Ranges of lithology intervals.
    well_log : pandas.Series
        Well log to use for matching.
    core_log : pandas.Series
        Core log or property to use for matching.
    max_shift : positive float
        Maximum shift of a boring sequence in meters.
    delta_from : float
        Start of the grid of initial shifts in meters.
    delta_to : float
        End of the grid of initial shifts in meters.
    delta_step : float
        Step of the grid of initial shifts in meters.
    max_iter : positive int
        Maximum number of SLSQP iterations.
    timeout : positive float
        Maximum time for an optimization run from each initial guess in
        seconds.

    Returns
    -------
    shifts : list of Shift
        Shift object for each initial guess, containing final loss and deltas.
    """
    well_depth_from = well_log.index.min()
    well_depth_to = well_log.index.max()
    well_log = well_log.dropna()
    log_interpolator = interp1d(well_log.index, well_log, kind="linear", fill_value="extrapolate")

    bi_n_lith_ints = []
    bi_gap_lengths = []

    core_depths = []
    core_logs = []

    boring_sequence = boring_sequence[["DEPTH_FROM", "DEPTH_TO", "CORE_RECOVERY"]]
    sequence_depth_from = boring_sequence["DEPTH_FROM"].min()
    sequence_depth_to = boring_sequence["DEPTH_TO"].max()

    for _, (bi_depth_from, bi_depth_to, recovery) in boring_sequence.iterrows():
        mask = ((lithology_intervals["DEPTH_FROM"] >= bi_depth_from) &
                (lithology_intervals["DEPTH_TO"] <= bi_depth_to))
        sequence_lithology_intervals = lithology_intervals[mask]
        bi_n_lith_ints.append(len(sequence_lithology_intervals))
        bi_gap_lengths.append(max(0, bi_depth_to - bi_depth_from - recovery))

        for _, (li_depth_from, li_depth_to) in sequence_lithology_intervals.iterrows():
            log_slice = core_log[li_depth_from:li_depth_to]
            core_depths.append(log_slice.index.values)
            core_logs.append(log_slice.values)

    core_logs = np.concatenate(core_logs)

    # Optimization constraints
    constraints = []

    starts = np.cumsum([0] + bi_n_lith_ints) + 1
    for start, end, gap_length in zip(starts[:-1], starts[1:], bi_gap_lengths):
        def con_gap_length(x, start=start, end=end, gap_length=gap_length):
            return gap_length - x[start:end].sum()
        constraints.append({"type": "ineq", "fun": con_gap_length})

    for i in range(sum(bi_n_lith_ints)):
        def con_non_negative_gap(x, i=i):
            return x[i + 1]
        constraints.append({"type": "ineq", "fun": con_non_negative_gap})

    max_shift_up = min(max_shift, max(0, sequence_depth_from - well_depth_from))
    def con_max_shift_up(x):
        return x[0] + max_shift_up
    constraints.append({"type": "ineq", "fun": con_max_shift_up})

    max_shift_down = min(max_shift, max(0, well_depth_to - sequence_depth_to))
    def con_max_shift_down(x):
        return max_shift_down - x[0]
    constraints.append({"type": "ineq", "fun": con_max_shift_down})

    # Optimization
    zero_deltas = np.zeros(np.sum(bi_n_lith_ints) + 1)
    zero_shift_loss = loss(zero_deltas, bi_n_lith_ints, core_depths, log_interpolator, core_logs)
    zero_shift = Shift(sequence_depth_from, sequence_depth_to, 0, zero_deltas[1:], zero_shift_loss)
    shifts = [zero_shift]

    futures = []
    init_deltas = generate_init_deltas(bi_n_lith_ints, bi_gap_lengths, delta_from, delta_to, delta_step)
    with mp.Pool() as pool: #pylint: disable=not-callable
        for init_delta in init_deltas:
            args = (loss, init_delta)
            kwargs = {
                "args": (bi_n_lith_ints, core_depths, log_interpolator, core_logs),
                "method": "SLSQP",
                "options": {"maxiter": max_iter, "ftol": 1e-3},
                "constraints": constraints,
            }
            futures.append(pool.apply_async(minimize, args=args, kwds=kwargs))

        for future, init_delta in zip(futures, init_deltas):
            try:
                res = future.get(timeout=timeout)
                future_deltas = res.x
                future_loss = res.fun
            except mp.TimeoutError:
                future_deltas = init_delta
                future_loss = loss(future_deltas, bi_n_lith_ints, core_depths, log_interpolator, core_logs)

            sequence_delta = trunc(future_deltas[0], 2)
            interval_deltas = np.clip(trunc(future_deltas[1:], 2), 0, None)
            interval_deltas = [np.cumsum(d) for d in np.split(interval_deltas, np.cumsum(bi_n_lith_ints)[:-1])]
            interval_deltas = np.concatenate(interval_deltas) + sequence_delta

            shift = Shift(sequence_depth_from + sequence_delta, sequence_depth_to + sequence_delta,
                          sequence_delta, interval_deltas, future_loss)
            shifts.append(shift)
    return shifts

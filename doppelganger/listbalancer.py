from __future__ import (
    absolute_import, division, print_function, unicode_literals
)

import logging

import cvxpy as cvx
import numpy as np


# Constants for Newton-Raphson list balancer
MAX_GAP = 0.0000001
MAX_ITERATIONS = 10000


def balance_cvx(hh_table, A, w, mu=None, verbose_solver=False):
    """Maximum Entropy allocaion method for a single unit

    Args:
        hh_table (numpy matrix): Table of households categorical data
        A (numpy matrix): Area marginals (controls)
        w (numpy array): Initial household allocation weights
        mu (numpy array): Importance weights of marginals fit accuracy
        verbose_solver (boolean): Provide detailed solver info

    Returns:
        (numpy matrix, numpy matrix): Household weights, relaxation factors
    """

    n_samples, n_controls = hh_table.shape
    x = cvx.Variable(n_samples)

    if mu is None:
        objective = cvx.Maximize(
            cvx.sum_entries(cvx.entr(x) + cvx.mul_elemwise(cvx.log(w.T), x))
        )

        constraints = [
            x >= 0,
            x.T * hh_table == A,
        ]
        prob = cvx.Problem(objective, constraints)
        prob.solve(solver=cvx.SCS, verbose=verbose_solver)

        return x.value

    else:
        # With relaxation factors
        z = cvx.Variable(n_controls)

        objective = cvx.Maximize(
            cvx.sum_entries(cvx.entr(x) + cvx.mul_elemwise(cvx.log(w.T), x)) +
            cvx.sum_entries(mu * (cvx.entr(z)))
        )

        constraints = [
            x >= 0,
            z >= 0,
            x.T * hh_table == cvx.mul_elemwise(A, z.T),
        ]
        prob = cvx.Problem(objective, constraints)
        prob.solve(solver=cvx.SCS, verbose=verbose_solver)

        return x.value, z.value


def balance_multi_cvx(
    hh_table, A, B, w, mu=1000., meta_mu=1000., verbose_solver=False
):
    """Maximum Entropy allocaion method for multiple balanced units

    Args:
        hh_table (numpy matrix): Table of households categorical data
        A (numpy matrix): Area marginals (controls)
        B (numpy matrix): Meta-marginals
        w (numpy array): Initial household allocation weights
        mu (float): Importance weights of marginals for accuracy of fit
        meta_mu (float): Importance weights of meta-marginals for accuracy of
            fit
        verbose_solver (boolean): Provide detailed solver info

    Returns:
        (numpy matrix, numpy matrix, numpy matrix): Household weights,
            relaxation factors, relaxation factors,
    """

    n_samples, n_controls = hh_table.shape

    # Solver won't converge with zero marginals. Identify and remove.
    zero_marginals = np.where(~A.any(axis=1))[0]
    zero_weights = np.zeros((1, n_controls))

    if zero_marginals.size:
        logging.info(
            '{} tract(s) with zero marginals encountered. '
            'Setting weights to zero'.format(zero_marginals.size)
        )

        # Need to remove problem tracts and add a row of zeros later
        A = np.delete(A, zero_marginals, axis=0)
        w = np.delete(w, zero_marginals, axis=0)
        mu = np.delete(mu, zero_marginals, axis=1)

    n_tracts = w.shape[0]
    x = cvx.Variable(n_tracts, n_samples)

    # Relative weights of tracts
    # (need to reshape for numpy broadcasting)
    wa = (np.sum(A, axis=1) / np.sum(A)).reshape(-1, 1)
    w = (np.array(w) * np.array(wa))

    # With relaxation factors
    z = cvx.Variable(n_controls, n_tracts)
    q = cvx.Variable(n_controls)

    I = np.ones((n_tracts, 1))

    solved = False
    importance_weights_relaxed = False
    while not solved:
        objective = cvx.Maximize(
            cvx.sum_entries(
                cvx.entr(x) + cvx.mul_elemwise(cvx.log(np.e * w), x)
            ) +
            cvx.sum_entries(
                cvx.mul_elemwise(
                    mu, cvx.entr(z) + cvx.mul_elemwise(cvx.log(np.e), z)
                )
            ) +
            cvx.sum_entries(
                cvx.mul_elemwise(
                    meta_mu, cvx.entr(q) + cvx.mul_elemwise(cvx.log(np.e), q)
                )
            )
        )

        constraints = [
            x >= 0,
            z >= 0,
            q >= 0,
            x * hh_table == cvx.mul_elemwise(A, z.T),
            cvx.mul_elemwise(A.T, z) * I == cvx.mul_elemwise(B.T, q)
        ]

        prob = cvx.Problem(objective, constraints)
        prob.solve(verbose=verbose_solver)

        try:
            prob.solve(verbose=verbose_solver)
            solved = True

        except cvx.SolverError:
            if np.all(mu == 1):
                # We can't reduce mu any further
                break
            mu = np.where(mu > 10, mu - 10, 1)
            importance_weights_relaxed = True

    if importance_weights_relaxed:
        logging.info(
            'Solver error encountered. Importance weights have been relaxed.')

    if not np.any(x.value):
        logging.info(
            'Solution infeasible. Using initial weights.')

    # If we didn't get a value return the initial weights
    weights_out = x.value if np.any(x.value) else w
    zs_out = z.value
    qs_out = q.value

    # Insert zeros
    if zero_marginals.size:
        weights_out = np.insert(weights_out, zero_marginals, zero_weights, 0)
        zs_out = np.insert(z.value, zero_marginals, zero_weights.T, 1)

    return weights_out, zs_out, qs_out


def balance_newton(hh_table, A, w, mu):
    """Newton-Raphson list balancer for single unit

    Args:
        hh_table (numpy matrix): table of households categorical data
        A (numpy matrix): marginals (controls)
        w (numpy array): initial household allocation weights
        mu (numpy array) importance weights for controls

    Returns:
        (numpy matrix, numpy matrix, numpy matrix): Household weights,
            relaxation factors
    """

    n_samples, n_controls = hh_table.shape

    sample_weight_lower = w / 5
    sample_weight_upper = w * 5

    alpha = np.ones(n_controls)

    # Initial relaxation factors
    z = np.ones(n_controls)

    # Set current and previous weights
    cur_w = np.copy(w)
    prev_w = np.copy(w)

    for it in range(MAX_ITERATIONS):
        for index, marginal in np.ndenumerate(A):
            # Calculate balancing factor
            marginal_ind = index[1]
            control_weight = mu[0, marginal_ind]

            data_col = np.reshape(hh_table[:, marginal_ind], (n_samples, 1))

            x = np.dot(prev_w.T, data_col)
            y = np.dot(prev_w.T, np.power(data_col, 2))

            if x > 0.0:
                if marginal > 0.0:
                    numer = x - (A[0, marginal_ind] * z[marginal_ind])
                    denom = y + (
                        A[0, marginal_ind] * z[marginal_ind] *
                        (1 / control_weight)
                    )
                    alpha[marginal_ind] = 1 - (numer / denom)

                else:
                    alpha[marginal_ind] = 0.01
            else:
                alpha[marginal_ind] = 1.0

            # Update HH weights
            for sample_ind in range(n_samples):
                if hh_table[sample_ind, marginal_ind] > 0.0:

                    cur_w[sample_ind] = prev_w[sample_ind] * \
                        alpha[marginal_ind]**hh_table[sample_ind, marginal_ind]
                    cur_w[sample_ind] = max(
                        cur_w[sample_ind], sample_weight_lower[sample_ind]
                    )
                    cur_w[sample_ind] = min(
                        cur_w[sample_ind], sample_weight_upper[sample_ind]
                    )

            # Update relaxation factors
            z[marginal_ind] = z[marginal_ind] * (1 / alpha[marginal_ind])**(1 / control_weight)

        # Check for convergence and break
        weight_diff = np.sum(np.absolute(cur_w - prev_w)) / cur_w.size
        if weight_diff <= MAX_GAP:
            break

        prev_w = cur_w.copy()

    return cur_w, z


def discretize_multi_weights(hh_table, x, gamma=100., verbose_solver=False):
    """Discretize weights in household table for multiple tracts

    Arguments:
        hh_table (numpy matrix): Table of households categorical data
        x (numpy matrix): Household weights
        gamma (float): Relaxation weight
        verbose_solver (boolean): Provide detailed solver info

    Returns:
        numpy array: Discretized household weights
    """

    n_samples, n_controls = hh_table.shape
    n_tracts = x.shape[0]

    # Integerize x values
    x_int = x.astype(int)

    # Get residuals in new marginals from truncating to int
    A_residuals = np.dot(x, hh_table) - np.dot(x_int, hh_table)
    x_residuals = x - x_int

    # Coefficients in objective function
    x_log = np.log(x_residuals)

    # Decision variables for optimization
    y = cvx.Variable(n_tracts, n_samples)

    # Relaxation factors
    U = cvx.Variable(n_tracts, n_controls)
    V = cvx.Variable(n_tracts, n_controls)

    objective = cvx.Maximize(
        cvx.sum_entries(
            cvx.sum_entries(cvx.mul_elemwise(x_log, y), axis=1) -
            (gamma - 1) * cvx.sum_entries(U, axis=1) -
            (gamma - 1) * cvx.sum_entries(V, axis=1)
        )
    )

    constraints = [
        y * hh_table <= A_residuals + U,
        y * hh_table >= A_residuals - V,
        U >= 0,
        V >= 0,
        y >= 0,
        y <= 1.0
    ]

    prob = cvx.Problem(objective, constraints)
    prob.solve(verbose=verbose_solver)

    # Make results binary and return
    return np.array(y.value > 0.5).astype(int)

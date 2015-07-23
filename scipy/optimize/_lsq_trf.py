"""Trust Region Reflective algorithm for least-squares optimization.

The algorithm is based on ideas from paper [STIR]_. The main idea is to
account for presence of the bounds by appropriate scaling of the variables (or
equivalently changing a trust-region shape). Let's introduce a vector v:

           | ub[i] - x[i], if g[i] < 0 and ub[i] < np.inf
    v[i] = | x[i] - lb[i], if g[i] > 0 and lb[i] > -np.inf
           | 1,           otherwise

where g is the gradient of a cost function and lb, ub are the bounds. Its
components are distances to the bounds at which the anti-gradient points (if
this distance is finite). Define a scaling matrix D = diag(v**0.5).
First-order optimality conditions can be stated as

    D^2 g(x) = 0.

Meaning that components of the gradient should be zero for strictly interior
variables, and components must point inside the feasible region for variables
on the bound.

Now consider this system of equations as a new optimization problem. If the
point x is strictly interior (not on the bound) then the left-hand side is
differentiable and the Newton step for it satisfies

    (D^2 H + diag(g) Jv) p = -D^2 g

where H is the Hessian matrix (or its J^T J approximation in least squares),
Jv is the Jacobian matrix of v with components -1, 1 or 0, such that all
elements of matrix C = diag(g) Jv are non-negative. Introduce the change
of the variables x = D x_h (_h would be "hat" in LaTeX). In the new variables
we have a Newton step satisfying

    B_h p_h = -g_h,

where B_h = D H D + C, g_h = D g. In least squares B_h = J_h^T J_h, where
J_h = J D. Note that J_h and g_h are proper Jacobian and gradient with respect
to "hat" variables. To guarantee global convergence we formulate a
trust-region problem based on the Newton step in the new variables:

    0.5 * p_h^T B_h p + g_h^T p_h -> min, ||p_h|| <= Delta

In the original space B = H + D^{-1} C D^{-1}, and the equivalent trust-region
problem is

    0.5 * p^T B p + g^T p -> min, ||D^{-1} p|| <= Delta

Here the meaning of the matrix D becomes more clear: it alters the shape
of a trust-region, such that large steps towards the bounds are not allowed.
In the implementation the trust-region problem is solved in "hat" space,
but handling of the bounds is done in the original space (see below and read
the code).

The introduction of the matrix D doesn't allow to ignore bounds, the algorithm
must keep iterates strictly feasible (to satisfy aforementioned
differentiability), the parameter theta controls step back from the boundary
(see the code for details).

The algorithm does another important trick. If the trust-region solution
doesn't fit into the bounds, then a reflected (from a firstly encountered
bound) search direction is considered. For motivation and analysis refer to
[STIR]_ paper (and other papers of the authors). In practices it doesn't need
a lot of justifications, the algorithm simply chooses the best step among
three: a constrained trust-region step, a reflected step and a constrained
Cauchy step (a minimizer along -g_h in "hat" space, or -D^2 g in the original
space).

Another feature is that a trust-region radius control strategy is modified to
account for appearance of the diagonal C matrix (called diag_h in the code).

Note, that all described peculiarities are completely gone as we consider
problems without bounds (the algorithm becomes a standard trust-region type
algorithm very similar to ones implemented in MINPACK).

The implementation supports two methods of solving the trust-region problem.
The first, called 'exact', applies SVD on Jacobian and then solves the problem
very accurately using the algorithm described in [JJMore]_. It is not
applicable to large problem. The second, called 'lsmr', uses the 2-D subspace
approach (sometimes called "indefinite dogleg"), where the problem is solved
in a subspace spanned by the gradient and the approximate Gauss-Newton step
found by ``scipy.sparse.linalg.lsmr``. A 2-D trust-region problem is
reformulated as a 4-th order algebraic equation and solved very accurately by
``numpy.roots``. The subspace approach allows to solve very large problems
(up to couple of millions of residuals on a regular PC), provided the Jacobian
matrix is sufficiently sparse.

References
----------
.. [STIR] Branch, M.A., T.F. Coleman, and Y. Li, "A Subspace, Interior,
      and Conjugate Gradient Method for Large-Scale Bound-Constrained
      Minimization Problems," SIAM Journal on Scientific Computing,
      Vol. 21, Number 1, pp 1-23, 1999.
.. [JJMore] More, J. J., "The Levenberg-Marquardt Algorithm: Implementation
    and Theory," Numerical Analysis, ed. G. A. Watson, Lecture
"""

from __future__ import division

import numpy as np
from numpy.linalg import norm
from scipy.linalg import svd, qr
from scipy.sparse import issparse
from scipy.sparse.linalg import LinearOperator, aslinearoperator, lsmr

from .optimize import OptimizeResult
from ._lsq_common import (
    step_size_to_bound, make_strictly_feasible, find_active_constraints,
    scaling_vector, intersect_trust_region, solve_lsq_trust_region,
    solve_trust_region_2d, print_header, print_iteration)


def lsq_linear_operator(Jop, diag_root):
    m, n = Jop.shape

    def matvec(x):
        return np.hstack((Jop.matvec(x), diag_root * x))

    def rmatvec(x):
        x1 = x[:m]
        x2 = x[m:]
        return Jop.rmatvec(x1) + diag_root * x2

    return LinearOperator((m + n, n), matvec=matvec, rmatvec=rmatvec)


def post_multiplied_operator(Jop, d):
    def matvec(x):
        return Jop.matvec(x * d)

    def rmatvec(x):
        return d * Jop.rmatvec(x)

    return LinearOperator(Jop.shape, matvec=matvec, rmatvec=rmatvec)


def minimize_quadratic(a, b, lb, ub):
    """Minimize a 1-d quadratic function subject to bounds.

    The free term is omitted, that is we consider y = a * t**2 + b * t.

    Returns
    -------
    t : float
        Minimum point.
    y : float
        Minimum value.
    """
    t = np.array([lb, ub])
    if a != 0:
        extremum = -0.5 * b / a
        if lb <= extremum <= ub:
            t = np.hstack((t, extremum))
    y = a * t**2 + b * t
    i = np.argmin(y)
    return t[i], y[i]


def build_1d_quadratic_function(J, diag, g, s, s0=None):
    """Compute coefficients of a 1-d quadratic function for the line search
    from a multidimensional quadratic function.

    The function is given as follows:
    ::

        f(t) = 0.5 * (s0 + t*s).T * (J.T*J + diag) * (s0 + t*s) +
               g.T * (s0 + t*s)

    Parameters
    ----------
    J : ndarray, shape (m, n)
        Jacobian matrix, affect quadratic term.
    diag : ndarray, shape (n,)
        Addition diagonal part, affect quadratic term.
    g : ndarray, shape (n,)
        Gradient, defines a linear term.
    s : ndarray, shape (n,)
        Direction of search.
    s0 : None or ndarray with shape (n,), optional
        Initial point. If None, assumed to be 0.

    Returns
    -------
    a : float
        Coefficient for t**2.
    b : float
        Coefficient for t.

    Notes
    -----
    The free term "c" is not returned as it is not usually required.
    """
    v = J.dot(s)
    a = 0.5 * (np.dot(v, v) + np.dot(s * diag, s))
    b = np.dot(g, s)
    if s0 is not None:
        u = J.dot(s0)
        b += np.dot(u, v) + np.dot(s0 * diag, s)

    return a, b


def evaluate_quadratic_function(J, diag, g, steps):
    """Compute values of a quadratic function arising in least-squares.

    The function is 0.5 * s.T * (J.T * J + diag) * s + g.T * s.

    Parameters
    ----------
    J : ndarray, shape (m, n)
        Jacobian matrix, affect quadratic term.
    diag : ndarray, shape (n,)
        Addition diagonal part, affect quadratic term.
    g : ndarray, shape (n,)
        Gradient, defines a linear term.
    steps : ndarray, shape (k, n)
        Array containing k steps as rows.

    Returns
    -------
    values : ndarray, shape (k,)
        Array containing k values of the function.
    """
    if isinstance(J, LinearOperator):
        Js = np.empty((steps.shape[0], J.shape[0]))
        for i, s in enumerate(steps):
            Js[i] = J.matvec(s)
        Js = Js.T
    else:
        Js = J.dot(steps.T)

    return 0.5 * (np.sum(Js**2, axis=0) +
                  np.sum(diag * steps**2, axis=1)) + np.dot(steps, g)


def find_reflected_step(x, J_h, diag_h, g_h, p, p_h, d, Delta, l, u, theta):
    """Find a singly reflected step.

    Also corrects the initial step p_h. This function must be called only
    if x + p is not within the bounds.
    """
    # Use term "stride" for scalar step length.
    p_stride, hits = step_size_to_bound(x, p, l, u)

    # Compute the reflected direction.
    r_h = np.copy(p_h)
    r_h[hits.astype(bool)] *= -1
    r = d * r_h

    # Restrict trust-region step, such that it hits the bound.
    p *= p_stride
    p_h *= p_stride
    x_on_bound = x + p

    # Reflected direction will cross first either feasible region or trust
    # region boundary.
    _, to_tr = intersect_trust_region(p_h, r_h, Delta)
    to_bound, _ = step_size_to_bound(x_on_bound, r, l, u)
    to_bound *= theta  # Stay interior.

    r_stride_u = min(to_bound, to_tr)

    # We want a reflected step be at the same theta distance from the bound,
    # so we introduce a lower bound on the allowed stride.
    # The formula below is correct as p_h and r_h has the same norm.
    if r_stride_u > 0:
        r_stride_l = (1 - theta) * p_stride / r_stride_u
    else:
        r_stride_l = -1

    # Check if reflection step is available.
    if r_stride_l <= r_stride_u:
        a, b = build_1d_quadratic_function(J_h, diag_h, g_h, r_h, s0=p_h)
        r_stride, _ = minimize_quadratic(a, b, r_stride_l, r_stride_u)
        r_h = p_h + r_h * r_stride
    else:
        r_h = None

    # Now correct p_h to make it strictly interior.
    p_h *= theta

    # If no reflection step, just return p_h for convenience.
    if r_h is None:
        return p_h, p_h
    else:
        return p_h, r_h


def find_gradient_step(x, a, b, g_h, d, Delta, lb, ub, theta):
    """Find a minimizer of a quadratic model along the scaled gradient.

    a, b - coefficients of a quadratic model along g_h, should be
    precomputed.
    """
    to_bound, _ = step_size_to_bound(x, -g_h * d, lb, ub)
    to_bound *= theta

    to_tr = Delta / norm(g_h)
    g_stride = min(to_bound, to_tr)

    g_stride, _ = minimize_quadratic(a, b, 0.0, g_stride)

    return -g_stride * g_h


def trf(fun, jac, x0, f0, J0, lb, ub, ftol, xtol, gtol, max_nfev, scaling,
        tr_solver, tr_options, verbose):
    # Start with strictly feasible guess.
    x = make_strictly_feasible(x0, lb, ub, rstep=1e-10)

    f = f0
    nfev = 1

    J = J0
    njev = 1
    m, n = J.shape

    if isinstance(J, LinearOperator):
        g = J.rmatvec(f)
    else:
        g = J.T.dot(f)

    if scaling == 'jac':
        if issparse(J):
            scale = np.asarray(J.power(2).sum(axis=0)).ravel()**0.5
        else:
            scale = np.sum(J**2, axis=0)**0.5
        scale[scale == 0] = 1
    else:
        scale = scaling

    v, jv = scaling_vector(x, g, lb, ub)
    v[jv != 0] *= scale[jv != 0]
    Delta = norm(x0 * scale / v**0.5)
    if Delta == 0:
        Delta = 1.0

    f_augmented = np.zeros((m + n))
    if tr_solver == 'exact':
        J_augmented = np.empty((m + n, n))
    elif tr_solver == 'lsmr':
        reg_term = 0.0
        regularize = tr_options.pop("regularize", True)

    cost = 0.5 * np.dot(f, f)

    if max_nfev is None:
        max_nfev = x0.size * 100

    alpha = 0.0  # "Levenberg-Marquardt" parameter

    termination_status = None
    iteration = 0
    step_norm = None
    actual_reduction = None

    if verbose == 2:
        print_header()

    while nfev < max_nfev:
        if scaling == 'jac':
            if issparse(J):
                new_scale = np.asarray(J.power(2).sum(axis=0)).ravel()**0.5
            else:
                new_scale = np.sum(J**2, axis=0)**0.5
            scale = np.maximum(scale, new_scale)

        if isinstance(J, LinearOperator):
            g = J.rmatvec(f)
        else:
            g = J.T.dot(f)

        v, jv = scaling_vector(x, g, lb, ub)
        g_norm = norm(g * v, ord=np.inf)

        if verbose == 2:
            print_iteration(iteration, nfev, cost, actual_reduction,
                            step_norm, g_norm)

        if g_norm < gtol:
            termination_status = 1

        if termination_status is not None:
            active_mask = find_active_constraints(x, lb, ub, rtol=xtol)
            return OptimizeResult(
                x=x, fun=f, jac=J, cost=cost, optimality=g_norm,
                active_mask=active_mask, nfev=nfev, njev=njev,
                status=termination_status)

        # Now compute variables in "hat" space. Here we also account for
        # scaling introduced by `scaling` parameter. This part is a bit tricky,
        # you have to write down the formulas and see how the trust-region
        # problem is formulated when the two types of scaling are applied.
        # The idea is that first we apply `scaling` and then apply Coleman-Li
        # approach in the new variables.

        # v is recomputed in the variables after applying `scaling`, note that
        # components which were identically 1 not affected.
        v[jv != 0] *= scale[jv != 0]

        # Here we apply two types of scaling.
        d = v**0.5 / scale

        # C = diag(g / scale) Jv
        diag_h = g * jv / scale

        # After all this were done, we continue normally.

        # "hat" gradient.
        g_h = d * g

        # Right multiply J by diag(d), After this transformation Jacobian
        # is in "hat" space.
        if issparse(J):
            J.data *= d.take(J.indices, mode='clip')  # scikit-learn recipe.
        elif isinstance(J, LinearOperator):
            J = post_multiplied_operator(J, d)
        else:
            J *= d

        f_augmented[:m] = f
        if tr_solver == 'exact':
            J_augmented[:m] = J
            J_augmented[m:] = np.diag(diag_h**0.5)
            U, s, V = svd(J_augmented, full_matrices=False)
            V = V.T
            uf = U.T.dot(f_augmented)
        elif tr_solver == 'lsmr':
            if regularize:
                a, b = build_1d_quadratic_function(J, diag_h, g_h, -g_h)
                to_tr = Delta / norm(g_h)
                _, g_value = minimize_quadratic(a, b, 0, to_tr)
                reg_term = -g_value / Delta**2

            Jop = aslinearoperator(J)
            lsmr_op = lsq_linear_operator(Jop, (diag_h + reg_term)**0.5)
            gn_h = lsmr(lsmr_op, f_augmented, **tr_options)[0]
            S = np.vstack((g_h, gn_h)).T
            S, _ = qr(S, mode='economic')
            if isinstance(J, LinearOperator):
                JS = np.vstack((J.matvec(S[:, 0]), J.matvec(S[:, 1]))).T
            else:
                JS = J.dot(S)
            B_S = np.dot(JS.T, JS) + np.dot(S.T * diag_h, S)
            g_S = S.T.dot(g_h)

        # theta controls step back step ratio from the bounds.
        theta = max(0.995, 1 - g_norm)
        actual_reduction = -1

        # In the following: p - trust-region solution, r - reflected solution,
        # c - minimizer along the scaled gradient, _h means the variable
        # is computed in "hat" space.
        while actual_reduction <= 0 and nfev < max_nfev:
            if tr_solver == 'exact':
                p_h, alpha, n_iter = solve_lsq_trust_region(
                    n, m, uf, s, V, Delta, initial_alpha=alpha)
            elif tr_solver == 'lsmr':
                p_S, _ = solve_trust_region_2d(B_S, g_S, Delta)
                p_h = S.dot(p_S)
            p = d * p_h

            to_bound, _ = step_size_to_bound(x, p, lb, ub)
            if to_bound >= 1:  # Trust region step is feasible.
                # Still step back from the bound.
                p_h *= min(theta * to_bound, 1)
                steps_h = np.atleast_2d(p_h)
            else:  # Otherwise consider a reflected and gradient steps.
                p_h, r_h = find_reflected_step(
                    x, J, diag_h, g_h, p, p_h, d, Delta, lb, ub, theta)

                if tr_solver == 'exact' or not regularize:
                    a, b = build_1d_quadratic_function(J, diag_h, g_h, -g_h)
                # else: a, b were already computed.

                c_h = find_gradient_step(x, a, b, g_h, d, Delta, lb, ub, theta)
                steps_h = np.array([p_h, r_h, c_h])

            qp_values = evaluate_quadratic_function(J, diag_h, g_h, steps_h)
            min_index = np.argmin(qp_values)
            step_h = steps_h[min_index]

            # qp_values are negative, also need to double it.
            predicted_reduction = -qp_values[min_index]

            step = d * step_h
            x_new = make_strictly_feasible(x + step, lb, ub, rstep=0)

            f_new = fun(x_new)
            nfev += 1

            # Usual trust-region step quality estimation.
            cost_new = 0.5 * np.dot(f_new, f_new)
            actual_reduction = cost - cost_new
            # Correction term is specific to the algorithm,
            # vanishes in unbounded case.
            correction = 0.5 * np.dot(step_h * diag_h, step_h)

            if predicted_reduction > 0:
                ratio = (actual_reduction - correction) / predicted_reduction
            else:
                ratio = 0

            if ratio < 0.25:
                Delta_new = 0.25 * norm(step_h)
                alpha *= Delta / Delta_new
                Delta = Delta_new
            elif ratio > 0.75 and norm(step_h) > 0.95 * Delta:
                Delta *= 2.0
                alpha *= 0.5

            ftol_satisfied = (abs(actual_reduction) < ftol * cost and
                              ratio > 0.25)

            step_norm = norm(step)
            xtol_satisfied = step_norm < xtol * (xtol + norm(x))

            if ftol_satisfied and xtol_satisfied:
                termination_status = 4
            elif ftol_satisfied:
                termination_status = 2
            elif xtol_satisfied:
                termination_status = 3

            if termination_status is not None:
                break

        if actual_reduction > 0:
            x = x_new
            f = f_new
            cost = cost_new

            J = jac(x, f)
            njev += 1
        elif nfev == max_nfev:  # Recompute J if algorithm is terminating.
            J = jac(x, f)
            # Don't increase njev, because it's the implementation detail.
        iteration += 1

    active_mask = find_active_constraints(x, lb, ub, rtol=xtol)
    return OptimizeResult(
        x=x, fun=f, jac=J, cost=cost, optimality=g_norm,
        active_mask=active_mask, nfev=nfev, njev=njev, status=0)
